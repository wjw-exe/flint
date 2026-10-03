# 燧石语言 (Flint)

**一门从零打造的、基于自研汇编的、图灵完备的编程语言。**

Flint 不是玩具脚本，而是一条完整的真实工具链：

```
.fl 高级语言源码
   │  词法分析 (lexer)
   ▼
抽象语法树 (parser)
   │  代码生成 (codegen)
   ▼
.flint-asm 汇编源码   ← 可读、可手写、可调试
   │  两趟扫描汇编 (assembler)
   ▼
二进制字节码 (.bin)
   │  虚拟机执行 (FlintVM)
   ▼
程序输出
```

一切由纯 Python 实现、零第三方依赖，运行环境只需 `python3`。

---

## 一、快速开始

```bash
cd flint-lang

# 运行一个 .fl 高级语言程序(自动完成 编译→汇编→执行)
python3 flint.py run examples/hello.fl

# 运行一个汇编程序
python3 flint.py run examples/hello.asm

# 只汇编, 得到字节码与反汇编清单
python3 flint.py asm examples/hello.asm -o out.bin --list out.lst
python3 flint.py disasm out.bin

# 运行 Brainfuck 程序 —— 图灵完备性演示
python3 flint.py bf examples/hello.bf

# 跑一遍全部测试
python3 tests/run_tests.py
```

CLI 子命令：`asm` / `run` / `disasm` / `bf` / `info`，`run` 支持 `--trace`(逐指令跟踪)、`--stats`、`--dump-mem N`、`--input FILE`。

---

## 二、指令集架构 (Flint-ASM)

### 机器模型

| 项目 | 规格 |
|---|---|
| 字长 | 32 位, 小端序 |
| 内存 | 65536 字节 (64 KB), 字节寻址 |
| 寄存器 | R0–R15 通用; **R15 即栈指针 SP**; PC、FLAGS 特殊 |
| 标志位 | Z(零) N(负) C(进位/无符号小于) O(溢出) |
| 指令编码 | R 型 / I 型 / B 型 / J 型, 均 4 字节(仅 MOVI/LDA 为 8 字节) |

### 指令表

| 助记符 | 语义 | 格式 |
|---|---|---|
| `MOV rd, rs` | rd = rs | R |
| `MOV rd, imm32` | rd = 立即数 | J |
| `ADD rd, rs1, rs2` | rd = rs1 + rs2 | R |
| `SUB rd, rs1, rs2` | rd = rs1 − rs2 | R |
| `MUL rd, rs1, rs2` | rd = rs1 × rs2 (低 32 位) | R |
| `DIV rd, rs1, rs2` | rd = rs1 ÷ rs2 (向零截断) | R |
| `MOD rd, rs1, rs2` | rd = rs1 % rs2 | R |
| `AND / OR / XOR rd, rs1, rs2` | 位运算 | R |
| `NOT rd, rs` | rd = ~rs | I |
| `SHL / SHR rd, rs1, rs2` | 移位(rs2 & 31) | R |
| `CMP rs1, rs2` | 比较, 仅更新标志 | R |
| `CMPI rs1, imm16` | 与立即数比较, 仅更新标志 | I |
| `JMP / JE / JNE / JG / JL / JGE / JLE label` | 条件/无条件跳转 | B |
| `CALL label` | 压入返回地址并跳转 | B |
| `RET` | 弹出返回地址 | – |
| `PUSH rs / POP rd` | 栈操作 | I |
| `LD rd, [rs+off] / ST [rs+off], rd` | 载入/存储 32 位字 | I |
| `LDB rd, [rs+off] / STB [rs+off], rd` | 载入/存储字节 | I |
| `LDA rd, label` | rd = 标签地址 | J |
| `HLT` | 停机, 退出码 = r0 & 0xff | – |
| `IN rd` | 读入一个字节 (EOF 时为 0) | I |
| `OUT rs / OUTI rs` | 输出字符 / 十进制整数 | I |
| `TRAP imm16` | 运行时陷阱(1=索引越界, 2=除零错误), 停机报错 | I |

### 汇编语法

```
; 注释
msg:  DB "Hello, World!", 10, 0   ; 数据段: 字符串/字节
arr:  DS 40                        ; 预留 40 字节
w:    DW 0x12345678                ; 32 位字

        MOV r0, msg                ; 立即数/标签
        CALL print_str
        HLT

print_str:
_loop:  LDB r1, [r0]
        CMPI r1, 0
        JE _done
        OUT r1
        MOV r1, 1
        ADD r0, r0, r1
        JMP _loop
_done:  RET
```

立即数支持十进制 / `0x` 十六进制 / `0b` 二进制 / `'字符'`；内存引用支持 `[r0]`、`[r0 + 4]`、`[r0 - 8]`。

### 调用约定 (cdecl 风格)

* 参数**从右向左**压栈，第 i 个参数位于 `FP + 8 + 4*i`；
* 序言：`PUSH r14; MOV r14, r15; SUB r15, r15, localsize`；
* 尾跋：`MOV r15, r14; POP r14; RET`；返回值在 `r0`；
* R14 = 帧指针 FP，R15 = 栈指针 SP（栈从内存顶部向下生长）。

---

## 三、高级语言 (Flint)

类 C 语法，编译器把每个程序编译成上面的汇编：

```c
// 全局变量与数组
char greeting[] = "你好，燧石语言！";
int primes[100];

// 递归函数 —— 真实栈帧, 支持任意深度
int fib(int n) {
    if (n < 2) return n;
    return fib(n - 1) + fib(n - 2);
}

int main() {
    int i = 0;                    // 局部变量, 支持声明初始化
    prints("Hello!\n");
    print(fib(15));               // 表达式、函数调用
    while (i < 10) {
        i = i + 1;
    }
    for (i = 0; i < 5; i = i + 1) {
        primes[i] = i * i;
    }
    if (i > 3 && i < 8 || i == 9) print(i);   // 短路逻辑
    return 0;
}
```

**类型**：`int`(32 位)、`char`(字节)、`void`；支持 `int a[10]` / `char s[100]` 数组（int 元素 4 字节、char 元素 1 字节），全局数组可静态初始化、字符串字面量全局共享。

**表达式**：`+ - * / %`、`<< >>`、`< <= > >= == !=`、`& | ^ ~ !`、`&& ||`(短路)、三目无、赋值与 `+= -= *= /= %=`。

**内置函数**：

| 函数 | 作用 |
|---|---|
| `print(x)` | 打印十进制整数 |
| `printc(c)` | 打印一个字节 |
| `prints(s)` | 打印以 `\0` 结尾的字符串 |
| `input()` | 读入一个字节(EOF 为 0) |
| `strcpy(dst, src)` | 复制字符串 |
| `strlen(s)` | 字符串长度 |
| `exit(code)` | 立即停机 |

**已知限制**：无指针、无结构体、无数组参数、无局部数组初始化（文档即规范）。

---

## 四、图灵完备性证明

> **定理：Flint（及 Flint-ASM）是图灵完备的。**

**证明思路（约简）**：

1. **Brainfuck 是图灵完备的**。Brainfuck 只有 8 条指令，操作一条无限长的字节纸带，已被广泛证明等价于图灵机（例如：可以用 Brainfuck 模拟任意 Turing Machine，反过来也可）。

2. **Flint 能模拟任意 Brainfuck 程序**。`examples/brainfuck.fl` 是一个固定长度（约 30000 字节纸带 + 指令指针 + 数据指针）的 Brainfuck 解释器，用 Flint 写成：
   - `char tape[30000]` —— 纸带（有限资源只是工程现实，理论容量可扩展）；
   - 每个 BF 指令（`><+-.,[]`）映射为一段 `if/while` 分支；
   - `[`/`]` 的括号匹配用深度计数循环实现。

   由于该解释器是**有限的固定程序**，把任意 BF 程序（其源码作为字符串内嵌）作为输入，就能模拟它的全部执行——即：Flint 可以模拟任意图灵机。

3. **Flint 程序总能编译到 Flint-ASM**。编译器是全函数：任意合法 Flint 程序都被翻译为汇编并执行于同一虚拟机。因此汇编层继承了同样的表达能力。

**结论**：存在一条「任意图灵机 → Brainfuck 程序 → 燧石解释器 → 燧石汇编」的完整约简链，故 **Flint / Flint-ASM 是图灵完备的**。

（与所有真实语言一样，虚拟机使用 64 KB 有限内存；图灵完备性在“内存可扩展”的理论语义下成立——这与 C、Java 等语言的情形完全一致。）

---

## 五、项目结构

```
flint-lang/
├── flint.py                 CLI 入口
├── flint_lang/
│   ├── isa.py               指令集定义与编码/反汇编
│   ├── assembler.py         汇编器(两趟扫描)
│   ├── vm.py                虚拟机(执行、标志位、I/O、跟踪)
│   ├── lexer.py             高级语言词法分析
│   ├── parser.py            高级语言语法分析(递归下降)
│   ├── codegen.py           高级语言 → 汇编 代码生成
│   └── compiler.py          编译总管线
├── examples/
│   ├── hello.asm / fib.asm  手写汇编示例
│   ├── hello.fl / fib.fl / sieve.fl / arith.fl
│   ├── strings.fl / echo.fl 高级语言示例
│   ├── brainfuck.fl         Brainfuck 解释器(图灵完备性)
│   └── hello.bf             Brainfuck 程序
├── tests/run_tests.py       17 项端到端测试
└── docs/architecture.html   工具链架构可视化
```

## 六、路线图 (Roadmap)

* [x] 自研指令集 + 汇编器 + 虚拟机
* [x] 类 C 高级语言编译器（递归、数组、短路逻辑、字符串）
* [x] Brainfuck 解释器 → 图灵完备性
* [ ] 指针与引用
* [ ] 结构体 / 联合体
* [ ] JIT 编译到 x86-64
* [ ] 自带标准库（字符串处理、集合、IO 封装）
* [ ] 汇编级调试器（断点、单步、内存视图）

---

MIT License — 学习与实验用途。请把它当作理解“语言如何从零诞生”的教学工程。
