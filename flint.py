#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flint v3 — Python 风格缩进、静态类型、编译型语言。

用法:
  python3 flint.py run  examples/fib.fl  [--input 文件] [--trace] [--stats]
  python3 flint.py run --native examples/fib.fl   # 编译为原生可执行文件并运行(不走 VM)
  python3 flint.py asm  examples/fib.fl   # 只输出 VM 汇编文本
  python3 flint.py native examples/fib.fl [-o out]  # x86-64 汇编后端 → 可执行文件

底层复用 flint-lang 的汇编器与虚拟机(需与本目录同级存放);
--native / native 走 asm.py: 直接生成 x86-64 AT&T 汇编(gcc 仅做汇编与链接, 不再经过 C)。
v3.0 新增: 条件表达式 a if c else b / 默认参数 / 运行时字符串拼接与比较 / sqrt、gcd、clamp。
"""

import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
# 兼容两种布局: 仓库/Windows 包内 flint-lang 与本目录同级; Linux 开发布局 flint-lang 与上级目录同级
for _cand in (
    os.path.join(_HERE, "flint-lang"),
    os.path.join(os.path.dirname(_HERE), "flint-lang"),
):
    if os.path.isdir(_cand):
        sys.path.insert(0, _cand)
        break

try:
    from flint_lang.assembler import assemble, AsmError
    from flint_lang.vm import VM, VMError
except ImportError:
    print("缺少依赖: 请把 flint-v2 与 flint-lang 放在同一级目录下(共用其汇编器与虚拟机)。",
          file=sys.stderr)
    sys.exit(1)

from lexer import tokenize, LexError
from parser import Parser, ParseError
from typecheck import TypeChecker, TypeCheckError
from codegen import CodeGen

__version__ = "3.0.0"


def parse_program(src: str):
    """源码 → 带类型 AST(词法 → 语法 → 类型检查)。"""
    tokens = tokenize(src)
    prog = Parser(tokens).parse_program()
    TypeChecker().check(prog)
    return prog


def compile_source(src: str) -> str:
    """源码 → 汇编文本: 词法 → 语法 → 类型检查 → 代码生成。"""
    return CodeGen().generate(parse_program(src))


def _compile_or_die(src):
    try:
        return compile_source(src)
    except (LexError, ParseError, TypeCheckError) as e:
        print(f"编译错误: {e}", file=sys.stderr)
        sys.exit(2)


def _parse_or_die(src):
    try:
        return parse_program(src)
    except (LexError, ParseError, TypeCheckError) as e:
        print(f"编译错误: {e}", file=sys.stderr)
        sys.exit(2)


def cmd_run(args):
    src = open(args.file, encoding="utf-8").read()
    if args.native:
        from asm import build as asm_build, AsmError
        exe = tempfile.mktemp(prefix="flint_", suffix=".exe")
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".fl", delete=False,
                                             encoding="utf-8") as f:
                f.write(src)
                flt = f.name
            try:
                asm_build(flt, exe)
            except AsmError as e:
                print(f"编译错误: {e}", file=sys.stderr)
                return 2
            finally:
                os.unlink(flt)
            inp = None
            if args.input:
                inp = open(args.input, "rb").read()
            r = subprocess.run([exe], input=inp, capture_output=True)
            sys.stdout.buffer.write(r.stdout)
            sys.stdout.buffer.flush()
            if r.stderr:
                sys.stderr.buffer.write(r.stderr)
                sys.stderr.buffer.flush()
            return r.returncode
        finally:
            if os.path.exists(exe):
                os.unlink(exe)
    asm_text = _compile_or_die(src)
    try:
        image, _symbols = assemble(asm_text)
    except AsmError as e:
        print(f"汇编错误: {e}", file=sys.stderr)
        return 2
    inp = None
    if args.input:
        inp = open(args.input, "rb").read()
    vm = VM(image, input_data=inp)
    try:
        out = vm.run(trace=args.trace, stats=args.stats)
    except VMError as e:
        print(f"运行时错误: {e}", file=sys.stderr)
        return 3
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return vm.exit_code


def cmd_asm(args):
    src = open(args.file, encoding="utf-8").read()
    print(_compile_or_die(src))
    return 0


def cmd_gfx(args):
    """gfx <文件.fl> [--frames N]: 以 2D 游戏引擎模式运行(Flint VM 线程 + Qt 主线程)。"""
    src = open(args.file, encoding="utf-8").read()
    asm_text = _compile_or_die(src)
    try:
        image, _symbols = assemble(asm_text)
    except AsmError as e:
        print(f"汇编错误: {e}", file=sys.stderr)
        return 2
    import threading
    from flint_engine import Engine
    eng = Engine()
    vm = VM(image, gfx=eng)
    vm_done = threading.Event()
    t = threading.Thread(target=lambda: (vm.run(trace=args.trace, stats=args.stats), vm_done.set()),
                         daemon=True)
    t.start()
    try:
        eng.run_app(max_frames=args.frames, vm_done=vm_done)
    except Exception as e:
        print(f"图形引擎错误: {e}", file=sys.stderr)
        return 3
    t.join(timeout=5)
    sys.stdout.buffer.write(vm.output)
    sys.stdout.buffer.flush()
    return vm.exit_code


def cmd_native(args):
    from asm import build as asm_build, AsmError
    out = args.output or os.path.splitext(args.file)[0]
    try:
        asm_build(args.file, out)
    except AsmError as e:
        print(f"编译错误: {e}", file=sys.stderr)
        return 2
    print(f"✓ 已生成原生可执行文件(x86-64 汇编后端): {out}")
    print(f"  运行: ./{out}")
    return 0


def _parse_args(argv):
    class A:
        pass

    a = A()
    a.file = None
    a.input = None
    a.trace = False
    a.stats = False
    a.native = False
    a.output = None
    a.frames = None
    i = 2
    while i < len(argv):
        if argv[i] == "--input":
            a.input = argv[i + 1]
            i += 2
        elif argv[i] == "--trace":
            a.trace = True
            i += 1
        elif argv[i] == "--stats":
            a.stats = True
            i += 1
        elif argv[i] == "--native":
            a.native = True
            i += 1
        elif argv[i] == "-o":
            a.output = argv[i + 1]
            i += 2
        elif argv[i] == "--frames":
            a.frames = int(argv[i + 1])
            i += 2
        elif argv[i].startswith("-"):
            i += 1
        else:
            if a.file is None:
                a.file = argv[i]
            i += 1
    return a


def main(argv=None):
    argv = list(argv) if argv is not None else sys.argv
    if len(argv) < 3:
        print("用法: python3 flint.py <run|asm|native> <文件.fl> [--native] [--input 文件] [-o out] [--trace] [--stats]\n  native/--native = x86-64 汇编后端(不再经 C)")
        return 1
    cmd = argv[1]
    if cmd not in ("run", "asm", "native", "gfx"):
        print(f"未知命令: {cmd}", file=sys.stderr)
        return 1
    args = _parse_args(argv)
    if cmd == "run":
        return cmd_run(args)
    if cmd == "asm":
        return cmd_asm(args)
    if cmd == "gfx":
        return cmd_gfx(args)
    return cmd_native(args)


if __name__ == "__main__":
    sys.exit(main())
