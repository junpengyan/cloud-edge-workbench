param([string]$Python = "")
$ErrorActionPreference = "Stop"
$projectDir = $PSScriptRoot
$pythonArgs = @()
if (-not $Python) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { $Python = $launcher.Source; $pythonArgs = @("-3") }
    else {
        $launcher = Get-Command python -ErrorAction SilentlyContinue
        if (-not $launcher) { throw "请先安装 Python 3.11 或 3.12，并勾选 Add Python to PATH。" }
        $Python = $launcher.Source
    }
}
& $Python @pythonArgs -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "需要 Python 3.10 及以上，建议使用 3.11 或 3.12。" }
$venvDir = Join-Path $projectDir ".venv"
& $Python @pythonArgs -m venv $venvDir
if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败，请检查 Python 安装及目录权限。" }
$localPython = Join-Path $venvDir "Scripts\python.exe"
& $localPython -m pip install -r (Join-Path $projectDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败，请检查网络或 pip 源配置后重试。" }
& $localPython -c "import fastapi, uvicorn, pydantic, httpx, requests, paho.mqtt.client, paramiko"
if ($LASTEXITCODE -ne 0) { throw "依赖校验失败，请查看上方错误。" }
Write-Host "安装完成。请双击启动工作台.bat，访问 http://127.0.0.1:18768/ 。"
