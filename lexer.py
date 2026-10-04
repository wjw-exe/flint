# -*- coding: utf-8 -*-
"""
lexer.py — Flint v3.0 词法分析器(缩进感知)。

与 Python 一致: 代码块用缩进表示(空格, 4 空格惯例), 生成 INDENT/DEDENT token。
不支持 Tab 缩进(会直接报错, 避免歧义)。

Token 种类:
  NUM / STR / ID / TYPE(i32|bool|str) / KW / OP / NEWLINE / INDENT / DEDENT / EOF
每个 token 为三元组 (kind, value, line)。

v2.1 新增:
  复合赋值 //= += -= *= /= %= ; 地板除 // ; 方括号/分号 [ ] ;
  关键字 list break continue pass len abs
"""

import re

KEYWORDS = {"def", "return", "if", "elif", "else", "while", "for", "in",
            "range", "print", "input", "True", "False", "and", "or", "not",
            "list", "break", "continue", "pass", "len", "abs",
            "min", "max", "sum", "pow"}
TYPES = {"i32", "bool", "str"}

_TOKEN_RE = re.compile(r"""
    (?P<ws>[ \t]+)
  | (?P<comment>\#[^\n]*)
  | (?P<num>\d+)
  | (?P<str>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
  | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>//=|//|->|==|!=|<=|>=|<<=|>>=|&=|\|=|\^=|\+=|-=|\*=|/=|%=|<<|>>|[+\-*/%<>=&|^~()=,:\ \[\];])
""", re.VERBOSE)

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\",
            '"': '"', "'": "'", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


class LexError(Exception):
    pass


def _unescape(raw: str) -> str:
    """去掉引号并处理转义序列。"""
    body = raw[1:-1]
    out = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            e = body[i + 1]
            if e in _ESCAPES:
                out.append(_ESCAPES[e])
                i += 2
            elif e == "x":
                out.append(chr(int(body[i + 2:i + 4], 16)))
                i += 4
            else:
                raise LexError(f"未知转义序列: \\{e}")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def tokenize(src: str):
    """源码 → token 列表(含 INDENT/DEDENT)。"""
    tokens = []
    indent_stack = [0]
    lines = src.split("\n")
    for lineno, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if stripped == "":
            continue
        # 检查缩进是否用了 Tab
        lead = raw[:len(raw) - len(raw.lstrip(" "))]
        if "\t" in lead:
            raise LexError(f"第 {lineno} 行: 缩进请使用空格, 不支持 Tab")
        indent = len(lead)
        if stripped.startswith("#"):
            continue  # 纯注释行
        # 缩进层级变化 → INDENT / DEDENT
        if indent > indent_stack[-1]:
            indent_stack.append(indent)
            tokens.append(("INDENT", "", lineno))
        else:
            while indent < indent_stack[-1]:
                indent_stack.pop()
                tokens.append(("DEDENT", "", lineno))
            if indent != indent_stack[-1]:
                raise LexError(f"第 {lineno} 行: 缩进不一致 (期望与某层对齐)")
        # 本行 token
        for m in _TOKEN_RE.finditer(stripped):
            kind = m.lastgroup
            val = m.group()
            if kind in ("ws", "comment"):
                continue
            if kind == "num":
                tokens.append(("NUM", int(val), lineno))
            elif kind == "str":
                tokens.append(("STR", _unescape(val), lineno))
            elif kind == "id":
                if val in KEYWORDS:
                    tokens.append(("KW", val, lineno))
                elif val in TYPES:
                    tokens.append(("TYPE", val, lineno))
                else:
                    tokens.append(("ID", val, lineno))
            else:
                tokens.append(("OP", val, lineno))
        tokens.append(("NEWLINE", "", lineno))
    while indent_stack:
        indent_stack.pop()
        tokens.append(("DEDENT", "", lineno))
    tokens.append(("EOF", "", lineno))
    return tokens
