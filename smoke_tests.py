# -*- coding: utf-8 -*-
"""Flint 仓库开箱自检: 用 VM 模式(零依赖)跑全部示例, 断言关键输出与退出码。

用法:
    python3 smoke_tests.py     (Linux/macOS)
    py smoke_tests.py          (Windows)

任何一个示例失败都返回非零退出码。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FLINT = os.path.join(HERE, "flint.py")
EX = os.path.join(HERE, "examples")

# (示例文件, 必须出现在输出中的关键片段; 空列表 = 只要求退出码 0)
CASES = [
    ("fib.fl", ["610"]),
    ("hello.fl", ["Hello, World!"]),
    ("bubble_sort.fl", ["5", "11", "12", "22", "25", "34", "64", "90"]),
    ("lists.fl", ["184"]),
    ("loops.fl", ["5050"]),
    ("prime.fl", ["1", "0", "1"]),
    ("break_continue.fl", ["101", "136", "10"]),
    ("pythonic.fl", ["112"]),
    ("v21_ext.fl", ["1024", "while ok", "for ok"]),
    ("echo.fl", []),  # 读取 stdin(EOF 即正常结束)
    # v3.0: 条件表达式 / 默认参数 / 运行时字符串拼接与比较 / sqrt / gcd / clamp
    ("v30_features.fl", ["Hello, World!", "Hi, Doubao!", "abcFlint", "10", "42", "12", "100", "50"]),
    # v3.1: 字符字面量 'a' + str(i32) 数字转字符串
    ("v31_features.fl", ["65 10", "1", "70 1", "item[0]=0; item[1]=1; item[2]=4;",
                         "x=-42 y=123456789", "len=33", "98", "25", "done"]),
]


def run_one(name, expect):
    path = os.path.join(EX, name)
    try:
        r = subprocess.run(
            [sys.executable, FLINT, "run", path],
            input=b"", capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, "超时"
    out = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        return False, "exit=%d, stderr=%s" % (r.returncode, r.stderr[:160])
    for tok in expect:
        if tok not in out:
            return False, "缺少输出 %r (实际前 160 字符: %r)" % (tok, out[:160])
    return True, ""


def main():
    ok = 0
    total = len(CASES)
    for name, expect in CASES:
        good, msg = run_one(name, expect)
        print("PASS  " + name if good else "FAIL  " + name + "  -> " + msg)
        ok += 1 if good else 0
    print("---- %d/%d 通过 ----" % (ok, total))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
