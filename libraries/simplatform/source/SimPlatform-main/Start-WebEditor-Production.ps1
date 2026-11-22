[CmdletBinding()]
param(
    [string]$AgentHost = "127.0.0.1",
    [ValidateRange(1, 65535)][int]$AgentPort = 8765,
    [ValidateRange(1, 65535)][int]$GatewayPort = 8080
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$library = Join-Path $root "build\windows-full\Release\sim_platform_c.dll"
$pluginDirectory = Join-Path $root "build\windows-full\Release\plugins"
$webRoot = Join-Path $root "web"
$logDirectory = Join-Path $env:TEMP "robotsim-web"
$statePath = Join-Path $logDirectory "processes.json"
$nullInput = Join-Path $logDirectory "stdin.null"

if (-not (Test-Path -LiteralPath $library)) { throw "sim_platform_c.dll was not found: $library" }
if (-not (Test-Path -LiteralPath (Join-Path $webRoot "dist\index.html"))) {
    throw "WebEditor production files were not found: $webRoot\dist\index.html"
}
$python = Get-Command python -ErrorAction Stop
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
if (-not (Test-Path -LiteralPath $nullInput)) { [IO.File]::WriteAllBytes($nullInput, [byte[]]@()) }

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = @((Join-Path $root "python"), $root, $env:PYTHONPATH) -join [IO.Path]::PathSeparator
$env:SIM_PLATFORM_LIBRARY = $library
$env:SIM_PLUGIN_DIR = $pluginDirectory
$libraryDirectory = Split-Path -Parent $library
$env:PATH = "$libraryDirectory$([IO.Path]::PathSeparator)$env:PATH"

function Get-ListeningProcess([int]$Port) {
    $ids = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    if ($ids.Count -eq 0) { return $null }
    if ($ids.Count -gt 1) { throw "Port $Port has multiple listening processes." }
    return Get-Process -Id $ids[0] -ErrorAction Stop
}
function Wait-Endpoint([string]$Url, [int]$Seconds = 30) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try { if ((Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2).StatusCode -eq 200) { return } }
        catch { Start-Sleep -Milliseconds 300 }
    } while ((Get-Date) -lt $deadline)
    throw "Service did not become ready: $Url"
}
function Start-ServiceProcess([string]$Name, [string[]]$Arguments) {
    $process = Start-Process -FilePath $python.Source -ArgumentList $Arguments -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardInput $nullInput -RedirectStandardOutput (Join-Path $logDirectory "$Name.out.log") `
        -RedirectStandardError (Join-Path $logDirectory "$Name.err.log") -PassThru
    return $process
}

$agent = Get-ListeningProcess $AgentPort
if (-not $agent) { $agent = Start-ServiceProcess "agent" @("-m", "agent_service.api", "--host", $AgentHost, "--port", $AgentPort) }
$gateway = Get-ListeningProcess $GatewayPort
if (-not $gateway) {
    $gateway = Start-ServiceProcess "gateway" @("-m", "web.gateway.server", "--host", "127.0.0.1", "--port", $GatewayPort, "--agent-host", $AgentHost, "--agent-port", $AgentPort, "--static-dir", (Join-Path $webRoot "dist"))
}
Wait-Endpoint "http://${AgentHost}:$AgentPort/health"
Wait-Endpoint "http://127.0.0.1:$GatewayPort/api/health"

[ordered]@{ version = 1; root = $root; updatedAt = [DateTime]::UtcNow.ToString("o"); services = [ordered]@{
    agent = [ordered]@{ pid = $agent.Id; startedAt = $agent.StartTime.ToUniversalTime().ToString("o") }
    gateway = [ordered]@{ pid = $gateway.Id; startedAt = $gateway.StartTime.ToUniversalTime().ToString("o") }
} } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding utf8
Write-Host "WebEditor: http://127.0.0.1:$GatewayPort"
Write-Host "Gateway:   http://127.0.0.1:$GatewayPort"
Write-Host "Agent:     http://${AgentHost}:$AgentPort"
Write-Host "Logs:      $logDirectory"
