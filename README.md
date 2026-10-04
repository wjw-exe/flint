# Flint v3.2.2 — Python 风格语法 · 静态类型 · 编译型语言 · 原生 x86-64 汇编后端

> 上一版 Flint 是类 C 大括号语法。v2 重写前端，**语法接近 Python**（缩进、`def`、`if`/`elif`/`else`、`while`、`for … in range(…)`），
> 但它是**编译型**语言：源码 → 词法/语法/类型检查 → 生成汇编 → 机器码。
> v2.1 让语法**无限接近 Python**（静态列表、复合赋值、break/continue、range 三参数、链式比较、类型推断、全局变量、多参数 print 等）。
> **v2.2 两项大动作**：① 命令再扩展——位运算 `& | ^ << >> ~`、位复合赋值、`in`/`not in` 成员测试、
> 新内置 `min/max/sum/pow`、`while-else`/`for-else` 循环 else 子句（全部与 Python 语义一致）；
> ② **移除 C 后端，改用真正的 x86-64 汇编后端**（`asm.py`）：直接产出 AT&T 汇编，gcc 仅做汇编与链接，
> 不再经过 C 源码。底层复用 `flint-lang` 汇编器与虚拟机（17 项测试全绿）。
> **v3.0 语言大版本**（游戏引擎不动）：① 条件表达式 `a if c else b`；② **默认参数值** `def f(x: i32, y: i32 = 10)`；
> ③ **运行时字符串拼接与比较**（新增 `STRCAT`/`STRCMP` 指令，`s + t`、`s == t`、字典序 `<`/`>` 全支持）；
> ④ 新内置 `sqrt / gcd / clamp`（TRAP 14-16，双后端同步）；⑤ 新增 `examples/v30_features.fl` 演示。
> **v3.1 小迭代**：① **字符字面量** `'a'`（单引号单字符 → 字符码 i32，与 `s[i]` 语义一致，可参与算术）；
> ② **`str(i32)` 数字转字符串**（TRAP 17，双后端同步），配合 v3.0 拼接实现完整 Python 风格格式化
> `print("i=" + str(i))`；③ 修复空字符串初始化生成非法数据行（`DB , 0` → `DB 0`）。
> **v3.2 纯 UI 库**（引擎不动）：`ui_window/ui_button/ui_label/ui_slider/ui_progress/ui_checkbox` 组件
> + `ui_clicked(边沿)/ui_value/ui_checked` 轮询 + `ui_set_text/ui_set_value` 更新 + `ui_present/ui_closed`。
> 新增 `ui` 命令与 `examples/ui_calc.fl` 计算器示例。**设计哲学：UI 只负责渲染与输入捕获，
> 一切计算由后端 .fl 代码完成**（无回调、无对象，控件用整数 ID 引用，状态轮询）。
> **v3.2.1 体验改进**：`run` 命令**自动路由**——编译后检测引擎 TRAP，UI 程序自动切 `ui` 模式弹窗、
> 图形程序自动切 `gfx` 模式弹窗，普通程序保持终端输出。以后用 `run` 跑任何程序都不会再报
> "UI 内置只能在 ui 模式下使用"。
> **v3.2.2 修复**：UI 组件参数装载 bug——`ui_button(…, str(n))` 等带字符串参数的调用，
> `str()`/`LDA` 子调用会覆盖已装载的 x/y 寄存器，导致组件坐标错乱（窗口空白）。
> 修复：UI 参数改为与 gfx 同款的 PUSH/POP 装载，任何子调用不再破坏参数寄存器。

## 为什么比 Python 快

Python 慢的根源：一切皆对象、动态类型（每次运算都要查类型）、大量运行时检查与 GC。
Flint v2 的设计把它们全部消灭在**编译期**：

| | Python | Flint v2.1 |
|---|---|---|
| 类型 | 动态, 运行时探测 | **静态类型, 编译期确定, 运行时零类型开销** |
| 变量 | 可随时换类型 | 声明时固定, 类型错误编译失败 |
| 执行 | 解释/JIT + 对象模型 | **编译为汇编字节码, 直接操作寄存器与内存** |
| 内存 | 自动 GC + 动态分配 | 栈帧自动回收, 无 GC, 列表编译期定长无堆分配 |
| 函数 | 动态函数对象 | 静态调用, 直接映射 CALL/RET |

### 编译期优化（v2.1 新增, 不损失性能）

1. **常量折叠**：`2 + 3 * 4` 在编译期直接算成字面量；`"a" + "b"` 折叠成 "ab"。
2. **range 字面量步长直判**：`for i in range(0, 1000000)` 的步长/边界是字面量时，编译期定死循环方向与增量，
   循环体里**没有**每圈的符号判断；字面量边界直接用 `CMPI` 比较，**免一次内存读**。
3. **复合赋值单次寻址**：`xs[i] += v` 只计算一次元素地址（普通写法要 LD 再 ST 两次寻址）。
4. **循环内无越界检查**：`for x in xs` 由循环条件保证索引安全，循环体内**零越界检查**。
5. **动态步长零检查**：`for i in range(0, n, step)` 只在循环入口做一次"步长为 0 → TRAP"判断，每圈仅一次方向分支。
6. **类型信息全部编译期确定**：索引、取模、除法等运算不做任何运行时类型探测。

### 虚拟机性能

VM 热循环取消了每指令一次的全内存拷贝反汇编（trace 模式才生成），实测 **~3 倍提速**：

| 基准 | v2.0 | v2.1(VM) | **v2.2 x86-64 汇编后端** |
|---|---|---|---|
| fib(15) 递归 | 194 ms | 62 ms | **1.0 ms** |
| sum 0..999999 | 56.1 s | 18.6 s | **2.2 ms** |
| 列表遍历 8000 元素 | 951 ms | 305 ms | **1.1 ms** |

### 原生汇编后端(v2.2)性能实测(本机 2026-10, 取最佳)

| 基准 | Flint x86-64 | C (gcc -O2) | Python 3.12 | 说明 |
|---|---|---|---|---|
| fib(35) 递归 | 76.8 ms | 14.6 ms | 843 ms | 比 Python 快 **11×**; 直译无寄存器分配, 慢于 C ~5× |
| sum 0..1e8 (i32 回绕) | 113.7 ms | 29.7 ms | 1050 ms | 比 Python 快 **9×**, 比 Node(247.6ms) 快 2.2× |
| 列表遍历 8000 | 1.11 ms | 1.14 ms | — | **与 C 持平** |

> 为什么比 C 慢一点: asm.py 是**直译式生成**(表达式沿用 PUSH/POP 栈机、无寄存器分配),
> 而 gcc -O2 会做寄存器分配/内联。纯算术与内存访问场景已与 C 持平;
> 递归调用协议每次多几条指令。比旧 C 后端慢 ~4.5×(C 后端由 gcc 优化), 但这是**纯汇编**产物。
> 后续可加寄存器分配把它推到 C 的水平。

（乱序环境测量，看倍数不看绝对值；每行数据均为指令数与输出双重校验。）

## 语法速览

```python
# 注释用 # ; 代码块用缩进(空格), 和 Python 一样

def fib(n: i32) -> i32:
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)

def main() -> i32:
    # 显式类型声明
    total: i32 = 0
    i: i32 = 1
    while i <= 100:
        total += i              # 复合赋值
        i += 1
    print(total)                # 5050

    # 类型推断: 首次赋值即声明
    x = 10
    x //= 4                     # 地板除复合赋值 → x = 2
    print(x, total)             # 多参数 print, 空格分隔

    # 静态列表(编译期定长, 无堆分配)
    xs: list[i32] = [10, 20, 30]
    print(len(xs), xs[1], xs[-1])   # 3 20 30 (支持负索引)
    xs[0] += 5                      # 索引复合赋值(单次寻址)
    s: i32 = 0
    for v in xs:                    # 遍历列表(循环内无越界检查)
        s += v
    buf: list[i32; 8] = []          # 定长零初始化
    print(len(buf), buf[0])         # 8 0

    # range 三种形式
    a: i32 = 0
    for k in range(5):              # 0..4
        a += k
    for k in range(2, 6):           # 2..5
        a += k
    for k in range(10, 0, -3):      # 10, 7, 4, 1 (负步长)
        a += k

    # 链式比较 / break / continue
    if 1 < x < 100:
        print("ok")
    while True:
        if input() == 0:
            break

    flag: bool = True
    print(flag and not False)   # 短路逻辑 and/or/not

    # ---- v3.0: 条件表达式 / 默认参数 / 运行时字符串拼接与比较 / 数学内置 ----
    score: i32 = 85
    grade: str = "A" if score >= 90 else "B"    # 条件表达式(右结合, 可嵌套)
    print(grade)                                # B

    def clamp0(x: i32, lo: i32 = 0, hi: i32 = 100) -> i32:
        return clamp(x, lo, hi)                 # 默认参数 + clamp 内置
    print(clamp0(200), clamp0(-5, -10, -1))     # 100 -5

    name: str = "Flint"
    print("Hello, " + name + "!")               # 运行时字符串拼接(STRCAT)
    print(name == "flint", name < "flint")      # 0 1  (字典序比较 STRCMP)
    print(sqrt(100), gcd(48, 36))               # 10 12
    return 0                    # main 的返回值 = 程序退出码
```

## 类型系统

| 类型 | 说明 |
|---|---|
| `i32` | 32 位有符号整数(核心类型, 直接映射寄存器) |
| `bool` | 真/假(底层即 0/1) |
| `str` | 字符串字面量/变量(print、len(s)、s[i] 取字符码) |
| `list[i32]` | 静态定长列表(编译期定长, 栈/数据段分配, 无 GC) |

**编译期强制**：条件必须是 `bool`、算术运算只接受 `i32`、函数参数/返回值类型严格匹配、
变量先声明后使用、不可重复声明（循环变量除外，可重绑定）。任何类型错误都会在编译时报错（带行号），不会带到运行时。

**运行时陷阱（TRAP 指令）**：索引越界 → `TRAP 1`；除零/零步长 → `TRAP 2`。带 `@pc` 地址停机报错。

## 运算符

- 算术：`+ - * / % //`（`/` 向零截断, `//` 地板除, 均 32 位回绕）
- 复合赋值：`+= -= *= /= %= //=`
- 比较：`< > <= >= == !=`（支持链式 `a < b < c`；**str 走字典序** v3.0）
- 逻辑：`and or not`（短路求值）
- 条件表达式：`a if cond else b`（v3.0, 右结合可嵌套）
- 优先级与 Python 一致：`not` > 比较 > `and` > `or`
- 内置函数：`print(a, b, ...)`（自动换行, 空格分隔）、`input()`、`len(xs|s)`、`abs(x)`、
  `min/max/sum/pow`、`sqrt(x)`、`gcd(a, b)`、`clamp(x, lo, hi)`（v3.0）、`str(x)`（v3.1 数字转字符串）、
  `getch(ms)`/`clrscr()`/`sleep(ms)`（终端）、`window/clear/fill_rect/...`（图形, 见 gfx 模式）

## 快速开始

### Windows（零依赖, 开箱即用）

```bat
py flint.py run examplesib.fl          :: VM 模式 → 610
py flint.py run examplesubble_sort.fl
py flint.py asm examplesib.fl          :: 查看编译出的 VM 汇编
py smoke_tests.py                        :: 仓库自检: 12 个示例全部通过
```

图形 IDE：`pip install PyQt6` 后 `py flint_ide.py examples\lists.fl`；
一键构建免 Python 环境包（`flint.exe` + `flint-ide.exe`）：双击 `build.bat`。

### Linux / macOS

```bash
python3 flint.py run examples/fib.fl
python3 flint.py run --native examples/fib.fl   # x86-64 汇编后端(需要 gcc)
python3 flint.py native examples/fib.fl -o fib  # 生成可执行文件
python3 smoke_tests.py
```

## 原生后端（x86-64 汇编, 需要 gcc）

`run --native` / `native` 由 `asm.py` 把同一份源码经同一前端（词法→语法→类型检查→AST）
**直接生成 x86-64 AT&T 汇编**，再交给 gcc 只做最后一步"汇编 + 链接"（`gcc -no-pie -O2`）——
**不经过 C 或任何中间语言**。运行时与 Python 完全无关（v2.2 起旧 C 后端已移除）。

- **Linux/macOS**：系统自带 gcc 即可。
- **Windows**：默认没有 gcc。安装 MinGW-w64（如 `winget install BrechtSanders.WinLibs.POSIX.Mingw-w64`），
  把 `gcc.exe` 加入 PATH，或用环境变量 `CC` 指向它。
- **没有 gcc 时不会崩溃**：VM 模式（`run`）完全可用；`--native` 会给出清晰中文提示并返回错误码。

**语义与 VM 后端完全一致**（同一 AST、同一类型系统）：
- i32 32 位回绕、`/` 向零截断、`//` 地板除、移位 &31 ——与 VM 逐字节一致
- 越界 → TRAP 1（exit 1）；除零/零步长 → TRAP 2（exit 2）

### 原生后端实测性能（Linux, 2026-10, 取最佳）

| 基准 | Flint x86-64 | C (gcc -O2) | Python 3.12 | 说明 |
|---|---|---|---|---|
| fib(35) 递归 | 76.8 ms | 14.6 ms | 843 ms | 比 Python 快 ~11×; 直译无寄存器分配 |
| sum 0..1e8 (i32 回绕) | 113.7 ms | 29.7 ms | 1050 ms | 比 Python 快 ~9× |
| 列表遍历 8000 | 1.11 ms | 1.14 ms | — | 与 C 持平 |

（乱序环境测量, 看倍数不看绝对值；每行数据均为指令数与输出双重校验。）

## 编译管线

```
fib.fl (Python 风格源码)
   │  ① lexer.py     缩进感知词法(INDENT/DEDENT)
   ▼
token 流
   │  ② parser.py    递归下降 → AST (常量折叠)
   ▼
AST
   │  ③ typecheck.py 静态类型检查(编译期报错)
   ▼
带类型 AST
   │  ④ codegen.py   AST → Flint-ASM 汇编文本(编译期优化)
   ▼
汇编源码 (可读, 可人工检查)
   │  ⑤ flint_lang.assembler.py  两趟扫描汇编
   ▼
字节码镜像
   │  ⑥ flint_lang.vm.py  虚拟机执行(含 TRAP 陷阱)
   ▼
程序输出 + 退出码
```

或走原生后端（同一前端, 在第 ④ 步后分叉）:

```
带类型 AST
   │  ④' asm.py  AST → x86-64 AT&T 汇编(直译, 无寄存器分配)
   ▼
AT&T 汇编 (可读, 可人工检查)
   │  ⑤' gcc -no-pie -O2  仅做汇编与链接
   ▼
可执行文件 (运行时与 Python 无关)
```

```

## 与 Python 的刻意差异（为了性能）

- **i32 32 位回绕**：`sum 0..999999` 超出 2³¹ 后回绕（Python 无界大整数, 会得到精确值）——性能换来的确定性
- 列表是**静态定长**：`list[i32]` 由字面量定长, `list[i32; N]` 显式定长（零初始化）；总内存受 VM 64KB 限制
- 字符串：字面量拼接编译期折叠（v2.1）；**运行时拼接/比较**（v3.0, `STRCAT`/`STRCMP` 指令, 每个拼接表达式分配一个 256B 池槽, 拼接结果过长会越界报错）；`s[i]` 返回字符码 i32
- **字符字面量** `'a'` → i32 字符码（v3.1, 与 `s[i]` 语义一致）；**`str(i32)`** 数字转字符串（v3.1, 十进制带负号, 配合拼接做格式化输出）
- **纯 UI 库**（v3.2）：`ui_window/ui_button/ui_label/ui_slider/ui_progress/ui_checkbox` 创建组件,
  `ui_clicked(边沿)/ui_value/ui_checked` 轮询状态, `ui_set_text/ui_set_value` 更新组件,
  `ui_present()/ui_closed()` 刷新与退出。**UI 只负责渲染与输入捕获, 一切计算由后端完成**——
  无回调、无对象、组件以整数 ID 引用；仅支持 VM/ui 模式（原生后端明确报错）
- **纯 UI 库**（v3.2）：`ui_window/ui_button/ui_label/ui_slider/ui_progress/ui_checkbox` 创建组件,
  `ui_clicked(边沿)/ui_value/ui_checked` 轮询状态, `ui_set_text/ui_set_value` 更新组件,
  `ui_present()/ui_closed()` 刷新与退出。**UI 只负责渲染与输入捕获, 一切计算由后端完成**——
  无回调、无对象、组件以整数 ID 引用；仅支持 VM/ui 模式（原生后端明确报错）
- 默认参数值必须是字面量（v3.0）；有默认值的参数之后不能再有无默认值参数（与 Python 规则一致）
- 无字典/类/对象/闭包/GC；`for … else`、`try` 等未实现
- 全局变量是只读的（无 `global` 关键字, 函数内同名赋值会创建局部变量, 与 Python 一致）

## 图灵完备性

v2 编译到与 v1 完全相同的 Flint-ASM/虚拟机，而 Flint-ASM 已通过 Brainfuck 解释器证明图灵完备
（见 flint-lang/README.md 的四段式证明）。因此 **Flint v2.1 同样是图灵完备的**。

## 项目结构

```
flint-windows/             (即本仓库)
├── flint.py               CLI: run / asm / native / gfx / ui
├── flint_ide.py           专属 IDE (PyQt6)
├── lexer.py parser.py typecheck.py codegen.py   前端: 缩进词法 → AST → 静态类型 → VM 汇编
├── asm.py                 x86-64 汇编后端: AST → AT&T 汇编 → gcc 汇编链接 → 可执行
├── flint_ui.py            纯 UI 库引擎 (PyQt6): 组件渲染/输入捕获, 计算全交给后端
├── smoke_tests.py         开箱自检: 12 个示例 VM 模式全跑 + 断言
├── flint-lang/            底层库: 32 位 ISA / 汇编器 / 虚拟机(17 项测试)
├── examples/              hello / fib / loops / prime / echo / lists / break_continue / bubble_sort / pythonic / v21_ext / ui_calc(计算器)
├── build.bat              Windows 一键构建 flint.exe + flint-ide.exe(免 Python)
└── run.bat                启动图形 IDE

> 顶层开发验证: run_tests 38 + asm_tests 12 + IDE 9 + flint-lang 17 = **76 项全绿**
> (测试套件随开发仓库维护; 本仓库以 `smoke_tests.py` 做开箱自检)。
> v2.2 起 `run --native` / `native` 走 asm.py 汇编后端, 旧 C 后端(native.py)已移除。
```
## 专属 IDE

`flint_ide.py` 是为本语言打造的 PyQt6 桌面 IDE：语法高亮、行号、一键编译运行、
输出/汇编/错误/输入四视图、错误自动跳转标红、可停止死循环。
```bash
pip install PyQt6
python3 flint_ide.py examples/lists.fl
```
详见 [README-IDE.md](README-IDE.md)。

## 纯 UI 库 (v3.2)

UI 库与后端彻底分离：**引擎只画组件、收输入；业务逻辑全部写在后端(.fl 代码)里**。
组件以整数 ID 引用，事件用轮询读取（`ui_clicked` 边沿触发：只在被点击的那一帧返回 1 并清零）。

```bash
python3 flint.py ui examples/ui_calc.fl     # 打开计算器(真机)
python3 flint.py ui x.fl --frames 3         # 离屏跑 3 帧(CI/测试)
```

```python
def main() -> i32:
    ui_window(340, 290)
    disp = ui_label(15, 15, "0")
    btn = ui_button(15, 195, 70, 34, "+")     # → i32 ID
    while not ui_closed():
        ui_present()
        if ui_clicked(btn):                   # 轮询, 无回调
            ui_set_text(disp, "1")
    return 0
```

| 内置 | 作用 | 返回 |
| --- | --- | --- |
| `ui_window(w, h)` | 设置窗口尺寸 | 0 |
| `ui_button(x, y, w, h, label)` | 按钮 | ID |
| `ui_label(x, y, text)` | 文本 | ID |
| `ui_slider(x, y, w, lo, hi, val)` | 滑块 | ID |
| `ui_progress(x, y, w, val)` | 进度条(0-100) | ID |
| `ui_checkbox(x, y, label, checked)` | 复选框 | ID |
| `ui_clicked(id)` | 点击事件(边沿, 读后清零) | bool |
| `ui_value(id)` | 滑块/进度条当前值 | i32 |
| `ui_checked(id)` | 复选框状态 | bool |
| `ui_set_text(id, text)` / `ui_set_value(id, v)` | 更新组件 | 0 |
| `ui_present()` | 刷新画面 | 0 |
| `ui_closed()` | 窗口是否已关闭 | bool |

> 架构与 2D 引擎一致：VM 线程跑程序 + Qt 主线程渲染 + 锁保护队列。
> 原生后端(asm.py)遇到 `ui_*` 明确报错：UI 仅支持 `ui` 模式(VM)。
