Write-Host "=== 0. OS & Hardware Specs ==="
$cpuInfo = Get-CimInstance Win32_Processor | Select-Object -First 1 Name, NumberOfCores, NumberOfLogicalProcessors
$osInfo = Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, OSArchitecture
Write-Host "OS:  $($osInfo.Caption) ($($osInfo.Version)) $($osInfo.OSArchitecture)"
Write-Host "CPU: $($cpuInfo.Name) | Cores: $($cpuInfo.NumberOfCores) | Threads: $($cpuInfo.NumberOfLogicalProcessors)"

Write-Host "`n=== 1. GPU & VRAM Status ==="
try {
    nvidia-smi
} catch {
    Write-Host "nvidia-smi not available"
}

Write-Host "`n=== 2. System RAM Status ==="
$os = Get-CimInstance Win32_OperatingSystem
$totalRam = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
$freeRam = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
$usedRam = [math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / 1MB, 2)
$ramPercent = [math]::Round(($usedRam / $totalRam) * 100, 1)
[PSCustomObject]@{
    "Total RAM (GB)" = $totalRam
    "Used RAM (GB)"  = $usedRam
    "Free RAM (GB)"  = $freeRam
    "Usage (%)"      = $ramPercent
} | Format-Table -AutoSize

Write-Host "=== 3. Disk Usage ==="
Get-PSDrive -PSProvider FileSystem | Select-Object Root, @{N="Total (GB)";E={[math]::Round(($_.Free + $_.Used)/1GB, 2)}}, @{N="Free (GB)";E={[math]::Round($_.Free/1GB, 2)}}, @{N="Used (GB)";E={[math]::Round($_.Used/1GB, 2)}} | Format-Table -AutoSize

Write-Host "=== 4. Target Service Ports (18088 / 8080) ==="
$conns = Get-NetTCPConnection -LocalPort 18088, 8080 -State Listen -ErrorAction SilentlyContinue
if ($conns) {
    $conns | Select-Object LocalAddress, LocalPort, State, OwningProcess | Format-Table -AutoSize
} else {
    Write-Host "No services currently listening on ports 18088 or 8080."
}

Write-Host "`n=== 5. Relevant Running Processes ==="
$procs = Get-Process | Where-Object { $_.ProcessName -match "python|uvicorn|llama" }
if ($procs) {
    $procs | Select-Object Id, ProcessName, @{N="Memory (MB)";E={[math]::Round($_.WorkingSet64/1MB, 2)}} | Format-Table -AutoSize
} else {
    Write-Host "No active Python, Uvicorn, or llama-server processes found."
}

Write-Host "`n=== 6. Project Local Models & Binaries ==="
$models = Get-ChildItem -Path . -Filter *.gguf -Recurse -ErrorAction SilentlyContinue
if ($models) {
    $models | Select-Object Name, @{N="Size (GB)";E={[math]::Round($_.Length/1GB, 2)}}, FullName | Format-Table -AutoSize
} else {
    Write-Host "No .gguf models found in project directory."
}

$bins = Get-ChildItem -Path . -Filter *llama-server*.exe -Recurse -ErrorAction SilentlyContinue
if ($bins) {
    $bins | Select-Object Name, FullName | Format-Table -AutoSize
} else {
    Write-Host "No llama-server.exe found in project root."
}

