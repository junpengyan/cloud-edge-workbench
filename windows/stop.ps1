$ErrorActionPreference="Stop"
$file=Join-Path $PSScriptRoot "data\server-process.json"
if (-not(Test-Path -LiteralPath $file)) { Write-Host "没有本项目启动记录。";exit }
$record=Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
$process=Get-Process -Id $record.pid -ErrorAction SilentlyContinue
if (-not $process) { Write-Host "服务已停止。";exit }
if ($process.StartTime.ToUniversalTime().Ticks -ne $record.started) { throw "进程身份已变化，拒绝停止其他进程。" }
$info=Get-CimInstance Win32_Process -Filter "ProcessId=$($record.pid)"
if ($info.CommandLine -notmatch "server.py") { throw "进程命令不匹配，拒绝停止。" }
Stop-Process -Id $record.pid
Write-Host "已停止本项目端口 $($record.port)，原有项目不受影响。"
