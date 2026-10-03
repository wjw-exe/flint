#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
燧石语言 (Flint) 命令行工具。

用法:
    python3 flint.py asm <file.asm> [-o out.bin] [--list out.lst]
    python3 flint.py run <file.asm|file.fl> [--trace] [--stats] [--dump-mem N] [--max-steps N] [--input FILE]
    python3 flint.py disasm <file.bin> [--start N] [--count N]
    python3 flint.py bf <file.bf> [--trace] [--stats]     # 经内置解释器运行 Brainfuck 程序
    python3 flint.py info
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flint_lang import isa, compiler, __version__
from flint_lang.assembler import AsmError
from flint_lang.vm import VM, VMError


def main(argv=None):
    ap = argparse.ArgumentParser(prog="flint", description="燧石语言 (Flint) 工具链")
    sub = ap.add_subparsers(dest="cmd")

    p_asm = sub.add_parser("asm", help="汇编 .asm → .bin")
    p_asm.add_argument("file")
    p_asm.add_argument("-o", "--output")
    p_asm.add_argument("--list", help="输出带地址的汇编清单")

    p_run = sub.add_parser("run", help="运行 .asm 或 .fl 源文件")
    p_run.add_argument("file")
    p_run.add_argument("--trace", action="store_true", help="逐指令跟踪")
    p_run.add_argument("--stats", action="store_true", help="打印执行统计")
    p_run.add_argument("--dump-mem", type=int, default=0, metavar="N", help="结束后打印前 N 个字")
    p_run.add_argument("--max-steps", type=int, default=None)
    p_run.add_argument("--input", default=None, help="提供 stdin 输入文件(字节)")

    p_dis = sub.add_parser("disasm", help="反汇编 .bin")
    p_dis.add_argument("file")
    p_dis.add_argument("--start", type=int, default=0)
    p_dis.add_argument("--count", type=int, default=64)

    p_bf = sub.add_parser("bf", help="运行 Brainfuck 程序(图灵完备性演示)")
    p_bf.add_argument("file")
    p_bf.add_argument("--trace", action="store_true")
    p_bf.add_argument("--stats", action="store_true")

    sub.add_parser("info", help="显示架构信息")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 1
    try:
        if args.cmd == "info":
            return cmd_info()
        if args.cmd == "asm":
            return cmd_asm(args)
        if args.cmd == "run":
            return cmd_run(args)
        if args.cmd == "disasm":
            return cmd_disasm(args)
        if args.cmd == "bf":
            return cmd_bf(args)
    except (AsmError, VMError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    return 0


def _read_input(args):
    if args.input:
        with open(args.input, "rb") as f:
            return f.read()
    return None


def cmd_info():
    print(f"燧石语言 Flint v{__version__}")
    print(f"内存: {isa.MEM_SIZE} 字节, 寄存器: R0-R15 (R15=SP), PC/FLAGS 特殊")
    print(f"指令: {len(isa.OPCODES)} 条")
    print(" ".join(sorted(set(isa.OPCODES) | {"MOV"})))
    return 0


def cmd_asm(args):
    with open(args.file, "r", encoding="utf-8") as f:
        src = f.read()
    image, symbols = compiler.compile_asm(src)
    out = args.output or os.path.splitext(args.file)[0] + ".bin"
    with open(out, "wb") as f:
        f.write(image)
    print(f"汇编完成: {len(image)} 字节 → {out}")
    if args.list:
        with open(args.list, "w", encoding="utf-8") as f:
            addr = 0
            while addr < len(image):
                sz = isa.instr_size(int.from_bytes(image[addr:addr + 4], "little"))
                f.write(f"{addr:06x}  {isa.disasm(image, addr)}\n")
                addr += sz
        print(f"清单: {args.list}")
    return 0


def cmd_run(args):
    ext = os.path.splitext(args.file)[1].lower()
    with open(args.file, "r", encoding="utf-8") as f:
        src = f.read()
    if ext == ".fl":
        asm_text, image, symbols = compiler.compile_flint(src)
    elif ext in (".asm", ".s"):
        image, symbols = compiler.compile_asm(src)
    else:
        print(f"不支持的文件类型: {ext}", file=sys.stderr)
        return 1
    vm = VM(image, input_data=_read_input(args))
    try:
        out = vm.run(max_steps=args.max_steps, trace=args.trace, stats=args.stats)
    except VMError as e:
        print(f"运行时错误: {e}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    if args.dump_mem:
        print(f"\n--- 内存前 {args.dump_mem} 个字 ---")
        vm.dump_memory(0, args.dump_mem)
    if args.stats:
        print(f"退出码: {vm.exit_code}")
    return vm.exit_code


def cmd_disasm(args):
    with open(args.file, "rb") as f:
        data = f.read()
    addr = args.start
    n = 0
    while addr < len(data) and n < args.count:
        sz = isa.instr_size(int.from_bytes(data[addr:addr + 4], "little"))
        print(f"{addr:06x}  {isa.disasm(data, addr)}")
        addr += sz
        n += 1
    return 0


_BF_TEMPLATE = '''\
// 由 flint bf 自动生成的 Brainfuck 解释器包装
char prog[] = "__BF_SOURCE__";

int main() {
    int ip = 0;
    int ptr = 0;
    while (prog[ip] != 0) {
        if (prog[ip] == '>') {
            ptr = ptr + 1;
        } else if (prog[ip] == '<') {
            ptr = ptr - 1;
        } else if (prog[ip] == '+') {
            tape[ptr] = tape[ptr] + 1;
        } else if (prog[ip] == '-') {
            tape[ptr] = tape[ptr] - 1;
        } else if (prog[ip] == '.') {
            printc(tape[ptr]);
        } else if (prog[ip] == ',') {
            tape[ptr] = input();
        } else if (prog[ip] == '[') {
            if (tape[ptr] == 0) {
                int depth1 = 1;
                while (depth1 > 0) {
                    ip = ip + 1;
                    if (prog[ip] == '[') { depth1 = depth1 + 1; }
                    if (prog[ip] == ']') { depth1 = depth1 - 1; }
                }
            }
        } else if (prog[ip] == ']') {
            if (tape[ptr] != 0) {
                int depth2 = 1;
                while (depth2 > 0) {
                    ip = ip - 1;
                    if (prog[ip] == ']') { depth2 = depth2 + 1; }
                    if (prog[ip] == '[') { depth2 = depth2 - 1; }
                }
            }
        }
        ip = ip + 1;
    }
    return 0;
}
'''

_BF_INTERPRETER_HEADER = """\
// Brainfuck 解释器: 证明燧石语言图灵完备(任何 BF 程序皆可被模拟)
char tape[30000];
"""


def cmd_bf(args):
    with open(args.file, "r", encoding="utf-8") as f:
        bf_src = f.read()
    # 只保留 BF 指令字符
    clean = "".join(c for c in bf_src if c in "><+-.,[]")
    if not clean:
        print("Brainfuck 源为空", file=sys.stderr)
        return 1
    fl_src = _BF_INTERPRETER_HEADER + _BF_TEMPLATE.replace("__BF_SOURCE__", clean)
    try:
        asm_text, image, symbols = compiler.compile_flint(fl_src)
    except Exception as e:
        print(f"编译错误: {e}", file=sys.stderr)
        return 1
    vm = VM(image)
    try:
        out = vm.run(trace=args.trace, stats=args.stats)
    except VMError as e:
        print(f"运行时错误: {e}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    if args.stats:
        print(f"\n[bf] 程序长度 {len(clean)}, 解释器执行 {vm.steps} 条指令")
    return vm.exit_code


if __name__ == "__main__":
    sys.exit(main())
