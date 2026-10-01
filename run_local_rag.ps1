# ==============================================================================
# Turbovec RAG 本地系统启动器 (PowerShell 原生运行 · 无需 Docker)
# 特性：4-bit 向量记忆检索、网页长文自动抓取入库、代码大纲与行号切片、System 2 思考链
# ==============================================================================

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "Turbovec RAG 智能统一系统 - 运行中 [Port 18088]"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "   Turbovec RAG · 本地智能记忆系统 (PowerShell 模式)" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# 检查当前目录
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# 端口检查 (单一对外端口 18088)
$portCheck = Get-NetTCPConnection -LocalPort 18088 -ErrorAction SilentlyContinue
if ($portCheck) {
    Write-Host "⚠️ 端口 18088 当前已被占用 (可能是之前已启动的实例)" -ForegroundColor Yellow
    Write-Host "   建议先关闭占用 18088 端口的进程以确保正常启动。" -ForegroundColor Yellow
    Write-Host ""
}

Write-Host "正在启动 Turbovec RAG 统一服务 (http://127.0.0.1:18088)..." -ForegroundColor Green
Write-Host "• 唯一服务入口: Web 界面与 OpenAI 兼容 API (/v1/chat/completions)" -ForegroundColor Cyan
Write-Host "• 向量检索引擎: Turbovec 4-bit 量化加速" -ForegroundColor Cyan
Write-Host "• 智能决策路由: Laya / BGE 双语语义路由" -ForegroundColor Cyan
Write-Host "• 网页资料支持: 自动抓取、清洗与向量化长文防爆窗" -ForegroundColor Cyan
Write-Host "--------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

try {
    python app\main.py
} catch {
    Write-Host ""
    Write-Host "❌ 服务异常: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    Write-Host ""
    Write-Host "========================================================" -ForegroundColor Yellow
    Write-Host "Turbovec RAG 服务已停止。窗口已锁定，不会自动关闭。" -ForegroundColor Yellow
    Write-Host "请查看上方日志排查原因。按任意键退出窗口..." -ForegroundColor Gray
    Write-Host "========================================================" -ForegroundColor Yellow
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}
