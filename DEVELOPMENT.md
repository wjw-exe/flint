# Flint v2.2 开发文档

> 燧石语言（Flint）——Python 风格语法、静态类型、编译型。VM 后端自研指令集 + 虚拟机；
> 原生后端直接生成 x86-64 汇编（不经 C、不经任何中间语言）。
>
> 本文件面向：想了解 Flint 内部实现的开发者、想给语言加新功能/新命令的贡献者。

---

## 1. 项目概览

| 项 | 值 |
|---|---|
| 版本 | v2.2.0 |
| 定位 | Python 风格语法 · 静态类型 · 编译型 · 图灵完备 |
| 双后端 | VM 后端（自研 32 位 ISA + 虚拟机，零依赖） / 原生后端（AST → x86-64 AT&T 汇编 → gcc 仅汇编链接） |
| 性能 | 原生后端比 CPython 快约 9~11×，数组遍历与 C 持平（见 §14） |
| 测试 | 开箱自检 10 项 + 底层 17 项全绿；开发侧 76 项全绿（38+12+9+17） |
| 平台 | Linux / macOS / Windows（Windows 有 PyInstaller 一键环境包 + VSCode 插件） |

核心设计一句话：**"Python 只负责编译"** —— 运行时不经过 Python、没有解释器循环、没有类型探测。

---

## 2. 总体架构

```
.f 源码 (Python 风格)
  │  ① lexer.py      缩进感知词法 (INDENT/DEDENT, 空格缩进, 禁 Tab)
  ▼
token 流
  │  ② parser.py     递归下降 → AST (数字/字符串常量折叠在此完成)
  ▼
AST
  │  ③ typecheck.py  静态类型检查 (编译期报错, 运行时零类型开销)
  ▼
带类型 AST
  ├─ ④ codegen.py    AST → Flint-ASM 汇编文本 (VM 后端, 含编译期优化)
  │     └─ flint_lang/assembler.py  两趟扫描汇编 → 字节码镜像
  │           └─ flint_lang/vm.py   虚拟机执行 (TRAP 陷阱)
  │                 └─ 程序输出 + 退出码
  │
  └─ ④' asm.py       AST → x86-64 AT&T 汇编 (原生后端, 直译)
        └─ gcc -no-pie -O2  仅做"汇编 + 链接"
              └─ ELF 可执行文件 (运行时与 Python 无关)
```

两条后端共享**同一前端**（词法→语法→类型检查→AST），因此语义严格一致。

---

## 3. 目录结构

```
flint-windows/                (即 GitHub wjw-exe/flint 仓库)
├── flint.py                  CLI: run / asm / native (+ --native / --input / -o / --trace / --stats)
├── flint_ide.py              PyQt6 图形 IDE (语法高亮/一键运行/四视图/可打断死循环)
├── lexer.py                  缩进感知词法分析器
├── parser.py                 递归下降解析器 → AST (常量折叠)
├── typecheck.py              静态类型检查器
├── codegen.py                AST → Flint-ASM (VM 后端代码生成)
├── asm.py                    x86-64 汇编后端 (AST → AT&T 汇编 → gcc 链接)
├── smoke_tests.py            仓库开箱自检 (10 个示例断言)
├── flint-lang/               底层库 (32 位 ISA / 汇编器 / 虚拟机, 独立 17 项测试)
│   └── flint_lang/
│       ├── isa.py            指令集架构定义与编码/反汇编
│       ├── assembler.py      两趟扫描汇编器
│       └── vm.py             虚拟机
├── examples/                 hello / fib / loops / prime / echo / lists /
│                             break_continue / bubble_sort / pythonic / v21_ext
├── build.bat                 Windows 一键构建 flint.exe + flint-ide.exe (免 Python)
├── run.bat                   启动图形 IDE
├── README.md                 用户首页 (快速开始/性能)
└── DEVELOPMENT.md            本文档
```

---

## 4. 前端：词法分析 (lexer.py)

- 与 Python 一致的缩进块：生成 `INDENT/DEDENT` token，**不支持 Tab 缩进**（直接报错，避免歧义）。
- Token 种类：`NUM / STR / ID / TYPE / KW / OP / NEWLINE / INDENT / DEDENT / EOF`，三元组 `(kind, value, line)`。
- 运算符覆盖：`//= // -> == != <= >= <<= >>= &= |= ^= += -= *= /= %= << >>` 及全部标点 `+ - * / % < > = & | ^ ~ ( ) , : [ ] ;`。
- 字符串支持转义 `\n \t \r \0 \\ \" \' \a \b \f \v` 与 `\xNN` 十六进制转义。
- 关键字表（v2.2）：`def return if elif else while for in range print input True False and or not list break continue pass len abs min max sum pow`；类型关键字：`i32 bool str`。

## 5. 语法与 AST (parser.py)

递归下降解析，语法与 Python 高度一致：

- `def f(a: i32, b: i32) -> i32:` 函数定义（参数/返回值显式类型）
- `x: i32 = 0` 显式声明；`x = 0` 类型推断（**首次赋值即声明**）
- `if / elif / else`、`while`、`for x in range(...)`、`for x in xs`（列表遍历）、`while-else` / `for-else`
- `break / continue / pass / return`、`print(a, b, c)` 多参数
- `xs: list[i32] = [1,2,3]` 静态列表；`list[i32; N]` 定长零初始化；`xs[i]` 负索引
- 复合赋值 `+= -= *= /= %= //= &= |= ^= <<= >>=`（索引复合赋值单次寻址）
- 位运算 `& | ^ << >> ~`、`in / not in` 成员测试、链式比较 `a < b < c`
- 内置 `min / max / sum / pow`
- 常量折叠：数字与字符串字面量在解析期直接计算（如 `2+3*4` → `14`，`"a"+"b"` → `"ab"`）

AST 节点（`parser.Node` 子类）：`Program FuncDef Decl Assign IndexAssign AugAssign Return If While For Break Continue Pass ExprStmt BinOp Unary Call Var Index IntLit BoolLit StrLit ListLit`。
错误统一为 `ParseError(line, msg)`：`第 N 行: ...`。

## 6. 静态类型系统 (typecheck.py)

- 类型：`i32`（32 位有符号整数，核心类型）、`bool`、`str`、`list[i32]` / `list[i32; N]`。
- 全部类型校验在**编译期**完成，类型错误直接编译失败（带行号），运行时零类型开销。
- 规则：条件必须是 `bool`；算术只接受 `i32`；函数参数/返回值严格匹配；变量先声明后使用；不可重复声明（循环变量除外，可重绑定）。
- 类型推断：首次赋值即声明（`x = 1` → `i32`）。
- 运算符分类（供类型检查与代码生成共用）：算术 `+ - * / %`、地板除 `//`、位运算 `& | ^ << >>`、比较 `== != < > <= >= in`、逻辑 `and or`（短路）、复合赋值 11 种。

## 7. VM 后端

### 7.1 Flint-ASM 指令集 (flint-lang/isa.py)

- 字长 32 位、小端；内存 65536 字节（64 KB）、字节寻址。
- 寄存器：`R0~R15`（R15 约定为栈指针 SP），PC、FLAGS 特殊寄存器；标志位 Z/N/C/O。
- 指令编码 4 字节（MOVI/LDA 为 8 字节双字）：R 型（三寄存器）、I 型（寄存器+imm16）、B 型（相对跳转 off24）、J 型（imm32）。

| 指令 | 语义 | 指令 | 语义 |
|---|---|---|---|
| MOVR / MOVI | 寄存器 / 立即数传送 | JMP/JE/JNE/JG/JL/JGE/JLE | 相对跳转（条件跳转） |
| ADD SUB MUL DIV MOD | 算术（DIV 向零截断） | CALL / RET | 函数调用 |
| AND OR XOR NOT | 位运算 | PUSH / POP | 栈操作 |
| SHL SHR | 移位（量 &31，逻辑右移） | LD ST LDB STB | 32 位字 / 字节访存 |
| CMP / CMPI | 比较（仅置标志） | LDA | 取标签地址（双字） |
| IN / OUT | 字节 I/O（EOF=0） | OUTI | 输出十进制有符号整数 |
| HLT | 停机 | TRAP | 运行时陷阱（imm16 编号） |

### 7.2 调用约定（与 flint-lang 一致）

- 参数**从右向左**压栈，第 i 个参数位于 `FP+8+4*i`（FP=R14，SP=R15）。
- 序言：`PUSH r14; MOV r14, r15; SUB r15, r15, 帧大小`；尾跋：`MOV r15, r14; POP r14; RET`。
- 返回值在 `r0`；局部变量位于 FP 下方：第 k 个局部变量在 `FP-4-4*k`。

### 7.3 表达式求值（codegen.py）

简易栈式生成：表达式结果总在 `r0`；二元运算把左操作数压栈、计算右操作数后弹回 `r1`（`r1`=左，`r0`=右）。嵌套表达式天然安全，无寄存器分配。

### 7.4 运行时语义

- `i32` 32 位回绕（算术/移位/幂/列表元素）。
- `/` 与 `%` 向零截断；`//` 地板除（异号且余数非零时商 −1）。
- 移位量按 `&31` 截断；逻辑右移。
- 运行时陷阱：索引越界 → `TRAP 1`（exit 1）；除零/零步长 → `TRAP 2`（exit 2）。
- 列表内存布局：`len` 在块底（4 字节），元素向上连续 4 字节/个；负索引在运行时修正。

### 7.5 虚拟机 (flint-lang/vm.py)

`VM(image, input_data)`：字节码镜像装入 64KB 内存，R15 初始化为内存顶（栈向下生长），逐指令解码执行；支持 `trace`（逐步跟踪）与 `stats`（执行步数统计）；`input_data` 提供 stdin 字节流（EOF 返回 0）。

---

## 8. 原生后端：x86-64 汇编 (asm.py)

### 8.1 流水线

AST → 静态类型检查 → **AT&T 汇编文本** → `gcc -no-pie -O2`（仅汇编+链接）→ ELF 可执行文件。**不经过 C 或任何中间语言**。

### 8.2 寄存器映射与栈帧

- 寄存器映射：`r0=rax r1=r11 r2=rcx r3=rdx r4=rsi r5=rdi r6=r8 r7=r9(scratch) r8=r10`。
- 栈帧（System V 风格）：参数从右向左 `pushq`，第 i 个参数在 `rbp+16+8i`；局部变量槽 8 字节（地址槽存 64 位指针，i32 用低 32 位）；帧大小 = 槽数×8，向上 16 对齐。
- Flint 调用约定与 VM 一致：call 破坏所有 caller-saved，活跃值由 PUSH/POP 保护。

### 8.3 语义一致性（与 VM 逐项对齐）

- i32 32 位回绕；地板除 `//`；`/` 与 `%` 向零截断。
- 除法/取模用 **64 位 idiv** 避免 `INT_MIN / -1` 溢出，商截断回 i32（= VM 回绕语义）。
- 除零 → TRAP 2（exit 2）；索引越界 → TRAP 1（exit 1），stderr 中文提示（`flt_trap` 运行时助手）。
- 列表布局：len 在块底，元素向上；变量槽存 64 位块底地址。
- 字符串按 UTF-8 字节存储、0 结尾；`s[i]` 按字节扫描。
- 文本 I/O 走 libc：`putchar` / `printf`（i32 十进制）。

### 8.4 已知平台限制（重要）

- **需要 gcc**：Windows 默认没有 gcc。缺失时 `--native` 会给出清晰中文报错（v2.2 修复，不再裸崩溃），VM 模式不受影响。Windows 原生模式需装 MinGW-w64 并设 `CC` 环境变量。
- **`flt_trap` 是 Linux syscall 实现**（`write` + `exit`）：Linux/macOS 下正常；Windows/MinGW 下若触发 trap，行为取决于环境（主路径 I/O 走 libc，不受影响）。如需完整 Windows 原生支持，可后续把 trap 改为 libc `write/exit` 或平台分支。

---

## 9. 编译期优化清单（v2.2，不损失性能）

1. **常量折叠**（parser）：`2+3*4` 与 `"a"+"b"` 编译期算出字面量。
2. **range 字面量步长直判**：步长/边界是字面量时，编译期定死循环方向与增量，循环体内**没有**每圈的符号判断；字面量边界用 `CMPI` 直接比较，免一次内存读。
3. **复合赋值单次寻址**：`xs[i] += v` 只计算一次元素地址（普通写法需 LD+ST 两次寻址）。
4. **循环内无越界检查**：`for x in xs` 由循环条件保证索引安全，循环体内零越界检查。
5. **动态步长零检查**：`for i in range(0, n, step)` 只在循环入口做一次"步长为 0 → TRAP 2"，每圈仅一次方向分支。
6. **类型信息全部编译期确定**：索引、取模、除法等运算无任何运行时类型探测。
7. **VM 热循环优化**：取消每指令一次的全内存拷贝反汇编（trace 模式才生成），实测 VM ~3 倍提速。

## 10. 图灵完备性

v2 编译到与 v1 完全相同的 Flint-ASM/虚拟机，而 Flint-ASM 已通过 **Brainfuck 解释器**证明图灵完备（见 `flint-lang/README.md` 四段式证明）。因此 Flint v2 同样图灵完备。

---

## 11. CLI (flint.py)

```
用法: python3 flint.py <run|asm|native> <文件.fl> [--native] [--input 文件] [-o out] [--trace] [--stats]
```

| 命令 | 行为 | 退出码 |
|---|---|---|
| `run` | VM 模式：编译→汇编→虚拟机执行（零依赖） | 编译错 2 / 运行时错 3 / 程序退出码 |
| `run --native` | 编译为原生可执行文件并运行（临时文件，跑完删除） | 同上 |
| `asm` | 只输出编译出的 Flint-ASM 汇编文本 | 0 |
| `native [-o out]` | x86-64 汇编后端 → 生成可执行文件（默认同名） | 0 / 2 |

依赖定位：`flint.py` / `flint_ide.py` 顶部**双布局兼容**查找 `flint-lang`（先同级、再上级），仓库/Windows 包内与 Linux 开发布局都能直接跑（v2.2 修复）。

## 12. 图形 IDE (flint_ide.py, PyQt6)

- 代码编辑器：语法高亮 / 行号 / 当前行高亮 / 编译错误自动跳转并标红
- 一键运行：词法→语法→类型检查→代码生成→汇编→虚拟机执行
- 底部四视图：输出 / 汇编（编译产物）/ 错误 / 输入（stdin）
- 停止执行（可打断死循环）、运行统计（退出码/执行指令数/耗时）
- 深色主题，快捷键齐全；依赖 `PyQt6`

## 13. Windows 构建与发布 (build.bat)

一键流程（依赖装在项目内 `.env`，**不占 D 盘、不污染系统环境**）：

1. **定位 Python**：`py -3` → `python` → 都没有则 winget 装 Python 3.12；找不到则提示手动安装。
2. **装依赖**：`pip install --target .env\site-packages pyqt6 pyinstaller`（已存在则跳过）。
3. **打包 CLI**：`PyInstaller --onefile --name flint --paths flint-lang flint.py` → `dist\flint.exe`。
4. **打包 IDE**：`PyInstaller --onefile --name flint-ide --paths flint-lang flint_ide.py` → `dist\flint-ide.exe`。
5. 复制 `examples` 到 `dist`。

> 发布注意事项（踩过的坑）：`build.bat` 必须是 **GBK(936) 编码 + CRLF 行尾**（UTF-8/LF 会被 cmd 按 GBK 解析碎裂，报 `'xx' 不是内部或外部命令`）；`.vscode/tasks.json`、`settings.json`、`README.md` 等是 UTF-8，VSCode 认 UTF-8 不受影响。

---

## 14. 测试体系

| 套件 | 数量 | 内容 | 运行 |
|---|---|---|---|
| `smoke_tests.py` | 10 | 开箱自检：VM 模式跑 10 个示例，断言关键输出与退出码 | `python3 smoke_tests.py` / `py smoke_tests.py` |
| `flint-lang/tests/run_tests.py` | 17 | 底层：汇编/反汇编/VM/镜像 | `python3 flint-lang/tests/run_tests.py` |
| `run_tests.py`（开发侧） | 38 | v2 前端端到端（VM 后端） | 开发仓库 |
| `asm_tests.py`（开发侧） | 12 | 9 示例 VM vs 汇编输出逐字节一致 + TRAP + 新命令 | 开发仓库 |
| `ide_smoke.py`（开发侧） | 9 | IDE 冒烟 | 开发仓库 |

开发侧合计 **76 项全绿**（38+12+9+17）。

**语义一致性验证方法**（asm_tests）：同一 .fl 源码分别走 VM 与原生后端，逐字节比较输出 + 退出码；TRAP 场景分别验证 exit 1 / exit 2。

## 15. 性能数据（原生后端 vs C vs Python，Linux 实测 2026-10）

| 基准 | Flint x86-64 | C (gcc -O2) | Python 3.12 | 说明 |
|---|---|---|---|---|
| fib(35) 递归 | 76.8 ms | 14.6 ms | 843 ms | 比 Python 快 ~11×；直译无寄存器分配 |
| sum 0..1e8 (i32 回绕) | 113.7 ms | 29.7 ms | 1050 ms | 比 Python 快 ~9×；比 Node(247.6ms) 快 2.2× |
| 列表遍历 8000 | 1.11 ms | 1.14 ms | — | **与 C 持平** |

VM 后端纵向：`fib(15)` 62ms→1.0ms、`sum 1e6` 18.6s→2.2ms、列表遍历 305ms→1.1ms（v2.1→v2.2 汇编后端）。

> 为什么比 C 慢一点：asm.py 是直译式生成（表达式沿用 PUSH/POP 栈机、**无寄存器分配**——用户明确的设计取舍），gcc -O2 会做寄存器分配/内联。纯算术与内存访问场景已与 C 持平。

---

## 16. 扩展指南：给语言加一个新命令/内置函数

以新增内置函数为例（如 `sqr(x)`），完整链路：

1. **lexer.py**：`KEYWORDS` 集合加 `"sqr"`（或用现有 token 流，内置函数名在调用处解析）。
2. **parser.py**：`Call` 节点解析不变（`sqr(5)` 已是合法调用语法）；如需特殊语法则在对应 grammar 方法加分支。
3. **typecheck.py**：`Call` 检查分支加签名 `sqr: (i32) -> i32`（参数个数与类型校验）。
4. **codegen.py**：`_gen_call` 的内置函数分发加 `sqr` → 生成 `MUL r0, r0, r0` 之类指令。
5. **asm.py**：`_gen_call` 的 x86 分发加 `sqr` → `imull %eax, %eax`。
6. **测试**：`smoke_tests.py` 或 `examples` 里加用例断言；开发侧在 `run_tests.py`/`asm_tests.py` 各加 VM 与原生一致性用例。
7. **文档**：README 运算符/内置表 + 本文档对应小节。

新增**运算符**同理：lexer 正则加 token → parser 优先级表 → typecheck 操作数类型 → codegen 指令映射 → asm.py 的 BIN32/CMP 映射表。

新增**指令**（改 ISA）：isa.py 操作码表 → assembler.py 解析 → vm.py 执行分支 → disasm 反汇编 → 必要时 asm.py 映射。需同步 4 处，避免 VM 与原生语义漂移（asm_tests 逐字节对比就是为此把关）。

---

## 17. 已知限制与设计取舍

| 取舍/限制 | 说明 |
|---|---|
| 无寄存器分配 | 用户明确"不搞寄存器分配，违背初心"——直译式生成，简单可读；代价是原生后端比 gcc -O2 慢约 5×（部分场景已持平） |
| 无 GC | 栈帧自动回收；列表编译期定长、无堆分配；总内存受 VM 64KB 限制 |
| i32 32 位回绕 | 超出 2³¹ 回绕（Python 无界大整数会得到精确值）——性能换确定性 |
| 静态定长列表 | `list[i32]` 由字面量定长、`list[i32; N]` 显式定长（零初始化） |
| 字符串 | 字面量拼接编译期折叠；**v3.0 起运行时拼接/比较**（`STRCAT`/`STRCMP`，每拼接一个 256B 池槽，过长越界报错）；`s[i]` 返回字符码 |
| 无字典/类/对象/闭包 | `for-else`、`try` 等未实现；全局变量只读（无 `global` 关键字） |
| 原生后端需 gcc | Windows 默认无 gcc：VM 模式零依赖可用；`--native` 报清晰提示（不会崩溃） |
| flt_trap 为 Linux syscall | 原生产物 trap 路径目前面向 Linux/macOS（见 §8.4） |

---

## 18. v3.0 大版本更新记录（2026-10-04，语言本体，不动游戏引擎）

### 18.1 新语法

1. **条件表达式**  if c else b（parser.py parse_expr 顶层，右结合可嵌套；typecheck 校验条件为 bool、两分支类型一致；codegen/asm.py 分支跳转生成）。
2. **默认参数值** def f(x: i32, y: i32 = 10)：
   - parser：参数列表支持 = 字面量，FuncDef 新增 defaults（与 params 对齐，无默认处 None）；
   - typecheck：默认值必须字面量且类型匹配；**有默认值参数之后不能再有无默认值参数**（与 Python 一致）；调用允许缺参（下限=必需参数数）；
   - codegen/asm.py：调用处从右向左压栈时**自动补默认值**。

### 18.2 运行时字符串拼接与比较（新 ISA 指令）

- STRCAT 0x25：STRCAT rd, rs1, rs2 —— 把 rs1、rs2 两个 0 结尾字符串**无缝拼接**到 rd 指向的缓冲（第二串覆盖第一串结尾 0；VM 实现于 vm.py，原生后端对应 lt_strcat 助手）。
- STRCMP 0x26：STRCMP rd, rs1, rs2 —— 
d = -1/0/1 无符号字节字典序（VM 实现；原生 lt_strcmp）。
- codegen _gen_binop：str + str → 分配 256B 池槽（__sbN，编译期数据段）→ STRCAT；比较 == != < > <= >= → STRCMP + 布尔映射。
- **已知边界**：每个拼接表达式独立占 256B 池槽（编译期分配）；拼接结果超 256B 会越界（TRAP 1）；嵌套/长串注意内存 64KB 总量。

### 18.3 新内置（TRAP 14-16，双后端同步）

| 内置 | 签名 | 语义 | VM | 原生 |
|---|---|---|---|---|
| sqrt | sqrt(x: i32) -> i32 | 整数平方根向下取整；负数 → 0 | TRAP 14 (math.isqrt) | lt_isqrt（6 次牛顿迭代+校正） |
| gcd | gcd(a: i32, b: i32) -> i32 | 最大公约数（取绝对值） | TRAP 15 (math.gcd) | lt_gcd（辗转相除） |
| clamp | clamp(x, lo, hi) -> i32 | 夹取到 [lo, hi] | TRAP 16 | lt_clamp（cmov） |

### 18.4 版本与交付

- lint.py __version__ = "3.0.0"；lint_ide.py __version__ = "3.0.0"（IDE 外壳版本同步，引擎代码未动）。
- 新增 examples/v30_features.fl（全部新特性演示）；smoke_tests.py 增至 **11 个用例**（10 旧 + v30）。
- 双后端一致性：codegen.py（VM 链）与 asm.py（x86 链）同步实现；回归 = smoke_tests.py 11/11 + lint-lang/tests/run_tests.py 17/17 + gfx 模式 offscreen 5 帧 exit 0。
- 已知边界：asm.py 的原生 lt_* 助手为 x86-64 Linux/macOS syscall 环境（同 §8.4）；Windows 无 gcc 时走 VM 模式，功能等价。


## 19. v3.1 小迭代记录（2026-10-04）

1. **字符字面量 `'a'`**：lexer 单引号单字符 → `CHAR` token（字符码），多字符报错提示用双引号；
   parser `parse_primary` 转 `IntLit`（与 `s[i]` 返回字符码的语义一致）；天然参与常量折叠与算术。
2. **`str(i32)` 数字转字符串**：parser 识别 `str(...)`（TYPE token 分支）→ 内置签名 `(i32) -> str`；
   codegen 分配 256B 池槽 → `TRAP 17`（VM 实现十进制带负号写入缓冲）；原生后端 `flt_itoa` 助手
   （divl 循环 + 符号处理 + 就地回拷；INT_MIN 边界已知）。配合 v3.0 `STRCAT` 可实现
   `print("i=" + str(i))` 完整 Python 风格格式化。
3. **修复**：空字符串初始化 `""` 曾生成 `DB , 0`（空数据行导致汇编器 `_split_data_items` 空 item 崩溃），
   现改为 `DB 0`。
4. **版本**：`flint.py`/`flint_ide.py` → 3.1.0；新增 `examples/v31_features.fl`；smoke 增至 **12 项**。
5. **回归**：smoke 12/12 + flint-lang 17/17 + gfx offscreen 5 帧 exit 0；打包后实测同款通过。
