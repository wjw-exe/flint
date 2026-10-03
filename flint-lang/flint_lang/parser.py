# -*- coding: utf-8 -*-
"""
flint_lang.parser — 燧石高级语言语法分析器。

语法概览(类 C):
    program  := (global_decl | func_def)*
    global   := type name ('[' expr? ']')? ('=' init)? ';'
    func     := type name '(' params ')' block
    params   := (type name (',' type name)*)?
    block    := '{' stmt* '}'
    stmt     := decl | block | if | while | for | return | expr ';' | ';'
    decl     := type name ('=' expr)? (',' name ('=' expr)?)* ';'
    expr     := 支持 + - * / % < > <= >= == != && || ! ~ 赋值 = += -= *= /= %=,
                数组下标, 函数调用, 字面量, 字符串

解析同时完成: 全局变量清单、每个函数的参数/局部变量布局(偏移)。
局部变量从 FP 向下分配, 参数在 FP 上方: 第 i 个参数位于 FP + 8 + 4*i。
"""

from .lexer import tokenize, LexError


class ParseError(Exception):
    pass


# ---------- AST 结点 ----------
# ('prog', globals, funcs)
# ('gdecl', type, name, init)  init ∈ {None, ('init-int',v), ('init-str',s), ('init-array',n)}
# ('func', rettype, name, params, body, layout)
# ('decl', type, name, init_expr)
# ('block', stmts)
# ('if', cond, then, els)
# ('while', cond, body)
# ('for', init, cond, upd, body)
# ('ret', expr)
# ('exprstmt', expr)
# ('num', v) ('chr', v) ('str', s)
# ('var', name) ('index', base, idx)
# ('call', name, args)
# ('bin', op, l, r) ('un', op, e)
# ('assign', target, value) ('assignop', op, target, value)
# target: ('var', name) | ('index', base, idx)


class Parser:
    def __init__(self, src):
        self.tokens = tokenize(src)
        self.pos = 0
        self.globals = []     # ('gdecl', type, name, init)
        self.funcs = []       # ('func', ...)
        self.gnames = set()
        self.fnames = set()

    # ---------- 工具 ----------
    def peek(self, k=None):
        t = self.tokens[self.pos] if self.pos < len(self.tokens) else ("EOF", None, 0)
        return t if k is None else t[0] == k

    def next(self):
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def error(self, msg):
        t = self.tokens[self.pos] if self.pos < len(self.tokens) else ("EOF", None, 0)
        raise ParseError(f"第 {t[2]} 行: {msg} (得到 {t[1]!r})")

    def expect(self, kind, val=None):
        t = self.next()
        if t[0] != kind or (val is not None and t[1] != val):
            self.error(f"期望 {val or kind}")
        return t

    def is_type(self):
        return self.peek("KEYWORD") and self.tokens[self.pos][1] in ("int", "char", "void")

    def parse_type(self):
        t = self.next()
        return t[1]

    # ---------- 顶层 ----------
    def parse_program(self):
        while self.pos < len(self.tokens):
            if not self.is_type():
                self.error("期望类型或函数定义")
            if self.peek("KEYWORD") and self.tokens[self.pos][1] == "void":
                # void 只能用于函数
                self.parse_func("void")
                continue
            # 前瞻: type name '(' → 函数; 否则 → 全局声明
            save = self.pos
            self.next()  # type
            if self.peek("ID"):
                name = self.tokens[self.pos][1]
                save2 = self.pos
                self.next()
                if self.peek("PUNCT") and self.tokens[self.pos][1] == "(":
                    self.pos = save
                    self.parse_func(None)
                    continue
                self.pos = save2
                self.pos = save  # 回退到类型开头
                self.parse_global()
            else:
                self.pos = save
                self.parse_global()
        return ("prog", self.globals, self.funcs)

    def parse_global(self):
        typ = self.parse_type()
        name_t = self.next()
        if name_t[0] != "ID":
            self.error("期望全局变量名")
        name = name_t[1]
        if name in self.gnames or name in self.fnames:
            self.error(f"名字重复定义: {name}")
        self.gnames.add(name)
        arr_size = None
        if self.peek("PUNCT") and self.tokens[self.pos][1] == "[":
            self.next()
            if self.peek("PUNCT") and self.tokens[self.pos][1] == "]":
                self.next()
                arr_size = None  # 由字符串初始化推断
            else:
                n = self.parse_expr()
                self.expect("PUNCT", "]")
                arr_size = n[1] if n[0] == "num" else self.error("数组大小必须是常量整数")
        init = None
        if self.peek("OP") and self.tokens[self.pos][1] == "=":
            self.next()
            if self.peek("STR"):
                init = ("init-str", self.next()[1])
            elif self.peek("NUM"):
                init = ("init-int", self.next()[1])
            elif self.peek("CHAR"):
                init = ("init-int", self.next()[1])
            elif arr_size is None and self.peek("PUNCT") and self.tokens[self.pos][1] == "{":
                # { 'a', 'b', ... } 字符数组字面量 —— 不支持, 提示使用字符串
                self.error("全局数组初始化请使用字符串: char a[] = \"...\"")
            else:
                self.error("全局初始化必须是整数/字符/字符串常量")
        self.expect("PUNCT", ";")
        if arr_size is None and init is None:
            self.error(f"数组 {name} 必须给出大小或字符串初始化")
        if arr_size is not None and init is not None:
            self.error(f"数组 {name} 不能同时给定大小与初始化")
        gtype = ("arr", typ, arr_size) if arr_size is not None or (init and init[0] == "init-str") else typ
        if init and init[0] == "init-str" and typ != "char":
            self.error("字符串初始化只能用于 char 数组")
        self.globals.append(("gdecl", gtype, name, init))

    def parse_func(self, known_ret):
        rettype = known_ret if known_ret else self.parse_type()
        name_t = self.next()
        if name_t[0] != "ID":
            self.error("期望函数名")
        name = name_t[1]
        if name in self.fnames or name in self.gnames:
            self.error(f"名字重复定义: {name}")
        self.fnames.add(name)
        self.expect("PUNCT", "(")
        params = []
        layout = {"params": {}, "locals": {}, "size": 0}
        if not (self.peek("PUNCT") and self.tokens[self.pos][1] == ")"):
            while True:
                if not self.is_type():
                    self.error("期望参数类型")
                ptype = self.parse_type()
                pname = self.next()
                if pname[0] != "ID":
                    self.error("期望参数名")
                if pname[1] in layout["params"] or pname[1] in layout["locals"]:
                    self.error(f"参数重名: {pname[1]}")
                idx = len(params)
                params.append((ptype, pname[1]))
                layout["params"][pname[1]] = (ptype, idx)
                if self.peek("PUNCT") and self.tokens[self.pos][1] == ",":
                    self.next()
                    continue
                break
        self.expect("PUNCT", ")")
        body = self.parse_block(layout)
        self.funcs.append(("func", rettype, name, params, body, layout))
        return ("func", rettype, name, params, body, layout)

    # ---------- 语句 ----------
    def parse_block(self, layout):
        self.expect("PUNCT", "{")
        stmts = []
        while not (self.peek("PUNCT") and self.tokens[self.pos][1] == "}"):
            if self.pos >= len(self.tokens):
                self.error("块未闭合")
            stmts.append(self.parse_stmt(layout))
        self.expect("PUNCT", "}")
        return ("block", stmts)

    def parse_stmt(self, layout):
        if self.is_type():
            return self.parse_decl(layout)
        t = self.peek()
        if t[0] == "PUNCT" and t[1] == "{":
            return self.parse_block(layout)
        if t[0] == "KEYWORD":
            kw = t[1]
            if kw == "if":
                return self.parse_if(layout)
            if kw == "while":
                return self.parse_while(layout)
            if kw == "for":
                return self.parse_for(layout)
            if kw == "return":
                self.next()
                if self.peek("PUNCT") and self.tokens[self.pos][1] == ";":
                    self.next()
                    return ("ret", None)
                e = self.parse_expr()
                self.expect("PUNCT", ";")
                return ("ret", e)
            self.error(f"意外的关键字: {kw}")
        if t[0] == "PUNCT" and t[1] == ";":
            self.next()
            return ("empty",)
        e = self.parse_expr()
        self.expect("PUNCT", ";")
        return ("exprstmt", e)

    def parse_decl(self, layout):
        typ = self.parse_type()
        names = []
        while True:
            name_t = self.next()
            if name_t[0] != "ID":
                self.error("期望变量名")
            name = name_t[1]
            arr_size = None
            if self.peek("PUNCT") and self.tokens[self.pos][1] == "[":
                self.next()
                if self.peek("PUNCT") and self.tokens[self.pos][1] == "]":
                    self.error("局部数组必须给出大小")
                n = self.parse_expr()
                self.expect("PUNCT", "]")
                arr_size = n[1] if n[0] == "num" else self.error("数组大小必须是常量")
            init = None
            if self.peek("OP") and self.tokens[self.pos][1] == "=":
                self.next()
                if self.peek("STR"):
                    self.error("局部数组/变量不能直接用字符串初始化, 请用 strcpy")
                init = self.parse_expr()
            # 分配布局
            vtype = ("arr", typ, arr_size) if arr_size is not None else typ
            if name in layout["params"] or name in layout["locals"]:
                self.error(f"变量重名: {name}")
            if arr_size is not None:
                esize = 1 if typ == "char" else 4
                size = esize * arr_size
            else:
                size = 1 if typ == "char" else 4
            layout["size"] += size
            layout["locals"][name] = (vtype, layout["size"])
            names.append(("decl", vtype, name, init))
            if self.peek("PUNCT") and self.tokens[self.pos][1] == ",":
                self.next()
                continue
            break
        self.expect("PUNCT", ";")
        return ("block", names) if len(names) > 1 else names[0]

    def parse_if(self, layout):
        self.expect("KEYWORD", "if")
        self.expect("PUNCT", "(")
        cond = self.parse_expr()
        self.expect("PUNCT", ")")
        then = self.parse_stmt(layout)
        els = None
        if self.peek("KEYWORD") and self.tokens[self.pos][1] == "else":
            self.next()
            els = self.parse_stmt(layout)
        return ("if", cond, then, els)

    def parse_while(self, layout):
        self.expect("KEYWORD", "while")
        self.expect("PUNCT", "(")
        cond = self.parse_expr()
        self.expect("PUNCT", ")")
        body = self.parse_stmt(layout)
        return ("while", cond, body)

    def parse_for(self, layout):
        self.expect("KEYWORD", "for")
        self.expect("PUNCT", "(")
        init = None
        if not (self.peek("PUNCT") and self.tokens[self.pos][1] == ";"):
            if self.is_type():
                init = self.parse_decl(layout)
            else:
                init = self.parse_expr()
                self.expect("PUNCT", ";")
        else:
            self.next()
        cond = None
        if not (self.peek("PUNCT") and self.tokens[self.pos][1] == ";"):
            cond = self.parse_expr()
        self.expect("PUNCT", ";")
        upd = None
        if not (self.peek("PUNCT") and self.tokens[self.pos][1] == ")"):
            upd = self.parse_expr()
        self.expect("PUNCT", ")")
        body = self.parse_stmt(layout)
        return ("for", init, cond, upd, body)

    # ---------- 表达式(优先级爬升) ----------
    def parse_expr(self):
        return self.parse_assign()

    def parse_assign(self):
        left = self.parse_or()
        t = self.peek()
        if t[0] == "OP" and t[1] in ("=", "+=", "-=", "*=", "/=", "%="):
            op = t[1]
            self.next()
            value = self.parse_assign()
            if left[0] not in ("var", "index"):
                self.error("赋值目标必须是变量或数组元素")
            if op == "=":
                return ("assign", left, value)
            return ("assignop", op[0], left, value)
        return left

    def parse_or(self):
        left = self.parse_and()
        while self.peek("OP") and self.tokens[self.pos][1] == "||":
            self.next()
            right = self.parse_and()
            left = ("bin", "||", left, right)
        return left

    def parse_and(self):
        left = self.parse_bit_or()
        while self.peek("OP") and self.tokens[self.pos][1] == "&&":
            self.next()
            right = self.parse_bit_or()
            left = ("bin", "&&", left, right)
        return left

    def parse_bit_or(self):
        left = self.parse_bit_xor()
        while self.peek("OP") and self.tokens[self.pos][1] == "|":
            self.next()
            right = self.parse_bit_xor()
            left = ("bin", "|", left, right)
        return left

    def parse_bit_xor(self):
        left = self.parse_bit_and()
        while self.peek("OP") and self.tokens[self.pos][1] == "^":
            self.next()
            right = self.parse_bit_and()
            left = ("bin", "^", left, right)
        return left

    def parse_bit_and(self):
        left = self.parse_eq()
        while self.peek("OP") and self.tokens[self.pos][1] == "&":
            self.next()
            right = self.parse_eq()
            left = ("bin", "&", left, right)
        return left

    def parse_eq(self):
        left = self.parse_rel()
        while self.peek("OP") and self.tokens[self.pos][1] in ("==", "!="):
            op = self.next()[1]
            right = self.parse_rel()
            left = ("bin", op, left, right)
        return left

    def parse_rel(self):
        left = self.parse_shift()
        while self.peek("OP") and self.tokens[self.pos][1] in ("<", ">", "<=", ">="):
            op = self.next()[1]
            right = self.parse_shift()
            left = ("bin", op, left, right)
        return left

    def parse_shift(self):
        left = self.parse_add()
        while self.peek("OP") and self.tokens[self.pos][1] in ("<<", ">>"):
            op = self.next()[1]
            right = self.parse_add()
            left = ("bin", op, left, right)
        return left

    def parse_add(self):
        left = self.parse_mul()
        while self.peek("OP") and self.tokens[self.pos][1] in ("+", "-"):
            op = self.next()[1]
            right = self.parse_mul()
            left = ("bin", op, left, right)
        return left

    def parse_mul(self):
        left = self.parse_unary()
        while self.peek("OP") and self.tokens[self.pos][1] in ("*", "/", "%"):
            op = self.next()[1]
            right = self.parse_unary()
            left = ("bin", op, left, right)
        return left

    def parse_unary(self):
        t = self.peek()
        if t[0] == "OP" and t[1] in ("-", "!", "~"):
            op = self.next()[1]
            return ("un", op, self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self):
        e = self.parse_primary()
        while True:
            t = self.peek()
            if t[0] == "PUNCT" and t[1] == "[":
                self.next()
                idx = self.parse_expr()
                self.expect("PUNCT", "]")
                e = ("index", e, idx)
            elif t[0] == "PUNCT" and t[1] == "(":
                self.next()
                args = []
                if not (self.peek("PUNCT") and self.tokens[self.pos][1] == ")"):
                    while True:
                        args.append(self.parse_expr())
                        if self.peek("PUNCT") and self.tokens[self.pos][1] == ",":
                            self.next()
                            continue
                        break
                self.expect("PUNCT", ")")
                e = ("call", e, args)
            else:
                return e

    def parse_primary(self):
        t = self.next()
        if t[0] == "NUM":
            return ("num", t[1])
        if t[0] == "CHAR":
            return ("chr", t[1])
        if t[0] == "STR":
            return ("str", t[1])
        if t[0] == "ID":
            if t[1] == "true":
                return ("num", 1)
            if t[1] == "false":
                return ("num", 0)
            return ("var", t[1])
        if t[0] == "KEYWORD" and t[1] in ("true", "false"):
            return ("num", 1 if t[1] == "true" else 0)
        if t[0] == "PUNCT" and t[1] == "(":
            e = self.parse_expr()
            self.expect("PUNCT", ")")
            return e
        self.error("期望表达式")


def parse(src):
    p = Parser(src)
    return p.parse_program()
