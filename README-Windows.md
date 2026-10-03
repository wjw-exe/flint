# Flint v2.2 · Windows 环境安装说明

这是一套**一键构建的 Windows 环境包**：双击一个脚本，自动帮你把"一堆 Py 文件"变成两个可以直接双击运行的程序。

## 使用方法（三步）

1. **双击 `build.bat`** —— 脚本自动：
   - 检测/安装 Python 3.12（没有就用 winget 自动装）
   - 安装 PyQt6（IDE 界面库）和 PyInstaller（打包器）
   - 打包出 `dist\flint.exe`（命令行工具）和 `dist\flint-ide.exe`（图形 IDE）
   - 拷贝示例程序到 `dist\examples\`
2. **双击 `run.bat`** —— 打开图形化 IDE（等 build 完成后）
3. 开始写代码：IDE 里打开 `examples\v21_ext.fl` 或自己新建 `.fl` 文件，点"运行"即可

> 首次构建需要联网下载依赖，约 3~8 分钟；完成后**环境一次到位**，之后不用再装任何东西。

## 两种运行方式

| 方式 | 命令 | 说明 |
|---|---|---|
| IDE（图形界面） | `run.bat` | 语法高亮、行号、一键运行、四视图 |
| 命令行（VM 模式） | `dist\flint.exe run examples\fib.fl` | 零依赖，直接可用 |
| 命令行（原生模式） | `dist\flint.exe run --native examples\fib.fl` | 编译为原生机器码执行，需要 gcc（见下） |
| 只编译不运行 | `dist\flint.exe native examples\fib.fl -o my.exe` | 生成原生可执行文件 |

## 关于 --native（原生编译）的 gcc 依赖

- **VM 模式**（`flint.exe run`）纯 Python 实现，**不需要任何外部工具**，双击即用。
- **--native / native** 走 x86-64 汇编后端：编译器（Python）直接产出 x86-64 汇编，由 **gcc** 完成最后的"汇编+链接"。Windows 上没有预装 gcc，可选装 MinGW-w64：
  1. `winget install -e --id BrechtSanders.WinLibs.POSIX.Mingw-w64`
  2. 设置环境变量 `CC` 指向 `gcc.exe`（或在 cmd 里 `set CC=...`）
- 不装 gcc 完全不影响 IDE 和 VM 模式的使用。

## 目录说明

```
flint-windows/
├── build.bat          一键构建(装依赖+打包 exe)
├── run.bat            一键启动 IDE
├── flint.py           CLI 入口
├── flint_ide.py       PyQt6 图形 IDE
├── asm.py             x86-64 汇编后端(原生编译用)
├── lexer/parser/typecheck/codegen.py   编译器前端
├── flint-lang/        底层汇编器与虚拟机(17 项测试)
├── examples/          示例程序
└── dist/              构建产物(flint.exe / flint-ide.exe / examples/)
```

## 关于本包的验证

本包已在开发机(Linux)上用**完全相同**的 PyInstaller 命令实测：
`flint` 单文件产物跑通 `run` / `run --native` / `native -o`（输出 610 = fib(15) 正确）；
`flint-ide` 单文件产物正常启动。`build.bat` 在 Windows 上执行的就是这套命令
（仅 Python 检测与 winget 安装部分不同）。

## 测试

- VM 后端：38 项全绿；底层 flint-lang：17 项全绿；汇编后端：12 项全绿；IDE：9 项全绿
- 构建完成后可在 cmd 里跑：`dist\flint.exe run examples\lists.fl`
