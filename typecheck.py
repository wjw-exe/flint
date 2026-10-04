# -*- coding: utf-8 -*-
"""
typecheck.py — Flint v3.0 静态类型检查器。

编译期完成全部类型校验, 类型错误直接编译失败, 运行时零类型开销
(这正是比 Python 快的关键之一: 没有动态类型探测)。

v2.1 新增:
  * 列表类型 list[i32] / list[i32; N] 与索引/赋值/遍历
  * 类型推断: 首次赋值即声明(x = 1 → i32)
  * 复合赋值 x += 1 / xs[i] *= 2
  * break / continue / pass
  * 链式比较 a < b < c
  * str 变量: 声明/赋值/print/len(s)/s[i]
  * 多参数 print / len / abs / 全局变量
"""

from parser import (Program, FuncDef, Decl, Assign, IndexAssign, AugAssign,
                    Return, If, While, For, Break, Continue, Pass, ExprStmt,
                    BinOp, Unary, Call, Var, Index, IntLit, BoolLit, StrLit,
                    ListLit, Ternary, type_str)

ARITH_OPS = ("+", "-", "*", "/", "%")
FLOOR_OPS = ("//",)
BIT_OPS = ("&", "|", "^", "<<", ">>")
CMP_OPS = ("==", "!=", "<", ">", "<=", ">=", "in")
LOGIC_OPS = ("and", "or")
AUG_OPS = {"+=": "+", "-=": "-", "*=": "*", "/=": "/", "%=": "%", "//=": "//",
           "&=": "&", "|=": "|", "^=": "^", "<<=": "<<", ">>=": ">>"}
AUG_BIT_OPS = ("&", "|", "^", "<<", ">>")


class TypeCheckError(Exception):
    def __init__(self, line, msg):
        super().__init__(f"第 {line} 行: {msg}")


def is_list(t):
    return isinstance(t, tuple) and t[0] == "list"


class TypeChecker:
    def __init__(self):
        self.funcs = {}
        self.globals_scope = {}

    def check(self, prog):
        """检查整个程序; 出错抛 TypeCheckError。"""
        # 全局变量
        for g in prog.globals:
            if g.name in self.globals_scope:
                raise TypeCheckError(g.line, f"全局变量 {g.name} 重复声明")
            self._check_global(g)
            self.globals_scope[g.name] = g.typ
        # 函数
        for f in prog.funcs:
            if f.name in self.funcs:
                raise TypeCheckError(f.line, f"函数 {f.name} 重复定义")
            self.funcs[f.name] = f
        if "main" not in self.funcs:
            raise TypeCheckError(1, "缺少入口函数 main()")

        # v3.0: 默认参数规则 —— 有默认值的参数之后不能再有无默认值参数
        for f in prog.funcs:
            seen_default = False
            for i, (pn, pt) in enumerate(f.params):
                d = (f.defaults or [])[i] if i < len(f.defaults or []) else None
                if d is not None:
                    seen_default = True
                    t = self.expr(d)
                    if not self._type_match(pt, t):
                        raise TypeCheckError(f.line,
                            f"参数 {pn} 的默认值类型应为 {type_str(pt)}, 得到 {type_str(t)}")
                    if not self._is_literal(d):
                        raise TypeCheckError(f.line,
                            f"参数 {pn} 的默认值必须是字面量(数字/True/False/字符串)")
                elif seen_default:
                    raise TypeCheckError(f.line,
                        f"函数 {f.name}: 有默认值的参数之后不能有无默认值参数")

        for f in prog.funcs:
            self.ret = f.ret
            self.loop_depth = 0
            self.scope = {pname: ptype for pname, ptype in f.params}
            for s in f.body:
                self.stmt(s)
            if f.ret not in (None, "void") and not self._has_return(f.body):
                raise TypeCheckError(f.line, f"函数 {f.name} 声明返回 {f.ret}, 但函数体里没有任何 return")
        return True

    def _check_global(self, g):
        """全局变量初值必须是字面量(数据段静态存放)。"""
        ok_lit = (g.expr is not None and self._is_literal(g.expr)) or (
            is_list(g.typ) and g.typ[2] is not None and g.expr is None)
        if not ok_lit:
            raise TypeCheckError(g.line, f"全局变量 {g.name} 的初值必须是字面量(数字/True/False/字符串/列表字面量)")
        t = self.expr(g.expr) if g.expr is not None else None
        if t is not None and not self._type_match(g.typ, t):
            raise TypeCheckError(g.line, f"全局变量 {g.name} 声明为 {type_str(g.typ)}, 初值是 {type_str(t)}")

    @staticmethod
    def _is_literal(e):
        if isinstance(e, (IntLit, BoolLit, StrLit)):
            return True
        if isinstance(e, ListLit):
            return all(TypeChecker._is_literal(x) for x in e.elems)
        return False

    @staticmethod
    def _type_match(decl, got):
        """声明类型与表达式类型匹配(允许列表字面量元素推断)。"""
        if decl == got:
            return True
        if is_list(decl) and is_list(got):
            if got[1] == "?":
                return True  # 空列表字面量, 元素类型以声明为准
            return decl[1] == got[1]  # 元素类型一致即可(大小由字面量/声明决定)
        return False

    # ---------- 语句 ----------
    def stmt(self, s):
        if isinstance(s, Decl):
            t = self.expr(s.expr) if s.expr is not None else None
            if is_list(s.typ):
                if s.expr is not None and not isinstance(s.expr, ListLit):
                    raise TypeCheckError(s.line, f"列表变量 {s.name} 的初值必须是列表字面量, 如 {type_str(s.typ)} = [...]")
                if t is not None and not self._type_match(s.typ, t):
                    raise TypeCheckError(s.line, f"变量 {s.name} 声明为 {type_str(s.typ)}, 初值是 {type_str(t)}")
                if s.typ[2] is not None and t is not None and isinstance(t, tuple) \
                        and isinstance(t[2], int) and t[2] > s.typ[2]:
                    raise TypeCheckError(s.line, f"列表字面量 {t[2]} 个元素超过声明大小 {s.typ[2]}")
            else:
                if t != s.typ:
                    raise TypeCheckError(s.line, f"变量 {s.name} 声明为 {s.typ}, 但初始值是 {type_str(t)}")
            if s.name in self.scope:
                raise TypeCheckError(s.line, f"变量 {s.name} 重复声明")
            self.scope[s.name] = s.typ

        elif isinstance(s, Assign):
            t = self.expr(s.expr)
            if t == "void":
                raise TypeCheckError(s.line, "void 函数(如 print)的结果不能用于赋值")
            if s.name in self.scope:
                if not self._type_match(self.scope[s.name], t):
                    raise TypeCheckError(s.line, f"变量 {s.name} 类型是 {type_str(self.scope[s.name])}, 赋值 {type_str(t)} 不匹配")
            else:
                # Python 风格: 首次赋值即声明, 类型由初值推断(静态)
                if is_list(t) and t[1] == "?":
                    raise TypeCheckError(s.line, "空列表无法推断元素类型, 请标注类型, 如 xs: list[i32] = []")
                self.scope[s.name] = t

        elif isinstance(s, IndexAssign):
            bt = self.expr(s.base)
            it = self.expr(s.index)
            if not is_list(bt):
                raise TypeCheckError(s.line, f"索引赋值目标必须是 list, 得到 {type_str(bt)}")
            if it != "i32":
                raise TypeCheckError(s.line, f"索引必须是 i32, 得到 {type_str(it)}")
            vt = self.expr(s.expr)
            if vt != bt[1]:
                raise TypeCheckError(s.line, f"元素赋值类型不匹配: list[{bt[1]}], 赋值 {type_str(vt)}")

        elif isinstance(s, AugAssign):
            op = AUG_OPS[s.op]
            if isinstance(s.target, Index):
                bt = self.expr(s.target.base)
                it = self.expr(s.target.index)
                if not is_list(bt) or it != "i32":
                    raise TypeCheckError(s.line, f"复合赋值目标索引不合法: 需要 list[i32] 索引")
                elem = bt[1]
                vt = self.expr(s.expr)
                if op in ARITH_OPS:
                    if elem != "i32" or vt != "i32":
                        raise TypeCheckError(s.line, f"{s.op} 只能用于 i32 元素, 得到 {elem} 和 {type_str(vt)}")
                elif op in FLOOR_OPS or op in BIT_OPS:
                    if elem != "i32" or vt != "i32":
                        raise TypeCheckError(s.line, f"{s.op} 只能用于 i32")
            else:
                name = s.target.name
                if name not in self.scope:
                    raise TypeCheckError(s.line, f"变量 {name} 未声明, 无法复合赋值")
                vt = self.expr(s.expr)
                t = self.scope[name]
                if op in ARITH_OPS or op in FLOOR_OPS or op in BIT_OPS:
                    if t != "i32" or vt != "i32":
                        raise TypeCheckError(s.line, f"{s.op} 只能用于 i32 变量, 得到 {type_str(t)} 和 {type_str(vt)}")

        elif isinstance(s, Return):
            if s.expr is None:
                if self.ret not in (None, "void"):
                    raise TypeCheckError(s.line, f"函数声明返回 {self.ret}, 却执行了无值 return")
                return
            t = self.expr(s.expr)
            want = self.ret or "void"
            if t != want:
                raise TypeCheckError(s.line, f"返回类型不匹配: 函数要求 {want}, 实际是 {type_str(t)}")

        elif isinstance(s, If):
            self._cond(s.cond, s.line)
            self.block(s.body)
            for c, b in s.elifs:
                self._cond(c, s.line)
                self.block(b)
            if s.else_body:
                self.block(s.else_body)

        elif isinstance(s, While):
            self._cond(s.cond, s.line)
            self.loop_depth += 1
            self.block(s.body)
            self.loop_depth -= 1
            if s.else_body:
                self.block(s.else_body)

        elif isinstance(s, For):
            self.loop_depth += 1
            if s.mode == "range":
                for e in (s.start, s.end, s.step):
                    if self.expr(e) != "i32":
                        raise TypeCheckError(s.line, "for range 的边界/步长必须是 i32")
                if isinstance(s.step, IntLit) and s.step.val == 0:
                    raise TypeCheckError(s.line, "range 步长不能为 0")
                self.scope[s.var] = "i32"  # 循环变量可重绑定(与 Python 一致)
                self.block(s.body)
            else:  # list
                tt = self.expr(s.iter)
                if not is_list(tt):
                    raise TypeCheckError(s.line, f"for x in ... 只能遍历 list, 得到 {type_str(tt)}")
                self.scope[s.var] = tt[1]
                self.block(s.body)
            self.loop_depth -= 1
            if s.else_body:
                self.block(s.else_body)

        elif isinstance(s, Break):
            if self.loop_depth == 0:
                raise TypeCheckError(s.line, "break 只能出现在循环(while/for)内部")
        elif isinstance(s, Continue):
            if self.loop_depth == 0:
                raise TypeCheckError(s.line, "continue 只能出现在循环(while/for)内部")
        elif isinstance(s, Pass):
            pass

        elif isinstance(s, ExprStmt):
            if not isinstance(s.expr, Call):
                raise TypeCheckError(s.line, "表达式语句只能是函数调用")
            self.call(s.expr)

        else:
            raise TypeCheckError(getattr(s, "line", 1), "无法识别的语句")

    def block(self, stmts):
        for s in stmts:
            self.stmt(s)

    def _cond(self, cond, line):
        t = self.expr(cond)
        if t != "bool":
            raise TypeCheckError(line, f"条件必须是 bool, 实际是 {type_str(t)} (请用比较/逻辑运算构造条件)")

    def _has_return(self, stmts):
        for s in stmts:
            if isinstance(s, Return):
                return True
            if isinstance(s, If):
                if self._has_return(s.body):
                    return True
                for _, b in s.elifs:
                    if self._has_return(b):
                        return True
            if isinstance(s, (While, For)):
                if self._has_return(s.body):
                    return True
        return False

    # ---------- 表达式 ----------
    def expr(self, e):
        if isinstance(e, IntLit):
            return "i32"
        if isinstance(e, BoolLit):
            return "bool"
        if isinstance(e, StrLit):
            return "str"
        if isinstance(e, ListLit):
            elem = None
            for x in e.elems:
                t = self.expr(x)
                if elem is None:
                    elem = t
                elif t != elem:
                    raise TypeCheckError(e.line, f"列表字面量元素类型不一致: {elem} 和 {t}")
            if not e.elems:
                return ("list", "?", 0)  # 空列表, 元素类型待定
            if elem not in ("i32", "bool"):
                raise TypeCheckError(e.line, f"列表元素仅支持 i32/bool, 得到 {elem}")
            return ("list", elem, len(e.elems))
        if isinstance(e, Ternary):
            ct = self.expr(e.cond)
            if ct != "bool":
                raise TypeCheckError(e.line, f"条件表达式的条件必须是 bool, 得到 {type_str(ct)}")
            it = self.expr(e.if_expr)
            et = self.expr(e.else_expr)
            if it != et:
                raise TypeCheckError(e.line, f"条件表达式两分支类型不一致: {type_str(it)} 和 {type_str(et)}")
            return it
        if isinstance(e, Var):
            if e.name in self.scope:
                return self.scope[e.name]
            if e.name in self.globals_scope:
                return self.globals_scope[e.name]
            raise TypeCheckError(e.line, f"变量 {e.name} 未定义")
        if isinstance(e, Index):
            bt = self.expr(e.base)
            it = self.expr(e.index)
            if it != "i32":
                raise TypeCheckError(e.line, f"索引必须是 i32, 得到 {type_str(it)}")
            if is_list(bt):
                return bt[1]
            if bt == "str":
                return "i32"  # s[i] → 字符码
            raise TypeCheckError(e.line, f"不能对 {type_str(bt)} 取索引")

        if isinstance(e, Unary):
            t = self.expr(e.operand)
            if e.op == "-":
                if t != "i32":
                    raise TypeCheckError(e.line, f"一元负号只能用于 i32, 得到 {t}")
                return "i32"
            if e.op == "~":
                if t != "i32":
                    raise TypeCheckError(e.line, f"按位取反 ~ 只能用于 i32, 得到 {t}")
                return "i32"
            if t != "bool":
                raise TypeCheckError(e.line, f"not 只能用于 bool, 得到 {t}")
            return "bool"

        if isinstance(e, BinOp):
            lt = self.expr(e.left)
            rt = self.expr(e.right)
            if e.op in LOGIC_OPS:
                if lt != "bool" or rt != "bool":
                    raise TypeCheckError(e.line, f"and/or 两侧必须是 bool, 得到 {type_str(lt)} 和 {type_str(rt)}")
                return "bool"
            if e.op in ARITH_OPS or e.op in FLOOR_OPS:
                if e.op == "+" and lt == "str" and rt == "str":
                    return "str"          # v3.0: 运行时字符串拼接 s + t (每表达式分配一个 256B 缓冲)
                if lt != "i32" or rt != "i32":
                    raise TypeCheckError(e.line, f"算术运算 {e.op} 两侧必须是 i32, 得到 {type_str(lt)} 和 {type_str(rt)}")
                return "i32"
            if e.op in BIT_OPS:
                if lt != "i32" or rt != "i32":
                    raise TypeCheckError(e.line, f"位运算 {e.op} 两侧必须是 i32, 得到 {type_str(lt)} 和 {type_str(rt)}")
                return "i32"
            if e.op in CMP_OPS:
                if e.op == "in":
                    # x in xs / 字符码 in str
                    if is_list(rt):
                        if lt != rt[1]:
                            raise TypeCheckError(e.line, f"in 左侧必须是 list[{type_str(rt[1])}] 的元素类型, 得到 {type_str(lt)}")
                        return "bool"
                    if rt == "str":
                        if lt != "i32":
                            raise TypeCheckError(e.line, f"字符串 in 左侧必须是字符码 i32, 得到 {type_str(lt)}")
                        return "bool"
                    raise TypeCheckError(e.line, f"in 右侧必须是 list 或 str, 得到 {type_str(rt)}")
                if lt != rt:
                    raise TypeCheckError(e.line, f"比较 {e.op} 两侧类型不一致: {type_str(lt)} 和 {type_str(rt)}")
                if lt in ("i32", "bool"):
                    return "bool"
                if lt == "str":
                    return "bool"          # v3.0: 字符串字典序比较 == != < > <= >=
                raise TypeCheckError(e.line, f"不能比较 {type_str(lt)} 类型")
                return "bool"
            raise TypeCheckError(e.line, f"未知运算符 {e.op}")

        if isinstance(e, Call):
            t = self.call(e)
            if t == "void":
                raise TypeCheckError(e.line, "void 函数(如 print)的结果不能用于表达式")
            return t

        raise TypeCheckError(getattr(e, "line", 1), "无法识别该表达式")

    def call(self, c: Call):
        if c.name == "print":
            for a in c.args:
                t = self.expr(a)
                if t not in ("i32", "bool", "str"):
                    raise TypeCheckError(c.line, f"print 不支持 {type_str(t)} 类型(仅 i32/bool/str)")
            return "void"
        if c.name == "input":
            if c.args:
                raise TypeCheckError(c.line, "input() 不需要参数")
            return "i32"
        if c.name == "getch":
            if len(c.args) != 1:
                raise TypeCheckError(c.line, "getch 需要 1 个参数(超时毫秒)")
            t = self.expr(c.args[0])
            if t != "i32":
                raise TypeCheckError(c.line, f"getch 参数必须是 i32, 得到 {type_str(t)}")
            return "i32"
        if c.name == "clrscr":
            if c.args:
                raise TypeCheckError(c.line, "clrscr() 不需要参数")
            return "void"
        if c.name == "sleep":
            if len(c.args) != 1:
                raise TypeCheckError(c.line, "sleep 需要 1 个参数(毫秒)")
            t = self.expr(c.args[0])
            if t != "i32":
                raise TypeCheckError(c.line, f"sleep 参数必须是 i32, 得到 {type_str(t)}")
            return "void"
        GFX = {"window": ("void", 2), "clear": ("void", 1), "fill_rect": ("void", 5),
               "fill_circle": ("void", 4), "draw_line": ("void", 5), "draw_char": ("void", 5),
               "poll_key": ("i32", 0), "window_closed": ("i32", 0), "present": ("void", 0)}
        if c.name in GFX:
            ret, n = GFX[c.name]
            if len(c.args) != n:
                raise TypeCheckError(c.line, f"{c.name} 需要 {n} 个参数, 得到 {len(c.args)}")
            for a in c.args:
                t = self.expr(a)
                if t != "i32":
                    raise TypeCheckError(c.line, f"{c.name} 参数必须是 i32, 得到 {type_str(t)}")
            return ret
        if c.name == "len":
            if len(c.args) != 1:
                raise TypeCheckError(c.line, "len 需要 1 个参数")
            t = self.expr(c.args[0])
            if not (is_list(t) or t == "str"):
                raise TypeCheckError(c.line, f"len 只能用于 list 或 str, 得到 {type_str(t)}")
            return "i32"
        if c.name == "abs":
            if len(c.args) != 1:
                raise TypeCheckError(c.line, "abs 需要 1 个参数")
            t = self.expr(c.args[0])
            if t != "i32":
                raise TypeCheckError(c.line, f"abs 只能用于 i32, 得到 {type_str(t)}")
            return "i32"
        if c.name in ("min", "max"):
            if len(c.args) < 1:
                raise TypeCheckError(c.line, f"{c.name} 至少需要 1 个参数")
            for a in c.args:
                if self.expr(a) != "i32":
                    raise TypeCheckError(c.line, f"{c.name} 的参数必须是 i32")
            return "i32"
        if c.name == "sum":
            if len(c.args) != 1:
                raise TypeCheckError(c.line, "sum 需要 1 个参数")
            t = self.expr(c.args[0])
            if not (is_list(t) and t[1] == "i32"):
                raise TypeCheckError(c.line, f"sum 只能用于 list[i32], 得到 {type_str(t)}")
            return "i32"
        if c.name == "pow":
            if len(c.args) != 2:
                raise TypeCheckError(c.line, "pow 需要 2 个参数: pow(底数, 指数)")
            if self.expr(c.args[0]) != "i32" or self.expr(c.args[1]) != "i32":
                raise TypeCheckError(c.line, "pow 的参数必须是 i32")
            return "i32"
        if c.name == "sqrt":
            if len(c.args) != 1:
                raise TypeCheckError(c.line, "sqrt 需要 1 个参数: sqrt(x) (整数平方根向下取整)")
            if self.expr(c.args[0]) != "i32":
                raise TypeCheckError(c.line, "sqrt 的参数必须是 i32")
            return "i32"
        if c.name == "gcd":
            if len(c.args) != 2:
                raise TypeCheckError(c.line, "gcd 需要 2 个参数: gcd(a, b)")
            if self.expr(c.args[0]) != "i32" or self.expr(c.args[1]) != "i32":
                raise TypeCheckError(c.line, "gcd 的参数必须是 i32")
            return "i32"
        if c.name == "clamp":
            if len(c.args) != 3:
                raise TypeCheckError(c.line, "clamp 需要 3 个参数: clamp(x, lo, hi)")
            for a in c.args:
                if self.expr(a) != "i32":
                    raise TypeCheckError(c.line, "clamp 的参数必须是 i32")
            return "i32"
        if c.name not in self.funcs:
            raise TypeCheckError(c.line, f"函数 {c.name} 未定义")
        f = self.funcs[c.name]
        n_required = len(f.params) - sum(1 for d in (f.defaults or []) if d is not None)
        if len(c.args) < n_required or len(c.args) > len(f.params):
            raise TypeCheckError(c.line,
                f"函数 {c.name} 需要 {n_required}-{len(f.params)} 个参数, 实际传入 {len(c.args)}")
        for (pname, ptype), a in zip(f.params, c.args):
            at = self.expr(a)
            if at != ptype:
                raise TypeCheckError(c.line, f"参数 {pname} 需要 {type_str(ptype)}, 传入 {type_str(at)}")
        return f.ret or "void"
