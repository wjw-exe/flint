# -*- coding: utf-8 -*-
"""
flint_lang.vm — 燧石虚拟机 (FlintVM)。

执行汇编器产出的字节码镜像。特性:
* 32 位寄存器算术, 自动回绕
* 标志位 Z/N/C/O, 条件跳转
* 字节/字内存访问
* 字符与十进制整数 I/O
* 逐步跟踪与统计
"""

import math
import sys
from . import isa

F_Z, F_N, F_C, F_O = isa.F_Z, isa.F_N, isa.F_C, isa.F_O


class VMError(Exception):
    pass


class VM:
    def __init__(self, image: bytes, input_data: bytes = None, mem_size: int = isa.MEM_SIZE, gfx=None):
        if len(image) > mem_size:
            raise VMError(f"镜像过大: {len(image)} 字节 > 内存 {mem_size}")
        self.mem_size = mem_size
        self.mem = bytearray(mem_size)
        self.mem[:len(image)] = image
        self.regs = [0] * isa.NUM_REGS
        self.regs[isa.SP_REG] = mem_size     # R15 即栈指针
        self.pc = 0
        self.flags = 0
        self.halted = False
        self.steps = 0
        self._in = None
        self._in_pos = 0
        if input_data is not None:
            self._in = bytearray(input_data)
        self.output = bytearray()
        self.gfx = gfx

    # ---------- 标志位 ----------
    def _set_alu_flags(self, result: int):
        """算术/逻辑运算后的标志: Z(零), N(符号为负)。"""
        r = isa.to_u32(result)
        self.flags = 0
        if r == 0:
            self.flags |= F_Z
        if isa.to_i32(r) < 0:
            self.flags |= F_N

    def _set_cmp_flags(self, a: int, b: int):
        """CMP 后标志: Z(相等), N(有符号 a<b), C(无符号 a<b)。"""
        ua, ub = isa.to_u32(a), isa.to_u32(b)
        self.flags = 0
        if ua == ub:
            self.flags |= F_Z
        if isa.to_i32(ua) < isa.to_i32(ub):
            self.flags |= F_N
        if ua < ub:
            self.flags |= F_C

    # ---------- I/O ----------
    def _read_byte(self) -> int:
        if self._in is not None:
            if self._in_pos < len(self._in):
                b = self._in[self._in_pos]
                self._in_pos += 1
                return b
            return 0  # EOF
        raw = sys.stdin.buffer.read(1)
        return raw[0] if raw else 0

    def _write_byte(self, b: int):
        self.output.append(b & 0xFF)

    def _getch(self, timeout_ms: int) -> int:
        """无缓冲读键(带超时): 返回键码; 0=超时无键; -1=EOF/输入耗尽。
        有输入缓冲(--input/IDE)时从缓冲读; 终端下 Windows 用 msvcrt, 其他用 termios raw。"""
        if self._in is not None:
            if self._in_pos < len(self._in):
                b = self._in[self._in_pos]
                self._in_pos += 1
                return b
            return -1
        if not sys.stdin.isatty():
            raw = sys.stdin.buffer.read(1)
            return raw[0] if raw else -1
        import time
        deadline = time.time() + max(0.0, timeout_ms / 1000.0)
        if sys.platform == "win32":
            import msvcrt
            while time.time() < deadline:
                if msvcrt.kbhit():
                    b = msvcrt.getch()
                    return b[0] if isinstance(b, bytes) else ord(b)
                time.sleep(0.01)
            return 0
        import termios, tty, select
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            r, _, _ = select.select([sys.stdin], [], [], max(0.0, timeout_ms / 1000.0))
            if r:
                b = sys.stdin.buffer.read(1)
                return b[0] if b else -1
            return 0
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    # ---------- 内存 ----------
    def _check(self, addr: int, n: int):
        if addr < 0 or addr + n > self.mem_size:
            raise VMError(f"内存越界访问 @0x{addr:x} (pc={self.pc:#06x})")

    def load_word(self, addr: int) -> int:
        self._check(addr, 4)
        return isa.parse_u32(self.mem, addr)

    def store_word(self, addr: int, val: int):
        self._check(addr, 4)
        self.mem[addr:addr + 4] = isa.to_u32(val).to_bytes(4, "little")

    def load_byte(self, addr: int) -> int:
        self._check(addr, 1)
        return self.mem[addr]

    def _cstr(self, addr: int) -> str:
        """读取 0 结尾 UTF-8 字符串(UI 组件文本)。"""
        out = bytearray()
        i = addr
        while i < len(self.mem) and i < addr + 4096:
            b = self.mem[i]
            if b == 0:
                break
            out.append(b)
            i += 1
        return out.decode("utf-8", errors="replace")

    def store_byte(self, addr: int, val: int):
        self._check(addr, 1)
        self.mem[addr] = val & 0xFF

    # ---------- 栈 ----------
    def push(self, val: int):
        self.regs[isa.SP_REG] -= 4
        self.store_word(self.regs[isa.SP_REG], val)

    def pop(self) -> int:
        v = self.load_word(self.regs[isa.SP_REG])
        self.regs[isa.SP_REG] += 4
        return v

    # ---------- 执行 ----------
    def fetch(self) -> int:
        if self.pc < 0 or self.pc + 4 > self.mem_size:
            raise VMError(f"PC 越界: {self.pc:#06x}")
        w = isa.parse_u32(self.mem, self.pc)
        name = isa.OP_TO_NAME.get((w >> 24) & 0xFF)
        self.pc += 8 if name in isa.J_TYPE_OPS else 4
        return w, name

    def step(self, trace: bool = False) -> str:
        """执行一条指令, 返回反汇编文本(仅 trace 模式生成, 避免全内存拷贝)。"""
        if self.halted:
            raise VMError("虚拟机已停机")
        w, name = self.fetch()
        op = (w >> 24) & 0xFF
        rd = (w >> 20) & 0xF
        rs1 = (w >> 16) & 0xF
        rs2 = (w >> 12) & 0xF
        imm16 = isa.sign_extend(w & 0xFFFF, 16)
        off24 = isa.sign_extend(w & 0xFFFFFF, 24)
        regs = self.regs
        if trace:
            asm = isa.disasm(bytes(self.mem), self.pc - (8 if name in isa.J_TYPE_OPS else 4))
        old_pc = self.pc

        try:
            if name == "MOVR":
                regs[rd] = regs[rs1]
            elif name == "MOVI":
                regs[rd] = self.load_word(self.pc - 4)
            elif name == "ADD":
                r = regs[rs1] + regs[rs2]
                regs[rd] = isa.to_u32(r); self._set_alu_flags(r)
            elif name == "SUB":
                r = regs[rs1] - regs[rs2]
                regs[rd] = isa.to_u32(r); self._set_alu_flags(r)
            elif name == "MUL":
                r = regs[rs1] * regs[rs2]
                regs[rd] = isa.to_u32(r); self._set_alu_flags(r)
            elif name == "DIV":
                b = regs[rs2]
                if b == 0:
                    raise VMError(f"除零错误 @pc={old_pc:#06x}")
                a = isa.to_i32(regs[rs1])
                q = int(a / b) if (a >= 0) == (isa.to_i32(b) >= 0) else -int(abs(a) // abs(b))
                regs[rd] = isa.to_u32(q); self._set_alu_flags(q)
            elif name == "MOD":
                b = regs[rs2]
                if b == 0:
                    raise VMError(f"除零错误 @pc={old_pc:#06x}")
                a = isa.to_i32(regs[rs1])
                q = int(a / b) if (a >= 0) == (isa.to_i32(b) >= 0) else -int(abs(a) // abs(b))
                r = a - q * isa.to_i32(b)
                regs[rd] = isa.to_u32(r); self._set_alu_flags(r)
            elif name == "AND":
                r = regs[rs1] & regs[rs2]
                regs[rd] = isa.to_u32(r); self._set_alu_flags(r)
            elif name == "OR":
                r = regs[rs1] | regs[rs2]
                regs[rd] = isa.to_u32(r); self._set_alu_flags(r)
            elif name == "XOR":
                r = regs[rs1] ^ regs[rs2]
                regs[rd] = isa.to_u32(r); self._set_alu_flags(r)
            elif name == "NOT":
                r = ~regs[rs1]
                regs[rd] = isa.to_u32(r); self._set_alu_flags(r)
            elif name == "SHL":
                r = regs[rs1] << (regs[rs2] & 31)
                regs[rd] = isa.to_u32(r); self._set_alu_flags(r)
            elif name == "SHR":
                r = regs[rs1] >> (regs[rs2] & 31)
                regs[rd] = isa.to_u32(r); self._set_alu_flags(r)
            elif name == "STRCAT":
                # rd = 目标缓冲地址; 依次拷贝 rs1、rs2 两个 0 结尾字符串(含结尾 0)
                dst = regs[rd]
                src = regs[rs1]
                i = 0
                while True:
                    b = self.load_byte(src + i)
                    self.store_byte(dst + i, b)
                    i += 1
                    if b == 0:
                        break
                src = regs[rs2]
                j = 0
                while True:
                    # 从 dst + (i-1) 起写: 覆盖第一个串的结尾 0, 两串无缝连接
                    b = self.load_byte(src + j)
                    self.store_byte(dst + (i - 1) + j, b)
                    j += 1
                    if b == 0:
                        break
            elif name == "STRCMP":
                # rd = -1/0/1: 字典序比较 rs1 与 rs2(逐字节无符号)
                a = regs[rs1]
                b = regs[rs2]
                i = 0
                res = 0
                while True:
                    x = self.load_byte(a + i)
                    y = self.load_byte(b + i)
                    if x != y:
                        res = -1 if x < y else 1
                        break
                    if x == 0:
                        break
                    i += 1
                regs[rd] = isa.to_u32(res)
            elif name == "CMP":
                self._set_cmp_flags(regs[rd], regs[rs2])
            elif name == "CMPI":
                self._set_cmp_flags(regs[rd], imm16)
            elif name in ("JMP", "JE", "JNE", "JG", "JL", "JGE", "JLE"):
                cond = {
                    "JMP": True,
                    "JE": self.flags & F_Z,
                    "JNE": not (self.flags & F_Z),
                    "JG": not (self.flags & F_Z) and not (self.flags & F_N),
                    "JL": bool(self.flags & F_N),
                    "JGE": not (self.flags & F_N),
                    "JLE": bool(self.flags & F_Z or self.flags & F_N),
                }[name]
                if cond:
                    self.pc = old_pc + off24
            elif name == "CALL":
                self.push(old_pc)
                self.pc = old_pc + off24
            elif name == "RET":
                self.pc = self.pop()
            elif name == "PUSH":
                self.push(regs[rd])
            elif name == "POP":
                regs[rd] = self.pop()
            elif name == "LD":
                regs[rd] = self.load_word(regs[rs1] + imm16)
            elif name == "ST":
                self.store_word(regs[rs1] + imm16, regs[rd])
            elif name == "LDB":
                regs[rd] = self.load_byte(regs[rs1] + imm16)
            elif name == "STB":
                self.store_byte(regs[rs1] + imm16, regs[rd])
            elif name == "LDA":
                regs[rd] = self.load_word(self.pc - 4)
            elif name == "IN":
                regs[rd] = self._read_byte()
            elif name == "OUT":
                self._write_byte(regs[rd])
            elif name == "OUTI":
                self.output += str(isa.to_i32(regs[rd])).encode("ascii")
            elif name == "TRAP":
                if imm16 == 1:
                    raise VMError(f"运行时陷阱: 索引越界 @pc={old_pc:#06x}")
                elif imm16 == 2:
                    raise VMError(f"运行时陷阱: 除零错误 @pc={old_pc:#06x}")
                elif imm16 == 10:
                    regs[rd] = self._getch(regs[0])       # getch(ms)
                elif imm16 == 11:
                    self.output += b"\x1b[2J\x1b[H"       # clrscr
                elif imm16 == 12:
                    import time
                    time.sleep(max(0.0, isa.to_i32(regs[0]) / 1000.0))   # sleep(ms)
                elif imm16 == 14:
                    x = isa.to_i32(regs[0])
                    regs[rd] = isa.to_u32(math.isqrt(x) if x >= 0 else 0)   # sqrt: 负数 → 0
                elif imm16 == 15:
                    a = abs(isa.to_i32(regs[0]))
                    b = abs(isa.to_i32(regs[1]))
                    regs[rd] = isa.to_u32(math.gcd(a, b))                   # gcd
                elif imm16 == 16:
                    x = isa.to_i32(regs[0]); lo = isa.to_i32(regs[1]); hi = isa.to_i32(regs[2])
                    regs[rd] = isa.to_u32(lo if x < lo else hi if x > hi else x)  # clamp
                elif imm16 == 17:
                    # str(x): 十进制字符串写入 r1 指向的缓冲(带负号), 结果地址 → rd
                    n = isa.to_i32(regs[0])
                    buf = regs[1]
                    txt = str(n).encode("ascii")
                    for i, b in enumerate(txt):
                        self.store_byte(buf + i, b)
                    self.store_byte(buf + len(txt), 0)
                    regs[rd] = buf
                elif 20 <= imm16 <= 28:
                    # 2D 游戏引擎系统调用: window/clear/fill_rect/fill_circle/
                    # draw_line/draw_char/poll_key/window_closed/present
                    if self.gfx is None:
                        raise VMError(f"图形内置(TRAP {imm16})只能在 gfx 模式下使用 @pc={old_pc:#06x}")
                    r = self.gfx.syscall(imm16, regs)
                    if r is not None:
                        regs[rd] = r
                elif 29 <= imm16 <= 41:
                    # 纯 UI 库系统调用(TRAP 29-41): 组件创建/状态读写/事件轮询。
                    # 文本参数(button/label/checkbox 标签、set_text)在 VM 侧读内存解码,
                    # 引擎只接收解码后的 str —— 引擎不碰 VM 内存。
                    if self.gfx is None:
                        raise VMError(f"UI 内置(TRAP {imm16})只能在 ui 模式下使用 @pc={old_pc:#06x}")
                    r = self._ui_call(imm16, regs)
                    if r is not None:
                        regs[rd] = r
                else:
                    raise VMError(f"运行时陷阱: 未定义陷阱 {imm16} @pc={old_pc:#06x}")
            elif name == "HLT":
                self.halted = True
            else:
                raise VMError(f"未知操作码 0x{op:02x} @pc={old_pc:#06x}")
        except VMError:
            raise
        except Exception as e:
            raise VMError(f"{name} 执行异常 @pc={old_pc:#06x}: {e}")
        self.steps += 1
        return asm if trace else ""

    def _ui_call(self, code, regs):
        """UI TRAP 分发: 字符串参数在此读内存解码后传给引擎。"""
        eng = self.gfx
        if code == 29:
            return eng.ui_window(regs[0], regs[1])
        if code == 30:
            return eng.ui_button(regs[0], regs[1], regs[2], regs[3], self._cstr(regs[4]))
        if code == 31:
            return eng.ui_label(regs[0], regs[1], self._cstr(regs[2]))
        if code == 32:
            return eng.ui_slider(regs[0], regs[1], regs[2], regs[3], regs[4], regs[5])
        if code == 33:
            return eng.ui_progress(regs[0], regs[1], regs[2], regs[3])
        if code == 34:
            return eng.ui_checkbox(regs[0], regs[1], self._cstr(regs[2]), regs[3])
        if code == 35:
            return eng.ui_clicked(regs[0])
        if code == 36:
            return eng.ui_value(regs[0])
        if code == 37:
            return eng.ui_checked(regs[0])
        if code == 38:
            return eng.ui_set_text(regs[0], self._cstr(regs[1]))
        if code == 39:
            return eng.ui_set_value(regs[0], regs[1])
        if code == 40:
            return eng.ui_present()
        if code == 41:
            return eng.ui_closed()
        return 0

    def run(self, max_steps: int = None, trace: bool = False, stats: bool = False):
        while not self.halted:
            if max_steps is not None and self.steps >= max_steps:
                raise VMError(f"超过最大步数限制 {max_steps}")
            asm = self.step(trace=trace)
            if trace:
                regs = ", ".join(f"r{i}={self.regs[i]}" for i in range(8))
                print(f"{self.pc:06x}  {asm:<24}  [{regs}]")
        if stats:
            print(f"[stats] 共执行 {self.steps} 条指令, 输出 {len(self.output)} 字节")
        return bytes(self.output)

    def dump_memory(self, addr: int = 0, words: int = 16):
        for i in range(words):
            a = addr + i * 4
            if a + 4 > self.mem_size:
                break
            print(f"{a:06x}: {self.load_word(a):08x}   ; {self.mem[a:a + 4]!r}")

    @property
    def exit_code(self) -> int:
        return self.regs[0] & 0xFF


def run_image(image: bytes, input_data: bytes = None, **kw):
    vm = VM(image, input_data=input_data)
    out = vm.run(**kw)
    return out, vm
