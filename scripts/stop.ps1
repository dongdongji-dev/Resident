#Requires -Version 5.1
# Resident 停止脚本（Windows）
# 按 data\install_mode.txt 自动分流（缺省 docker）：
#   docker：docker compose stop（只停不删容器）
#   host  ：结束宿主 console / kernel 进程（.venv 的 python）
# 沙箱容器默认保留；NapCat 容器默认不动。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

function Info([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn([string]$m) { Write-Host "!! $m" -ForegroundColor Yellow }

function Get-InstallMode {
    $f = Join-Path $Root 'data\install_mode.txt'
    if (Test-Path $f) {
        $m = (Get-Content -Raw $f).Trim()
        if ($m -eq 'host' -or $m -eq 'docker') { return $m }
    }
    return 'docker'
}

$mode = Get-InstallMode

if ($mode -eq 'host') {
    $py = Join-Path $Root '.venv\Scripts\python.exe'
    $killed = $false
    if (Test-Path $py) {
        $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -eq $py }
        foreach ($proc in $procs) {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            $killed = $true
        }
    }
    $pidFile = Join-Path $Root 'data\logs\console.pid'
    if (Test-Path $pidFile) { Remove-Item $pidFile -ErrorAction SilentlyContinue }
    if ($killed) { Info 'Resident 已停止' } else { Info '未发现运行中的 console/kernel' }
    Write-Host '  （沙箱容器保留；如需停止：docker stop resident-sandbox）'
    exit 0
}

& docker --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host '未找到 docker（Docker Desktop 可能未安装），无需停止' -ForegroundColor Yellow
    exit 0
}

& docker compose stop
if ($LASTEXITCODE -eq 0) {
    Info 'Resident 已停止（容器保留）'
} else {
    Warn '停止失败，请查看上方输出'
    exit 1
}

& docker inspect -f '{{.State.Running}}' resident-napcat 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host '  （NapCat 容器仍在运行；如需停止：docker stop resident-napcat）'
}
