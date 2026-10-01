# ==============================================================================
# 本地纯净大模型原生启动器 (PowerShell 原生运行 · 完全脱离 Docker)
# 上下文窗口由 app\llm_launcher.py 自动决定：按真实 KV 大小估算 → 启动后用 nvidia-smi 实测 →
# 只要总显存低于红线 (总显存 - vram_reserve_mb，默认 ≈3.8 GiB) 就自动加长上下文并重启一次，
# 超过红线则自动缩小；实测结果写入 app\storage\ctx_calibration.json，下次启动直接命中。
# 模型、KV 量化 (默认 K=q8_0 / V=q4_0)、端口等参数统一在 llm_models.json 中配置。
# ==============================================================================

param(
    [string]$ModelChoice = ""
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "本地原生大模型引擎 (llama-server) - 运行中"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Launcher = Join-Path $ScriptDir "app\llm_launcher.py"
$PORT = 18089

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "   Turbovec RAG · 本地原生大模型引擎 (PowerShell 模式)" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# 1. 端口专有保障：只终止占用 18089 的 llama 进程，绝不误杀其他程序
$portCheck = Get-NetTCPConnection -LocalPort $PORT -State Listen -ErrorAction SilentlyContinue
if ($portCheck) {
    foreach ($conn in $portCheck) {
        $p = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($p -and $p.ProcessName -like "*llama*") {
            Write-Host "⚠️ 检测到旧 llama-server (PID $($p.Id)) 占用端口 $PORT，正在释放显存..." -ForegroundColor Yellow
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        } elseif ($p) {
            Write-Host "❌ 端口 $PORT 被非 llama 进程占用: $($p.ProcessName) (PID $($p.Id))，请先手动关闭。" -ForegroundColor Red
            Read-Host "按回车退出"
            exit 1
        }
    }
    Start-Sleep -Seconds 1.5
    Write-Host "✔ 旧进程已终结，GPU 显存与端口已释放！" -ForegroundColor Green
}

# 2. 模型菜单选择
Write-Host "请选择要启动的大模型 (上下文自动加长至显存红线):" -ForegroundColor Yellow
Write-Host "  [1] Qwen 3.5 4B               (通用对话 · 混合注意力，KV 极省，上下文最长)"
Write-Host "  [2] Microsoft Phi-4-mini 3.8B (深度推理)"
Write-Host "  [3] Qwen 2.5 7B               (旗舰综合 · 显存最紧)"
Write-Host "  [4] Qwen3 4B                 (支持工具调用 · 需先下载: python scratch\download_model.py 4)"
Write-Host "  [5] Qwen3 8B                 (GPU+CPU 混合 · 较慢 · 需先下载: python scratch\download_model.py 5)"
Write-Host ""

$choice = $ModelChoice
if (-not $choice) {
    $choice = Read-Host "请输入序号 (1-5，直接回车默认启动 [1] Qwen 3.5 4B)"
}
if ($choice -notin @("1", "2", "3", "4", "5")) {
    $choice = "1"
}

Write-Host ""
Write-Host "• 显存策略: llama.cpp --fit 先自动适配，若离红线仍有余量再用 -ngl 99 -c N 自动加长一次" -ForegroundColor Cyan
Write-Host "• KV 量化: 见 llm_models.json (默认 Key=q8_0 / Value=q4_0)" -ForegroundColor Cyan
Write-Host "• 监听地址: http://127.0.0.1:$PORT (仅本机；网关通过 app\storage\llama_api_key.txt 共享密钥访问)" -ForegroundColor Cyan
Write-Host "正在规划上下文并装载模型，可能会自动重启一次以加长上下文..." -ForegroundColor Yellow
Write-Host "提示: 启动成功后请访问 Turbovec RAG 智能系统 http://127.0.0.1:18088" -ForegroundColor Green
Write-Host "(本窗口保持运行，关闭此窗口即可停止模型服务)" -ForegroundColor DarkGray
Write-Host ""

# 3. 后台延迟唤起浏览器
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 8
    Start-Process "http://127.0.0.1:18088"
} | Out-Null

# 4. 运行启动器 (前台常驻，日志直接输出到本窗口)
try {
    & python $Launcher --model $choice
} catch {
    Write-Host ""
    Write-Host "❌ 启动异常: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    Write-Host ""
    Write-Host "========================================================" -ForegroundColor Yellow
    Write-Host "服务进程已结束。窗口已锁定，不会自动关闭。" -ForegroundColor Yellow
    Write-Host "请查看上方日志排查原因。按任意键退出窗口..." -ForegroundColor Gray
    Write-Host "========================================================" -ForegroundColor Yellow
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}
