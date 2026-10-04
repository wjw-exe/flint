# -*- coding: utf-8 -*-
"""
codegen.py — AST → Flint-ASM 汇编文本 (Flint v3.0)。

调用约定(与 flint-lang 一致):
  * 参数从右向左压栈, 第 i 个参数位于 FP+8+4*i (FP = r14, SP = r15)
  * 序言: PUSH r14; MOV r14, r15; SUB r15, r15, 帧大小
  * 尾跋: MOV r15, r14; POP r14; RET
  * 返回值在 r0
  * 局部变量位于 FP 下方: 第 k 个局部变量在 FP - 4 - 4*k

表达式求值: 简易栈式代码生成 —— 结果总在 r0; 二元运算把左操作数压栈,
计算右操作数后弹回 r1(r1=左, r0=右)。嵌套表达式天然安全。

v2.1 新增:
  * 列表: 槽中保存"首地址"(len 字 + 元素连续存放), 索引/赋值/遍历/len
  * 运行时越界 → TRAP 1; 除零 → TRAP 2
  * 复合赋值: 单次寻址(索引情况只算一次地址)
  * range 字面量步长: 编译期定循环方向, 免每圈符号判断; 字面量边界用 CMPI
  * 动态步长: 循环入口一次判零(TRAP 2), 每圈仅一次方向分支
  * 常量折叠已在 parser 完成(数字/字符串)
  * 全局变量 → 数据段(只读, 无 global 关键字)
"""

from parser import (Program, FuncDef, Decl, Assign, IndexAssign, AugAssign,
                    Return, If, While, For, Break, Continue, Pass, ExprStmt,
                    BinOp, Unary, Call, Var, Index, IntLit, BoolLit, StrLit,
                    ListLit, Ternary)


def is_list(t):
    return isinstance(t, tuple) and t[0] == "list"

ARITH_ASM = {"+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD",
             "&": "AND", "|": "OR", "^": "XOR", "<<": "SHL", ">>": "SHR"}
CMP_CC = {"==": "JE", "!=": "JNE", "<": "JL", ">": "JG", "<=": "JLE", ">=": "JGE"}
LOGIC_CC = {"==": "JE", "!=": "JNE", "<": "JG", ">": "JL", "<=": "JGE", ">=": "JLE"}  # 逻辑比较(取反跳转)
AUG_TO_OP = {"+=": "+", "-=": "-", "*=": "*", "/=": "/", "%=": "%", "//=": "//",
             "&=": "&", "|=": "|", "^=": "^", "<<=": "<<", ">>=": ">>"}
CMPI_MIN, CMPI_MAX = -32768, 32767


class CodeGen:
    def __init__(self):
        self.lines = []
        self.data_lines = []
        self.label_n = 0
        self.str_n = 0
        self.funcs = {}
        self.global_types = {}
        self.global_data = []        # 全局变量数据项 [(label, kind, payload)]
        self.locals = {}
        self.var_types = {}
        self.for_ends = {}           # id(For) → 结束值槽偏移(None 表示用 CMPI)
        self.for_steps = {}          # id(For) → 步长槽偏移(None 表示字面量步长)
        self.for_lit_storage = {}    # id(For) → 列表字面量的隐藏存储槽偏移
        self.list_storage = {}       # 变量名 → 列表存储块偏移(len 字位置)
        self.for_list_base = {}      # id(For) → 遍历基础地址槽偏移
        self.for_list_idx = {}       # id(For) → 遍历索引槽偏移
        self.loop_stack = []         # [(break_label, continue_label)]
        self.ret_label = None
        self.str_slot_n = 0            # v3.0: 运行时字符串拼接的临时池槽计数

    # ---------- 工具 ----------
    def _label(self):
        self.label_n += 1
        return f"L{self.label_n}"

    def _mem(self, off):
        """FP 相对地址表达式字符串。"""
        if off >= 0:
            return f"[r14 + {off}]"
        return f"[r14 - {-off}]"

    def _emit(self, s):
        self.lines.append("    " + s)

    def _new_str(self, val):
        self.str_n += 1
        lab = f"__s{self.str_n}"
        data = val.encode("utf-8")
        if data:
            self.data_lines.append(f"{lab}: DB {', '.join(str(b) for b in data)}, 0")
        else:
            self.data_lines.append(f"{lab}: DB 0")   # v3.1: 空字符串仅结尾 0
        return lab

    def _new_str_slot(self):
        """v3.0: 为运行时字符串拼接分配 256 字节临时池槽(拼接结果存活至语句结束)。"""
        self.str_slot_n += 1
        lab = f"__sb{self.str_slot_n}"
        self.data_lines.append(f"{lab}: DB {', '.join(['0'] * 256)}")
        return lab

    # ---------- 顶层 ----------
    def generate(self, prog: Program):
        for f in prog.funcs:
            self.funcs[f.name] = f
        for g in prog.globals:
            self.global_types[g.name] = g.typ
            self._add_global_data(g)
        self._emit("_start:")
        self._emit("CALL main")
        self._emit("HLT")
        for f in prog.funcs:
            self.gen_func(f)
        for label, kind, payload in self.global_data:
            if kind == "str":
                data = payload.encode("utf-8")
                self.data_lines.append(f"{label}: DB {', '.join(str(b) for b in data)}, 0")
            else:  # dw 列表
                self.data_lines.append(f"{label}: DW {', '.join(str(x) for x in payload)}")
        return "\n".join(self.lines + self.data_lines)

    def _add_global_data(self, g):
        lab = f"__g_{g.name}"
        t = g.typ
        if is_list(t):
            lit = g.expr.elems if g.expr is not None else []
            size = t[2] if t[2] is not None else len(lit)
            vals = [size]
            for e in lit:
                vals.append(self._lit_value(e))
            vals += [0] * (size - len(lit))
            self.global_data.append((lab, "dw", vals))
        elif t == "str":
            self.global_data.append((lab, "str", g.expr.val))
        else:
            self.global_data.append((lab, "dw", [self._lit_value(g.expr)]))

    @staticmethod
    def _lit_value(e):
        if isinstance(e, BoolLit):
            return 1 if e.val else 0
        return e.val

    # ---------- 函数 ----------
    def _max_list_sizes(self, stmts):
        """预扫描: 每个列表变量在 Decl/Assign 中出现的最大字面量大小。
        存储块必须按最大值分配, 否则 `xs = []` 后再赋更大的列表会把初始化写到未分配帧内存,
        撞上其他局部变量/循环临时槽(曾复现: len 字与循环索引同址导致输出错误)。"""
        sizes = {}

        def walk(ss):
            for s in ss:
                if isinstance(s, Decl) and is_list(s.typ):
                    size = s.typ[2] if s.typ[2] is not None else (
                        len(s.expr.elems) if s.expr else 0)
                    sizes[s.name] = max(sizes.get(s.name, 0), size)
                elif isinstance(s, Assign) and isinstance(s.expr, ListLit):
                    sizes[s.name] = max(sizes.get(s.name, 0), len(s.expr.elems))
                elif isinstance(s, For):
                    walk(s.body)
                elif isinstance(s, If):
                    walk(s.body)
                    for _, b in s.elifs:
                        walk(b)
                    if s.else_body:
                        walk(s.else_body)
                elif isinstance(s, While):
                    walk(s.body)

        walk(stmts)
        return sizes

    def gen_func(self, f):
        self.locals = {pname: 8 + 4 * i for i, (pname, _) in enumerate(f.params)}
        self.var_types = dict(f.params)
        self.for_ends = {}
        self.for_steps = {}
        self.for_lit_storage = {}
        self.for_list_base = {}
        self.for_list_idx = {}
        self.list_storage = {}
        self.loop_stack = []
        self.ret_label = self._label()
        slot = 0
        self._list_max = self._max_list_sizes(f.body)
        nslots = self._collect(f.body, slot)
        frame = nslots * 4
        self.lines.append(f"{f.name}:")
        self._emit("PUSH r14")
        self._emit("MOV r14, r15")
        if frame > 0:
            self._emit(f"MOV r2, {frame}")
            self._emit("SUB r15, r15, r2")
        for s in f.body:
            self.gen_stmt(s)
        self._emit(f"{self.ret_label}:")
        self._emit("MOV r15, r14")
        self._emit("POP r14")
        self._emit("RET")

    def _collect(self, stmts, slot):
        """第一遍: 按顺序分配局部变量槽(与 typecheck 的声明顺序一致); 返回新槽计数。
        列表变量占 1 个地址槽 + size+1 个存储槽([len][elem0]... 沿 FP 下方递减)。"""
        for s in stmts:
            if isinstance(s, Decl):
                if is_list(s.typ):
                    base_size = s.typ[2] if s.typ[2] is not None else len(s.expr.elems)
                    size = max(base_size, self._list_max.get(s.name, 0))
                    self.locals[s.name] = -4 - 4 * slot
                    self.var_types[s.name] = s.typ
                    slot += 1
                    self.list_storage[s.name] = -4 - 4 * slot   # 存储块顶(len 位于块底)
                    slot += size + 1
                else:
                    n = 1
                    self.locals[s.name] = -4 - 4 * slot
                    self.var_types[s.name] = s.typ
                    slot += n
            elif isinstance(s, Assign):
                if s.name not in self.var_types:
                    t = self._infer_type(s.expr)
                    if is_list(t) and isinstance(s.expr, ListLit):
                        self.locals[s.name] = -4 - 4 * slot
                        self.var_types[s.name] = t
                        slot += 1
                        self.list_storage[s.name] = -4 - 4 * slot
                        slot += len(s.expr.elems) + 1
                    else:
                        n = 1
                        self.locals[s.name] = -4 - 4 * slot
                        self.var_types[s.name] = t
                        slot += n
            elif isinstance(s, For):
                if s.mode == "range":
                    self.locals[s.var] = -4 - 4 * slot
                    self.var_types[s.var] = "i32"
                    slot += 1
                    if not isinstance(s.step, IntLit) or \
                            not (isinstance(s.end, IntLit) and CMPI_MIN <= s.end.val <= CMPI_MAX):
                        self.for_ends[id(s)] = -4 - 4 * slot
                        slot += 1
                    if not isinstance(s.step, IntLit):
                        self.for_steps[id(s)] = -4 - 4 * slot
                        slot += 1
                else:
                    self.locals[s.var] = -4 - 4 * slot
                    self.var_types[s.var] = self._elem_type(s.iter)
                    slot += 1
                    if isinstance(s.iter, ListLit):
                        self.for_lit_storage[id(s)] = -4 - 4 * slot
                        slot += len(s.iter.elems) + 1
                    self.for_list_base[id(s)] = -4 - 4 * slot
                    slot += 1
                    self.for_list_idx[id(s)] = -4 - 4 * slot
                    slot += 1
                slot = self._collect(s.body, slot)
                if s.else_body:
                    slot = self._collect(s.else_body, slot)
            elif isinstance(s, If):
                slot = self._collect(s.body, slot)
                for _, b in s.elifs:
                    slot = self._collect(b, slot)
                if s.else_body:
                    slot = self._collect(s.else_body, slot)
            elif isinstance(s, While):
                slot = self._collect(s.body, slot)
                if s.else_body:
                    slot = self._collect(s.else_body, slot)
        return slot

    def _infer_type(self, e):
        if isinstance(e, IntLit):
            return "i32"
        if isinstance(e, BoolLit):
            return "bool"
        if isinstance(e, StrLit):
            return "str"
        if isinstance(e, ListLit):
            elem = self._elem_type_of_literal(e.elems)
            return ("list", elem, len(e.elems))
        if isinstance(e, Var):
            return self.var_types.get(e.name) or self.global_types.get(e.name) or "i32"
        if isinstance(e, Index):
            bt = self._infer_type(e.base)
            return bt[1] if is_list(bt) else "i32"
        if isinstance(e, Unary):
            return self._infer_type(e.operand)
        if isinstance(e, BinOp):
            if e.op in ("and", "or", "==", "!=", "<", ">", "<=", ">=", "in"):
                return "bool"
            if e.op == "+" and self._infer_type(e.left) == "str":
                return "str"
            return "i32"
        if isinstance(e, Ternary):
            return self._infer_type(e.if_expr)   # 两分支同类型(typecheck 已保证)
        if isinstance(e, Call):
            t = {"print": "void", "input": "i32", "getch": "i32", "clrscr": "void",
                    "sleep": "void", "len": "i32", "abs": "i32",
                    "min": "i32", "max": "i32", "sum": "i32", "pow": "i32",
                    "window": "void", "clear": "void", "fill_rect": "void",
                    "fill_circle": "void", "draw_line": "void", "draw_char": "void",
                    "poll_key": "i32", "window_closed": "i32", "present": "void",
                    "sqrt": "i32", "gcd": "i32", "clamp": "i32",
                    "str": "str"}.get(e.name)
            if t is not None:
                return t
            f = self.funcs.get(e.name)      # v3.0: 用户函数调用的返回类型
            if f is not None:
                return f.ret or "void"
            return "i32"
        return "i32"

    def _elem_type(self, e):
        t = self._infer_type(e)
        return t[1] if is_list(t) else "i32"

    @staticmethod
    def _elem_type_of_literal(elems):
        for e in elems:
            if isinstance(e, IntLit):
                return "i32"
            if isinstance(e, BoolLit):
                return "bool"
        return "?"

    def _t(self, e):
        return self._infer_type(e)

    # ---------- 语句 ----------
    def gen_stmt(self, s):
        if isinstance(s, Decl):
            if is_list(s.typ):
                lit = s.expr.elems if s.expr is not None else []
                size = s.typ[2] if s.typ[2] is not None else len(lit)
                store = self.list_storage[s.name]
                self._gen_list_init(store, size, lit)
                self._emit("MOV r0, r14")
                self._emit(f"MOV r1, {store - 4 * size}")
                self._emit("ADD r0, r0, r1")
                self._emit(f"ST {self._mem(self.locals[s.name])}, r0")
            else:
                self.gen_expr(s.expr)
                self._emit(f"ST {self._mem(self.locals[s.name])}, r0")
        elif isinstance(s, Assign):
            if isinstance(s.expr, ListLit):
                size = len(s.expr.elems)
                store = self.list_storage[s.name]
                self._gen_list_init(store, size, s.expr.elems)
                self._emit("MOV r0, r14")
                self._emit(f"MOV r1, {store - 4 * size}")
                self._emit("ADD r0, r0, r1")
                self._emit(f"ST {self._mem(self.locals[s.name])}, r0")
            else:
                self.gen_expr(s.expr)
                self._emit(f"ST {self._mem(self.locals[s.name])}, r0")
        elif isinstance(s, IndexAssign):
            # 目标列表地址入栈 → 索引 → 检查 → 地址入栈 → 值 → 写回
            self.gen_expr(s.base)
            self._emit("PUSH r0")
            self.gen_expr(s.index)
            self._emit("POP r1")
            self._bounds_check()
            self._emit("MOV r2, 4")
            self._emit("MUL r0, r0, r2")
            self._emit("ADD r0, r0, r2")
            self._emit("ADD r1, r1, r0")
            self._emit("PUSH r1")       # 元素地址(单次寻址)
            self.gen_expr(s.expr)
            self._emit("POP r2")
            self._emit("ST [r2], r0")
        elif isinstance(s, AugAssign):
            self._gen_aug_assign(s)
        elif isinstance(s, Return):
            if s.expr is not None:
                self.gen_expr(s.expr)
            self._emit(f"JMP {self.ret_label}")
        elif isinstance(s, If):
            # 统一 if/elif/else 链: 每个条件假 → 跳下一个条件; 最后一个条件假 → else/出口
            l_after = self._label()     # 整体出口
            conds = [(s.cond, s.body)] + list(s.elifs)
            for idx, (c, b) in enumerate(conds):
                is_last = idx == len(conds) - 1
                self.gen_expr(c)
                self._emit("CMPI r0, 0")
                if is_last:
                    l_false = self._label()
                    self._emit(f"JE {l_false}")
                    for st in b:
                        self.gen_stmt(st)
                    self._emit(f"JMP {l_after}")
                    if s.else_body:
                        self._emit(f"{l_false}:")
                        for st in s.else_body:
                            self.gen_stmt(st)
                    else:
                        self._emit(f"{l_false}:")
                    self._emit(f"{l_after}:")
                else:
                    l_next = self._label()
                    self._emit(f"JE {l_next}")
                    for st in b:
                        self.gen_stmt(st)
                    self._emit(f"JMP {l_after}")
                    self._emit(f"{l_next}:")
        elif isinstance(s, While):
            l_cond = self._label()
            l_after = self._label()   # 循环之后(无 else 时= l_end; 有 else 时= else 后)
            l_end = self._label() if s.else_body else l_after
            self.loop_stack.append((l_after, l_cond))
            self._emit(f"{l_cond}:")
            self.gen_expr(s.cond)
            self._emit("CMPI r0, 0")
            self._emit(f"JE {l_end}")
            for st in s.body:
                self.gen_stmt(st)
            self._emit(f"JMP {l_cond}")
            if s.else_body:
                self._emit(f"{l_end}:")
                for st in s.else_body:
                    self.gen_stmt(st)
            self._emit(f"{l_after}:")
            self.loop_stack.pop()
        elif isinstance(s, For):
            self.gen_for(s)
        elif isinstance(s, Break):
            self._emit(f"JMP {self.loop_stack[-1][0]}")
        elif isinstance(s, Continue):
            self._emit(f"JMP {self.loop_stack[-1][1]}")
        elif isinstance(s, Pass):
            pass
        elif isinstance(s, ExprStmt):
            self.gen_expr(s.expr)
        else:
            raise RuntimeError("codegen: 未知语句节点")

    def _gen_aug_assign(self, s):
        op = s.op
        if isinstance(s.target, Index):
            # 地址(单次寻址) → 旧值 → 表达式 → 运算 → 写回
            self.gen_expr(s.target.base)
            self._emit("PUSH r0")
            self.gen_expr(s.target.index)
            self._emit("POP r1")
            self._bounds_check()
            self._emit("MOV r2, 4")
            self._emit("MUL r0, r0, r2")
            self._emit("ADD r0, r0, r2")
            self._emit("ADD r1, r1, r0")
            self._emit("PUSH r1")        # 元素地址
            self._emit("LD r0, [r1]")
            self._emit("PUSH r0")        # 旧值
            self.gen_expr(s.expr)
            self._emit("POP r1")
            self._gen_binop_body(AUG_TO_OP[op], r1_is_left=True)
            self._emit("POP r2")
            self._emit("ST [r2], r0")
        else:
            name = s.target.name
            self._emit(f"LD r0, {self._mem(self.locals[name])}")
            self._emit("PUSH r0")
            self.gen_expr(s.expr)
            self._emit("POP r1")
            self._gen_binop_body(AUG_TO_OP[op], r1_is_left=True)
            self._emit(f"ST {self._mem(self.locals[name])}, r0")

    # ---------- 循环 ----------
    def gen_for(self, s):
        l_cond = self._label()
        l_inc = self._label()
        l_after = self._label()          # 循环整体之后(break 也跳这里, 跳过 else)
        l_end = self._label() if s.else_body else l_after   # 正常结束进入 else
        self.loop_stack.append((l_after, l_inc))
        if s.mode == "range":
            self._gen_for_range(s, l_cond, l_inc, l_end)
        else:
            self._gen_for_list(s, l_cond, l_inc, l_end)
        # _gen_for_range/list 内部已 emit {l_end}:, else 块紧跟其后; break 跳 l_after
        if s.else_body:
            for st in s.else_body:
                self.gen_stmt(st)
            self._emit(f"{l_after}:")
        self.loop_stack.pop()

    def _gen_for_range(self, s, l_cond, l_inc, l_end):
        var_off = self.locals[s.var]
        step_is_lit = isinstance(s.step, IntLit)
        # 初始值
        self.gen_expr(s.start)
        self._emit(f"ST {self._mem(var_off)}, r0")
        # 结束值: 字面量且在 CMPI 范围 → 免槽; 否则存槽
        if id(s) in self.for_ends:
            self.gen_expr(s.end)
            self._emit(f"ST {self._mem(self.for_ends[id(s)])}, r0")
        # 步长: 字面量 → 编译期方向; 动态 → 存槽 + 入口一次判零
        if not step_is_lit:
            self.gen_expr(s.step)
            self._emit(f"ST {self._mem(self.for_steps[id(s)])}, r0")
            l_ok = self._label()
            self._emit(f"LD r0, {self._mem(self.for_steps[id(s)])}")
            self._emit("CMPI r0, 0")
            self._emit(f"JNE {l_ok}")
            self._emit("TRAP 2")
            self._emit(f"{l_ok}:")
        # 循环条件
        self._emit(f"{l_cond}:")
        self._emit(f"LD r0, {self._mem(var_off)}")
        if id(s) in self.for_ends:
            self._emit(f"LD r1, {self._mem(self.for_ends[id(s)])}")
            if step_is_lit:
                if s.step.val > 0:
                    self._emit("CMP r0, r1")
                    self._emit(f"JGE {l_end}")
                else:
                    self._emit("CMP r0, r1")
                    self._emit(f"JLE {l_end}")
            else:
                # 动态步长: 方向分支(每圈仅一次)
                self._emit("LD r2, " + self._mem(self.for_steps[id(s)]))
                l_pos = self._label()
                self._emit("CMPI r2, 0")
                self._emit(f"JG {l_pos}")
                self._emit("CMP r0, r1")
                self._emit(f"JLE {l_end}")
                self._emit(f"JMP {l_pos}")
                self._emit(f"{l_pos}:")
                self._emit("CMP r0, r1")
                self._emit(f"JGE {l_end}")
        else:
            # 字面量边界用 CMPI(免一次内存读)
            if step_is_lit and s.step.val > 0:
                self._emit(f"CMPI r0, {s.end.val}")
                self._emit(f"JGE {l_end}")
            elif step_is_lit:
                self._emit(f"CMPI r0, {s.end.val}")
                self._emit(f"JLE {l_end}")
            else:
                raise RuntimeError("动态步长必须有结束槽")
        for st in s.body:
            self.gen_stmt(st)
        # 增量
        self._emit(f"{l_inc}:")
        self._emit(f"LD r0, {self._mem(var_off)}")
        if step_is_lit:
            self._emit(f"MOV r1, {s.step.val}")
        else:
            self._emit(f"LD r1, {self._mem(self.for_steps[id(s)])}")
        self._emit("ADD r0, r0, r1")
        self._emit(f"ST {self._mem(var_off)}, r0")
        self._emit(f"JMP {l_cond}")
        self._emit(f"{l_end}:")

    def _gen_for_list(self, s, l_cond, l_inc, l_end):
        var_off = self.locals[s.var]
        base_off = self.for_list_base[id(s)]
        idx_off = self.for_list_idx[id(s)]
        if id(s) in self.for_lit_storage:
            store_off = self.for_lit_storage[id(s)]
            lit = s.iter
            len_off = store_off - 4 * len(lit.elems)
            self._emit(f"MOV r0, {len(lit.elems)}")
            self._emit(f"ST {self._mem(len_off)}, r0")
            for k, e in enumerate(lit.elems):
                self.gen_expr(e)
                self._emit(f"ST {self._mem(len_off + 4 + 4 * k)}, r0")
            self._emit("MOV r0, r14")
            self._emit(f"MOV r1, {len_off}")
            self._emit("ADD r0, r0, r1")
            self._emit(f"ST {self._mem(base_off)}, r0")
        else:
            self.gen_expr(s.iter)
            self._emit(f"ST {self._mem(base_off)}, r0")
        self._emit("MOV r0, 0")
        self._emit(f"ST {self._mem(idx_off)}, r0")
        self._emit(f"{l_cond}:")
        self._emit(f"LD r0, {self._mem(base_off)}")
        self._emit("LD r0, [r0]")                  # len
        self._emit(f"LD r1, {self._mem(idx_off)}")
        self._emit("CMP r1, r0")
        self._emit(f"JGE {l_end}")
        # x = xs[idx]
        self._emit(f"LD r0, {self._mem(base_off)}")
        self._emit(f"LD r1, {self._mem(idx_off)}")
        self._emit("MOV r2, 4")
        self._emit("MUL r1, r1, r2")
        self._emit("ADD r1, r1, r2")
        self._emit("ADD r0, r0, r1")
        self._emit("LD r0, [r0]")
        self._emit(f"ST {self._mem(var_off)}, r0")
        for st in s.body:
            self.gen_stmt(st)
        self._emit(f"{l_inc}:")
        self._emit(f"LD r0, {self._mem(idx_off)}")
        self._emit("MOV r1, 1")
        self._emit("ADD r0, r0, r1")
        self._emit(f"ST {self._mem(idx_off)}, r0")
        self._emit(f"JMP {l_cond}")
        self._emit(f"{l_end}:")

    def _gen_list_init(self, store_top, size, lit):
        """把列表字面量写入存储块: len 在块底, 元素向上增长(与全局数据段一致)。"""
        len_off = store_top - 4 * size
        self._emit(f"MOV r0, {size}")
        self._emit(f"ST {self._mem(len_off)}, r0")
        for k, e in enumerate(lit):
            self.gen_expr(e)
            self._emit(f"ST {self._mem(len_off + 4 + 4 * k)}, r0")
        rest = size - len(lit)
        if rest > 0:
            self._emit("MOV r0, 0")
            self._emit("MOV r2, r14")
            self._emit(f"MOV r3, {len_off + 4 + 4 * len(lit)}")
            self._emit("ADD r2, r2, r3")
            self._emit(f"MOV r3, {rest}")
            lf = self._label()
            lfd = self._label()
            self._emit(f"{lf}:")
            self._emit("CMPI r3, 0")
            self._emit(f"JE {lfd}")
            self._emit("ST [r2], r0")
            self._emit("MOV r4, 4")
            self._emit("ADD r2, r2, r4")
            self._emit("MOV r4, 1")
            self._emit("SUB r3, r3, r4")
            self._emit(f"JMP {lf}")
            self._emit(f"{lfd}:")

    def _bounds_check(self):
        """r1=列表基址, r0=索引 → 负索引修正 + 越界检查(TRAP 1)。"""
        lp = self._label()
        lb = self._label()
        self._emit("CMPI r0, 0")
        self._emit(f"JGE {lp}")
        self._emit("LD r2, [r1]")
        self._emit("ADD r0, r0, r2")
        self._emit(f"{lp}:")
        self._emit("CMPI r0, 0")      # 修正后仍为负 → 越界
        self._emit(f"JGE {lb}")
        self._emit("TRAP 1")
        self._emit(f"{lb}:")
        lb2 = self._label()
        self._emit("LD r2, [r1]")
        self._emit("CMP r0, r2")
        self._emit(f"JL {lb2}")
        self._emit("TRAP 1")
        self._emit(f"{lb2}:")

    # ---------- 表达式 ----------
    def gen_expr(self, e):
        if isinstance(e, IntLit):
            self._emit(f"MOV r0, {e.val}")
        elif isinstance(e, BoolLit):
            self._emit(f"MOV r0, {1 if e.val else 0}")
        elif isinstance(e, StrLit):
            lab = self._new_str(e.val)
            self._emit(f"LDA r0, {lab}")
        elif isinstance(e, Var):
            if e.name in self.var_types:
                self._emit(f"LD r0, {self._mem(self.locals[e.name])}")
            elif e.name in self.global_types:
                lab = f"__g_{e.name}"
                if self.global_types[e.name] == "str" or is_list(self.global_types[e.name]):
                    self._emit(f"LDA r0, {lab}")
                else:
                    self._emit(f"LDA r0, {lab}")
                    self._emit("LD r0, [r0]")
            else:
                raise RuntimeError(f"codegen: 未声明变量 {e.name}")
        elif isinstance(e, Index):
            bt = self._infer_type(e.base)
            self._gen_index(e, is_str=bt == "str")
        elif isinstance(e, Unary):
            if e.op == "-":
                self.gen_expr(e.operand)
                self._emit("MOV r1, 0")
                self._emit("SUB r0, r1, r0")
            elif e.op == "~":
                self.gen_expr(e.operand)
                self._emit("NOT r0, r0")
            else:  # not
                self.gen_expr(e.operand)
                self._emit("CMPI r0, 0")
                self._emit("MOV r0, 1")
                lz = self._label()
                self._emit(f"JE {lz}")
                self._emit("MOV r0, 0")
                self._emit(f"{lz}:")
        elif isinstance(e, Ternary):
            self.gen_expr(e.cond)
            self._emit("CMPI r0, 0")
            l_else = self._label()
            l_end = self._label()
            self._emit(f"JE {l_else}")
            self.gen_expr(e.if_expr)
            self._emit(f"JMP {l_end}")
            self._emit(f"{l_else}:")
            self.gen_expr(e.else_expr)
            self._emit(f"{l_end}:")
        elif isinstance(e, BinOp):
            self._gen_binop(e)
        elif isinstance(e, Call):
            self.gen_call(e)
        elif isinstance(e, ListLit):
            raise RuntimeError("codegen: 列表字面量必须出现在声明/初始化中")
        else:
            raise RuntimeError(f"codegen: 未知表达式 {type(e).__name__}")

    def _gen_index(self, e, is_str):
        """求值 e.base[e.index] → r0。r1=基址, r0=索引。"""
        self.gen_expr(e.base)
        self._emit("PUSH r0")
        self.gen_expr(e.index)
        self._emit("POP r1")
        self._bounds_check()
        if is_str:
            # s[i]: 逐字节扫描找目标字符
            self._emit("MOV r2, r1")
            self._emit("MOV r3, 0")
            lscan = self._label()
            lfound = self._label()
            self._emit(f"{lscan}:")
            self._emit("LDB r4, [r2]")
            self._emit("CMP r3, r0")
            self._emit(f"JE {lfound}")
            self._emit("CMPI r4, 0")
            lmiss = self._label()
            self._emit(f"JNE {lmiss}")
            self._emit("TRAP 1")           # 索引超出字符串长度
            self._emit(f"{lmiss}:")
            self._emit("MOV r5, 1")
            self._emit("ADD r2, r2, r5")
            self._emit("ADD r3, r3, r5")
            self._emit(f"JMP {lscan}")
            self._emit(f"{lfound}:")
            self._emit("MOV r0, r4")
        else:
            self._emit("MOV r2, 4")
            self._emit("MUL r0, r0, r2")
            self._emit("ADD r0, r0, r2")
            self._emit("ADD r1, r1, r0")   # 元素地址 = base + 4 + 4*idx
            self._emit("LD r0, [r1]")

    def _gen_binop(self, e):
        op = e.op
        lt = self._t(e.left)
        rt = self._t(e.right)
        if op == "+" and (lt == "str" or rt == "str"):
            # v3.0: 运行时字符串拼接 → 写入编译期分配的 256B 临时池槽
            self.gen_expr(e.left)          # r0 = 左串地址
            self._emit("PUSH r0")
            self.gen_expr(e.right)         # r0 = 右串地址
            self._emit("POP r1")           # r1 = 左串地址
            slot = self._new_str_slot()
            self._emit(f"LDA r2, {slot}")
            self._emit("STRCAT r2, r1, r0")
            self._emit("MOV r0, r2")
            return
        if op in ("==", "!=", "<", ">", "<=", ">=") and (lt == "str" or rt == "str"):
            # v3.0: 字符串字典序比较 (STRCMP 返回 -1/0/1)
            self.gen_expr(e.left)
            self._emit("PUSH r0")
            self.gen_expr(e.right)
            self._emit("POP r1")
            self._emit("STRCMP r2, r1, r0")
            self._emit("MOV r0, 1")
            l_t = self._label()
            if op == "==":
                self._emit("CMPI r2, 0"); self._emit(f"JE {l_t}")
            elif op == "!=":
                self._emit("CMPI r2, 0"); self._emit(f"JNE {l_t}")
            elif op == "<":
                self._emit("CMPI r2, -1"); self._emit(f"JE {l_t}")
            elif op == ">":
                self._emit("CMPI r2, 1"); self._emit(f"JE {l_t}")
            elif op == "<=":
                self._emit("CMPI r2, 1"); self._emit(f"JNE {l_t}")    # r2 != 1 → true
            elif op == ">=":
                self._emit("CMPI r2, -1"); self._emit(f"JNE {l_t}")   # r2 != -1 → true
            self._emit("MOV r0, 0")
            self._emit(f"{l_t}:")
            return
        if op in ("and", "or"):
            # 短路求值
            self.gen_expr(e.left)
            self._emit("CMPI r0, 0")
            l_r = self._label()
            if op == "and":
                self._emit(f"JE {l_r}")      # 左假 → 结果 0
            else:
                self._emit(f"JNE {l_r}")     # 左真 → 结果 1
            self.gen_expr(e.right)
            self._emit("CMPI r0, 0")
            self._emit("MOV r0, 0")
            l_end = self._label()
            self._emit(f"JE {l_end}")
            self._emit("MOV r0, 1")
            self._emit(f"JMP {l_end}")
            self._emit(f"{l_r}:")
            self._emit("MOV r0, 0" if op == "and" else "MOV r0, 1")
            self._emit(f"{l_end}:")
            return
        if op in ("==", "!=", "<", ">", "<=", ">="):
            self.gen_expr(e.left)
            self._emit("PUSH r0")
            self.gen_expr(e.right)
            self._emit("POP r1")
            self._emit("CMP r1, r0")
            self._emit("MOV r0, 1")
            l_t = self._label()
            self._emit(f"{CMP_CC[op]} {l_t}")
            self._emit("MOV r0, 0")
            self._emit(f"{l_t}:")
            return
        if op == "in":
            # x in xs / 字符码 in str: 线性扫描; 找到 → 1, 否则 → 0
            self.gen_expr(e.left)
            self._emit("PUSH r0")          # 目标值
            self.gen_expr(e.right)
            self._emit("POP r3")           # r3 = 目标值; r0 = 字符串地址或列表基址
            bt = self._infer_type(e.right)
            l_scan = self._label()
            l_found = self._label()
            l_miss = self._label()
            l_done = self._label()
            if bt == "str":
                # 逐字节扫描(字符串以 0 结尾), r0 自增
                self._emit(f"{l_scan}:")
                self._emit("LDB r1, [r0 + 0]")
                self._emit("CMPI r1, 0")
                self._emit(f"JE {l_miss}")
                self._emit("CMP r1, r3")
                self._emit(f"JE {l_found}")
                self._emit("MOV r5, 1")
                self._emit("ADD r0, r0, r5")
                self._emit(f"JMP {l_scan}")
            else:
                # list: [r0]=len, 元素 = base+4+4*idx
                self._emit("LD r1, [r0]")  # len
                self._emit("MOV r2, 0")    # idx
                self._emit(f"{l_scan}:")
                self._emit("CMP r2, r1")
                self._emit(f"JGE {l_miss}")
                self._emit("MOV r4, 4")
                self._emit("MUL r4, r2, r4")
                self._emit("MOV r5, 4")
                self._emit("ADD r4, r4, r5")
                self._emit("ADD r4, r0, r4")
                self._emit("LD r4, [r4]")
                self._emit("CMP r4, r3")
                self._emit(f"JE {l_found}")
                self._emit("MOV r5, 1")
                self._emit("ADD r2, r2, r5")
                self._emit(f"JMP {l_scan}")
            self._emit(f"{l_miss}:")
            self._emit("MOV r0, 0")
            self._emit(f"JMP {l_done}")
            self._emit(f"{l_found}:")
            self._emit("MOV r0, 1")
            self._emit(f"{l_done}:")
            return
        # 算术
        self.gen_expr(e.left)
        self._emit("PUSH r0")
        self.gen_expr(e.right)
        self._emit("POP r1")
        self._gen_binop_body(op, r1_is_left=True)

    def _gen_binop_body(self, op, r1_is_left=True):
        """r1=左, r0=右 → r0=结果(// 走地板除序列)。"""
        if op == "//":
            # 地板除: 结果 = 截断除(正除) - (异号且有余数)
            self._emit("MOV r2, r1")
            self._emit("MOV r3, r0")
            self._emit("DIV r0, r1, r0")      # 截断商
            self._emit("MOV r4, r2")
            self._emit("MOV r5, r3")
            self._emit("MOD r5, r4, r5")      # 余数(与原除一致)
            self._emit("XOR r4, r2, r3")      # 异号?
            l_done = self._label()
            self._emit("CMPI r4, 0")
            self._emit(f"JGE {l_done}")
            self._emit("CMPI r5, 0")
            self._emit(f"JE {l_done}")
            self._emit("MOV r4, 1")
            self._emit("SUB r0, r0, r4")
            self._emit(f"{l_done}:")
            return
        self._emit(f"{ARITH_ASM[op]} r0, r1, r0")

    # ---------- 调用 ----------
    def gen_call(self, c: Call):
        if c.name == "print":
            self._gen_print(c.args)
            return
        if c.name == "input":
            self._emit("IN r0")
            return
        if c.name == "getch":
            self.gen_expr(c.args[0])
            self._emit("TRAP 10")       # 系统调用: 无缓冲读键(超时ms)
            return
        if c.name == "clrscr":
            self._emit("TRAP 11")       # 系统调用: 清屏
            return
        if c.name == "sleep":
            self.gen_expr(c.args[0])
            self._emit("TRAP 12")       # 系统调用: 延时(毫秒)
            return
        GFX = {"window": 20, "clear": 21, "fill_rect": 22, "fill_circle": 23,
               "draw_line": 24, "draw_char": 25, "poll_key": 26,
               "window_closed": 27, "present": 28}
        if c.name in GFX:
            for a in c.args:            # 参数依次压栈
                self.gen_expr(a)
                self._emit("PUSH r0")
            n = len(c.args)
            for i in range(n):          # 反序弹出: 最后一个参数 → 最高寄存器
                self._emit(f"POP r{n - 1 - i}")
            self._emit(f"TRAP {GFX[c.name]}")
            return
        if c.name == "len":
            arg = c.args[0]
            if self._t(arg) == "str":
                self.gen_expr(arg)
                self._emit("MOV r1, r0")
                self._emit("MOV r2, 0")
                lscan = self._label()
                ldone = self._label()
                self._emit(f"{lscan}:")
                self._emit("LDB r3, [r1]")
                self._emit("CMPI r3, 0")
                self._emit(f"JE {ldone}")
                self._emit("MOV r4, 1")
                self._emit("ADD r1, r1, r4")
                self._emit("ADD r2, r2, r4")
                self._emit(f"JMP {lscan}")
                self._emit(f"{ldone}:")
                self._emit("MOV r0, r2")
            else:
                self.gen_expr(arg)
                self._emit("LD r0, [r0]")
            return
        if c.name == "abs":
            self.gen_expr(c.args[0])
            l_pos = self._label()
            self._emit("CMPI r0, 0")
            self._emit(f"JGE {l_pos}")
            self._emit("MOV r1, 0")
            self._emit("SUB r0, r1, r0")
            self._emit(f"{l_pos}:")
            return
        if c.name in ("min", "max"):
            # 逐个比较取极值
            self.gen_expr(c.args[0])
            for a in c.args[1:]:
                self._emit("PUSH r0")
                self.gen_expr(a)
                self._emit("POP r1")
                l_keep = self._label()
                self._emit("CMP r1, r0")
                cc = "JLE" if c.name == "min" else "JGE"   # 新值更小/更大则替换
                self._emit(f"{cc} {l_keep}")
                self._emit("MOV r0, r1")
                self._emit(f"{l_keep}:")
            return
        if c.name == "sum":
            # sum(xs): 遍历求和(len 在块底, 元素 base+4+4*idx)
            self.gen_expr(c.args[0])
            self._emit("MOV r1, r0")          # 基址
            self._emit("LD r2, [r1]")         # len
            self._emit("MOV r3, 0")           # 和
            self._emit("MOV r4, 0")           # idx
            lscan = self._label()
            ldone = self._label()
            self._emit(f"{lscan}:")
            self._emit("CMP r4, r2")
            self._emit(f"JGE {ldone}")
            self._emit("MOV r5, 4")
            self._emit("MUL r5, r4, r5")
            self._emit("MOV r6, 4")
            self._emit("ADD r5, r5, r6")
            self._emit("ADD r5, r1, r5")
            self._emit("LD r5, [r5]")
            self._emit("ADD r3, r3, r5")
            self._emit("MOV r6, 1")
            self._emit("ADD r4, r4, r6")
            self._emit(f"JMP {lscan}")
            self._emit(f"{ldone}:")
            self._emit("MOV r0, r3")
            return
        if c.name == "str":
            self.gen_expr(c.args[0])       # r0 = 数字
            self._emit("PUSH r0")
            slot = self._new_str_slot()
            self._emit(f"LDA r1, {slot}")  # r1 = 目标缓冲(池槽)
            self._emit("POP r0")
            self._emit("TRAP 17")          # 十进制字符串写入 r1, 结果地址 → r0
            return
        if c.name == "sqrt":
            self.gen_expr(c.args[0])
            self._emit("TRAP 14")       # 整数平方根(向下取整; 负数 → 0)
            return
        if c.name == "gcd":
            self.gen_expr(c.args[0])    # r0 = a
            self._emit("PUSH r0")
            self.gen_expr(c.args[1])    # r0 = b
            self._emit("POP r1")        # r1 = a
            self._emit("MOV r2, r0")    # r2 = b
            self._emit("MOV r0, r1")    # r0 = a
            self._emit("MOV r1, r2")    # r1 = b
            self._emit("TRAP 15")
            return
        if c.name == "clamp":
            self.gen_expr(c.args[0])    # r0 = x
            self._emit("PUSH r0")
            self.gen_expr(c.args[1])    # r0 = lo
            self._emit("PUSH r0")
            self.gen_expr(c.args[2])    # r0 = hi
            self._emit("POP r2")        # r2 = lo
            self._emit("POP r1")        # r1 = x
            self._emit("MOV r3, r0")    # r3 = hi
            self._emit("MOV r0, r1")    # r0 = x
            self._emit("MOV r1, r2")    # r1 = lo
            self._emit("MOV r2, r3")    # r2 = hi
            self._emit("TRAP 16")
            return
        if c.name == "pow":
            # pow(a, b): 循环乘(32 位回绕); b<0 → 1(与 x86 幂语义一致)
            self.gen_expr(c.args[0])
            self._emit("PUSH r0")             # 底数
            self.gen_expr(c.args[1])
            self._emit("POP r1")              # r1 = 底数
            self._emit("MOV r2, r0")          # r2 = 指数
            self._emit("MOV r0, 1")           # 结果
            lscan = self._label()
            ldone = self._label()
            self._emit(f"{lscan}:")
            self._emit("CMPI r2, 0")
            self._emit(f"JLE {ldone}")
            self._emit("MUL r0, r0, r1")
            self._emit("MOV r3, 1")
            self._emit("SUB r2, r2, r3")
            self._emit(f"JMP {lscan}")
            self._emit(f"{ldone}:")
            return
        # 用户函数 (v3.0: 缺失参数自动补默认值, 仍从右向左压栈)
        func = self.funcs.get(c.name)
        defaults = func.defaults if func is not None else []
        args = list(c.args)
        while len(args) < len(self.funcs[c.name].params):
            dv = defaults[len(args)]
            if dv is None:
                raise RuntimeError(f"codegen: 函数 {c.name} 缺参数")
            args.append(dv)
        for a in reversed(args):
            self.gen_expr(a)
            self._emit("PUSH r0")
        self._emit(f"CALL {c.name}")
        if args:
            self._emit(f"MOV r2, {4 * len(args)}")
            self._emit("ADD r15, r15, r2")

    def _gen_print(self, args):
        for i, arg in enumerate(args):
            if self._is_str_expr(arg):
                self._gen_str_addr(arg)
                lscan = self._label()
                ldone = self._label()
                self._emit(f"{lscan}:")
                self._emit("LDB r1, [r0]")
                self._emit("CMPI r1, 0")
                self._emit(f"JE {ldone}")
                self._emit("OUT r1")
                self._emit("MOV r2, 1")
                self._emit("ADD r0, r0, r2")
                self._emit(f"JMP {lscan}")
                self._emit(f"{ldone}:")
            else:
                self.gen_expr(arg)
                self._emit("OUTI r0")
            if i < len(args) - 1:
                self._emit("MOV r2, 32")
                self._emit("OUT r2")
        self._emit("MOV r2, 10")
        self._emit("OUT r2")

    def _is_str_expr(self, e):
        if isinstance(e, StrLit):
            return True
        if isinstance(e, Ternary):
            return self._infer_type(e) == "str"
        if isinstance(e, Call):
            return self._infer_type(e) == "str"   # 返回 str 的用户函数/内置调用
        if isinstance(e, BinOp):
            return self._infer_type(e) == "str"   # v3.0: 字符串拼接表达式
        if isinstance(e, Var):
            return self.var_types.get(e.name) == "str" or self.global_types.get(e.name) == "str"
        return False

    def _gen_str_addr(self, e):
        if isinstance(e, StrLit):
            lab = self._new_str(e.val)
            self._emit(f"LDA r0, {lab}")
        else:  # Var(str)/Ternary/拼接表达式: 直接求值得到字符串地址
            self.gen_expr(e)
