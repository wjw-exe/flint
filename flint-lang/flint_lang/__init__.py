# -*- coding: utf-8 -*-
"""
燧石语言 (Flint) —— 一门基于自研汇编、图灵完备的编程语言。

包含:
    isa         指令集架构与编码
    assembler   汇编器 (.asm → .bin)
    vm          虚拟机 (执行 .bin)
    lexer/parser/codegen/compiler  高级语言 (.fl) 编译到汇编再汇编为字节码
"""

from . import isa, assembler, vm, lexer, parser, codegen, compiler

__version__ = "1.0.0"
