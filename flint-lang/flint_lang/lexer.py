# -*- coding: utf-8 -*-
"""
flint_lang.lexer — 燧石高级语言词法分析器。

token 类型: ID / NUM / STR / CHAR / OP / PUNCT / KEYWORD
注释: // 行注释, /* ... */ 块注释
"""

import re


class LexError(Exception):
    pass


KEYWORDS = {"int", "char", "void", "if", "else", "while", "for", "return", "true", "false"}

_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\",
                   "'": "'", '"': '"', "a": "\a", "b": "\b", "f": "\f", "v": "\v"}

_OP3 = {"==="}  # 占位, 未来扩展
_OP2 = {"==", "!=", "<=", ">=", "&&", "||", "+=", "-=", "*=", "/=", "%=", "<<", ">>"}
_OP1 = set("+-*/%<>=!&|^~(){}[],;")

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<num>0[xX][0-9a-fA-F]+|\d+)
  | (?P<char>'(?:\\.|[^'\\])')
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<op><<|>>|&&|\|\||[+\-*/%<>=!&|^~]=?)
  | (?P<punct>[(){}[\],;])
  | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
""", re.VERBOSE | re.DOTALL)


def _unescape(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            e = s[i + 1]
            if e in _SIMPLE_ESCAPES:
                out.append(_SIMPLE_ESCAPES[e])
                i += 2
            elif e == "x" and i + 3 < len(s):
                out.append(chr(int(s[i + 2:i + 4], 16)))
                i += 4
            else:
                raise LexError(f"未知转义序列: \\{e}")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def tokenize(src: str):
    """源码 → token 列表 [(kind, value, line), ...]。"""
    tokens = []
    line = 1
    for m in _TOKEN_RE.finditer(src):
        kind = m.lastgroup
        val = m.group()
        if kind == "ws":
            line += val.count("\n")
            continue
        if kind == "comment":
            line += val.count("\n")
            continue
        if kind == "num":
            tokens.append(("NUM", int(val, 0), line))
        elif kind == "char":
            tokens.append(("CHAR", ord(_unescape(val[1:-1])), line))
        elif kind == "str":
            tokens.append(("STR", _unescape(val[1:-1]), line))
        elif kind == "op":
            tokens.append(("OP", val, line))
        elif kind == "punct":
            tokens.append(("PUNCT", val, line))
        elif kind == "id":
            tokens.append(("KEYWORD" if val in KEYWORDS else "ID", val, line))
    return tokens
