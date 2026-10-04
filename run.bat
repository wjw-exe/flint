@echo off
chcp 65001 >nul
title Flint v3.2.1 IDE
cd /d "%~dp0"
if not exist "dist\flint-ide.exe" (
  echo 尚未构建环境, 请先双击 build.bat 一键构建
  pause
  exit /b 1
)
start "" "dist\flint-ide.exe" %*
