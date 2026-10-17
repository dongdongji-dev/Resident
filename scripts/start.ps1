#Requires -Version 5.1
# Resident 启动脚本（Windows）
# 按 data\install_mode.txt 自动分流（缺省 docker）：
#   docker：docker compose up -d
#   host  ：宿主 venv 启动 console（并确保沙箱容器运行）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\start.ps1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

function Info([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn([string]$m) { Write-Host "!! $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "错误：$m" -ForegroundColor Red; exit 1 }
function New-Utf8NoBom { return (New-Object System.Text.UTF8Encoding($false)) }

function Get-InstallMode {
    $f = Join-Path $Root 'data\install_mode.txt'
    if (Test-Path $f) {
        $m = (Get-Content -Raw $f).Trim()
        if ($m -eq 'host' -or $m -eq 'docker') { return $m }
    }
    return 'docker'
}

function Wait-Port([int]$Port, [int]$TimeoutSeconds) {
    for ($i = 0; $i -lt $TimeoutSeconds; $i++) {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $client.Connect('127.0.0.1', $Port)
            $client.Close()
            return $true
        } catch {
            $client.Close()
            Start-Sleep -Seconds 1
        }
    }
    return $false
}

function Test-DockerReady {
    & docker info *> $null
    return ($LASTEXITCODE -eq 0)
}

function Ensure-Docker {
    & docker --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Fail '未找到 docker。请先安装 Docker Desktop：https://www.docker.com/products/docker-desktop/'
    }
    if (-not (Test-DockerReady)) {
        $dd = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
        if (Test-Path $dd) {
            Info 'Docker 未运行，尝试启动 Docker Desktop…'
            Start-Process $dd | Out-Null
            $ok = $false
            for ($i = 0; $i -lt 60; $i++) {
                if (Test-DockerReady) { $ok = $true; break }
                Start-Sleep -Seconds 2
            }
            if (-not $ok) { Fail 'Docker Desktop 启动超时，请手动启动后重试' }
        } else {
            Fail 'Docker 未运行，请先启动 Docker Desktop 后重试'
        }
    }
    Info 'Docker 就绪'
}

function Pull-ImageFallback([string]$Official, [int]$TimeoutSec = 600) {
    & docker image inspect $Official *> $null
    if ($LASTEXITCODE -eq 0) { return }
    Info "拉取镜像：$Official"
    $log = Join-Path $env:TEMP ("resident-pull-" + [guid]::NewGuid().ToString('N') + '.out')
    $p = Start-Process -FilePath 'docker' -ArgumentList @('pull', $Official) `
        -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    if (-not $p.WaitForExit($TimeoutSec * 1000)) {
        try { $p.Kill() } catch { }
        Fail "拉取超时（${TimeoutSec}s）：$Official"
    }
    if ($p.ExitCode -ne 0) { Fail "拉取失败：$Official（见安装文档「代理」章节）" }
}

function Test-RegistryReachable {
    if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) { return $true }
    & curl.exe -sI -m 6 'https://ghcr.io/v2/' *> $null
    return ($LASTEXITCODE -eq 0)
}

function Try-PullImage([string]$Image, [int]$TimeoutSec = 300) {
    # 非致命拉取：成功 true / 失败 false（失败后调用方回退本地构建）
    $log = Join-Path $env:TEMP ("resident-pull-" + [guid]::NewGuid().ToString('N') + '.out')
    try {
        $p = Start-Process -FilePath 'docker' -ArgumentList @('pull', $Image) `
            -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    } catch { return $false }
    if (-not $p.WaitForExit($TimeoutSec * 1000)) {
        try { $p.Kill() } catch { }
        return $false
    }
    return ($p.ExitCode -eq 0)
}

function Ensure-Sandbox {
    & docker image inspect 'resident-sandbox:latest' *> $null
    if ($LASTEXITCODE -ne 0) {
        $official = 'ghcr.io/dongdongji-dev/resident-sandbox:latest'
        $got = $false
        if (Test-RegistryReachable) {
            Info '拉取官方沙箱镜像（ghcr.io）'
            $got = Try-PullImage $official
        }
        if ($got) {
            & docker tag $official 'resident-sandbox:latest'
        } else {
            Info '构建沙箱镜像 resident-sandbox:latest'
            Pull-ImageFallback 'python:3.12-slim'
            $dockerDir = Join-Path $Root 'docker'
            & docker build --build-arg BASE_IMAGE=python:3.12-slim `
                -t resident-sandbox:latest $dockerDir
            if ($LASTEXITCODE -ne 0) { Fail '构建沙箱镜像失败' }
        }
    }
    & docker container inspect 'resident-sandbox' *> $null
    if ($LASTEXITCODE -ne 0) {
        Info '创建沙箱容器 resident-sandbox'
        $ws = Join-Path $Root 'data\workspace'
        & docker create --name resident-sandbox --add-host host.docker.internal:host-gateway `
            -e http_proxy= -e https_proxy= -e HTTP_PROXY= -e HTTPS_PROXY= `
            -e all_proxy= -e ALL_PROXY= -e no_proxy= -e NO_PROXY= `
            -v "${ws}:/home/sandbox" -p 19090:19090 resident-sandbox:latest
        if ($LASTEXITCODE -ne 0) { Fail '创建沙箱容器失败' }
    }
    $running = (& docker inspect -f '{{.State.Running}}' resident-sandbox 2>$null)
    if ($running -ne 'true') { & docker start resident-sandbox | Out-Null }
    Info '沙箱容器运行中'
}

function Write-StartSummary([string]$Mode) {
    $token = ''
    if (Test-Path '.env') {
        foreach ($line in [System.IO.File]::ReadAllLines('.env', [System.Text.Encoding]::UTF8)) {
            if ($line -match '^RESIDENT_CONSOLE_TOKEN=(.*)$') { $token = $Matches[1].Trim(); break }
        }
    }
    Write-Host ''
    Write-Host 'Resident 运行信息：'
    Write-Host '  访问地址：http://127.0.0.1:18082/'
    if ($token) {
        if ($token -eq 'changeme') {
            Write-Host '  登录密钥：见 .env 的 RESIDENT_CONSOLE_TOKEN（changeme，待修改）'
        } else {
            Write-Host "  登录密钥：$token"
        }
    } else {
        Write-Host '  登录密钥：见 .env 的 RESIDENT_CONSOLE_TOKEN'
    }
    if ($Mode -eq 'host') { Write-Host '  日志：data\logs\console.log' }
    else { Write-Host '  日志：docker compose logs -f resident' }
    Write-Host '  停止：scripts\stop.ps1'
}

$mode = Get-InstallMode
Ensure-Docker

if ($mode -eq 'host') {
    $py = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path $py)) {
        Fail '未找到 .venv（先运行：scripts\install.ps1 -Mode host）'
    }
    Ensure-Sandbox

    $pidFile = Join-Path $Root 'data\logs\console.pid'
    if (Test-Path $pidFile) {
        $old = (Get-Content -Raw $pidFile).Trim()
        if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
            Info "console 已在运行（pid $old）"
            Write-StartSummary 'host'
            exit 0
        }
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $Root 'data\logs') | Out-Null
    $log = Join-Path $Root 'data\logs\console.log'
    $errLog = Join-Path $Root 'data\logs\console.err.log'
    # 子进程 stdio 固定 UTF-8（Windows 重定向默认 GBK，中文日志会写坏）
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $proc = Start-Process -FilePath $py `
        -ArgumentList @('-m', 'kernel.console', '--config', 'config\console.yaml') `
        -WorkingDirectory $Root -RedirectStandardOutput $log -RedirectStandardError $errLog `
        -PassThru -WindowStyle Hidden
    [System.IO.File]::WriteAllText($pidFile, "$($proc.Id)", (New-Utf8NoBom))
    Info '等待控制台就绪（18082）…'
    if (Wait-Port 18082 180) { Info '控制台已就绪：http://127.0.0.1:18082/' }
    else { Warn '等待超时，排查：data\logs\console.log' }
    Write-StartSummary 'host'
    exit 0
}

& docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail '当前 docker 没有 compose 插件，请升级 Docker Desktop' }
& docker compose up -d
if ($LASTEXITCODE -ne 0) { Fail '启动失败，排查：docker compose logs -f resident' }
Info '等待控制台就绪（18082）…'
if (Wait-Port 18082 180) { Info '控制台已就绪：http://127.0.0.1:18082/' }
else { Warn '等待超时，排查：docker compose logs -f resident' }
Write-StartSummary 'docker'
