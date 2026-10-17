#Requires -Version 5.1
# Resident 安装脚本（Windows）。需要 Docker Desktop 在运行。
#
# 设计原则（与 macOS/Linux 的 install.sh 一致）：
#   · 幂等：可重复执行（已存在复用/跳过；交互项默认值取当前配置）
#   · 网络自适应：探测出口地区（大陆则 pip/npm 用国内源）；
#     Docker 镜像一律官方源（带超时；需代理见安装文档「代理」章节）
#   · 交互确认：扫码等环节会暂停等待
#
# 部署方式（都需要 Docker；agent 命令在沙箱容器内执行）：
#   1) docker 拉取远程镜像运行（未发布/拉取失败时自动本地构建）
#   2) host   宿主 Python（检查/下载 → venv → pip 安装依赖），Docker 承载沙箱与 NapCat
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 [参数]
#     -Mode docker|host          部署方式（默认交互询问）
#     -WithQQ / -NoQQ            是否安装 NapCat（QQ 适配器）
#     -NapcatConfig / -NapcatLater   NapCat 现在配置 / 稍后配置
#     -Cn / -NoCn                中国大陆镜像加速
#     -NoStart                   只安装不启动
#     -Yes                       非交互（全部默认值）
#     -DryRun                    只打印将执行的操作
#     -Help                      显示本帮助
[CmdletBinding()]
param(
    [ValidateSet('host', 'docker', '')]
    [string]$Mode = '',
    [switch]$WithQQ,
    [switch]$NoQQ,
    [switch]$NapcatConfig,
    [switch]$NapcatLater,
    [switch]$Cn,
    [switch]$NoCn,
    [switch]$NoStart,
    [switch]$Yes,
    [switch]$DryRun,
    [switch]$Help
)

$ErrorActionPreference = 'Continue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
$script:IsDry = $DryRun.IsPresent
$script:IsYes = $Yes.IsPresent
$script:CnSpecified = $PSBoundParameters.ContainsKey('Cn') -or $PSBoundParameters.ContainsKey('NoCn')
$script:UseCn = $false
$script:UvBin = ''

$CnPipIndex = 'https://mirrors.aliyun.com/pypi/simple/'
$CnNpmRegistry = 'https://registry.npmmirror.com'
$CnAptMirror = 'mirrors.aliyun.com'

if ($Help) {
    Get-Content -LiteralPath $MyInvocation.MyCommand.Path |
        Where-Object { $_ -match '^#' -and $_ -notmatch '^#Requires' } |
        ForEach-Object { $_ -replace '^# ?', '' }
    exit 0
}

function Info([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn([string]$m) { Write-Host "!! $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "错误：$m" -ForegroundColor Red; exit 1 }
function Step([string]$m) { Write-Host ""; Write-Host "── $m" -ForegroundColor Magenta }

function Invoke-Run {
    param([string]$Display, [scriptblock]$Action)
    if ($script:IsDry) { Write-Host "    [dry-run] $Display"; return }
    $global:LASTEXITCODE = 0
    & $Action
    if ($LASTEXITCODE -ne 0) { Fail "命令失败（exit $LASTEXITCODE）：$Display" }
}

function Invoke-Try {
    param([string]$Display, [scriptblock]$Action)
    if ($script:IsDry) { Write-Host "    [dry-run] $Display"; return $true }
    $global:LASTEXITCODE = 0
    & $Action
    return ($LASTEXITCODE -eq 0)
}

function New-Token {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    return ([Convert]::ToBase64String($bytes)).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-Utf8NoBom { return (New-Object System.Text.UTF8Encoding($false)) }

function Get-EnvVal([string]$File, [string]$Key) {
    if (-not (Test-Path -LiteralPath $File)) { return '' }
    foreach ($line in [System.IO.File]::ReadAllLines($File, [System.Text.Encoding]::UTF8)) {
        if ($line -match "^$([regex]::Escape($Key))=(.*)$") { return $Matches[1].Trim('"', "'") }
    }
    return ''
}

function Set-EnvVar([string]$File, [string]$Key, [string]$Value) {
    $lines = @()
    if (Test-Path -LiteralPath $File) {
        $lines = [System.IO.File]::ReadAllLines($File, [System.Text.Encoding]::UTF8)
    }
    $done = $false
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        if ($line -match "^$([regex]::Escape($Key))=") { $out.Add("$Key=$Value"); $done = $true }
        else { $out.Add($line) }
    }
    if (-not $done) { $out.Add("$Key=$Value") }
    [System.IO.File]::WriteAllLines($File, $out, (New-Utf8NoBom))
}

function Get-YamlTopVal([string]$File, [string]$Section, [string]$Key) {
    $cur = ''
    foreach ($line in [System.IO.File]::ReadAllLines($File, [System.Text.Encoding]::UTF8)) {
        if ($line -match '^[A-Za-z_][A-Za-z0-9_]*:') { $cur = ($line -split ':')[0] }
        if ($cur -eq $Section -and $line -match "^  $([regex]::Escape($Key)):(.*)$") {
            $v = $Matches[1]
            return ($v -replace '\s+#.*$', '').Trim()
        }
    }
    return ''
}

function Set-YamlTopKey([string]$File, [string]$Section, [string]$Key, [string]$Value) {
    $cur = ''
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in [System.IO.File]::ReadAllLines($File, [System.Text.Encoding]::UTF8)) {
        if ($line -match '^[A-Za-z_][A-Za-z0-9_]*:') { $cur = ($line -split ':')[0] }
        if ($cur -eq $Section -and $line -match "^  $([regex]::Escape($Key)):") {
            $out.Add("  ${Key}: $Value")
        } else { $out.Add($line) }
    }
    [System.IO.File]::WriteAllLines($File, $out, (New-Utf8NoBom))
}

function Confirm-Ask([string]$Prompt, [bool]$Default = $false) {
    if ($script:IsYes -or $script:IsDry) { return $Default }
    $suffix = if ($Default) { '[Y/n]' } else { '[y/N]' }
    $answer = Read-Host "$Prompt $suffix"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return ($answer -match '^[yY]')
}

function Ask-Value([string]$Prompt, [string]$Default, [bool]$AllowEmpty = $false) {
    if ($script:IsYes -or $script:IsDry) {
        if ($AllowEmpty -and $Default -eq '') { return '' }
        return $Default
    }
    $answer = Read-Host "$Prompt（当前/默认：$Default）"
    if ([string]::IsNullOrWhiteSpace($answer)) {
        if ($AllowEmpty) { return '' }
        return $Default
    }
    return $answer
}

function Pause-User([string]$Message) {
    # 醒目提示 + 必须输入 Y 才继续
    if ($script:IsYes -or $script:IsDry) { return }
    while ($true) {
        Write-Host ''
        Write-Host "[操作] $Message" -ForegroundColor Yellow
        $ans = Read-Host '完成后输入 Y 继续（Ctrl+C 退出）'
        if ($ans -match '^[yY]') { return }
        Write-Host '请输入 Y 确认。' -ForegroundColor Yellow
    }
}

function Wait-Port([int]$Port, [int]$TimeoutSeconds) {
    if ($script:IsDry) { return $true }
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

function Get-UrlDirect([string]$Url, [int]$TimeoutSec = 5) {
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Proxy = $null
        $req.Timeout = $TimeoutSec * 1000
        $resp = $req.GetResponse()
        $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $text = $reader.ReadToEnd()
        $reader.Close()
        $resp.Close()
        return $text
    } catch { return '' }
}

function Test-Region {
    $out = Get-UrlDirect 'https://www.cloudflare.com/cdn-cgi/trace'
    if ($out) { if ($out -match '(?m)^loc=CN$') { return 'cn' } else { return 'global' } }
    $out = Get-UrlDirect 'https://ipinfo.io/country'
    if ($out) { if ($out -match 'CN') { return 'cn' } else { return 'global' } }
    return 'unknown'
}

function Pull-Timeout([int]$TimeoutSec, [string]$Image, [switch]$Quiet) {
    # 带硬超时的 docker pull（避免坏网络把安装卡死）
    if ($script:IsDry) {
        if (-not $Quiet) { Write-Host "    [dry-run] docker pull $Image（超时 ${TimeoutSec}s）" }
        return $true
    }
    $log = Join-Path $env:TEMP ("resident-pull-" + [guid]::NewGuid().ToString('N') + '.out')
    $err = "$log.err"
    try {
        $p = Start-Process -FilePath 'docker' -ArgumentList @('pull', $Image) `
            -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError $err
    } catch {
        if (-not $Quiet) { Warn "拉取失败：$Image" }
        return $false
    }
    if (-not $p.WaitForExit($TimeoutSec * 1000)) {
        try { $p.Kill() } catch { }
        if (-not $Quiet) { Warn "拉取超时（${TimeoutSec}s）：$Image" }
        return $false
    }
    if ($p.ExitCode -eq 0) { return $true }
    if (-not $Quiet) {
        Warn "拉取失败：$Image"
        try {
            Get-Content -Tail 3 $err -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "    $_" }
        } catch { }
    }
    return $false
}

function Pull-ImageFallback([string]$Official, [int]$TimeoutSec = 600) {
    # Docker 镜像一律官方源（带超时；到不了 Docker Hub 时配置代理，见安装文档「代理」章节）
    & docker image inspect $Official *> $null
    if ($LASTEXITCODE -eq 0) { Info "镜像已存在：$Official"; return }
    if ($script:IsDry) {
        Write-Host "    [dry-run] docker pull $Official（超时 ${TimeoutSec}s）"
        return
    }
    Info "拉取镜像：$Official"
    if (-not (Pull-Timeout $TimeoutSec $Official)) {
        Fail "拉取失败：$Official（见安装文档「代理」章节）"
    }
}

function Test-RegistryReachable {
    # 6 秒探测官方镜像源（ghcr.io）；没有 curl 就假定可达（后面有超时兜底）
    if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) { return $true }
    & curl.exe -sI -m 6 'https://ghcr.io/v2/' *> $null
    return ($LASTEXITCODE -eq 0)
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

# ── 检查 ─────────────────────────────────────────────────────────────
function Assert-Docker {
    & docker --version *> $null
    if ($LASTEXITCODE -ne 0) { Fail '未找到 docker。请先安装 Docker Desktop：https://www.docker.com/products/docker-desktop/' }
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) { Fail 'Docker 未运行。请先启动 Docker Desktop 后重试' }
    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) { Fail '当前 docker 没有 compose 插件（需要 docker compose v2），请升级 Docker Desktop' }
}

# ── python helper 脚本（写入工作区，供容器内执行）────────────────────
function Write-InstallerHelpers {
    if (-not $script:IsDry) {
        New-Item -ItemType Directory -Force -Path 'data/workspace/.installer' | Out-Null
    }
    $inject = @'
"""把 OneBot11 WebSocket 服务器（端口 3001）注入 NapCat 配置（幂等）。"""
import json, os, sys, glob
cfg_dir = sys.argv[1]
token = os.environ.get("WS_TOKEN", "")
files = sorted(glob.glob(os.path.join(cfg_dir, "onebot11_*.json")),
               key=os.path.getmtime, reverse=True)
if not files:
    print("NO_CONFIG"); sys.exit(1)
path = files[0]
qq = os.path.basename(path)[len("onebot11_"):-len(".json")]
doc = json.load(open(path, encoding="utf-8"))
net = doc.setdefault("network", {})
servers = net.setdefault("websocketServers", [])
entry = next((e for e in servers if isinstance(e, dict) and e.get("port") == 3001), None)
if entry is None:
    entry = {"enable": True, "name": "resident", "host": "0.0.0.0", "port": 3001,
             "reportSelfMessage": True, "enableForcePushEvent": True,
             "messagePostFormat": "array", "token": token, "debug": False,
             "heartInterval": 30000}
    servers.append(entry)
elif token:
    entry["token"] = token
json.dump(doc, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"OK {os.path.basename(path)} qq={qq} token={'set' if entry.get('token') else 'empty'}")
'@
    $probe = @'
"""裸 WebSocket 握手验证 NapCat WS（url+token）。退出码 0=101 成功。"""
import base64, os, socket, sys
url = os.environ.get("WS_URL", "host.docker.internal:3001")
token = os.environ.get("WS_TOKEN", "")
host, _, port = url.partition(":")
port = int(port or 3001)
try:
    s = socket.create_connection((host, port), timeout=6)
except OSError as exc:
    print(f"CONNECT-FAIL {exc}"); sys.exit(1)
try:
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
           f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
           f"Authorization: Bearer {token}\r\n\r\n")
    s.sendall(req.encode())
    head = s.recv(4096).decode("utf-8", "replace").splitlines()
    first = head[0] if head else ""
    print(f"HTTP {first}")
    sys.exit(0 if " 101 " in first else 1)
finally:
    s.close()
'@
    if ($script:IsDry) {
        Write-Host '    [dry-run] 写入 data/workspace/.installer/{ws_inject.py,ws_probe.py}'
        return
    }
    [System.IO.File]::WriteAllText((Join-Path $Root 'data/workspace/.installer/ws_inject.py'), $inject, (New-Utf8NoBom))
    [System.IO.File]::WriteAllText((Join-Path $Root 'data/workspace/.installer/ws_probe.py'), $probe, (New-Utf8NoBom))
}

# ── 配置（.env / agent.yaml / console.yaml）──────────────────────────
function Invoke-EnvBootstrap {
    $cfg = 'config/agent.yaml'
    $consoleCfg = 'config/console.yaml'
    if (-not (Test-Path '.env')) {
        Invoke-Run 'copy .env.example .env' { Copy-Item '.env.example' '.env' }
        Info '已从 .env.example 生成 .env'
    }
    if (-not (Test-Path $cfg)) { Fail "缺少 $cfg" }
    if (-not (Test-Path $consoleCfg)) { Fail "缺少 $consoleCfg" }

    if ($script:UseCn) {
        Set-EnvVar '.env' 'RESIDENT_NPM_REGISTRY' $CnNpmRegistry
        Set-EnvVar '.env' 'RESIDENT_PIP_INDEX_URL' $CnPipIndex
        Set-EnvVar '.env' 'RESIDENT_APT_MIRROR' $CnAptMirror
    } else {
        if ((Get-EnvVal '.env' 'RESIDENT_NPM_REGISTRY')) { Set-EnvVar '.env' 'RESIDENT_NPM_REGISTRY' '' }
        if ((Get-EnvVal '.env' 'RESIDENT_PIP_INDEX_URL')) { Set-EnvVar '.env' 'RESIDENT_PIP_INDEX_URL' '' }
        if ((Get-EnvVal '.env' 'RESIDENT_APT_MIRROR')) { Set-EnvVar '.env' 'RESIDENT_APT_MIRROR' '' }
    }

    # 模型
    Write-Host ''
    Write-Host '  模型配置'
    Write-Host '    协议：1) openai-compatible（最常用）  2) anthropic  3) openai(responses)'
    $curProto = Get-YamlTopVal $cfg 'llm' 'protocol'
    $protoDefault = if ($curProto -eq 'anthropic') { '2' } elseif ($curProto -eq 'openai') { '3' } else { '1' }
    $protoChoice = Ask-Value '选择协议 1/2/3' $protoDefault
    $protocol = switch ($protoChoice) { '2' { 'anthropic' } '3' { 'openai' } default { 'openai-compatible' } }
    if ($protocol -eq 'openai-compatible') {
        Write-Host '    variant：1) generic  2) deepseek  3) qwen  4) glm  5) openai'
        $curVar = Get-YamlTopVal $cfg 'llm' 'variant'
        $varDefault = switch ($curVar) { 'deepseek' { '2' } 'qwen' { '3' } 'glm' { '4' } 'openai' { '5' } default { '1' } }
        $varChoice = Ask-Value '选择 variant 1-5' $varDefault
        $variant = switch ($varChoice) { '2' { 'deepseek' } '3' { 'qwen' } '4' { 'glm' } '5' { 'openai' } default { 'generic' } }
        Set-YamlTopKey $cfg 'llm' 'variant' $variant
    }
    Set-YamlTopKey $cfg 'llm' 'protocol' $protocol
    $baseUrl = Ask-Value 'LLM_BASE_URL' (Get-EnvVal '.env' 'LLM_BASE_URL')
    Set-EnvVar '.env' 'LLM_BASE_URL' $baseUrl
    $apiKey = Ask-Value 'LLM_API_KEY ' '（保持当前）'
    if ($apiKey -ne '（保持当前）') { Set-EnvVar '.env' 'LLM_API_KEY' $apiKey }
    $model = Ask-Value 'LLM_MODEL  ' (Get-EnvVal '.env' 'LLM_MODEL')
    Set-EnvVar '.env' 'LLM_MODEL' $model
    # 占位值醒目提醒（不阻塞安装，但明确告知系统将不可用）
    $llmUrlNow = Get-EnvVal '.env' 'LLM_BASE_URL'
    if ([string]::IsNullOrEmpty($llmUrlNow) -or $llmUrlNow -match 'your-llm|changeme') {
        Write-Host '  [注意] LLM_BASE_URL 仍是占位值，安装后无法调用模型（在 .env 填写）' -ForegroundColor Yellow
    }

    # 图床
    Write-Host ''
    Write-Host '  图床（读图）'
    $imgDefault = (Get-YamlTopVal $cfg 'imgbed' 'enabled') -eq 'true'
    if (Confirm-Ask '启用图床模式？（读图上传图床；否则本地 base64）' $imgDefault) {
        $imgUrl = Ask-Value '图床上传地址 upload_url' (Get-YamlTopVal $cfg 'imgbed' 'upload_url')
        $imgKey = Ask-Value '图床 API Key' '（保持当前）'
        if ($imgKey -ne '（保持当前）') { Set-EnvVar '.env' 'IMGBED_API_KEY' $imgKey }
        Set-YamlTopKey $cfg 'imgbed' 'enabled' 'true'
        Set-YamlTopKey $cfg 'imgbed' 'upload_url' $imgUrl
        Set-YamlTopKey $cfg 'readimage' 'mode' 'imgbed'
    } else {
        Set-YamlTopKey $cfg 'imgbed' 'enabled' 'false'
        Set-YamlTopKey $cfg 'readimage' 'mode' 'local'
    }

    # 邮箱
    Write-Host ''
    Write-Host '  邮箱'
    $mailDefault = (Get-YamlTopVal $cfg 'email' 'enabled') -eq 'true'
    if (Confirm-Ask '为 agent 设置邮箱？' $mailDefault) {
        $addr = Ask-Value '邮箱地址' (Get-YamlTopVal $cfg 'email' 'username')
        $pw = Ask-Value '邮箱授权码（IMAP/SMTP 同一个）' '（保持当前）'
        Set-YamlTopKey $cfg 'email' 'enabled' 'true'
        Set-YamlTopKey $cfg 'email' 'username' $addr
        if ($pw -ne '（保持当前）') {
            Set-EnvVar '.env' 'EMAIL_IMAP_PASSWORD' $pw
            Set-EnvVar '.env' 'EMAIL_SMTP_PASSWORD' $pw
        }
    } else {
        Set-YamlTopKey $cfg 'email' 'enabled' 'false'
    }

    # 控制台监听
    Write-Host ''
    Write-Host '  控制台监听'
    $lanDefault = ((Get-YamlTopVal $consoleCfg 'console' 'host') -eq '0.0.0.0') -or
                  ((Get-EnvVal '.env' 'RESIDENT_CONSOLE_BIND') -eq '0.0.0.0')
    if (Confirm-Ask '控制台对局域网开放？（是=局域网可访问；否=仅本机）' $lanDefault) {
        Set-YamlTopKey $consoleCfg 'console' 'host' '0.0.0.0'
        Set-EnvVar '.env' 'RESIDENT_CONSOLE_BIND' '0.0.0.0'
        Write-Host '    已开放（/api/* 仍需 token）'
    } else {
        Set-YamlTopKey $consoleCfg 'console' 'host' '127.0.0.1'
        Set-EnvVar '.env' 'RESIDENT_CONSOLE_BIND' '127.0.0.1'
    }

    # token
    Write-Host ''
    Write-Host '  鉴权 token'
    foreach ($key in @('RESIDENT_API_KEY', 'RESIDENT_CONSOLE_TOKEN')) {
        $value = Get-EnvVal '.env' $key
        if ([string]::IsNullOrEmpty($value) -or $value -eq 'changeme') {
            $value = New-Token
            if ($script:IsDry) { Write-Host "    [dry-run] 生成 $key" }
            else { Set-EnvVar '.env' $key $value; Info "已生成 $key" }
        } else {
            $input = Ask-Value $key '（保持当前）'
            if ($input -ne '（保持当前）') { Set-EnvVar '.env' $key $input }
        }
    }

    # 工作区执行模式固定沙箱
    if ((Get-YamlTopVal $cfg 'workspace_tools' 'mode') -ne 'sandbox') {
        Warn 'workspace_tools.mode 非 sandbox（当前版本仅支持沙箱），已修正'
        Set-YamlTopKey $cfg 'workspace_tools' 'mode' 'sandbox'
    }
}

function Invoke-InitWorkspace {
    Info '创建工作区 data/workspace'
    if (-not $script:IsDry) {
        New-Item -ItemType Directory -Force -Path 'data/workspace/.skills', 'data/workspace/.secrets' | Out-Null
    }
}

function Invoke-SeedSkill([string]$Name) {
    if (-not (Test-Path "skills/$Name")) { Warn "仓库缺少 skills/$Name，跳过"; return }
    if (Test-Path "data/workspace/.skills/$Name") { Info "skill 已存在，跳过：$Name"; return }
    Invoke-Run "复制 skills/$Name" { Copy-Item -Recurse "skills/$Name" "data/workspace/.skills/$Name" }
    Info "写入 skill：$Name"
}

# ── NapCat ───────────────────────────────────────────────────────────
function Invoke-NapcatInstall {
    Step '安装 NapCat'
    Invoke-SeedSkill 'qq_adapter'
    Pull-ImageFallback 'mlikiowa/napcat-docker:latest' 1800
    if (-not $script:IsDry) { New-Item -ItemType Directory -Force -Path 'data/napcat/config', 'data/napcat/QQ' | Out-Null }
    & docker container inspect resident-napcat *> $null
    if ($LASTEXITCODE -ne 0) {
        Info '创建 NapCat 容器（WS 3001 / WebUI 6099）'
        $napcatDir = (Join-Path $Root 'data/napcat')
        Invoke-Run 'docker create resident-napcat' {
            docker create --name resident-napcat -p 3001:3001 -p 6099:6099 `
                -e http_proxy= -e https_proxy= -e HTTP_PROXY= -e HTTPS_PROXY= `
                -e all_proxy= -e ALL_PROXY= -e no_proxy= -e NO_PROXY= `
                -v "${napcatDir}\config:/app/napcat/config" `
                -v "${napcatDir}\QQ:/app/.config/QQ" `
                --restart unless-stopped mlikiowa/napcat-docker:latest
        }
    }
    $running = (& docker inspect -f '{{.State.Running}}' resident-napcat 2>$null)
    if ($running -ne 'true') { Invoke-Run 'docker start resident-napcat' { docker start resident-napcat } }
    Info 'NapCat 容器运行中'
}

function Invoke-NapcatConfigure {
    Step '配置 NapCat'
    Write-InstallerHelpers
    Write-Host ''
    Write-Host '  QQ 登录：'
    Write-Host '    1. 查看二维码：docker logs -f resident-napcat（或 WebUI：http://127.0.0.1:6099）'
    Write-Host '    2. 手机 QQ 扫码登录'
    Write-Host ''
    Pause-User '完成 QQ 扫码登录'
    if ($script:IsDry) {
        Write-Host '    [dry-run] 轮询 onebot11_*.json → 停容器 → 注入 WS(3001) → 启容器'
        Write-Host '    [dry-run] 写 .secrets/napcat_token.txt、core_api_key.txt、local.yaml'
        Write-Host '    [dry-run] docker exec 沙箱 ws_probe.py（存活尝试）'
        return $true
    }
    Info '检测登录配置…'
    $found = ''
    for ($i = 0; $i -lt 60; $i++) {
        $found = Get-ChildItem 'data/napcat/config' -Filter 'onebot11_*.json' -ErrorAction SilentlyContinue |
                 Select-Object -First 1
        if ($found) { break }
        Start-Sleep -Seconds 2
    }
    if (-not $found) {
        Warn '未检测到登录配置（onebot11_*.json）'
        Warn '确认扫码成功后重跑：scripts\install.ps1 -WithQQ'
        return $false
    }
    Info "检测到配置：$($found.Name)"
    $existingToken = ''
    try {
        $doc = Get-Content -Raw -Encoding UTF8 $found.FullName | ConvertFrom-Json
        $entry = $doc.network.websocketServers | Where-Object { $_.port -eq 3001 } | Select-Object -First 1
        if ($entry) { $existingToken = "$($entry.token)" }
    } catch { }
    $defaultToken = if ($existingToken) { $existingToken } else { '（自动生成）' }
    $token = Ask-Value '设置 WebSocket Token（留空自动生成）' $defaultToken $true
    if (-not $token -or $token -eq '（自动生成）') {
        if ($existingToken) { $token = $existingToken } else { $token = New-Token }
    }
    Info '停止 NapCat，写入 WS 配置'
    Invoke-Run 'docker stop resident-napcat' { docker stop resident-napcat | Out-Null }
    Info '注入 WebSocket 服务器（端口 3001）'
    $instDir = (Join-Path $Root 'data/workspace/.installer')
    $napcatCfgDir = (Join-Path $Root 'data/napcat/config')
    $ok = Invoke-Try 'WS 注入' {
        docker run --rm -v "${instDir}:/inst:ro" -v "${napcatCfgDir}:/cfg" `
            -e "WS_TOKEN=$token" --entrypoint python3 resident-sandbox:latest /inst/ws_inject.py /cfg
    }
    if (-not $ok) { Warn 'WS 注入失败（检查 NapCat 配置目录）'; return $false }
    Invoke-Run 'docker start resident-napcat' { docker start resident-napcat | Out-Null }
    Info '等待 WS 端口 3001…'
    Wait-Port 3001 60 | Out-Null
    # 存活检查要沙箱容器在跑：先启动服务（幂等；最后的启动步会是空操作）
    $sandboxRunning = ((& docker inspect -f '{{.State.Running}}' resident-sandbox 2>$null) -eq 'true')
    if (-not $sandboxRunning) {
        Info '启动服务进行沙箱存活检查'
        & docker compose up -d
        if ($LASTEXITCODE -ne 0) {
            Warn '启动失败，无法存活检查。重跑：scripts\install.ps1 -WithQQ'
            return $false
        }
        for ($j = 0; $j -lt 60; $j++) {
            $ready = ((& docker inspect -f '{{.State.Running}}' resident-sandbox 2>$null) -eq 'true')
            if ($ready) { break }
            Start-Sleep -Seconds 1
        }
    }
    New-Item -ItemType Directory -Force -Path 'data/workspace/.secrets' | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $Root 'data/workspace/.secrets/napcat_token.txt'), ($token + "`n"), (New-Utf8NoBom))
    $coreKey = Get-EnvVal '.env' 'RESIDENT_API_KEY'
    [System.IO.File]::WriteAllText((Join-Path $Root 'data/workspace/.secrets/core_api_key.txt'), ($coreKey + "`n"), (New-Utf8NoBom))
    New-Item -ItemType Directory -Force -Path 'data/workspace/.skills/qq_adapter/config' | Out-Null
    $coreUrl = 'http://host.docker.internal:18086'
    if ($Mode -eq 'docker') { $coreUrl = 'http://127.0.0.1:18086' }
    $localYaml = @"
# 本机覆盖（由 scripts/install.ps1 生成；通用默认见 config/default.yaml）
napcat:
  ws_url: ws://host.docker.internal:3001
core:
  url: $coreUrl
"@
    [System.IO.File]::WriteAllText((Join-Path $Root 'data/workspace/.skills/qq_adapter/config/local.yaml'), $localYaml, (New-Utf8NoBom))
    $sandboxRunning = ((& docker inspect -f '{{.State.Running}}' resident-sandbox 2>$null) -eq 'true')
    if ($sandboxRunning) {
        Info '沙箱内存活检查'
        $probeOk = Invoke-Try 'WS 存活探测' {
            docker exec -e 'WS_URL=host.docker.internal:3001' -e "WS_TOKEN=$token" `
                resident-sandbox python3 /home/sandbox/.installer/ws_probe.py
        }
        if ($probeOk) { Info 'NapCat WS 存活检查通过'; return $true }
        Warn '存活检查失败（token 或网络）。重跑：scripts\install.ps1 -WithQQ'
        return $false
    }
    Warn '沙箱未运行，跳过存活检查（重跑 -WithQQ 补检）'
    return $false
}

# ── 收尾输出 ─────────────────────────────────────────────────────────
function Write-Summary([bool]$NoStart) {
    $cfg = 'config/agent.yaml'
    $llmPh = $false
    $u = Get-EnvVal '.env' 'LLM_BASE_URL'
    $k = Get-EnvVal '.env' 'LLM_API_KEY'
    $m = Get-EnvVal '.env' 'LLM_MODEL'
    if ([string]::IsNullOrEmpty($u) -or $u -match 'your-llm|changeme') { $llmPh = $true }
    if ($k -eq 'changeme') { $llmPh = $true }
    if ([string]::IsNullOrEmpty($m) -or $m -match 'your-model|changeme') { $llmPh = $true }
    Write-Host ''
    if ($NoStart) { Info '安装完成（未启动，-NoStart）' } else { Info '安装完成' }
    Write-Host '  ─────────────────────────────────────────────'
    Write-Host '  访问地址：http://127.0.0.1:18082/'
    Write-Host "  登录密钥 RESIDENT_CONSOLE_TOKEN = $(Get-EnvVal '.env' 'RESIDENT_CONSOLE_TOKEN')"
    Write-Host "  内部密钥 RESIDENT_API_KEY       = $(Get-EnvVal '.env' 'RESIDENT_API_KEY')"
    Write-Host "  模型：protocol=$(Get-YamlTopVal $cfg 'llm' 'protocol') variant=$(Get-YamlTopVal $cfg 'llm' 'variant') model=$(Get-EnvVal '.env' 'LLM_MODEL')"
    Write-Host "  LLM_BASE_URL = $(Get-EnvVal '.env' 'LLM_BASE_URL')"
    $napcatTokenFile = 'data/workspace/.secrets/napcat_token.txt'
    if (Test-Path $napcatTokenFile) {
        Write-Host "  NapCat WS Token = $((Get-Content -Raw $napcatTokenFile).Trim())"
        Write-Host '  NapCat WS：宿主 ws://127.0.0.1:3001 / 沙箱 ws://host.docker.internal:3001'
        Write-Host '  NapCat WebUI：http://127.0.0.1:6099'
    }
    if ($llmPh) {
        Write-Host '  ─────────────────────────────────────────────'
        Write-Host '  [注意] LLM 配置仍是占位值，系统无法调用模型' -ForegroundColor Yellow
        Write-Host '     编辑 .env 的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 后重启：' -ForegroundColor Yellow
        Write-Host '     前端「重启 Kernel」按钮（或 scripts\stop.ps1 ; scripts\start.ps1）' -ForegroundColor Yellow
    }
    Write-Host '  ─────────────────────────────────────────────'
    Write-Host '  运维：'
    Write-Host '    启动/停止：scripts\start.ps1 / scripts\stop.ps1'
    Write-Host '    日志：docker compose logs -f resident'
    Write-Host '    改模型：.env 的 LLM_*，改后重启'
    Write-Host '    改监听：.env RESIDENT_CONSOLE_BIND（改后 start）'
    Write-Host '    改图床：config/agent.yaml imgbed + .env IMGBED_API_KEY；readimage.mode'
    Write-Host '    改邮箱：config/agent.yaml email + .env EMAIL_IMAP/SMTP_PASSWORD'
    Write-Host '    改人设：config/prompt/user/persona.md（自定义系统提示词，下一轮生效）'
    Write-Host '    加技能：目录拷入 data/workspace/.skills/'
}

# ── 模式与宿主安装（Windows host 模式） ──────────────────────────────
function Get-InstallMode {
    $f = Join-Path $Root 'data\install_mode.txt'
    if (Test-Path $f) {
        $m = (Get-Content -Raw $f).Trim()
        if ($m -eq 'host' -or $m -eq 'docker') { return $m }
    }
    return 'docker'
}

function Set-InstallMode([string]$M) {
    if ($script:IsDry) { return }
    New-Item -ItemType Directory -Force -Path (Join-Path $Root 'data') | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $Root 'data\install_mode.txt'), $M, (New-Utf8NoBom))
}

function Resolve-Python {
    foreach ($cand in @('python', 'python3')) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        & $cand -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { return $cmd.Source }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @('-3.13', '-3.12', '-3.11', '-3.10')) {
            $exe = (& py $v -c 'import sys; print(sys.executable)' 2>$null)
            if ($LASTEXITCODE -eq 0 -and $exe) { return "$exe".Trim() }
        }
    }
    return ''
}

function Install-UvPython {
    $uv = 'uv'
    if (-not (Get-Command uv -ErrorAction SilentlyContinue) -and
        -not (Test-Path "$env:USERPROFILE\.local\bin\uv.exe")) {
        Info '安装 uv'
        try { Invoke-RestMethod 'https://astral.sh/uv/install.ps1' | Invoke-Expression }
        catch { return $false }
    }
    if (Test-Path "$env:USERPROFILE\.local\bin\uv.exe") { $uv = "$env:USERPROFILE\.local\bin\uv.exe" }
    Info '获取 Python 3.12'
    & $uv python install 3.12 *> $null
    if ($LASTEXITCODE -ne 0) { return $false }
    $script:UvBin = $uv
    return $true
}

function Ensure-Sandbox {
    & docker image inspect 'resident-sandbox:latest' *> $null
    if ($LASTEXITCODE -ne 0) {
        $official = 'ghcr.io/dongdongji-dev/resident-sandbox:latest'
        $got = $false
        if ($script:IsDry) {
            Write-Host "    [dry-run] 拉取官方沙箱镜像 $official（失败则本地构建）"
            $got = $true
        } elseif (Test-RegistryReachable) {
            Info '拉取官方沙箱镜像（ghcr.io）'
            $got = Pull-Timeout 300 $official -Quiet
        }
        if ($got) {
            Invoke-Run '标记沙箱镜像' { docker tag $official 'resident-sandbox:latest' }
        } else {
            Info '构建沙箱镜像 resident-sandbox:latest'
            Pull-ImageFallback 'python:3.12-slim'
            $dockerDir = Join-Path $Root 'docker'
            $aptMirror = Get-EnvVal '.env' 'RESIDENT_APT_MIRROR'
            Invoke-Run 'docker build 沙箱镜像' {
                docker build --build-arg BASE_IMAGE=python:3.12-slim `
                    --build-arg APT_MIRROR=$aptMirror `
                    --build-arg http_proxy= --build-arg https_proxy= `
                    --build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= `
                    -t resident-sandbox:latest $dockerDir
            }
        }
    }
    & docker container inspect 'resident-sandbox' *> $null
    if ($LASTEXITCODE -ne 0) {
        Info '创建沙箱容器 resident-sandbox'
        $ws = Join-Path $Root 'data\workspace'
        Invoke-Run 'docker create 沙箱容器' {
            docker create --name resident-sandbox --add-host host.docker.internal:host-gateway `
                -e http_proxy= -e https_proxy= -e HTTP_PROXY= -e HTTPS_PROXY= `
                -e all_proxy= -e ALL_PROXY= -e no_proxy= -e NO_PROXY= `
                -v "${ws}:/home/sandbox" -p 19090:19090 resident-sandbox:latest
        }
    }
    $running = (& docker inspect -f '{{.State.Running}}' resident-sandbox 2>$null)
    if ($running -ne 'true') { Invoke-Run '启动沙箱' { docker start resident-sandbox | Out-Null } }
    Info '沙箱容器运行中'
}

function Invoke-HostInstall {
    Step '安装依赖（宿主 Python）'
    if ($script:IsDry) {
        Write-Host '    [dry-run] 检查/获取 Python → venv → pip install -r requirements.txt'
        Write-Host '    [dry-run] 写入 .env 的 RESIDENT_PYTHON'
        Write-Host '    [dry-run] 构建前端（npm 或 Docker 构建）'
        Write-Host '    [dry-run] 准备沙箱（镜像 + 容器）'
        return
    }
    $python = Resolve-Python
    if (-not $python) {
        if (-not (Confirm-Ask '未找到 Python>=3.10。用 uv 自动获取一个隔离的 Python？' $true)) {
            Fail '请安装 Python 3.10+ 后重跑：https://www.python.org/downloads/'
        }
        if (-not (Install-UvPython)) {
            Fail 'uv 获取 Python 失败。请手动安装 Python 3.10+ 后重跑'
        }
    }
    if (-not (Test-Path (Join-Path $Root '.venv\Scripts\python.exe'))) {
        Info '创建虚拟环境 .venv'
        if ($script:UvBin) {
            Invoke-Run 'uv venv' { & $script:UvBin venv --seed --python 3.12 .venv }
        } else {
            Invoke-Run 'python -m venv' { & $python -m venv .venv }
        }
    } else {
        Info '复用 .venv'
    }
    $vpy = Join-Path $Root '.venv\Scripts\python.exe'
    Info '安装 Python 依赖'
    if ($script:UseCn) { $env:PIP_INDEX_URL = $CnPipIndex }
    $env:PYTHONUTF8 = '1'
    try {
        Invoke-Run 'pip install' { & $vpy -m pip install -r (Join-Path $Root 'requirements.txt') }
    } finally {
        Remove-Item Env:\PIP_INDEX_URL -ErrorAction SilentlyContinue
        Remove-Item Env:\PYTHONUTF8 -ErrorAction SilentlyContinue
    }
    Invoke-Run '写入 .env 的 RESIDENT_PYTHON' { Set-EnvVar '.env' 'RESIDENT_PYTHON' '.venv\Scripts\python.exe' }

    Step '构建前端'
    if (Test-Path 'src/kernel/console/frontend/dist/index.html') {
        Info '跳过前端构建（产物已存在）'
    } else {
        & npm --version *> $null
        if ($LASTEXITCODE -eq 0) {
            Push-Location (Join-Path $Root 'frontend')
            try {
                if ($script:UseCn) { Invoke-Run 'npm ci' { npm ci --registry $CnNpmRegistry } }
                else { Invoke-Run 'npm ci' { npm ci } }
                Invoke-Run 'npm run build' { npm run build }
            } finally { Pop-Location }
        } else {
            Info '无 Node：用 Docker 构建前端'
            Pull-ImageFallback 'node:20-slim'
            $npmReg = ''
            if ($script:UseCn) { $npmReg = "--registry=$CnNpmRegistry" }
            Invoke-Run 'docker 构建前端' {
                docker run --rm -v "${Root}:/build" -w /build node:20-slim `
                    bash -c "cd frontend && npm ci $npmReg && npm run build"
            }
        }
    }

    Step '准备沙箱'
    Ensure-Sandbox
    Set-InstallMode 'host'
}

# ── 主流程 ───────────────────────────────────────────────────────────
if ($WithQQ -and $NoQQ) { Fail '-WithQQ 与 -NoQQ 不能同时使用' }
if ($NapcatConfig -and $NapcatLater) { Fail '-NapcatConfig 与 -NapcatLater 不能同时使用' }
if ($DryRun) { $script:IsYes = $true }

Write-Host 'Resident 安装'

Step '环境'
Write-Host "    OS      : $([System.Environment]::OSVersion.VersionString)"
Write-Host "    Docker  : $(& docker --version 2>$null)"
Step '前置检查'
Assert-Docker
Info 'Docker Desktop 就绪'

if (-not $Mode) {
    if ($script:IsYes -or $script:IsDry) {
        $Mode = 'docker'
    } else {
        Write-Host '  部署方式：'
        Write-Host '    1) docker 拉取远程镜像运行（未发布时自动本地构建）'
        Write-Host '    2) host   宿主 Python（venv + pip），Docker 承载沙箱'
        $mc = Read-Host '  选择 [1/2，默认 1]'
        if ($mc -eq '2' -or $mc -eq 'host') { $Mode = 'host' } else { $Mode = 'docker' }
    }
}
Info "部署方式：$Mode"
Set-InstallMode $Mode

Step '网络检测'
if ($script:CnSpecified) {
    $script:UseCn = $Cn.IsPresent
} elseif ($script:IsYes -or $script:IsDry) {
    $script:UseCn = ((Test-Region) -eq 'cn')
} else {
    $region = Test-Region
    if ($region -eq 'cn') { Info '检测到大陆网络出口'; $script:UseCn = $true }
    elseif ($region -eq 'global') { Info '检测到非大陆网络出口'; $script:UseCn = $false }
    else { Warn '出口检测失败'; $script:UseCn = Confirm-Ask '按大陆网络处理（使用国内镜像）？' $false }
}
if ($script:UseCn) { Info '网络判定：大陆（pip/npm 使用国内源）' } else { Info '网络判定：非大陆' }

Step '镜像拉取：官方源'

Step '配置'
Invoke-EnvBootstrap

Step '工作区与技能'
Invoke-InitWorkspace
Invoke-SeedSkill 'resident_dev'
Invoke-SeedSkill 'subagent'

# subagent notice 回推地址：host 模式（Windows/Docker Desktop）沙箱为桥接
# 容器，127.0.0.1 打不到宿主 kernel，写 local.yaml 覆盖为 host.docker.internal；
# docker 模式（共享网络命名空间）默认 127.0.0.1 即可，不写。
$subLocal = Join-Path $Root 'data/workspace/.skills/subagent/config/local.yaml'
if ($Mode -eq 'host' -and -not (Test-Path $subLocal)) {
    Invoke-Run '写入 subagent local.yaml' {
        $subDir = Join-Path $Root 'data/workspace/.skills/subagent/config'
        New-Item -ItemType Directory -Force -Path $subDir | Out-Null
        [System.IO.File]::WriteAllText($subLocal,
            "# 本机覆盖（由安装脚本生成；通用默认见 config/default.yaml）`nnotice:`n  url: http://host.docker.internal:18086/api/notice`n",
            (New-Utf8NoBom))
    }
    Info '已为 subagent 写入 local.yaml（host.docker.internal）'
}

if ($Mode -eq 'host') {
    Invoke-HostInstall
} else {
    Invoke-Run '写入 .env 的 RESIDENT_PYTHON' { Set-EnvVar '.env' 'RESIDENT_PYTHON' '.venv/bin/python' }
    # 老 .env 没有镜像源键时补默认官方地址（不覆盖已有值）
    $officialApp = 'ghcr.io/dongdongji-dev/resident-app:latest'
    $officialSandbox = 'ghcr.io/dongdongji-dev/resident-sandbox:latest'
    if (-not (Get-EnvVal '.env' 'RESIDENT_APP_IMAGE')) {
        Invoke-Run '写入 .env 的镜像来源' { Set-EnvVar '.env' 'RESIDENT_APP_IMAGE' $officialApp }
    }
    if (-not (Get-EnvVal '.env' 'RESIDENT_SANDBOX_IMAGE')) {
        Invoke-Run '写入 .env 的镜像来源' { Set-EnvVar '.env' 'RESIDENT_SANDBOX_IMAGE' $officialSandbox }
    }
    Step '拉取远程镜像'
    $pulled = $false
    if ($script:IsDry) {
        Write-Host '    [dry-run] docker compose pull（失败则本地构建）'
        $pulled = $true
    } elseif (-not (Test-RegistryReachable)) {
        Warn '官方镜像源 ghcr.io 不可达，跳过拉取直接本地构建'
    } else {
        $log = Join-Path $env:TEMP ("resident-compose-pull-" + [guid]::NewGuid().ToString('N') + '.out')
        $p = Start-Process -FilePath 'docker' -ArgumentList @('compose', 'pull') `
            -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err"
        if ($p.WaitForExit(300 * 1000)) {
            if ($p.ExitCode -eq 0) { $pulled = $true }
        } else {
            try { $p.Kill() } catch { }
            Warn '拉取远程镜像超时（300s）'
        }
    }
    if ($pulled) {
        Info '远程镜像就绪'
    } else {
        Warn '远程镜像不可用，改为本地构建'
        Step '预拉基础镜像'
        Pull-ImageFallback 'node:20-slim'
        Pull-ImageFallback 'docker:27-cli'
        Pull-ImageFallback 'python:3.12-slim'
        Step '构建镜像'
        Warn '首次构建需要拉取依赖，可能较久'
        if (-not ($script:IsDry)) {
            docker compose build
            if ($LASTEXITCODE -ne 0) { Fail '安装失败：镜像构建未通过' }
        } else {
            Write-Host '    [dry-run] docker compose build'
        }
    }
}

# NapCat
$napcatConfigured = $false
$napcatOk = $true
$doQQ = $false
if ($WithQQ) { $doQQ = $true }
elseif ($NoQQ) { $doQQ = $false }
elseif ($script:IsYes -or $script:IsDry) { $doQQ = (Test-Path 'data/workspace/.skills/qq_adapter') }
else { $doQQ = Confirm-Ask '是否安装 NapCat（QQ 适配器，含容器）？' (Test-Path 'data/workspace/.skills/qq_adapter') }

if ($doQQ) {
    Invoke-NapcatInstall
    $doCfg = $false
    if ($NapcatConfig) { $doCfg = $true }
    elseif ($NapcatLater) { $doCfg = $false }
    elseif ($script:IsYes -or $script:IsDry) {
        $doCfg = [bool](Get-ChildItem 'data/napcat/config' -Filter 'onebot11_*.json' -ErrorAction SilentlyContinue)
    } else { $doCfg = Confirm-Ask '现在配置 NapCat（扫码登录 + 自动建 WS）？' $true }
    if ($doCfg) {
        if (Invoke-NapcatConfigure) { $napcatConfigured = $true } else { $napcatOk = $false }
    } else {
        Info 'NapCat 未配置，之后重跑：scripts\install.ps1 -WithQQ'
    }
}

if (-not $napcatOk) {
    Fail '安装失败：NapCat 未配置完成。重跑：scripts\install.ps1 -WithQQ'
}

if ($NoStart) {
    Step '跳过启动'
    Write-Host '    启动：scripts\start.ps1'
    Write-Summary $true
    exit 0
}

Step '启动服务'
if ($Mode -eq 'host') {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'start.ps1')
    if ($LASTEXITCODE -ne 0) { Fail '安装失败：服务启动未通过' }
    Write-Summary $false
    exit 0
}
Invoke-Run 'docker compose up -d' { docker compose up -d }
Info '等待控制台就绪（18082）…'
if (Wait-Port 18082 180) {
    Info '控制台已就绪：http://127.0.0.1:18082/'
    Write-Summary $false
    exit 0
}
Warn '等待超时，排查：docker compose logs -f resident'
Fail '安装失败：服务启动未通过'
