$LLAMA_EXE = "$env:USERPROFILE\.docker\bin\inference\com.docker.llama-server.exe"
$MODEL_4B = "$env:USERPROFILE\.docker\models\bundles\sha256\3c870873bc00ae437f5670e7aebd7ec538e9bf2dc56238df6f521066dfea2820\model\Qwen3.5-4B-Q4_K_M.gguf"

Get-Process -Name *llama* -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

$llamaArgs = @(
    "-m", $MODEL_4B,
    "-ngl", "99",
    "-dev", "Vulkan1",
    "-c", "8192",
    "-np", "1",
    "-ctk", "q8_0",
    "-ctv", "q5_0",
    "-fa", "on",
    "-b", "2048",
    "-ub", "512",
    "--reasoning", "on",
    "--reasoning-format", "deepseek",
    "--tools", "all",
    "-ag",
    "--port", "18089",
    "--host", "0.0.0.0"
)

Write-Host "正在启动 Qwen 3.5 4B (100% 纯 GPU · 8192 上下文)..."
Start-Process -FilePath $LLAMA_EXE -ArgumentList $llamaArgs -WindowStyle Hidden
Start-Sleep -Seconds 6

$health = $false
for ($i = 0; $i -lt 25; $i++) {
    try {
        $res = Invoke-RestMethod -Uri "http://127.0.0.1:18089/health" -TimeoutSec 1
        if ($res.status -eq "ok") {
            $health = $true
            break
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

if ($health) {
    Write-Host "✔ llama-server 启动成功并就绪！"
    $slots = Invoke-RestMethod -Uri "http://127.0.0.1:18089/slots"
    Write-Host "实时 Slot 上下文: $($slots[0].n_ctx)"
} else {
    Write-Host "❌ 启动超时"
}
