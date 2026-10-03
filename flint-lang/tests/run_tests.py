#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_tests.py — 燧石语言端到端测试套件。

覆盖:
1. 汇编器: hello.asm / fib.asm (标签、数据段、分支、I/O)
2. 高级语言编译器: hello / fib(递归) / sieve(数组) / arith(运算) / strings(字符串) / echo(输入)
3. Brainfuck 解释器(图灵完备性演示)
4. 虚拟机: 标志位、32 位回绕、除零错误、越界检查
5. 反汇编一致性

运行: python3 tests/run_tests.py
"""

import os
import sys
import io
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flint_lang.compiler import compile_flint, compile_asm
from flint_lang.vm import VM, VMError
from flint_lang import isa, assembler

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def run_fl(src, inp=b""):
    asm, img, sym = compile_flint(src)
    vm = VM(img, input_data=inp)
    out = vm.run()
    return bytes(out), vm


def run_asm(src, inp=b""):
    img, sym = compile_asm(src)
    vm = VM(img, input_data=inp)
    out = vm.run()
    return bytes(out), vm


def load(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


def main():
    print("== 1. 汇编程序 ==")
    out, vm = run_asm(load("examples/hello.asm"))
    check("hello.asm 输出", out == b"Hello, World!\n", repr(out))
    out, vm = run_asm(load("examples/fib.asm"))
    check("fib.asm 输出", out == b"Fib(20) = 6765\n", repr(out))

    print("== 2. 高级语言 ==")
    out, vm = run_fl(load("examples/hello.fl"))
    check("hello.fl 输出(含中文)", out == "Hello, World!\n你好，燧石语言！\n7\n1998\n".encode(), repr(out))
    out, vm = run_fl(load("examples/fib.fl"))
    check("fib.fl 递归输出", out == b"fib(15) = 610\n", repr(out))
    primes = "2 3 5 7 11 13 17 19 23 29 31 37 41 43 47 53 59 61 67 71 73 79 83 89 97 101 103 107 109 113 127 131 137 139 149 151 157 163 167 173 179 181 191 193 197 199 \n"
    out, vm = run_fl(load("examples/sieve.fl"))
    check("sieve.fl 素数筛", out == primes.encode(), repr(out))
    out, vm = run_fl(load("examples/arith.fl"))
    expected = "\n".join(["3", "1", "-3", "-1", "2", "11", "9", "-1", "16", "16",
                          "1", "0", "1", "0", "21", "100"]) + "\n"
    check("arith.fl 运算综合", out == expected.encode(), repr(out))
    out, vm = run_fl(load("examples/strings.fl"))
    check("strings.fl 字符串", out == b"Hello, Flint!\n13\n", repr(out))
    out, vm = run_fl(load("examples/echo.fl"), inp="Hello 燧石!".encode())
    check("echo.fl 输入回显", out == "Hello 燧石!".encode(), repr(out))

    print("== 3. Brainfuck(图灵完备性) ==")
    out, vm = run_fl(load("examples/brainfuck.fl"))
    check("brainfuck.fl 解释器", out == b"Hello World!\n", repr(out))

    print("== 4. 虚拟机语义 ==")
    out, vm = run_asm("""
        MOV r0, 0x7fffffff
        MOV r1, 1
        ADD r0, r0, r1       ; 溢出回绕 → 0x80000000 (负数)
        OUTI r0              ; 打印 -2147483648
        HLT
    """)
    check("32 位回绕", out == b"-2147483648", repr(out))

    out, vm = run_asm("""
        MOV r0, -7
        MOV r1, 2
        DIV r0, r0, r1
        OUTI r0
        MOV r0, 10
        OUT r0
        MOV r0, -7
        MOV r1, 2
        MOD r0, r0, r1
        OUTI r0
        HLT
    """)
    check("除/模向零截断", out == b"-3\n-1", repr(out))

    try:
        run_asm("MOV r0, 1\nMOV r1, 0\nDIV r2, r0, r1\nHLT")
        check("除零报错", False)
    except VMError:
        check("除零报错", True)

    try:
        run_asm("MOV r0, 0xffff\nLD r1, [r0 + 100]\nHLT")
        check("越界报错", False)
    except VMError:
        check("越界报错", True)

    out, vm = run_asm("""
        MOV r0, 5
        MOV r1, 3
        CMP r0, r1
        JG _gt
        OUTI r0
        JMP _end
_gt:
        OUTI r1
_end:
        HLT
    """)
    check("条件跳转 JG", out == b"3", repr(out))

    print("== 5. 汇编器/反汇编一致性 ==")
    asm_txt = load("examples/hello.asm")
    img, sym = compile_asm(asm_txt)
    addr = 0
    re_asm = []
    while addr < len(img):
        sz = isa.instr_size(int.from_bytes(img[addr:addr + 4], "little"))
        re_asm.append(isa.disasm(img, addr))
        addr += sz
    check("反汇编非空", len(re_asm) > 0)
    check("镜像大小合理", 0 < len(img) < 0x10000, len(img))

    out, vm = run_asm("""
        ; 测试 LDA + 数据段标签 + 字符串
        MOV r0, msg
        CALL puts
        HLT
puts:
        LDB r1, [r0]
        CMPI r1, 0
        JE _done
        OUT r1
        MOV r1, 1
        ADD r0, r0, r1
        JMP puts
_done:
        RET
msg:
        DB "OK", 0
    """)
    check("LDA/标签/数据段", out == b"OK", repr(out))

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
