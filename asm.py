#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
asm.py — Flint 的 x86-64 汇编后端(直接产出 AT&T 汇编, 不再经过 C)。

流水线: AST → 静态类型检查 → x86-64 汇编文本 → gcc -no-pie -O2 汇编链接 → 可执行文件。

语义与 VM 后端逐项一致:
  * i32 32 位回绕(算术/移位/幂/列表元素)
  * 地板除 //(异号且余数非零时商 -1);  / 与 % 向零截断
  * 除法/取模: 用 64 位 idiv 避免 INT_MIN/-1 溢出, 商截断回 i32(=VM 回绕语义)
  * 除零 → TRAP 2 (exit code 2, stderr 提示); 索引越界 → TRAP 1 (exit code 1)
  * 列表内存布局: len 在块底(4 字节), 元素向上 4 字节/个; 变量槽存 64 位块底地址
  * 字符串按 UTF-8 字节存储, 0 结尾; s[i] 按字节扫描

栈帧(x86-64 System V 风格):
  * 参数从右向左 pushq; 第 i 个参数在 rbp+16+8i
  * 局部变量槽 8 字节(地址槽存 64 位指针, i32 用低 32 位); 帧大小 = 槽数*8, 向上取 16 对齐
  * 寄存器映射: r0=rax r1=r11 r2=rcx r3=rdx r4=rsi r5=rdi r6=r8 r7=r9(scratch) r8=r10
  * flint 函数调用约定: 与 VM 一致 —— call 破坏所有 caller-saved, 活跃值由 PUSH/POP 保护
"""

import os
import re
import subprocess
import sys
import tempfile

# 便于独立运行调试
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "flint-lang"))

from parser import (Program, FuncDef, Decl, Assign, IndexAssign, AugAssign,
                    Return, If, While, For, Break, Continue, Pass, ExprStmt,
                    BinOp, Unary, Call, Var, Index, IntLit, BoolLit, StrLit,
                    ListLit)
from typecheck import TypeChecker, TypeCheckError, is_list

REG = {"r0": "%eax", "r1": "%r11d", "r2": "%ecx", "r3": "%edx", "r4": "%esi",
       "r5": "%edi", "r6": "%r8d", "r7": "%r9d", "r8": "%r10d"}
INT_MIN = -2147483648

# 三操作数指令 → 两操作数 x86 指令
BIN32 = {"+": "addl", "-": "subl", "*": "imull", "&": "andl", "|": "orl", "^": "xorl"}


class AsmError(Exception):
    pass


class AsmGen:
    def __init__(self):
        self.lines = []
        self.data_lines = []      # (.rodata / .data 统一收集)
        self.ro_lines = []
        self.da_lines = []
        self.label_n = 0
        self.strs = {}            # 内容 → 标签
        self.funcs = {}
        self.global_types = {}
        self.global_data = []     # (label, kind, payload)
        self.cur = None
        self.locals = {}          # 函数内: 名 → 偏移(8 字节槽/参数 16+8i)
        self.var_types = {}
        self.list_storage = {}
        self.for_ends = {}
        self.for_steps = {}
        self.for_lit_storage = {}
        self.for_list_base = {}
        self.for_list_idx = {}
        self.loop_stack = []
        self.ret_label = None

    # ---------- 基础设施 ----------
    def _label(self):
        self.label_n += 1
        return f"L{self.label_n}"

    def _emit(self, s):
        self.lines.append("    " + s)

    def _mem(self, off):
        """FP 相对地址(8 字节槽/参数已换算成 x86 偏移)。"""
        return f"{off}(%rbp)"

    def _pushq(self, reg="%rax"):
        self._emit(f"pushq {reg}")
        self._stack_off += 8

    def _popq(self, reg="%r11"):
        self._emit(f"popq {reg}")
        self._stack_off -= 8

    def _new_str(self, val):
        if val in self.strs:
            return self.strs[val]
        lab = f"__s{len(self.strs) + 1}"
        self.strs[val] = lab
        data = val.encode("utf-8")
        self.ro_lines.append(f"{lab}: .byte {', '.join(str(b) for b in data)}, 0")
        return lab

    # ---------- 入口 ----------
    def generate(self, prog: Program):
        for f in prog.funcs:
            self.funcs[f.name] = f
        for g in prog.globals:
            self.global_types[g.name] = g.typ
            self._add_global_data(g)
        self._emit_global_data()
        for f in prog.funcs:
            self.gen_func(f)
        head = [".section .text", ".globl main", ".globl flt_trap"]
        tail = ["", ".section .rodata",
                ".LCout: .string \"%d\"", ".LCs: .string \"%s\""]
        tail += self.ro_lines
        if self.da_lines:
            tail += ["", ".section .data"] + self.da_lines
        # 运行时助手(trap 打印 + exit, 纯 syscall)
        head += [
            "flt_trap:",
            "    movl %edi, %r10d",
            "    cmpl $1, %edi",
            "    jne 2f",
            "    leaq .Ltrap1(%rip), %rsi",
            "    jmp 3f",
            "2:  leaq .Ltrap2(%rip), %rsi",
            "3:  movl $2, %edi",
            "    movl $1, %eax",
            "    syscall",
            "    movl %r10d, %edi",
            "    movl $60, %eax",
            "    syscall",
            "", ".section .rodata",
            ".Ltrap1: .ascii \"\\xe8\\xbf\\x90\\xe6\\x97\\xb6\\xe8\\xbf\\x9b\\xe9\\x99\\xb7\\xe4\\xba\\x95: \\xe7\\xb4\\xa2\\xe5\\xbc\\x95\\xe8\\xb6\\x8a\\xe7\\x95\\x8c\\n\"",
            ".Ltrap2: .ascii \"\\xe8\\xbf\\x90\\xe6\\x97\\xb6\\xe8\\xbf\\x9b\\xe9\\x99\\xb7\\xe4\\xba\\x95: \\xe9\\x99\\xa4\\xe9\\x9b\\xb6\\xe9\\x94\\x99\\xe8\\xaf\\xaf\\n\"",
            ".section .text"]
        return "\n".join(head + self.lines + tail)

    def _add_global_data(self, g):
        lab = f"__g_{g.name}"
        t = g.typ
        if is_list(t):
            lit = g.expr.elems if g.expr is not None else []
            size = t[2] if t[2] is not None else len(lit)
            vals = [size] + [self._lit_value(e) for e in lit]
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

    def _emit_global_data(self):
        for lab, kind, payload in self.global_data:
            if kind == "str":
                data = payload.encode("utf-8")
                self.ro_lines.append(f"{lab}: .byte {', '.join(str(b) for b in data)}, 0")
            else:
                self.da_lines.append(f"{lab}: .long {', '.join(str(x) for x in payload)}")

    # ---------- 变量收集(8 字节槽) ----------
    def _max_list_sizes(self, stmts):
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
                    if s.else_body:
                        walk(s.else_body)
                elif isinstance(s, If):
                    walk(s.body)
                    for _, b in s.elifs:
                        walk(b)
                    if s.else_body:
                        walk(s.else_body)
                elif isinstance(s, While):
                    walk(s.body)
                    if s.else_body:
                        walk(s.else_body)
        walk(stmts)
        return sizes

    def _collect(self, stmts, slot):
        """第一遍: 按顺序分配 8 字节局部槽(与 typecheck 声明顺序一致)。"""
        for s in stmts:
            if isinstance(s, Decl):
                if is_list(s.typ):
                    base_size = s.typ[2] if s.typ[2] is not None else len(s.expr.elems)
                    size = max(base_size, self._list_max.get(s.name, 0))
                    self.locals[s.name] = -8 - 8 * slot
                    self.var_types[s.name] = s.typ
                    slot += 1
                    self.list_storage[s.name] = -8 - 8 * slot
                    slot += size + 1
                else:
                    self.locals[s.name] = -8 - 8 * slot
                    self.var_types[s.name] = s.typ
                    slot += 1
            elif isinstance(s, Assign):
                if s.name not in self.var_types:
                    t = self._infer_type(s.expr)
                    if is_list(t) and isinstance(s.expr, ListLit):
                        self.locals[s.name] = -8 - 8 * slot
                        self.var_types[s.name] = t
                        slot += 1
                        self.list_storage[s.name] = -8 - 8 * slot
                        slot += len(s.expr.elems) + 1
                    else:
                        self.locals[s.name] = -8 - 8 * slot
                        self.var_types[s.name] = t
                        slot += 1
            elif isinstance(s, For):
                if s.mode == "range":
                    self.locals[s.var] = -8 - 8 * slot
                    self.var_types[s.var] = "i32"
                    slot += 1
                    if not isinstance(s.step, IntLit) or \
                            not (isinstance(s.end, IntLit) and -32768 <= s.end.val <= 32767):
                        self.for_ends[id(s)] = -8 - 8 * slot
                        slot += 1
                    if not isinstance(s.step, IntLit):
                        self.for_steps[id(s)] = -8 - 8 * slot
                        slot += 1
                else:
                    self.locals[s.var] = -8 - 8 * slot
                    self.var_types[s.var] = self._elem_type(s.iter)
                    slot += 1
                    if isinstance(s.iter, ListLit):
                        self.for_lit_storage[id(s)] = -8 - 8 * slot
                        slot += len(s.iter.elems) + 1   # 预留存储块(size+1 个 4 字节字)
                    self.for_list_base[id(s)] = -8 - 8 * slot
                    slot += 1
                    self.for_list_idx[id(s)] = -8 - 8 * slot
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
            return "i32"
        if isinstance(e, Call):
            return {"print": "void", "input": "i32", "len": "i32", "abs": "i32",
                    "min": "i32", "max": "i32", "sum": "i32", "pow": "i32"}.get(e.name, "i32")
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

    # ---------- 指令助手 ----------
    def _bin32(self, op, rd, rs1, rs2):
        """r_rd = r_rs1 op r_rs2 (32 位)。r7(%r9) 作暂存。"""
        rrd, r1, r2 = REG[rd], REG[rs1], REG[rs2]
        if rrd == r1:
            self._emit(f"{op} {r2}, {rrd}")
        elif rrd == r2:
            self._emit(f"movl {r2}, %r9d")
            self._emit(f"movl {r1}, {rrd}")
            self._emit(f"{op} %r9d, {rrd}")
        else:
            self._emit(f"movl {r1}, {rrd}")
            self._emit(f"{op} {r2}, {rrd}")

    def _shift(self, op, rd, rs1, rs2):
        """r_rd = r_rs1 op r_rs2 (x86 移位按 cl 低 5 位掩码 = &31)。"""
        rrd, r1, r2 = REG[rd], REG[rs1], REG[rs2]
        if rrd == r1:
            self._emit(f"movl {r2}, %ecx")
            self._emit(f"{op} %cl, {rrd}")
        elif rrd == r2:
            self._emit(f"movl {r2}, %r9d")
            self._emit(f"movl {r1}, {rrd}")
            self._emit(f"movl %r9d, %ecx")
            self._emit(f"{op} %cl, {rrd}")
        else:
            self._emit(f"movl {r1}, {rrd}")
            self._emit(f"movl {r2}, %ecx")
            self._emit(f"{op} %cl, {rrd}")

    def _gen_divmod(self, rd, rs1, rs2, want_rem):
        """r_rd = r_rs1 / r_rs2 或 % (64 位 idiv, 商/余数截断回 i32; INT_MIN/-1 回绕)。"""
        self._emit(f"movl {REG[rs2]}, %r9d")     # 除数暂存(可能 == rax)
        l_trap = self._label()
        l_ok = self._label()
        self._emit("cmpl $0, %r9d")
        self._emit(f"je {l_trap}")
        self._emit(f"movslq {REG[rs1]}, %rax")   # 被除数符号扩展
        self._emit("movslq %r9d, %r10")
        self._emit("cqto")
        self._emit(f"idivq %r10")
        if want_rem:
            self._emit(f"movl %edx, {REG[rd]}")
        else:
            self._emit(f"movl %eax, {REG[rd]}")
        self._emit(f"jmp {l_ok}")
        self._emit(f"{l_trap}:")
        self._emit("movl $2, %edi")
        self._emit("call flt_trap")
        self._emit(f"{l_ok}:")

    def _call_lib(self, setup, clobbers_result=False):
        """在 rsp 已对齐(rsp%16==0)的前提下调用 libc。"""
        self._emit(setup)

    # ---------- 函数 ----------
    def gen_func(self, f):
        self.cur = f.name
        self.locals = {pname: 16 + 8 * i for i, (pname, _) in enumerate(f.params)}
        self.var_types = dict(f.params)
        self.for_ends = {}
        self.for_steps = {}
        self.for_lit_storage = {}
        self.for_list_base = {}
        self.for_list_idx = {}
        self.list_storage = {}
        self.loop_stack = []
        self.ret_label = self._label()
        self._stack_off = 0
        slot = 0
        self._list_max = self._max_list_sizes(f.body)
        nslots = self._collect(f.body, slot)
        frame = (nslots * 8 + 15) // 16 * 16
        self._emit(f"{f.name}:")
        self._emit("pushq %rbp")
        self._emit("movq %rsp, %rbp")
        if frame > 0:
            self._emit(f"subq ${frame}, %rsp")
        for s in f.body:
            self.gen_stmt(s)
        self._emit(f".L{self.ret_label}:")
        self._emit("movq %rbp, %rsp")
        self._emit("popq %rbp")
        self._emit("ret")

    def _emit_label(self, lab):
        self._emit(f".L{lab}:")

    # ---------- 语句 ----------
    def gen_stmt(self, s):
        if isinstance(s, Decl):
            if is_list(s.typ):
                lit = s.expr.elems if s.expr is not None else []
                size = s.typ[2] if s.typ[2] is not None else len(lit)
                store = self.list_storage[s.name]
                self._gen_list_init(store, size, lit)
                self._emit(f"leaq {store - 4 * size}(%rbp), %rax")
                self._emit(f"movq %rax, {self._mem(self.locals[s.name])}")
            else:
                self.gen_expr(s.expr)
                self._emit(f"movl %eax, {self._mem(self.locals[s.name])}")
        elif isinstance(s, Assign):
            if is_list(self._infer_type(s.expr)) or self._infer_type(s.expr) == "str":
                self.gen_expr(s.expr)
                self._emit(f"movq %rax, {self._mem(self.locals[s.name])}")
            else:
                self.gen_expr(s.expr)
                self._emit(f"movl %eax, {self._mem(self.locals[s.name])}")
        elif isinstance(s, IndexAssign):
            self._gen_index_assign(s)
        elif isinstance(s, AugAssign):
            self._gen_aug_assign(s)
        elif isinstance(s, Return):
            if s.expr is not None:
                self.gen_expr(s.expr)
            self._emit(f"jmp .L{self.ret_label}")
        elif isinstance(s, If):
            # 统一 if/elif/else 链: 每个条件假 → 跳下一个条件; 最后一个条件假 → else/出口
            l_after = self._label()     # 整体出口
            conds = [(s.cond, s.body)] + list(s.elifs)
            for idx, (c, b) in enumerate(conds):
                is_last = idx == len(conds) - 1
                self.gen_expr(c)
                self._emit("cmpl $0, %eax")
                if is_last:
                    l_false = self._label()
                    self._emit(f"je .L{l_false}")
                    for st in b:
                        self.gen_stmt(st)
                    self._emit(f"jmp .L{l_after}")
                    if s.else_body:
                        self._emit_label(l_false)
                        for st in s.else_body:
                            self.gen_stmt(st)
                    else:
                        self._emit_label(l_false)
                    self._emit_label(l_after)
                else:
                    l_next = self._label()
                    self._emit(f"je .L{l_next}")
                    for st in b:
                        self.gen_stmt(st)
                    self._emit(f"jmp .L{l_after}")
                    self._emit_label(l_next)
        elif isinstance(s, While):
            l_cond = self._label()
            l_after = self._label()
            l_end = self._label() if s.else_body else l_after
            self.loop_stack.append((l_after, l_cond))
            self._emit_label(l_cond)
            self.gen_expr(s.cond)
            self._emit("cmpl $0, %eax")
            self._emit(f"je .L{l_end}")
            for st in s.body:
                self.gen_stmt(st)
            self._emit(f"jmp .L{l_cond}")
            if s.else_body:
                self._emit_label(l_end)
                for st in s.else_body:
                    self.gen_stmt(st)
            self._emit_label(l_after)
            self.loop_stack.pop()
        elif isinstance(s, For):
            self.gen_for(s)
        elif isinstance(s, Break):
            self._emit(f"jmp .L{self.loop_stack[-1][0]}")
        elif isinstance(s, Continue):
            self._emit(f"jmp .L{self.loop_stack[-1][1]}")
        elif isinstance(s, Pass):
            pass
        elif isinstance(s, ExprStmt):
            self.gen_expr(s.expr)
        else:
            raise AsmError("未知语句节点")

    # ---------- 列表 / 索引 ----------
    def _gen_list_init(self, store_top, size, lit):
        """len 在块底, 元素向上增长(与 VM/全局数据段一致)。"""
        len_off = store_top - 4 * size
        self._emit(f"movl ${size}, {len_off}(%rbp)")
        for k, e in enumerate(lit):
            self.gen_expr(e)
            self._emit(f"movl %eax, {len_off + 4 + 4 * k}(%rbp)")
        rest = size - len(lit)
        if rest > 0:
            self._emit("movl $0, %eax")
            self._emit(f"leaq {len_off + 4 + 4 * len(lit)}(%rbp), %r11")
            self._emit(f"movl ${rest}, %ecx")
            lf = self._label()
            lfd = self._label()
            self._emit_label(lf)
            self._emit("cmpl $0, %ecx")
            self._emit(f"je .L{lfd}")
            self._emit("movl %eax, (%r11)")
            self._emit("addq $4, %r11")
            self._emit("decl %ecx")
            self._emit(f"jmp .L{lf}")
            self._emit_label(lfd)

    def _bounds_check(self):
        """r0(rax)=idx, r1(r11)=列表基址 → 越界 TRAP 1。"""
        self._emit("movl (%r11), %ecx")      # len
        lb = self._label()
        self._emit("cmpl $0, %eax")
        self._emit(f"jge .L{lb}")
        self._emit("addl %ecx, %eax")        # 负索引 + len
        self._emit_label(lb)
        lb2 = self._label()
        self._emit("cmpl %ecx, %eax")
        self._emit(f"jl .L{lb2}")
        self._emit("movl $1, %edi")
        self._emit("call flt_trap")
        self._emit_label(lb2)

    def _gen_index_assign(self, s):
        self.gen_expr(s.base)
        self._pushq()
        self.gen_expr(s.index)
        self._popq()
        self._bounds_check()
        self._emit("leaq 4(%r11,%rax,4), %rax")   # 元素地址 = base + 4 + 4*idx
        self._pushq()
        self.gen_expr(s.expr)
        self._popq()
        self._emit("movl %eax, (%r11)")

    # ---------- 复合赋值 ----------
    def _gen_aug_assign(self, s):
        op = s.op
        if isinstance(s.target, Index):
            self.gen_expr(s.target.base)
            self._pushq()
            self.gen_expr(s.target.index)
            self._popq()
            self._bounds_check()
            self._emit("leaq 4(%r11,%rax,4), %rax")
            self._pushq()              # 元素地址
            self._emit("movl (%rax), %eax")       # 旧值
            self._pushq()
            self.gen_expr(s.expr)
            self._popq()               # 旧值
            self._gen_binop_body(AUG_TO_OP[op], left_in_r11=True)
            self._popq()               # 元素地址
            self._emit("movl %eax, (%r11)")
        else:
            name = s.target.name
            self.gen_expr(s.expr)
            t = self.var_types.get(name) or self.global_types.get(name)
            if is_list(t) or t == "str":
                raise AsmError(f"{s.op} 不能用于 list/str 变量")
            self._emit(f"movl {self._mem(self.locals[name])}, %r11d")
            self._gen_binop_body(AUG_TO_OP[op], left_in_r11=True)
            self._emit(f"movl %eax, {self._mem(self.locals[name])}")

    # ---------- 循环 ----------
    def gen_for(self, s):
        l_cond = self._label()
        l_inc = self._label()
        l_after = self._label()
        l_end = self._label() if s.else_body else l_after
        self.loop_stack.append((l_after, l_inc))
        if s.mode == "range":
            self._gen_for_range(s, l_cond, l_inc, l_end)
        else:
            self._gen_for_list(s, l_cond, l_inc, l_end)
        if s.else_body:
            for st in s.else_body:
                self.gen_stmt(st)
            self._emit_label(l_after)
        self.loop_stack.pop()

    def _gen_for_range(self, s, l_cond, l_inc, l_end):
        var_off = self.locals[s.var]
        step_is_lit = isinstance(s.step, IntLit)
        self.gen_expr(s.start)
        self._emit(f"movl %eax, {self._mem(var_off)}")
        if id(s) in self.for_ends:
            self.gen_expr(s.end)
            self._emit(f"movl %eax, {self._mem(self.for_ends[id(s)])}")
        if not step_is_lit:
            self.gen_expr(s.step)
            self._emit(f"movl %eax, {self._mem(self.for_steps[id(s)])}")
            l_ok = self._label()
            self._emit(f"movl {self._mem(self.for_steps[id(s)])}, %eax")
            self._emit("cmpl $0, %eax")
            self._emit(f"jne .L{l_ok}")
            self._emit("movl $2, %edi")
            self._emit("call flt_trap")
            self._emit_label(l_ok)
        self._emit_label(l_cond)
        self._emit(f"movl {self._mem(var_off)}, %eax")
        if id(s) in self.for_ends:
            self._emit(f"movl {self._mem(self.for_ends[id(s)])}, %r11d")
            if step_is_lit:
                if s.step.val > 0:
                    self._emit("cmpl %r11d, %eax")
                    self._emit(f"jge .L{l_end}")
                else:
                    self._emit("cmpl %r11d, %eax")
                    self._emit(f"jle .L{l_end}")
            else:
                self._emit(f"movl {self._mem(self.for_steps[id(s)])}, %ecx")
                l_pos = self._label()
                self._emit("cmpl $0, %ecx")
                self._emit(f"jg .L{l_pos}")
                self._emit("cmpl %r11d, %eax")
                self._emit(f"jle .L{l_end}")
                self._emit(f"jmp .L{l_pos}")
                self._emit_label(l_pos)
                self._emit("cmpl %r11d, %eax")
                self._emit(f"jge .L{l_end}")
        else:
            if step_is_lit and s.step.val > 0:
                self._emit(f"cmpl ${s.end.val}, %eax")
                self._emit(f"jge .L{l_end}")
            elif step_is_lit:
                self._emit(f"cmpl ${s.end.val}, %eax")
                self._emit(f"jle .L{l_end}")
            else:
                raise AsmError("动态步长必须有结束槽")
        for st in s.body:
            self.gen_stmt(st)
        self._emit_label(l_inc)
        self._emit(f"movl {self._mem(var_off)}, %eax")
        if step_is_lit:
            self._emit(f"movl ${s.step.val}, %r11d")
        else:
            self._emit(f"movl {self._mem(self.for_steps[id(s)])}, %r11d")
        self._emit("addl %r11d, %eax")
        self._emit(f"movl %eax, {self._mem(var_off)}")
        self._emit(f"jmp .L{l_cond}")
        self._emit_label(l_end)

    def _gen_for_list(self, s, l_cond, l_inc, l_end):
        var_off = self.locals[s.var]
        base_off = self.for_list_base[id(s)]
        idx_off = self.for_list_idx[id(s)]
        if id(s) in self.for_lit_storage:
            store_off = self.for_lit_storage[id(s)]
            lit = s.iter
            len_off = store_off - 4 * len(lit.elems)
            self._emit(f"movl ${len(lit.elems)}, {len_off}(%rbp)")
            for k, e in enumerate(lit.elems):
                self.gen_expr(e)
                self._emit(f"movl %eax, {len_off + 4 + 4 * k}(%rbp)")
            self._emit(f"leaq {len_off}(%rbp), %rax")
            self._emit(f"movq %rax, {self._mem(base_off)}")
        else:
            self.gen_expr(s.iter)
            self._emit(f"movq %rax, {self._mem(base_off)}")
        self._emit("movl $0, %eax")
        self._emit(f"movl %eax, {self._mem(idx_off)}")
        self._emit_label(l_cond)
        self._emit(f"movq {self._mem(base_off)}, %r11")
        self._emit("movl (%r11), %ecx")           # len
        self._emit(f"movl {self._mem(idx_off)}, %eax")
        self._emit("cmpl %ecx, %eax")
        self._emit(f"jge .L{l_end}")
        # 元素 = base + 4 + 4*idx
        self._emit("leaq 4(%r11,%rax,4), %rax")
        self._emit("movl (%rax), %eax")
        self._emit(f"movl %eax, {self._mem(var_off)}")
        for st in s.body:
            self.gen_stmt(st)
        self._emit_label(l_inc)
        self._emit(f"movl {self._mem(idx_off)}, %eax")
        self._emit("incl %eax")
        self._emit(f"movl %eax, {self._mem(idx_off)}")
        self._emit(f"jmp .L{l_cond}")
        self._emit_label(l_end)

    # ---------- 表达式 ----------
    def gen_expr(self, e):
        if isinstance(e, IntLit):
            self._emit(f"movl ${e.val}, %eax")
        elif isinstance(e, BoolLit):
            self._emit(f"movl ${1 if e.val else 0}, %eax")
        elif isinstance(e, StrLit):
            lab = self._new_str(e.val)
            self._emit(f"leaq {lab}(%rip), %rax")
        elif isinstance(e, Var):
            self._gen_var_load(e)
        elif isinstance(e, Index):
            bt = self._infer_type(e.base)
            self._gen_index(e, is_str=bt == "str")
        elif isinstance(e, Unary):
            if e.op == "-":
                self.gen_expr(e.operand)
                self._emit("negl %eax")
            elif e.op == "~":
                self.gen_expr(e.operand)
                self._emit("notl %eax")
            else:  # not
                self.gen_expr(e.operand)
                self._emit("cmpl $0, %eax")
                self._emit("movl $1, %eax")
                lz = self._label()
                self._emit(f"je .L{lz}")
                self._emit("movl $0, %eax")
                self._emit_label(lz)
        elif isinstance(e, BinOp):
            self._gen_binop(e)
        elif isinstance(e, Call):
            self.gen_call(e)
        elif isinstance(e, ListLit):
            raise AsmError("列表字面量必须出现在声明/初始化中")
        else:
            raise AsmError(f"未知表达式 {type(e).__name__}")

    def _gen_var_load(self, e):
        name = e.name
        if name in self.var_types:
            t = self.var_types[name]
            if is_list(t) or t == "str":
                self._emit(f"movq {self._mem(self.locals[name])}, %rax")
            else:
                self._emit(f"movl {self._mem(self.locals[name])}, %eax")
        elif name in self.global_types:
            t = self.global_types[name]
            lab = f"__g_{name}"
            if is_list(t) or t == "str":
                self._emit(f"leaq {lab}(%rip), %rax")
            else:
                self._emit(f"movl {lab}(%rip), %eax")
        else:
            raise AsmError(f"未声明变量 {name}")

    def _gen_index(self, e, is_str):
        """求值 e.base[e.index] → rax。"""
        self.gen_expr(e.base)
        self._pushq()
        self.gen_expr(e.index)
        self._popq()
        self._bounds_check()
        if is_str:
            # s[i]: 逐字节扫描找第 idx 个字符的起始字节
            self._emit("movl %eax, %ecx")        # 剩余字符数
            self._emit("movq %r11, %rax")        # 当前扫描位置
            l_scan = self._label()
            l_found = self._label()
            l_done = self._label()
            self._emit_label(l_scan)
            self._emit("movzbl (%rax), %r11d")
            self._emit("cmpl $0, %r11d")
            self._emit(f"je .L{l_done}")        # 越界(字符串短) → 0
            self._emit("cmpl $0, %ecx")
            self._emit(f"je .L{l_found}")
            self._emit("decl %ecx")
            self._emit("addq $1, %rax")
            self._emit(f"jmp .L{l_scan}")
            self._emit_label(l_found)
            self._emit("movzbl (%rax), %eax")
            self._emit_label(l_done)
        else:
            self._emit("leaq 4(%r11,%rax,4), %rax")
            self._emit("movl (%rax), %eax")

    # ---------- 二元 ----------
    def _gen_binop(self, e):
        op = e.op
        if op in ("and", "or"):
            self.gen_expr(e.left)
            self._emit("cmpl $0, %eax")
            l_r = self._label()
            if op == "and":
                self._emit(f"je .L{l_r}")
            else:
                self._emit(f"jne .L{l_r}")
            self.gen_expr(e.right)
            self._emit("cmpl $0, %eax")
            self._emit("movl $0, %eax")
            l_end = self._label()
            self._emit(f"je .L{l_end}")
            self._emit("movl $1, %eax")
            self._emit(f"jmp .L{l_end}")
            self._emit_label(l_r)
            self._emit("movl $0, %eax" if op == "and" else "movl $1, %eax")
            self._emit_label(l_end)
            return
        if op in ("==", "!=", "<", ">", "<=", ">="):
            self.gen_expr(e.left)
            self._pushq()
            self.gen_expr(e.right)
            self._popq()
            self._emit("cmpl %eax, %r11d")       # 左 - 右
            self._emit("movl $1, %eax")
            l_t = self._label()
            self._emit(f"{CMP_CC[op]} .L{l_t}")
            self._emit("movl $0, %eax")
            self._emit_label(l_t)
            return
        if op == "in":
            self.gen_expr(e.left)
            self._pushq()
            self.gen_expr(e.right)
            self._popq("%rdx")               # rdx = 目标值
            bt = self._infer_type(e.right)
            l_scan = self._label()
            l_found = self._label()
            l_miss = self._label()
            l_done = self._label()
            if bt == "str":
                # r0=rax = 字符串地址
                self._emit_label(l_scan)
                self._emit("movzbl (%rax), %r11d")
                self._emit("cmpl $0, %r11d")
                self._emit(f"je .L{l_miss}")
                self._emit("cmpl %edx, %r11d")
                self._emit(f"je .L{l_found}")
                self._emit("addq $1, %rax")
                self._emit(f"jmp .L{l_scan}")
            else:
                self._emit("movl (%rax), %ecx")  # len
                self._emit("movl $0, %esi")      # idx
                self._emit_label(l_scan)
                self._emit("cmpl %ecx, %esi")
                self._emit(f"jge .L{l_miss}")
                self._emit("leaq 4(%rax,%rsi,4), %r11")
                self._emit("movl (%r11), %r11d")
                self._emit("cmpl %edx, %r11d")
                self._emit(f"je .L{l_found}")
                self._emit("incl %esi")
                self._emit(f"jmp .L{l_scan}")
            self._emit_label(l_miss)
            self._emit("movl $0, %eax")
            self._emit(f"jmp .L{l_done}")
            self._emit_label(l_found)
            self._emit("movl $1, %eax")
            self._emit_label(l_done)
            return
        self.gen_expr(e.left)
        self._pushq()
        self.gen_expr(e.right)
        self._popq()
        self._gen_binop_body(op, left_in_r11=True)

    def _gen_binop_body(self, op, left_in_r11=True):
        """r11=左, rax=右 → rax=结果。"""
        if op == "//":
            # 地板除: q = 截断商; 若异号且余数非 0 → q-1
            self._emit("movl %eax, %r9d")        # 保存右(b)
            l_trap = self._label()
            l_ok = self._label()
            self._emit("cmpl $0, %r9d")
            self._emit(f"je .L{l_trap}")
            self._emit(f"movslq %r11d, %rax")
            self._emit("movslq %r9d, %r10")
            self._emit("cqto")
            self._emit("idivq %r10")             # rax=商, rdx=余
            self._emit("movl %edx, %esi")        # r
            self._emit("movl %r11d, %ecx")
            self._emit("xorl %r9d, %ecx")        # 异号?
            l_done = self._label()
            self._emit("cmpl $0, %ecx")
            self._emit(f"jge .L{l_done}")
            self._emit("cmpl $0, %esi")
            self._emit(f"je .L{l_done}")
            self._emit("subq $1, %rax")
            self._emit(f"jmp .L{l_done}")
            self._emit_label(l_trap)
            self._emit("movl $2, %edi")
            self._emit("call flt_trap")
            self._emit_label(l_done)
            return
        if op in ("/", "%"):
            want_rem = op == "%"
            # r1=左, r0=右 → 结果回 r0(rax)
            self._emit(f"movl %eax, %r9d")       # 除数
            l_trap = self._label()
            l_ok = self._label()
            self._emit("cmpl $0, %r9d")
            self._emit(f"je .L{l_trap}")
            self._emit("movslq %r11d, %rax")
            self._emit("movslq %r9d, %r10")
            self._emit("cqto")
            self._emit("idivq %r10")
            if want_rem:
                self._emit("movl %edx, %eax")
            self._emit(f"jmp .L{l_ok}")
            self._emit_label(l_trap)
            self._emit("movl $2, %edi")
            self._emit("call flt_trap")
            self._emit_label(l_ok)
            return
        if op in ("<<", ">>"):
            self._shift("shll" if op == "<<" else "shrl", "r0", "r1", "r0")
            return
        self._bin32(BIN32[op], "r0", "r1", "r0")

    # ---------- 调用 ----------
    def gen_call(self, c):
        if c.name == "print":
            self._gen_print(c.args)
            return
        if c.name == "input":
            need = (16 - self._stack_off % 16) % 16
            if need:
                self._emit(f"subq ${need}, %rsp")
                self._stack_off += need
            self._emit("call getchar")
            if need:
                self._emit(f"addq ${need}, %rsp")
                self._stack_off -= need
            l_eof = self._label()
            l_done = self._label()
            self._emit("cmpl $-1, %eax")
            self._emit(f"je .L{l_eof}")
            self._emit("movzbl %al, %eax")
            self._emit(f"jmp .L{l_done}")
            self._emit_label(l_eof)
            self._emit("movl $0, %eax")
            self._emit_label(l_done)
            return
        if c.name == "len":
            arg = c.args[0]
            if self._infer_type(arg) == "str":
                self.gen_expr(arg)
                self._emit("movq %rax, %r11")
                self._emit("movl $0, %ecx")
                l_scan = self._label()
                l_done = self._label()
                self._emit_label(l_scan)
                self._emit("movzbl (%r11), %edx")
                self._emit("cmpl $0, %edx")
                self._emit(f"je .L{l_done}")
                self._emit("incq %r11")
                self._emit("incl %ecx")
                self._emit(f"jmp .L{l_scan}")
                self._emit_label(l_done)
                self._emit("movl %ecx, %eax")
            else:
                self.gen_expr(arg)
                self._emit("movl (%rax), %eax")
            return
        if c.name == "abs":
            self.gen_expr(c.args[0])
            l_pos = self._label()
            self._emit("cmpl $0, %eax")
            self._emit(f"jge .L{l_pos}")
            self._emit("negl %eax")
            self._emit_label(l_pos)
            return
        if c.name in ("min", "max"):
            self.gen_expr(c.args[0])
            for a in c.args[1:]:
                self._pushq()
                self.gen_expr(a)
                self._popq()
                l_keep = self._label()
                self._emit("cmpl %eax, %r11d")
                cc = "jle" if c.name == "min" else "jge"
                self._emit(f"{cc} .L{l_keep}")
                self._emit("movl %r11d, %eax")
                self._emit_label(l_keep)
            return
        if c.name == "sum":
            self.gen_expr(c.args[0])
            self._emit("movq %rax, %r11")        # 基址
            self._emit("movl (%r11), %ecx")      # len
            self._emit("movl $0, %edx")          # 和
            self._emit("movl $0, %esi")          # idx
            l_scan = self._label()
            l_done = self._label()
            self._emit_label(l_scan)
            self._emit("cmpl %ecx, %esi")
            self._emit(f"jge .L{l_done}")
            self._emit("leaq 4(%r11,%rsi,4), %r8")
            self._emit("movl (%r8), %r8d")
            self._emit("addl %r8d, %edx")
            self._emit("incl %esi")
            self._emit(f"jmp .L{l_scan}")
            self._emit_label(l_done)
            self._emit("movl %edx, %eax")
            return
        if c.name == "pow":
            self.gen_expr(c.args[0])
            self._pushq()             # 底数
            self.gen_expr(c.args[1])
            self._popq()              # r11 = 底数
            self._emit("movl %eax, %ecx")        # 指数
            self._emit("movl $1, %eax")          # 结果
            l_scan = self._label()
            l_done = self._label()
            self._emit_label(l_scan)
            self._emit("cmpl $0, %ecx")
            self._emit(f"jle .L{l_done}")
            self._emit("imull %r11d, %eax")
            self._emit("decl %ecx")
            self._emit(f"jmp .L{l_scan}")
            self._emit_label(l_done)
            return
        # 用户函数: 参数从右向左压栈; 对齐垫在参数之下(参数位置 = rbp+16+8i)
        d = len(c.args)
        base = self._stack_off
        pad = 0
        if (base + 8 * d) % 16 != 0:
            self._emit("subq $8, %rsp")
            self._stack_off += 8
            pad = 8
        for a in reversed(c.args):
            self.gen_expr(a)
            self._pushq()
        self._emit(f"call {c.name}")
        self._stack_off = base
        if d:
            self._emit(f"addq ${8 * d + pad}, %rsp")

    def _gen_print(self, args):
        for i, arg in enumerate(args):
            if self._is_str_expr(arg):
                self.gen_expr(arg)
                l_scan = self._label()
                l_done = self._label()
                self._emit_label(l_scan)
                self._emit("movzbl (%rax), %r11d")
                self._emit("cmpl $0, %r11d")
                self._emit(f"je .L{l_done}")
                # OUT r1 (保护 rax, 对齐)
                self._emit("pushq %rax")
                self._emit("subq $8, %rsp")
                self._emit("movl %r11d, %edi")
                self._emit("call putchar")
                self._emit("addq $8, %rsp")
                self._emit("popq %rax")
                self._emit("addq $1, %rax")
                self._emit(f"jmp .L{l_scan}")
                self._emit_label(l_done)
            else:
                self.gen_expr(arg)
                self._emit("pushq %rax")
                self._emit("subq $8, %rsp")
                self._emit("movl %eax, %esi")
                self._emit("leaq .LCout(%rip), %rdi")
                self._emit("xorl %eax, %eax")
                self._emit("call printf")
                self._emit("addq $8, %rsp")
                self._emit("popq %rax")
            if i < len(args) - 1:
                self._emit("pushq %rax")
                self._emit("subq $8, %rsp")
                self._emit("movl $32, %edi")
                self._emit("call putchar")
                self._emit("addq $8, %rsp")
                self._emit("popq %rax")
        self._emit("pushq %rax")
        self._emit("subq $8, %rsp")
        self._emit("movl $10, %edi")
        self._emit("call putchar")
        self._emit("addq $8, %rsp")
        self._emit("popq %rax")

    def _is_str_expr(self, e):
        if isinstance(e, StrLit):
            return True
        if isinstance(e, Var):
            return self.var_types.get(e.name) == "str" or self.global_types.get(e.name) == "str"
        return False


CMP_CC = {"==": "je", "!=": "jne", "<": "jl", ">": "jg", "<=": "jle", ">=": "jge"}
AUG_TO_OP = {"+=": "+", "-=": "-", "*=": "*", "/=": "/", "%=": "%", "//=": "//",
             "&=": "&", "|=": "|", "^=": "^", "<<=": "<<", ">>=": ">>"}


def compile_asm(src: str) -> str:
    """Flint 源码 → x86-64 AT&T 汇编文本。"""
    from lexer import tokenize
    from parser import Parser
    prog = Parser(tokenize(src)).parse_program()
    TypeChecker().check(prog)
    return AsmGen().generate(prog)


def build(flint_path: str, out_path: str):
    """编译 .fl 文件并链接为可执行文件(gcc 仅做汇编与链接)。

    Windows 通常没有预装 gcc: 缺失时抛 AsmError 中文提示, 不再裸抛
    FileNotFoundError(开箱即挂)。VM 模式零依赖, 不受影响。
    """
    with open(flint_path, encoding="utf-8") as f:
        src = f.read()
    asm_text = compile_asm(src)
    with tempfile.NamedTemporaryFile("w", suffix=".s", delete=False, encoding="utf-8") as f:
        f.write(asm_text)
        asm_path = f.name
    try:
        cc = os.environ.get("CC", "gcc")
        cmd = [cc, "-no-pie", "-O2", "-o", out_path, asm_path]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            raise AsmError(
                "未找到汇编器/链接器 '%s' (x86-64 原生后端需要 gcc)。\n"
                "  - Linux/macOS: 安装 gcc 即可\n"
                "  - Windows: 安装 MinGW-w64 后设置 CC 环境变量指向 gcc.exe\n"
                "  - 或改用零依赖的 VM 模式: python flint.py run <file.fl>" % cc
            ) from None
        if r.returncode != 0:
            raise AsmError("gcc 汇编/链接失败:\n" + r.stderr)
    finally:
        os.unlink(asm_path)
    return out_path
