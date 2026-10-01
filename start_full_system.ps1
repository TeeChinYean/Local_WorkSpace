# ==============================================================================
# Turbovec RAG 全套完整系统启动器 (PowerShell 模式)
# 一键联动启动：本地 GPU 大模型 (llama-server) + Turbovec RAG Web 检索系统
# ==============================================================================

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "Turbovec RAG 全套系统联动启动器"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "   Turbovec RAG · 全套系统一键联动启动器" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "正在按顺序连接并启动系统全部组件:" -ForegroundColor Yellow
Write-Host "  [1/2] 启动本地 GPU 大模型推理引擎 (PowerShell 独立窗口)" -ForegroundColor White
Write-Host "  [2/2] 启动 Turbovec RAG 知识检索系统 (PowerShell 独立窗口)" -ForegroundColor White
Write-Host ""

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1. 在独立窗口启动本地大模型引擎
$llmBat = Join-Path $ScriptDir "run_local_llm.bat"
Start-Process "cmd.exe" -ArgumentList "/c `"$llmBat`""

# 2. 等待 2 秒后在独立窗口启动 Turbovec RAG Web 服务
Start-Sleep -Seconds 2
$ragBat = Join-Path $ScriptDir "run_local_rag.bat"
Start-Process "cmd.exe" -ArgumentList "/c `"$ragBat`""

Write-Host ""
Write-Host "✔ 两个服务均已在各自专用窗口中成功启动！" -ForegroundColor Green
Write-Host "--------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "• 唯一服务入口: http://localhost:18088 [Turbovec 4-bit · Web UI · OpenAI API · 知识库]" -ForegroundColor Cyan
Write-Host "• 显存策略: 只要不到 VRAM 极限 -0.3GB 绝不截断上下文" -ForegroundColor Cyan
Write-Host "--------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "提示: 保持对应子窗口运行即可；关闭对应窗口即可停止该项服务。" -ForegroundColor Gray
Write-Host ""
Write-Host "两个服务的专用控制台窗口已就绪。本窗口已锁定，可安全最小化或关闭。" -ForegroundColor DarkGray
