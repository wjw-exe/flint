# -*- coding: utf-8 -*-
"""
parser.py — Flint v3.0 递归下降解析器。

把 token 流解析为抽象语法树(AST)。语法与 Python 高度一致:
  def 函数定义 / if-elif-else / while / for x in range(...) / for x in xs
  break / continue / pass / return / print(a, b, c) / 全局变量声明
  x: i32 = 值(显式标注) 或 x = 值(类型推断, 首次赋值即声明)
  xs: list[i32] = [1, 2, 3] / xs[i] / xs[i] = v / x += 1
  // 地板除 / 链式比较 a < b < c / 字符串字面量拼接折叠
缩进块由 INDENT/DEDENT 驱动, 不需要任何大括号。
"""


class ParseError(Exception):
    def __init__(self, line, msg):
        super().__init__(f"第 {line} 行: {msg}")


# ---------- 类型表示 ----------
# 标量: "i32" / "bool" / "str"
# 列表: ("list", 元素类型, 大小或 None)  大小=显式声明的元素个数
def type_str(t) -> str:
    if isinstance(t, tuple):
        size = f"; {t[2]}" if t[2] is not None else ""
        return f"list[{t[1]}{size}]"
    return t


# ---------- 数值语义(与虚拟机一致) ----------
def trunc_div(a, b):
    """向零截断除法(VM DIV 语义)。"""
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def trunc_mod(a, b):
    return a - trunc_div(a, b) * b


# ---------- AST 节点 ----------
class Node:
    def __init__(self, line):
        self.line = line


class Program(Node):
    def __init__(self, globals_, funcs):
        super().__init__(1)
        self.globals = globals_    # [Decl, ...] 顶层变量声明
        self.funcs = funcs


class FuncDef(Node):
    def __init__(self, line, name, params, ret, body, defaults=None):
        super().__init__(line)
        self.name = name
        self.params = params      # [(名字, 类型), ...]
        self.ret = ret            # 类型 或 None(void)
        self.body = body
        self.defaults = defaults or []   # v3.0: 与 params 对齐的默认值列表(无默认处为 None)


class Ternary(Node):
    """条件表达式: a if c else b (Python 风格三目, v3.0)"""

    def __init__(self, line, cond, if_expr, else_expr):
        super().__init__(line)
        self.cond = cond
        self.if_expr = if_expr
        self.else_expr = else_expr


class Decl(Node):
    """声明: name: TYPE = expr | name: TYPE(仅 list 可省略初值)"""

    def __init__(self, line, name, typ, expr):
        super().__init__(line)
        self.name = name
        self.typ = typ
        self.expr = expr          # 可为 None


class Assign(Node):
    """赋值: name = expr(首次出现即声明, 类型由初值推断)"""

    def __init__(self, line, name, expr):
        super().__init__(line)
        self.name = name
        self.expr = expr


class IndexAssign(Node):
    """索引赋值: xs[i] = expr"""

    def __init__(self, line, base, index, expr):
        super().__init__(line)
        self.base = base          # Var
        self.index = index
        self.expr = expr


class AugAssign(Node):
    """复合赋值: x += e / xs[i] *= e"""

    def __init__(self, line, target, op, expr):
        super().__init__(line)
        self.target = target      # Var 或 Index
        self.op = op              # "+=" 等
        self.expr = expr


class Return(Node):
    def __init__(self, line, expr):
        super().__init__(line)
        self.expr = expr


class If(Node):
    def __init__(self, line, cond, body, elifs, else_body):
        super().__init__(line)
        self.cond = cond
        self.body = body
        self.elifs = elifs
        self.else_body = else_body


class While(Node):
    """else_body: 循环未 break 正常结束时执行(Python 的 while-else 语义)。"""

    def __init__(self, line, cond, body, else_body=None):
        super().__init__(line)
        self.cond = cond
        self.body = body
        self.else_body = else_body


class For(Node):
    """for x in range(a, b, step) 或 for x in xs; else_body: 循环未 break 时执行(Python 语义)。"""

    def __init__(self, line, mode, var, start, end, step, iter_expr, body, else_body=None):
        super().__init__(line)
        self.mode = mode          # "range" | "list"
        self.var = var
        self.start = start        # range: 起始
        self.end = end            # range: 结束(不含)
        self.step = step          # range: 步长(字面量可编译期定方向)
        self.iter = iter_expr     # list: 被遍历的列表表达式
        self.body = body
        self.else_body = else_body


class Break(Node):
    pass


class Continue(Node):
    pass


class Pass(Node):
    pass


class ExprStmt(Node):
    """表达式语句(函数调用)。"""

    def __init__(self, line, expr):
        super().__init__(line)
        self.expr = expr


class BinOp(Node):
    def __init__(self, line, op, left, right):
        super().__init__(line)
        self.op = op
        self.left = left
        self.right = right


class Unary(Node):
    def __init__(self, line, op, operand):
        super().__init__(line)
        self.op = op
        self.operand = operand


class Call(Node):
    def __init__(self, line, name, args):
        super().__init__(line)
        self.name = name
        self.args = args


class Var(Node):
    def __init__(self, line, name):
        super().__init__(line)
        self.name = name


class Index(Node):
    """列表/字符串索引: base[idx]"""

    def __init__(self, line, base, index):
        super().__init__(line)
        self.base = base
        self.index = index


class IntLit(Node):
    def __init__(self, line, val):
        super().__init__(line)
        self.val = val


class BoolLit(Node):
    def __init__(self, line, val):
        super().__init__(line)
        self.val = val


class StrLit(Node):
    def __init__(self, line, val):
        super().__init__(line)
        self.val = val


class ListLit(Node):
    def __init__(self, line, elems):
        super().__init__(line)
        self.elems = elems


# ---------- 解析器 ----------
class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.p = 0

    def peek(self):
        return self.tokens[self.p]

    def peek2(self):
        return self.tokens[self.p + 1] if self.p + 1 < len(self.tokens) else ("EOF", "", -1)

    def next(self):
        t = self.tokens[self.p]
        self.p += 1
        return t

    def at(self, kind, val=None):
        k = self.peek()
        if k[0] != kind:
            return False
        return val is None or k[1] == val

    def at_op_oneof(self, vals):
        k = self.peek()
        return k[0] == "OP" and k[1] in vals

    def expect(self, kind, val=None):
        k = self.peek()
        if k[0] != kind or (val is not None and k[1] != val):
            raise ParseError(k[2], f"期望 {kind}{' ' + str(val) if val else ''}, 实际得到 {k[1]!r}")
        return self.next()

    # ---------- 类型 ----------
    def parse_type(self):
        k = self.peek()
        if k[0] == "TYPE":
            self.next()
            return k[1]
        if k[0] == "KW" and k[1] == "list":
            self.next()
            self.expect("OP", "[")
            elem = self.expect("TYPE")[1]
            if elem not in ("i32", "bool"):
                raise ParseError(k[2], f"list 元素类型仅支持 i32/bool, 得到 {elem}")
            size = None
            if self.at("OP", ";"):
                self.next()
                size = self.expect("NUM")[1]
            self.expect("OP", "]")
            return ("list", elem, size)
        raise ParseError(k[2], f"期望类型, 得到 {k[1]!r}")

    # ---------- 顶层 ----------
    def parse_program(self):
        globals_, funcs = [], []
        while self.peek()[0] != "EOF":
            k = self.peek()
            if k[0] in ("NEWLINE", "DEDENT"):
                self.next()
                continue
            if k[0] == "KW" and k[1] == "def":
                funcs.append(self.parse_func())
            elif k[0] == "ID":
                globals_.append(self.parse_global_decl())
            else:
                raise ParseError(k[2], f"顶层只能定义函数 def 或变量声明, 得到 {k[1]!r}")
        return Program(globals_, funcs)

    def parse_global_decl(self):
        """顶层变量声明(仅字面量初值): g: i32 = 5 / data: list[i32] = [1, 2]"""
        line = self.peek()[2]
        name = self.expect("ID")[1]
        self.expect("OP", ":")
        typ = self.parse_type()
        self.expect("OP", "=")
        expr = self.parse_expr()
        self.expect("NEWLINE")
        return Decl(line, name, typ, expr)

    # ---------- 函数 ----------
    def parse_func(self):
        line = self.next()[2]  # def
        name = self.expect("ID")[1]
        self.expect("OP", "(")
        params = []
        defaults = []
        if not self.at("OP", ")"):
            while True:
                pn = self.expect("ID")[1]
                self.expect("OP", ":")
                pt = self.parse_type()
                default = None
                if self.at("OP", "="):       # v3.0: 默认参数值 def f(x: i32, y: i32 = 10)
                    self.next()
                    default = self.parse_expr()
                params.append((pn, pt))
                defaults.append(default)
                if self.at("OP", ","):
                    self.next()
                    continue
                break
        self.expect("OP", ")")
        ret = None
        if self.at("OP", "->"):
            self.next()
            ret = self.parse_type()
        self.expect("OP", ":")
        self.expect("NEWLINE")
        self.expect("INDENT")
        body = self.parse_block()
        self.expect("DEDENT")
        return FuncDef(line, name, params, ret, body, defaults)

    def parse_block(self):
        stmts = []
        while self.peek()[0] not in ("DEDENT", "EOF"):
            stmts.append(self.parse_stmt())
        return stmts

    # ---------- 语句 ----------
    def parse_stmt(self):
        k = self.peek()
        if k[0] == "KW":
            if k[1] == "return":
                self.next()
                expr = None
                if self.peek()[0] != "NEWLINE":
                    expr = self.parse_expr()
                self.expect("NEWLINE")
                return Return(k[2], expr)
            if k[1] == "if":
                return self.parse_if()
            if k[1] == "while":
                return self.parse_while()
            if k[1] == "for":
                return self.parse_for()
            if k[1] == "break":
                self.next()
                self.expect("NEWLINE")
                return Break(k[2])
            if k[1] == "continue":
                self.next()
                self.expect("NEWLINE")
                return Continue(k[2])
            if k[1] == "pass":
                self.next()
                self.expect("NEWLINE")
                return Pass(k[2])
            if k[1] == "print":
                self.next()
                self.expect("OP", "(")
                args = []
                if not self.at("OP", ")"):
                    args.append(self.parse_expr())
                    while self.at("OP", ","):
                        self.next()
                        args.append(self.parse_expr())
                self.expect("OP", ")")
                self.expect("NEWLINE")
                return ExprStmt(k[2], Call(k[2], "print", args))
            raise ParseError(k[2], f"语句不能以 {k[1]} 开头")
        if k[0] == "ID":
            return self.parse_id_stmt()
        raise ParseError(k[2], f"无法解析的语句: {k[1]!r}")

    def parse_id_stmt(self):
        """以 ID 开头的语句: 声明 / 赋值 / 复合赋值 / 索引赋值 / 调用。"""
        line = self.peek()[2]
        name = self.next()[1]
        if self.at("OP", ":"):
            self.next()
            typ = self.parse_type()
            if self.at("OP", "="):
                self.next()
                expr = self.parse_expr()
            elif isinstance(typ, tuple) and typ[2] is not None:
                expr = None  # list[i32; N] 无初值 → 全零
            else:
                raise ParseError(line, f"变量 {name} 声明需要初值 (= 值)")
            self.expect("NEWLINE")
            return Decl(line, name, typ, expr)
        if self.at("OP", "["):
            # 索引赋值 xs[i] = v / 索引复合赋值 xs[i] += v
            self.next()
            idx = self.parse_expr()
            self.expect("OP", "]")
            if self.at_op_oneof(("+=", "-=", "*=", "/=", "%=", "//=",
                                 "&=", "|=", "^=", "<<=", ">>=")):
                op = self.next()[1]
                expr = self.parse_expr()
                self.expect("NEWLINE")
                return AugAssign(line, Index(line, Var(line, name), idx), op, expr)
            self.expect("OP", "=")
            value = self.parse_expr()
            self.expect("NEWLINE")
            return IndexAssign(line, Var(line, name), idx, value)
        if self.at_op_oneof(("+=", "-=", "*=", "/=", "%=", "//=",
                             "&=", "|=", "^=", "<<=", ">>=")):
            op = self.next()[1]
            expr = self.parse_expr()
            self.expect("NEWLINE")
            return AugAssign(line, Var(line, name), op, expr)
        if self.at("OP", "="):
            self.next()
            expr = self.parse_expr()
            self.expect("NEWLINE")
            return Assign(line, name, expr)
        if self.at("OP", "("):
            call = self.parse_call_tail(line, name)
            self.expect("NEWLINE")
            return ExprStmt(line, call)
        raise ParseError(line, f"变量 {name} 需要以 ': 类型 = 值' 声明或以 '=' 赋值")

    def parse_if(self):
        line = self.next()[2]
        cond = self.parse_expr()
        self.expect("OP", ":")
        self.expect("NEWLINE")
        self.expect("INDENT")
        body = self.parse_block()
        self.expect("DEDENT")
        elifs = []
        while self.at("KW", "elif"):
            self.next()
            c = self.parse_expr()
            self.expect("OP", ":")
            self.expect("NEWLINE")
            self.expect("INDENT")
            b = self.parse_block()
            self.expect("DEDENT")
            elifs.append((c, b))
        else_body = None
        if self.at("KW", "else"):
            self.next()
            self.expect("OP", ":")
            self.expect("NEWLINE")
            self.expect("INDENT")
            else_body = self.parse_block()
            self.expect("DEDENT")
        return If(line, cond, body, elifs, else_body)

    def parse_while(self):
        line = self.next()[2]
        cond = self.parse_expr()
        self.expect("OP", ":")
        self.expect("NEWLINE")
        self.expect("INDENT")
        body = self.parse_block()
        self.expect("DEDENT")
        else_body = None
        if self.at("KW", "else"):
            self.next()
            self.expect("OP", ":")
            self.expect("NEWLINE")
            self.expect("INDENT")
            else_body = self.parse_block()
            self.expect("DEDENT")
        return While(line, cond, body, else_body)

    def parse_for(self):
        line = self.next()[2]
        var = self.expect("ID")[1]
        self.expect("KW", "in")
        if self.at("KW", "range") and self.p + 1 < len(self.tokens) \
                and self.tokens[self.p + 1][0] == "OP" and self.tokens[self.p + 1][1] == "(":
            self.next()
            self.expect("OP", "(")
            args = [self.parse_expr()]
            while self.at("OP", ","):
                self.next()
                args.append(self.parse_expr())
            self.expect("OP", ")")
            if len(args) == 1:
                start, end, step = IntLit(line, 0), args[0], IntLit(line, 1)
            elif len(args) == 2:
                start, end, step = args[0], args[1], IntLit(line, 1)
            elif len(args) == 3:
                start, end, step = args[0], args[1], args[2]
            else:
                raise ParseError(line, "range 接受 1~3 个参数")
            self.expect("OP", ":")
            self.expect("NEWLINE")
            self.expect("INDENT")
            body = self.parse_block()
            self.expect("DEDENT")
            else_body = self._parse_loop_else()
            return For(line, "range", var, start, end, step, None, body, else_body)
        # for x in <list 表达式>
        iter_expr = self.parse_expr()
        self.expect("OP", ":")
        self.expect("NEWLINE")
        self.expect("INDENT")
        body = self.parse_block()
        self.expect("DEDENT")
        else_body = self._parse_loop_else()
        return For(line, "list", var, None, None, None, iter_expr, body, else_body)

    def _parse_loop_else(self):
        """循环体后的 else: 块(同缩进, Python 的 while/for-else 语义)。"""
        if not self.at("KW", "else"):
            return None
        line = self.next()[2]
        self.expect("OP", ":")
        self.expect("NEWLINE")
        self.expect("INDENT")
        body = self.parse_block()
        self.expect("DEDENT")
        return body

    # ---------- 表达式 ----------
    def parse_expr(self):
        node = self.parse_or()
        if self.at("KW", "if"):            # v3.0: 条件表达式 a if c else b (右结合)
            line = node.line
            self.next()
            cond = self.parse_or()
            self.expect("KW", "else")
            else_e = self.parse_expr()
            return Ternary(line, cond, node, else_e)
        return node

    def parse_or(self):
        node = self.parse_and()
        while self.at("KW", "or"):
            self.next()
            node = BinOp(node.line, "or", node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_not()
        while self.at("KW", "and"):
            self.next()
            node = BinOp(node.line, "and", node, self.parse_not())
        return node

    def parse_not(self):
        if self.at("KW", "not"):
            line = self.next()[2]
            return Unary(line, "not", self.parse_not())
        return self.parse_cmp()

    def _at_cmp_op(self):
        """比较运算符或成员测试 in(注意 in 是关键字 token 而非 OP)。"""
        return (self.at_op_oneof(("<", ">", "<=", ">=", "==", "!="))
                or self.at("KW", "in")
                or (self.at("KW", "not") and self.peek2()[:2] == ("KW", "in")))

    def parse_cmp(self):
        """比较 + 成员测试, 支持链式: a < b < c → (a<b) and (b<c); x in xs。"""
        left = self.parse_bitor()
        if not self._at_cmp_op():
            return left
        # x not in xs → not (x in xs) (与 Python 一致)
        neg = False
        if self.at("KW", "not"):
            self.next()
            neg = True
        op = self.next()[1]
        right = self.parse_bitor()
        node = BinOp(left.line, op, left, right)
        if neg:
            node = Unary(node.line, "not", node)
        while self._at_cmp_op():
            neg2 = False
            if self.at("KW", "not"):
                self.next()
                neg2 = True
            op2 = self.next()[1]
            right2 = self.parse_bitor()
            mid = right  # 中间表达式复用(求值两次, 见 README)
            part = BinOp(mid.line, op2, mid, right2)
            if neg2:
                part = Unary(part.line, "not", part)
            node = BinOp(node.line, "and", node, part)
            right = right2
        return node

    def parse_bitor(self):
        node = self.parse_bitxor()
        while self.at_op_oneof(("|",)):
            self.next()
            node = BinOp(node.line, "|", node, self.parse_bitxor())
        return self._fold(node)

    def parse_bitxor(self):
        node = self.parse_bitand()
        while self.at_op_oneof(("^",)):
            self.next()
            node = BinOp(node.line, "^", node, self.parse_bitand())
        return self._fold(node)

    def parse_bitand(self):
        node = self.parse_shift()
        while self.at_op_oneof(("&",)):
            self.next()
            node = BinOp(node.line, "&", node, self.parse_shift())
        return self._fold(node)

    def parse_shift(self):
        node = self.parse_add()
        while self.at_op_oneof(("<<", ">>")):
            op = self.next()[1]
            node = BinOp(node.line, op, node, self.parse_add())
        return self._fold(node)

    def parse_add(self):
        node = self.parse_mul()
        while self.at_op_oneof(("+", "-")):
            op = self.next()[1]
            node = BinOp(node.line, op, node, self.parse_mul())
        return self._fold(node)

    def parse_mul(self):
        node = self.parse_unary()
        while self.at_op_oneof(("*", "/", "%", "//")):
            op = self.next()[1]
            node = BinOp(node.line, op, node, self.parse_unary())
        return self._fold(node)

    def parse_unary(self):
        if self.at("OP", "-"):
            self.next()
            operand = self.parse_unary()
            if isinstance(operand, IntLit):
                return IntLit(operand.line, -operand.val)  # 常量折叠
            return Unary(operand.line, "-", operand)
        if self.at("OP", "~"):
            self.next()
            operand = self.parse_unary()
            if isinstance(operand, IntLit):
                return IntLit(operand.line, ~operand.val)  # 常量折叠
            return Unary(operand.line, "~", operand)
        return self.parse_primary()

    def parse_primary(self):
        k = self.peek()
        if k[0] == "NUM":
            self.next()
            return IntLit(k[2], k[1])
        if k[0] == "STR":
            self.next()
            return StrLit(k[2], k[1])
        if k[0] == "CHAR":
            self.next()
            return IntLit(k[2], k[1])        # v3.1: 字符字面量 → 字符码 i32
        if k[0] == "TYPE" and k[1] == "str":
            self.next()
            self.expect("OP", "(")
            arg = self.parse_expr()
            self.expect("OP", ")")
            return Call(k[2], "str", [arg])  # v3.1: str(i32) 数字转字符串
        if k[0] == "KW" and k[1] in ("True", "False"):
            self.next()
            return BoolLit(k[2], k[1] == "True")
        if k[0] == "KW" and k[1] == "input":
            self.next()
            self.expect("OP", "(")
            self.expect("OP", ")")
            return Call(k[2], "input", [])
        if k[0] == "KW" and k[1] in ("len", "abs"):
            self.next()
            self.expect("OP", "(")
            arg = self.parse_expr()
            self.expect("OP", ")")
            return Call(k[2], k[1], [arg])
        if k[0] == "KW" and k[1] in ("min", "max", "sum", "pow"):
            self.next()
            self.expect("OP", "(")
            args = [self.parse_expr()]
            while self.at("OP", ","):
                self.next()
                args.append(self.parse_expr())
            self.expect("OP", ")")
            return Call(k[2], k[1], args)
        if k[0] == "ID":
            self.next()
            if self.at("OP", "("):
                return self.parse_call_tail(k[2], k[1])
            node = Var(k[2], k[1])
            while self.at("OP", "["):
                self.next()
                idx = self.parse_expr()
                self.expect("OP", "]")
                node = Index(k[2], node, idx)
            return node
        if k[0] == "OP" and k[1] == "(":
            self.next()
            e = self.parse_expr()
            self.expect("OP", ")")
            return e
        if k[0] == "OP" and k[1] == "[":
            self.next()
            elems = []
            if not self.at("OP", "]"):
                elems.append(self.parse_expr())
                while self.at("OP", ","):
                    self.next()
                    elems.append(self.parse_expr())
            self.expect("OP", "]")
            return ListLit(k[2], elems)
        raise ParseError(k[2], f"无法解析的表达式: {k[1]!r}")

    def parse_call_tail(self, line, name):
        self.expect("OP", "(")
        args = []
        if not self.at("OP", ")"):
            args.append(self.parse_expr())
            while self.at("OP", ","):
                self.next()
                args.append(self.parse_expr())
        self.expect("OP", ")")
        return Call(line, name, args)

    # ---------- 编译期常量折叠(性能优化) ----------
    @staticmethod
    def _fold(node):
        """字面量算术折叠: 2+3 → 5; "a"+"b" → "ab"。"""
        if not isinstance(node, BinOp):
            return node
        if node.op == "+" and isinstance(node.left, StrLit) and isinstance(node.right, StrLit):
            return StrLit(node.line, node.left.val + node.right.val)
        if isinstance(node.left, IntLit) and isinstance(node.right, IntLit):
            a, b = node.left.val, node.right.val
            if node.op == "+":
                return IntLit(node.line, a + b)
            if node.op == "-":
                return IntLit(node.line, a - b)
            if node.op == "*":
                return IntLit(node.line, a * b)
            if node.op == "/" and b != 0:
                return IntLit(node.line, trunc_div(a, b))
            if node.op == "%" and b != 0:
                return IntLit(node.line, trunc_mod(a, b))
            if node.op == "//" and b != 0:
                return IntLit(node.line, a // b)  # 地板除
            if node.op == "&":
                return IntLit(node.line, a & b)
            if node.op == "|":
                return IntLit(node.line, a | b)
            if node.op == "^":
                return IntLit(node.line, a ^ b)
            if node.op == "<<":
                return IntLit(node.line, a << (b & 31))
            if node.op == ">>":
                return IntLit(node.line, a >> (b & 31))
        return node


def parse_source(src: str) -> Program:
    """便捷入口: 源码字符串 → AST。"""
    from lexer import tokenize
    return Parser(tokenize(src)).parse_program()
