@echo off
chcp 65001 >nul
title 本地原生大模型引擎 · 启动器

echo ========================================================
echo    Turbovec RAG · 本地原生大模型引擎 (CMD 启动器)
echo ========================================================
echo.
echo  请选择要启动的大模型:
echo.
echo  [1] Qwen 3.5 4B               (通用对话 · 上下文自动加长至显存红线)
echo  [2] Microsoft Phi-4-mini 3.8B (深度推理 · 上下文自动加长至显存红线)
echo  [3] Qwen 2.5 7B               (旗舰综合 · 上下文自动加长至显存红线)
echo  [4] Qwen3 4B                 (支持工具调用 · 需先下载)
echo  [5] Qwen3 8B                 (GPU+CPU 混合 · 较慢 · 需先下载)
echo.
set /p CHOICE="请输入序号 (1-5，直接回车默认启动 [1] Qwen 3.5 4B): "

if "%CHOICE%"=="" set CHOICE=1
if "%CHOICE%"=="1" goto LAUNCH
if "%CHOICE%"=="2" goto LAUNCH
if "%CHOICE%"=="3" goto LAUNCH
if "%CHOICE%"=="4" goto LAUNCH
if "%CHOICE%"=="5" goto LAUNCH
echo 无效选择，使用默认 [1] Qwen 3.5 4B
set CHOICE=1

:LAUNCH
echo.
echo 已选择: [%CHOICE%]，正在唤起 PowerShell 引擎...
powershell.exe -NoExit -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_local_llm.ps1" -ModelChoice "%CHOICE%"
