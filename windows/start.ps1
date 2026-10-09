param([int]$Port = 18768, [string]$Python = "")
$ErrorActionPreference = "Stop"
$projectDir = $PSScriptRoot
if (-not $Python) {
    $localPython = Join-Path $projectDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $localPython) { $Python=$localPython }
    else { throw "尚未安装本项目运行环境，请先双击安装依赖.bat，完成后重新启动。" }
}
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
    if ($health.app -eq "unified-phase-one") { Write-Host "工作台已运行：http://127.0.0.1:$Port"; exit 0 }
    throw "端口 $Port 已被其他服务使用，请指定 -Port。"
} catch {
    if ($_.Exception.Message -like "端口*") { throw }
}
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback,$Port)
try { $listener.Start() } catch { throw "端口 $Port 被占用；不会停止已有服务。" } finally { $listener.Stop() }
$env:UNIFIED_PORT=[string]$Port
$env:PYTHONUTF8="1"
$env:PYTHONDONTWRITEBYTECODE="1"
$logDir=Join-Path $projectDir "data"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$proc=Start-Process -FilePath $Python -ArgumentList "server.py" -WorkingDirectory $projectDir -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir "server.log") -RedirectStandardError (Join-Path $logDir "server.err.log")
$record=@{pid=$proc.Id;port=$Port;started=$proc.StartTime.ToUniversalTime().Ticks;directory=$projectDir}
$record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logDir "server-process.json") -Encoding UTF8
for ($i=0;$i -lt 30;$i++) {
    Start-Sleep -Milliseconds 300
    try { $health=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2; if ($health.app -eq "unified-phase-one") { Write-Host "已启动：http://127.0.0.1:$Port"; exit 0 } } catch {}
    if ($proc.HasExited) { throw "服务未能启动，请查看本项目 data/server.err.log。" }
}
throw "服务启动超时，请查看本项目日志。"
