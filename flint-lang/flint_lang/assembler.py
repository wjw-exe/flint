# -*- coding: utf-8 -*-
"""
flint_lang.assembler — 燧石汇编器(两趟扫描)。

把 Flint-ASM 文本汇编为可直接加载进虚拟机的字节码镜像。

语法
----
* 注释: `;` 到行尾
* 标签: `名字:` 单独位于行首
* 指令: `助记符 操作数, 操作数, ...`
* 数据段伪指令:
    DB  <项>[, <项>]...   逐字节存放(字符串、字符、常量表达式)
    DW  <项>[, <项>]...   逐 32 位字存放
    DS  <n>               预留 n 字节零填充
* 操作数:
    寄存器: r0~r15(亦可用 sp 表示 r15)
    立即数: 十进制 / 0x 十六进制 / 0b 二进制 / '字符'
    标签地址
    内存:   [reg] 或 [reg + 常量] (用于 LD/ST/LDB/STB)
    常量表达式: 支持 + - * / ( ) 与标签

伪指令
------
* MOV rd, imm        → J 型(32 位立即数)
* MOV rd, rs         → R 型
* PUSH r / POP r / IN r / OUT r / OUTI r / NOT rd, rs
* CALL / JMP / JE / JNE / JG / JL / JGE / JLE  label
"""

import re
from . import isa

REG_ALIAS = {"sp": 15}
R_TYPE_OPS = isa.R_TYPE_OPS
J_TYPE_OPS = isa.J_TYPE_OPS
MNEMONICS = set(isa.OPCODES) | {"MOV"}   # MOV 为 MOVR/MOVI 的助记符别名


def _is_reg_token(t) -> bool:
    """token 是否为寄存器标识符。"""
    if t[0] != "ident":
        return False
    v = t[1].lower()
    return v in REG_ALIAS or re.fullmatch(r"r(1[0-5]|[0-9])", v) is not None


def _clean_commas(operands):
    """剥离操作数列表中的逗号 token。"""
    return [t for t in operands if not (t[0] == "sym" and t[1] == ",")]


class AsmError(Exception):
    pass


# ---------- 词法 ----------
_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<comment>;.*$)
  | (?P<label>[A-Za-z_.][A-Za-z0-9_.]*:)
  | (?P<ident>[A-Za-z_.][A-Za-z0-9_.]*)
  | (?P<num>0[xX][0-9a-fA-F]+|0[bB][01]+|\d+)
  | (?P<charr>'(?:\\.|[^'])' )
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<sym>,|\[|\]|\+|-|\*|/|\(\)|\(|\))
    """,
    re.VERBOSE | re.MULTILINE,
)

_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", "'": "'", '"': '"', "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


def _parse_escape(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            e = s[i + 1]
            if e in _SIMPLE_ESCAPES:
                out.append(_SIMPLE_ESCAPES[e])
                i += 2
            elif e == "x":
                out.append(chr(int(s[i + 2:i + 4], 16)))
                i += 4
            else:
                raise AsmError(f"未知转义: \\{e}")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _tokenize_line(text: str):
    """单行 → token 列表。"""
    tokens = []
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        val = m.group()
        if kind in ("ws", "comment"):
            continue
        if kind == "label":
            tokens.append(("label", val[:-1]))
        elif kind == "num":
            tokens.append(("num", int(val, 0)))
        elif kind == "charr":
            body = val[1:-1]
            tokens.append(("num", ord(_parse_escape(body))))
        elif kind == "str":
            tokens.append(("str", _parse_escape(val[1:-1])))
        elif kind == "ident":
            tokens.append(("ident", val.upper() if val.upper() in MNEMONICS else val))
        else:
            tokens.append(("sym", val))
    return tokens


def tokenize(text: str):
    """源码 → 按物理行切分的 token 列表(list[list[token]])。"""
    out = []
    for raw in text.split("\n"):
        toks = _tokenize_line(raw)
        if toks:
            out.append(toks)
    return out


# ---------- 行结构 ----------
def split_line(line_tokens):
    """提取行首标签, 返回 (label_or_None, 指令 token 列表)。"""
    label = None
    if line_tokens and line_tokens[0][0] == "label":
        label = line_tokens[0][1]
        line_tokens = line_tokens[1:]
    return label, line_tokens


# ---------- 常量表达式 ----------
class ExprParser:
    """解析常量表达式: + - * / ( ) 数字/字符/标签/寄存器/内存引用。"""

    def __init__(self, tokens, symtab, pc, is_data=False):
        self.ts = tokens
        self.i = 0
        self.symtab = symtab
        self.pc = pc  # 当前指令地址, 用于取标签地址
        self.is_data = is_data

    def peek(self):
        return self.ts[self.i] if self.i < len(self.ts) else ("eof", None)

    def next(self):
        t = self.ts[self.i]
        self.i += 1
        return t

    def error(self, msg):
        raise AsmError(f"{msg} (pc={self.pc:#06x})")

    def parse(self):
        v = self.expr()
        if self.peek()[0] != "eof":
            self.error(f"表达式后有多余内容: {self.peek()}")
        return v

    def expr(self):
        return self.add_sub()

    def add_sub(self):
        v = self.mul_div()
        while True:
            k, s = self.peek()
            if k == "sym" and s in ("+", "-"):
                self.next()
                r = self.mul_div()
                v = v + r if s == "+" else v - r
            else:
                return v

    def mul_div(self):
        v = self.unary()
        while True:
            k, s = self.peek()
            if k == "sym" and s in ("*", "/"):
                self.next()
                r = self.unary()
                v = v * r if s == "*" else int(v / r)
            else:
                return v

    def unary(self):
        k, s = self.peek()
        if k == "sym" and s == "-":
            self.next()
            return -self.unary()
        return self.primary()

    def primary(self):
        k, v = self.next()
        if k == "num":
            return v
        if k == "ident":
            if v not in self.symtab:
                self.error(f"未定义标签/符号: {v}")
            return self.symtab[v]
        if k == "sym" and v == "(":
            e = self.expr()
            if self.next()[0:2] != ("sym", ")"):
                self.error("缺少右括号")
            return e
        self.error(f"意外的常量表达式项: {v}")


# ---------- 指令尺寸与编码 ----------
def _reg(t, pc):
    """token → 寄存器号"""
    k, v = t
    if k != "ident":
        raise AsmError(f"期望寄存器, 得到 {v} (pc={pc:#06x})")
    v = v.lower()
    if v in REG_ALIAS:
        return REG_ALIAS[v]
    if re.fullmatch(r"r(1[0-5]|[0-9])", v):
        return int(v[1:])
    raise AsmError(f"非法寄存器: {v} (pc={pc:#06x})")


class Assembler:
    def __init__(self):
        self.text = ""
        self.image = b""
        self.symbols = {}          # 标签 → 地址
        self.instructions = []     # 每行: (address, opname, operands, line_tokens)

    # ---- 第一趟: 计算每条指令/数据的大小与标签地址 ----
    def pass1(self, text):
        lines = [(split_line(ts)) for ts in tokenize(text)]
        pc = 0
        symbols = {}
        insts = []
        for label, ts in lines:
            if label is not None:
                if label in symbols:
                    raise AsmError(f"标签重复定义: {label}")
                symbols[label] = pc
            if not ts:
                continue
            k, head = ts[0]
            if k == "ident" and head.upper() in ("DB", "DW", "DS"):
                rest = ts[1:]
                if head.upper() == "DS":
                    e = ExprParser(rest, symbols, pc)
                    n = e.parse()
                    size = n
                else:
                    # 计算数据项个数
                    size = 0
                    items = self._split_data_items(rest)
                    for item in items:
                        if item[0][0] == "str":
                            size += len(item[0][1])
                        else:
                            size += 1 if head.upper() == "DB" else 4
                insts.append((pc, head.upper(), rest, ts, size))
                pc += size
                continue
            if k != "ident" or head.upper() not in MNEMONICS:
                raise AsmError(f"无法识别的指令: {ts[0][1]} (pc={pc:#06x})")
            size = self._instr_size(head.upper(), ts[1:])
            insts.append((pc, head.upper(), ts[1:], ts, size))
            pc += size
        self.symbols = symbols
        self.instructions = insts
        return pc

    @staticmethod
    def _split_data_items(rest):
        """把 DB/DW 操作数按逗号拆成列表, 每项是 token 列表。"""
        items = []
        cur = []
        depth = 0
        for t in rest:
            if t[0] == "sym":
                if t[1] == "(":
                    depth += 1
                elif t[1] == ")":
                    depth -= 1
                elif t[1] == "," and depth == 0:
                    items.append(cur)
                    cur = []
                    continue
            cur.append(t)
        if cur:
            items.append(cur)
        return items

    def _instr_size(self, op, operands):
        operands = _clean_commas(operands)
        if op == "HLT" or op == "RET":
            return 4
        if op in ("JMP", "JE", "JNE", "JG", "JL", "JGE", "JLE", "CALL"):
            return 4
        if op == "MOV":
            # 第二操作数是寄存器 → MOVR(R 型); 否则 → MOVI(J 型 32 位立即数)
            return 4 if (_is_reg_token(operands[1]) if len(operands) >= 2 else False) else 8
        if op == "LDA":
            return 8
        if op in R_TYPE_OPS or op in ("NOT", "CMP", "CMPI", "PUSH", "POP", "IN", "OUT", "OUTI", "TRAP"):
            return 4
        if op in ("LD", "ST", "LDB", "STB"):
            return 4
        raise AsmError(f"未知指令: {op}")

    # ---- 第二趟: 编码 ----
    def pass2(self):
        out = bytearray()
        for pc, op, operands, ts, size in self.instructions:
            if op in ("DB", "DW", "DS"):
                if op == "DS":
                    out += b"\0" * size
                    continue
                items = self._split_data_items(operands or [])
                for item in items:
                    if item[0][0] == "str":
                        for ch in item[0][1]:
                            out.append(ord(ch) & 0xFF)
                    else:
                        e = ExprParser(item, self.symbols, pc)
                        v = e.parse()
                        if op == "DB":
                            out.append(v & 0xFF)
                        else:
                            out += isa.fmt_u32(v & 0xFFFFFFFF)
                continue
            out += self._encode_instr(op, operands, pc)
        return bytes(out)

    def _encode_instr(self, op, operands, pc):
        operands = _clean_commas(operands)
        # ---------- 无操作数 ----------
        if op in ("HLT", "RET"):
            return isa.fmt_u32(isa.OPCODES[op] << 24)

        # ---------- 分支 ----------
        if op in ("JMP", "JE", "JNE", "JG", "JL", "JGE", "JLE", "CALL"):
            if len(operands) != 1:
                raise AsmError(f"{op} 需要一个标签操作数")
            e = ExprParser(operands, self.symbols, pc)
            target = e.parse()
            off = target - (pc + 4)
            if not (-(1 << 23) <= off < (1 << 23)):
                raise AsmError(f"{op} 跳转偏移越界: {off} (pc={pc:#06x})")
            return isa.enc_b(op, off)

        if op in ("PUSH", "POP", "IN", "OUT", "OUTI"):
            rd = _reg(operands[0], pc)
            return isa.enc_i(op, rd, 0, 0)

        if op in R_TYPE_OPS:
            rd = _reg(operands[0], pc)
            rs1 = _reg(operands[1], pc)
            rs2 = _reg(operands[2], pc)
            return isa.enc_r(op, rd, rs1, rs2)

        if op == "NOT":
            rd = _reg(operands[0], pc)
            rs1 = _reg(operands[1], pc)
            return isa.enc_i(op, rd, rs1, 0)

        if op == "CMP":
            rs1 = _reg(operands[0], pc)
            rs2 = _reg(operands[1], pc)
            return isa.enc_r(op, rs1, rs1, rs2)

        if op == "CMPI":
            rs1 = _reg(operands[0], pc)
            e = ExprParser(operands[1:], self.symbols, pc)
            v = e.parse()
            if not (-32768 <= v <= 32767):
                raise AsmError(f"CMPI 立即数越界: {v}")
            return isa.enc_i("CMPI", rs1, 0, v)

        if op == "TRAP":
            e = ExprParser(operands, self.symbols, pc)
            v = e.parse()
            if not (0 <= v <= 0xFFFF):
                raise AsmError(f"TRAP 代码越界: {v}")
            return isa.enc_i("TRAP", 0, 0, v)

        if op == "MOV":
            rd = _reg(operands[0], pc)
            if _is_reg_token(operands[1]):
                rs = _reg(operands[1], pc)
                return isa.enc_r("MOVR", rd, rs, 0)
            e = ExprParser(operands[1:], self.symbols, pc)
            v = e.parse()
            return isa.enc_j("MOVI", rd, v & 0xFFFFFFFF)

        if op == "LDA":
            rd = _reg(operands[0], pc)
            e = ExprParser(operands[1:], self.symbols, pc)
            v = e.parse()
            return isa.enc_j("LDA", rd, v & 0xFFFFFFFF)

        if op in ("LD", "ST", "LDB", "STB"):
            return self._encode_mem(op, operands, pc)

        raise AsmError(f"无法编码指令: {op}")

    def _encode_mem(self, op, operands, pc):
        """内存访问: LD rd, [base + off] / ST [base + off], rd"""
        if op in ("LD", "LDB"):
            if len(operands) < 2:
                raise AsmError(f"{op} 格式: {op} rd, [base + off]")
            rd = _reg(operands[0], pc)
            base, off = self._parse_mem_ref(operands[1:], pc)
        else:
            if len(operands) < 2:
                raise AsmError(f"{op} 格式: {op} [base + off], rd")
            rd = _reg(operands[-1], pc)
            base, off = self._parse_mem_ref(operands[:-1], pc)
        return isa.enc_i(op, rd, base, off)

    def _parse_mem_ref(self, operands, pc):
        """解析 [reg] 或 [reg + 常量] → (base_reg, offset)"""
        if not operands or operands[0] != ("sym", "["):
            raise AsmError(f"期望 [ 开始内存引用 (pc={pc:#06x})")
        i = 1
        if i >= len(operands):
            raise AsmError("内存引用缺少寄存器")
        base = _reg(operands[i], pc)
        i += 1
        if i >= len(operands):
            raise AsmError(f"内存引用缺少右括号 ] (pc={pc:#06x})")
        off = 0
        if operands[i][0] == "sym" and operands[i][1] in ("+", "-"):
            sign = -1 if operands[i][1] == "-" else 1
            i += 1
            rest = operands[i:]
            j = next((k for k, t in enumerate(rest) if t == ("sym", "]")), None)
            if j is None:
                raise AsmError(f"内存引用缺少右括号 ] (pc={pc:#06x})")
            e = ExprParser(rest[:j], self.symbols, pc)
            off = sign * e.parse()
            if rest[j + 1:]:
                raise AsmError(f"内存引用后有多余内容 (pc={pc:#06x})")
            return base, off
        if operands[i] == ("sym", "]"):
            return base, 0
        raise AsmError(f"内存引用格式错误 (pc={pc:#06x}): {operands[i]}")

    # ---- 入口 ----
    def assemble(self, text):
        total = self.pass1(text)
        self.image = self.pass2()
        if len(self.image) != total:
            raise AsmError(f"内部错误: 镜像大小不一致 {len(self.image)} != {total}")
        if total > isa.MEM_SIZE:
            raise AsmError(f"程序过大: {total} 字节, 超出内存 {isa.MEM_SIZE}")
        return self.image, self.symbols


def assemble(text) -> tuple:
    """便捷入口: 返回 (字节码镜像 bytes, 符号表 dict)。"""
    a = Assembler()
    return a.assemble(text)
