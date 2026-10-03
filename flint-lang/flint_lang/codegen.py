# -*- coding: utf-8 -*-
"""
flint_lang.codegen — 燧石高级语言 → Flint-ASM 代码生成。

调用约定
--------
* 寄存器: r14 = 帧指针 FP, r15 = 栈指针 SP, 其余为临时寄存器
* 函数参数: 从右向左压栈(与 C 的 cdecl 一致), 第 i 个参数位于 FP + 8 + 4*i
* 局部变量: 从 FP 向下分配, 变量位于 FP - offset
* 返回值: r0
* 布尔值: 0 / 1
* 数组: int 元素 4 字节, char 元素 1 字节
* 内置函数: print / printc / prints / input / exit / strcpy / strlen

代码生成器输出汇编文本, 再由汇编器编码为字节码 —— 中间层可读可调试。
"""

from .parser import parse, ParseError

BUILTINS = {"print", "printc", "prints", "input", "exit", "strcpy", "strlen"}

BIN_ALOPS = {"+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD"}
BIN_SHIFTS = {"<<": "SHL", ">>": "SHR"}
BIN_CMPOPS = {"==": "JE", "!=": "JNE", "<": "JL", ">": "JG", "<=": "JLE", ">=": "JGE"}
BIN_BITOPS = {"&": "AND", "|": "OR", "^": "XOR"}


class CodegenError(Exception):
    pass


def _asm_escape(s: str) -> str:
    """字符串 → 汇编 DB 内可安全书写的转义形式(按 UTF-8 逐字节)。"""
    out = []
    for b in s.encode("utf-8"):
        if 32 <= b <= 126 and b not in (34, 92):  # 可打印 ASCII, 排除 " 与 \
            out.append(chr(b))
        else:
            out.append(f"\\x{b:02x}")
    return '"' + "".join(out) + '"'


class CodeGen:
    def __init__(self, prog):
        self.prog = prog
        self.lines = []
        self.label_count = 0
        self.str_count = 0
        self.str_literals = {}          # 字符串内容 → 标签
        self.globals = {g[2]: g for g in prog[1]}   # name → ('gdecl', type, name, init)
        self.funcs = {f[2]: f for f in prog[2]}
        self.cur_layout = None
        self.cur_ret = None
        if "main" not in self.funcs:
            raise CodegenError("缺少 main 函数")
        self.emit("CALL _f_main")
        self.emit("HLT")

    # ---------- 工具 ----------
    def emit(self, *parts):
        self.lines.append(" ".join(str(p) for p in parts))

    def label(self, name):
        self.lines.append(f"{name}:")

    def next_label(self):
        n = f"_L{self.label_count}"
        self.label_count += 1
        return n

    def get_str_label(self, s):
        if s not in self.str_literals:
            self.str_literals[s] = f"_s{self.str_count}"
            self.str_count += 1
        return self.str_literals[s]

    # ---------- 符号解析 ----------
    def resolve(self, name):
        """返回 (kind, type, loc)。loc: ('local', off) / ('param', idx) / ('global', label)"""
        layout = self.cur_layout
        if name in layout["locals"]:
            vtype, off = layout["locals"][name]
            return ("local", vtype, ("local", off))
        if name in layout["params"]:
            vtype, idx = layout["params"][name]
            return ("param", vtype, ("param", idx))
        if name in self.globals:
            g = self.globals[name]
            return ("global", g[1], ("global", f"_g_{name}"))
        if name in self.funcs:
            return ("func", None, ("func", name))
        raise CodegenError(f"未定义的符号: {name}")

    def elem_size(self, t):
        if t == "int":
            return 4
        if t == "char":
            return 1
        if t[0] == "arr":
            return 4 if t[1] == "int" else 1
        raise CodegenError(f"非法类型: {t}")

    def is_char_type(self, t):
        return t == "char" or (t[0] == "arr" and t[1] == "char")

    # ---------- 数据段 ----------
    def data_section(self):
        out = ["\n; ---------- 数据段 ----------"]
        for name, g in self.globals.items():
            gtype, init = g[1], g[3]
            if gtype[0] == "arr" and gtype[1] == "char" and init and init[0] == "init-str":
                out.append(f"_g_{name}:")
                out.append(f"    DB {_asm_escape(init[1])}")
                out.append("    DB 0")
            elif gtype[0] == "arr":
                out.append(f"_g_{name}:")
                out.append(f"    DS {gtype[2] * self.elem_size(gtype)}")
            elif init and init[0] == "init-int":
                out.append(f"_g_{name}:")
                out.append(f"    DW {init[1]}")
            else:
                out.append(f"_g_{name}:")
                out.append("    DS 4" if gtype == "int" else "    DS 1")
        for s, lab in self.str_literals.items():
            out.append(f"{lab}:")
            out.append(f"    DB {_asm_escape(s)}")
            out.append("    DB 0")
        return "\n".join(out)

    # ---------- 函数 ----------
    def gen_program(self):
        for f in self.funcs.values():
            self.gen_function(f)
        self.lines.append(self.data_section())
        return "\n".join(self.lines)

    def gen_function(self, f):
        rettype, name, params, body, layout = f[1], f[2], f[3], f[4], f[5]
        self.cur_layout = layout
        self.cur_ret = rettype
        self.emit(f"\n; ---------- 函数 {name} ----------")
        self.label(f"_f_{name}")
        # 序言: 保存 FP, 建立新帧, 分配局部空间
        self.emit("PUSH r14")
        self.emit("MOV r14, r15")
        if layout["size"] > 0:
            self.emit("MOV r0,", layout["size"])
            self.emit("SUB r15, r15, r0")
        self.gen_stmt(body)
        # 隐式 return
        self.emit("MOV r0, 0")
        self.emit_epilogue()
        self.cur_layout = None

    def emit_epilogue(self):
        self.emit("MOV r15, r14")
        self.emit("POP r14")
        self.emit("RET")

    # ---------- 语句 ----------
    def gen_stmt(self, st):
        kind = st[0]
        if kind == "block":
            for s in st[1]:
                self.gen_stmt(s)
        elif kind == "decl":
            self.gen_decl(st)
        elif kind == "if":
            self.gen_if(st)
        elif kind == "while":
            self.gen_while(st)
        elif kind == "for":
            self.gen_for(st)
        elif kind == "ret":
            if st[1] is not None:
                self.gen_expr(st[1])
            else:
                self.emit("MOV r0, 0")
            self.emit_epilogue()
        elif kind == "exprstmt":
            self.gen_expr(st[1])
        elif kind == "empty":
            pass
        else:
            raise CodegenError(f"未知语句: {kind}")

    def gen_decl(self, st):
        vtype, name, init = st[1], st[2], st[3]
        if init is None:
            return
        if vtype[0] == "arr":
            raise CodegenError("局部数组不能初始化")
        self.gen_expr(init)          # 值 → r0
        self.emit("PUSH r0")
        self.gen_addr(("var", name))  # 地址 → r0
        self.emit("POP r1")
        if vtype == "char":
            self.emit("STB [r0], r1")
        else:
            self.emit("ST [r0], r1")

    def gen_if(self, st):
        _, cond, then, els = st
        self.gen_expr(cond)
        self.emit("CMPI r0, 0")
        if els is not None:
            l_else = self.next_label()
            l_end = self.next_label()
            self.emit("JE", l_else)
            self.gen_stmt(then)
            self.emit("JMP", l_end)
            self.label(l_else)
            self.gen_stmt(els)
            self.label(l_end)
        else:
            l_end = self.next_label()
            self.emit("JE", l_end)
            self.gen_stmt(then)
            self.label(l_end)

    def gen_while(self, st):
        _, cond, body = st
        l_top = self.next_label()
        l_end = self.next_label()
        self.label(l_top)
        self.gen_expr(cond)
        self.emit("CMPI r0, 0")
        self.emit("JE", l_end)
        self.gen_stmt(body)
        self.emit("JMP", l_top)
        self.label(l_end)

    def gen_for(self, st):
        _, init, cond, upd, body = st
        if init is not None:
            if init[0] == "decl":
                self.gen_decl(init)
            else:
                self.gen_expr(init)
        l_top = self.next_label()
        l_end = self.next_label()
        self.label(l_top)
        if cond is not None:
            self.gen_expr(cond)
            self.emit("CMPI r0, 0")
            self.emit("JE", l_end)
        self.gen_stmt(body)
        if upd is not None:
            self.gen_expr(upd)
        self.emit("JMP", l_top)
        self.label(l_end)

    # ---------- 地址计算(结果在 r0) ----------
    def gen_addr(self, target):
        if target[0] == "var":
            kind, vtype, loc = self.resolve(target[1])
            if kind == "local":
                self.emit("MOV r0, r14")
                self.emit("MOV r1,", loc[1])
                self.emit("SUB r0, r0, r1")
            elif kind == "param":
                self.emit("MOV r0, r14")
                self.emit("MOV r1,", 8 + 4 * loc[1])
                self.emit("ADD r0, r0, r1")
            elif kind == "global":
                self.emit("LDA r0,", loc[1])
            else:
                raise CodegenError(f"不能取 {target[1]} 的地址")
        elif target[0] == "index":
            base, idx = target[1], target[2]
            self.gen_base_addr(base)     # 数组基址 → r0
            self.emit("PUSH r0")
            self.gen_expr(idx)           # 下标 → r0
            esize = self.elem_size(self.base_type(base))
            if esize != 1:
                self.emit("MOV r1,", esize)
                self.emit("MUL r0, r0, r1")
            self.emit("POP r1")
            self.emit("ADD r0, r1, r0")  # 地址 → r0
        else:
            raise CodegenError(f"非法赋值目标: {target}")

    def gen_base_addr(self, base):
        if base[0] == "str":
            self.emit("LDA r0,", self.get_str_label(base[1]))
            return
        kind, vtype, loc = self.resolve(base[1])
        if vtype[0] != "arr":
            raise CodegenError(f"{base[1]} 不是数组")
        if kind == "local":
            self.emit("MOV r0, r14")
            self.emit("MOV r1,", loc[1])
            self.emit("SUB r0, r0, r1")
        elif kind == "param":
            raise CodegenError("暂不支持数组参数")
        elif kind == "global":
            self.emit("LDA r0,", loc[1])
        else:
            raise CodegenError(f"无法定位数组 {base[1]}")

    def base_type(self, base):
        if base[0] == "str":
            return ("arr", "char", None)
        _, vtype, _ = self.resolve(base[1])
        return vtype

    # ---------- 表达式(结果在 r0) ----------
    def gen_expr(self, e):
        kind = e[0]
        if kind == "num":
            self.emit("MOV r0,", e[1])
        elif kind == "chr":
            self.emit("MOV r0,", e[1])
        elif kind == "str":
            self.emit("LDA r0,", self.get_str_label(e[1]))
        elif kind == "var":
            k, vtype, loc = self.resolve(e[1])
            if vtype[0] == "arr":
                # 数组名作为表达式 = 数组基址
                self.gen_base_addr(e)
            elif k == "local":
                self.emit("MOV r0, r14")
                if vtype == "char":
                    self.emit("LDB r0, [r0 -", f"{loc[1]}]")
                else:
                    self.emit("LD r0, [r0 -", f"{loc[1]}]")
            elif k == "param":
                self.emit("MOV r0, r14")
                self.emit("MOV r1,", 8 + 4 * loc[1])
                self.emit("ADD r0, r0, r1")
                if vtype == "char":
                    self.emit("LDB r0, [r0]")
                else:
                    self.emit("LD r0, [r0]")
            elif k == "global":
                self.emit("LDA r0,", loc[1])
                if vtype == "char":
                    self.emit("LDB r0, [r0]")
                else:
                    self.emit("LD r0, [r0]")
            else:
                raise CodegenError(f"{e[1]} 不是变量")
        elif kind == "index":
            self.gen_addr(e)
            if self.is_char_type(self.base_type(e[1])):
                self.emit("LDB r0, [r0]")
            else:
                self.emit("LD r0, [r0]")
        elif kind == "call":
            self.gen_call(e)
        elif kind == "bin":
            self.gen_bin(e)
        elif kind == "un":
            self.gen_un(e)
        elif kind == "assign":
            self.gen_assign(e)
        elif kind == "assignop":
            self.gen_assignop(e)
        else:
            raise CodegenError(f"未知表达式: {kind}")

    def gen_bin(self, e):
        op, l, r = e[1], e[2], e[3]
        if op in BIN_ALOPS:
            self.gen_expr(l)
            self.emit("PUSH r0")
            self.gen_expr(r)
            self.emit("POP r1")
            self.emit(BIN_ALOPS[op], "r0, r1, r0")
        elif op in BIN_SHIFTS:
            self.gen_expr(l)
            self.emit("PUSH r0")
            self.gen_expr(r)
            self.emit("POP r1")
            self.emit(BIN_SHIFTS[op], "r0, r1, r0")
        elif op in BIN_BITOPS:
            self.gen_expr(l)
            self.emit("PUSH r0")
            self.gen_expr(r)
            self.emit("POP r1")
            self.emit(BIN_BITOPS[op], "r0, r1, r0")
        elif op in BIN_CMPOPS:
            self.gen_expr(l)
            self.emit("PUSH r0")
            self.gen_expr(r)
            self.emit("POP r1")
            self.emit("CMP r1, r0")
            l1 = self.next_label()
            l2 = self.next_label()
            self.emit(BIN_CMPOPS[op], l1)
            self.emit("MOV r0, 0")
            self.emit("JMP", l2)
            self.label(l1)
            self.emit("MOV r0, 1")
            self.label(l2)
        elif op == "&&":
            lf = self.next_label()
            le = self.next_label()
            self.gen_expr(l)
            self.emit("CMPI r0, 0")
            self.emit("JE", lf)
            self.gen_expr(r)
            self.emit("CMPI r0, 0")
            self.emit("JE", lf)
            self.emit("MOV r0, 1")
            self.emit("JMP", le)
            self.label(lf)
            self.emit("MOV r0, 0")
            self.label(le)
        elif op == "||":
            lt = self.next_label()
            le = self.next_label()
            self.gen_expr(l)
            self.emit("CMPI r0, 0")
            self.emit("JNE", lt)
            self.gen_expr(r)
            self.emit("CMPI r0, 0")
            self.emit("JNE", lt)
            self.emit("MOV r0, 0")
            self.emit("JMP", le)
            self.label(lt)
            self.emit("MOV r0, 1")
            self.label(le)
        else:
            raise CodegenError(f"未知二元运算符: {op}")

    def gen_un(self, e):
        op, x = e[1], e[2]
        self.gen_expr(x)
        if op == "-":
            self.emit("MOV r1, 0")
            self.emit("SUB r0, r1, r0")
        elif op == "!":
            l1 = self.next_label()
            l2 = self.next_label()
            self.emit("CMPI r0, 0")
            self.emit("JE", l1)
            self.emit("MOV r0, 0")
            self.emit("JMP", l2)
            self.label(l1)
            self.emit("MOV r0, 1")
            self.label(l2)
        elif op == "~":
            self.emit("NOT r0, r0")
        else:
            raise CodegenError(f"未知一元运算符: {op}")

    def gen_assign(self, e):
        target, value = e[1], e[2]
        self.gen_expr(value)           # 值 → r0
        self.emit("PUSH r0")
        self.gen_addr(target)          # 地址 → r0
        self.emit("POP r1")
        if self.is_char_type(self.target_type(target)):
            self.emit("STB [r0], r1")
        else:
            self.emit("ST [r0], r1")
        self.emit("MOV r0, r1")        # 赋值表达式值为右值

    def gen_assignop(self, e):
        op, target, value = e[1], e[2], e[3]
        self.gen_expr(target)          # 当前值 → r0
        self.emit("PUSH r0")
        self.gen_expr(value)           # 增量 → r0
        self.emit("POP r1")
        self.emit(BIN_ALOPS[op], "r0, r1, r0")
        self.emit("PUSH r0")
        self.gen_addr(target)
        self.emit("POP r1")
        if self.is_char_type(self.target_type(target)):
            self.emit("STB [r0], r1")
        else:
            self.emit("ST [r0], r1")
        self.emit("MOV r0, r1")

    def target_type(self, target):
        if target[0] == "var":
            _, vtype, _ = self.resolve(target[1])
            return vtype
        return self.base_type(target[1])

    # ---------- 调用 ----------
    def gen_call(self, e):
        callee, args = e[1], e[2]
        if callee[0] == "var" and callee[1] in BUILTINS:
            self.gen_builtin(callee[1], args)
            return
        if callee[0] != "var" or callee[1] not in self.funcs:
            raise CodegenError(f"调用未定义函数: {callee}")
        for a in reversed(args):
            self.gen_expr(a)
            self.emit("PUSH r0")
        self.emit("CALL", f"_f_{callee[1]}")
        if args:
            # 清理参数(返回值保持在 r0, r1 此时为死值可直接使用)
            self.emit("MOV r1,", 4 * len(args))
            self.emit("ADD r15, r15, r1")

    # ---------- 内置函数 ----------
    def gen_builtin(self, name, args):
        if name == "print":
            self._check_args(name, args, 1)
            self.gen_expr(args[0])
            self.emit("OUTI r0")
        elif name == "printc":
            self._check_args(name, args, 1)
            self.gen_expr(args[0])
            self.emit("OUT r0")
        elif name == "prints":
            self._check_args(name, args, 1)
            self.gen_expr(args[0])
            l_loop = self.next_label()
            l_end = self.next_label()
            self.label(l_loop)
            self.emit("LDB r1, [r0]")
            self.emit("CMPI r1, 0")
            self.emit("JE", l_end)
            self.emit("OUT r1")
            self.emit("MOV r1, 1")
            self.emit("ADD r0, r0, r1")
            self.emit("JMP", l_loop)
            self.label(l_end)
        elif name == "input":
            self._check_args(name, args, 0)
            self.emit("IN r0")
        elif name == "exit":
            self._check_args(name, args, 1)
            self.gen_expr(args[0])
            self.emit("HLT")
        elif name == "strlen":
            self._check_args(name, args, 1)
            self.gen_expr(args[0])
            l_loop = self.next_label()
            l_end = self.next_label()
            self.emit("MOV r1, 0")
            self.label(l_loop)
            self.emit("LDB r2, [r0]")
            self.emit("CMPI r2, 0")
            self.emit("JE", l_end)
            self.emit("MOV r3, 1")
            self.emit("ADD r1, r1, r3")
            self.emit("ADD r0, r0, r3")
            self.emit("JMP", l_loop)
            self.label(l_end)
            self.emit("MOV r0, r1")
        elif name == "strcpy":
            self._check_args(name, args, 2)
            self.gen_expr(args[0])
            self.emit("PUSH r0")
            self.gen_expr(args[1])
            self.emit("PUSH r0")
            self.emit("POP r2")   # src
            self.emit("POP r1")   # dst
            l_loop = self.next_label()
            l_end = self.next_label()
            self.label(l_loop)
            self.emit("LDB r3, [r2]")
            self.emit("STB [r1], r3")
            self.emit("CMPI r3, 0")
            self.emit("JE", l_end)
            self.emit("MOV r4, 1")
            self.emit("ADD r1, r1, r4")
            self.emit("ADD r2, r2, r4")
            self.emit("JMP", l_loop)
            self.label(l_end)
            self.emit("MOV r0, 0")

    def _check_args(self, name, args, n):
        if len(args) != n:
            raise CodegenError(f"{name} 需要 {n} 个参数, 实际 {len(args)}")


def compile_to_asm(src):
    """源码 → 汇编文本。"""
    prog = parse(src)
    cg = CodeGen(prog)
    return cg.gen_program()
