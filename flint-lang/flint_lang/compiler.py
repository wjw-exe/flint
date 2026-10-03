# -*- coding: utf-8 -*-
"""
flint_lang.compiler — 燧石编译器总管线。

    .fl 源码 → 词法 → 语法 → 代码生成(.asm) → 汇编器(.bin) → 虚拟机

对外提供:
    compile_flint(src)      -> (asm_text, image_bytes, symbols)
    compile_asm(src)        -> (image_bytes, symbols)
"""

from . import assembler as asm_mod
from .codegen import compile_to_asm, CodegenError
from .parser import parse, ParseError
from .lexer import LexError
from .assembler import AsmError
from .vm import VM, VMError, run_image

__all__ = ["compile_flint", "compile_asm", "run_image", "VM", "VMError",
           "CodegenError", "ParseError", "LexError", "AsmError", "parse", "compile_to_asm"]


def compile_asm(src):
    """汇编文本 → (字节码镜像, 符号表)。"""
    return asm_mod.assemble(src)


def compile_flint(src):
    """高级语言源码 → (汇编文本, 字节码镜像, 符号表)。"""
    asm_text = compile_to_asm(src)
    image, symbols = asm_mod.assemble(asm_text)
    return asm_text, image, symbols
