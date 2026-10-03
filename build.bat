@echo off
setlocal EnableDelayedExpansion
title Flint v2.2 - Windows 一键环境构建

echo.
echo  ============================================================
echo    Flint v2.2 - Windows 一键环境构建
echo    (依赖装入 .env, C盘项目目录内, 不占用 D 盘)
echo  ============================================================
echo.

REM ---------- 1. 定位 Python ----------
echo [1/4] 检测 Python ...
set "PY="
where py >nul 2>nul && for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
if not defined PY (
  where python >nul 2>nul && for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
)
if not defined PY (
  echo     未检测到 Python, 尝试用 winget 自动安装 ...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements >nul 2>nul
  for /f "delims=" %%i in ('dir /b /s "%LOCALAPPDATA%\Programs\Python\Python3*\python.exe" 2^>nul') do set "PY=%%i"
)
if not defined PY (
  echo     [失败] 请手动安装 Python 3.10+ : https://www.python.org/downloads/
  echo           安装时勾选 "Add python.exe to PATH"
  pause
  exit /b 1
)
echo     使用 Python: %PY%
"%PY%" -V

REM ---------- 2. 依赖 (.env, C 盘) ----------
echo.
echo [2/4] 检查依赖 (PyQt6 + PyInstaller) ...
set "SP=%~dp0.env\site-packages"
if exist "%SP%\PyInstaller" (
  echo     依赖已存在, 跳过安装
) else (
  "%PY%" -m pip install --target "%SP%" pyqt6 pyinstaller >build_deps.log 2>&1
  if errorlevel 1 (
    echo     [失败] 依赖安装出错, 详见 build_deps.log
    pause
    exit /b 1
  )
  echo     依赖安装完成
)

REM ---------- 3. 打包 CLI (flint.exe) ----------
echo.
echo [3/4] 打包命令行工具 flint.exe ...
cd /d "%~dp0"
set "PYTHONPATH=%SP%"
"%PY%" -m PyInstaller -y --onefile --name flint --paths flint-lang flint.py >build_cli.log 2>&1
if errorlevel 1 (
  echo     [失败] 打包 flint.exe 出错, 详见 build_cli.log
  pause
  exit /b 1
)
echo     flint.exe 完成

REM ---------- 4. 打包 IDE (flint-ide.exe) ----------
echo.
echo [4/4] 打包图形化 IDE flint-ide.exe (含 PyQt6, 需几分钟) ...
"%PY%" -m PyInstaller -y --onefile --name flint-ide --paths flint-lang flint_ide.py >build_ide.log 2>&1
if errorlevel 1 (
  echo     [失败] 打包 flint-ide.exe 出错, 详见 build_ide.log
  pause
  exit /b 1
)
echo     flint-ide.exe 完成

REM ---------- 5. 拷贝示例 ----------
if not exist "dist\examples" xcopy /e /i /q examples dist\examples >nul
del build_cli.log build_ide.log build_deps.log >nul 2>nul

echo.
echo  ============================================================
echo    构建完成! 环境已就绪
echo  ============================================================
echo     IDE:       双击 dist\flint-ide.exe    (或运行 run.bat)
echo     命令行:    dist\flint.exe run examples\fib.fl
echo     VM 模式零依赖, 直接可用
echo     原生模式:  dist\flint.exe run --native examples\fib.fl
echo               (需要 gcc, 可选装 MinGW-w64 后设置 CC 环境变量)
echo  ============================================================
pause