[CmdletBinding()]
param(
    [string]$AgentHost = "127.0.0.1",
    [ValidateRange(1, 65535)][int]$AgentPort = 8765
)

# $env:QWEN_BASE_URL = "http://10.70.98.7:8080/v1"
# $env:QWEN_MODEL = "Qwen3.8-27B"
# $env:QWEN_API_KEY = ""
# $env:QWEN_TIMEOUT_SECONDS = "300"

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$webRoot = Join-Path $root "web"
$viteScript = Join-Path $webRoot "node_modules\vite\bin\vite.js"
$viteConfig = Join-Path $webRoot "editor\vite.config.ts"
$pluginDirectory = Join-Path $root "build\windows-full\Release\plugins"
$logDirectory = Join-Path $env:TEMP "robotsim-web"
$statePath = Join-Path $logDirectory "processes.json"
$nullInput = Join-Path $logDirectory "stdin.null"

$library = @(
    (Join-Path $root "build\windows-full\Release\sim_platform_c.dll")
    (Join-Path $root "build\windows-gui\Release\sim_platform_c.dll")
    (Join-Path $root "build\Release\sim_platform_c.dll")
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $library) {
    throw "sim_platform_c.dll was not found. Build the sim_c_api target first."
}
if (-not (Test-Path -LiteralPath $viteScript)) {
    throw "Web dependencies are missing. Run: cd web; corepack pnpm install --force"
}

$python = Get-Command python -ErrorAction Stop
$node = Get-Command node -ErrorAction Stop

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
if (-not (Test-Path -LiteralPath $nullInput)) {
    [IO.File]::WriteAllBytes($nullInput, [byte[]]@())
}

# Establish the complete platform runtime environment before any child process
# inherits it.
$libraryDirectory = Split-Path -Parent $library
$pythonPaths = @((Join-Path $root "python"), $root)
if ($env:PYTHONPATH) { $pythonPaths += $env:PYTHONPATH }
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = $pythonPaths -join [IO.Path]::PathSeparator
$env:SIM_PLATFORM_LIBRARY = $library
$env:SIM_PLUGIN_DIR = $pluginDirectory
if (-not (($env:PATH -split [IO.Path]::PathSeparator) |
        Where-Object { $_.TrimEnd('\') -ieq $libraryDirectory.TrimEnd('\') })) {
    $env:PATH = "$libraryDirectory$([IO.Path]::PathSeparator)$env:PATH"
}

function Get-ListeningProcess([int]$Port) {
    $processIds = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    if ($processIds.Count -gt 1) {
        throw "Port $Port has multiple listening processes."
    }
    if ($processIds.Count -eq 0) { return $null }
    return Get-Process -Id $processIds[0] -ErrorAction Stop
}

function Test-ProcessCommand([System.Diagnostics.Process]$Process, [string[]]$Markers) {
    $details = Get-CimInstance Win32_Process -Filter "ProcessId = $($Process.Id)" -ErrorAction SilentlyContinue
    if (-not $details.CommandLine) { return $false }
    foreach ($marker in $Markers) {
        if ($details.CommandLine.IndexOf($marker, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            return $false
        }
    }
    return $true
}

$trackedServices = [ordered]@{}
function Register-Service([string]$Name, [System.Diagnostics.Process]$Process) {
    $trackedServices[$Name] = [ordered]@{
        pid = $Process.Id
        startedAt = $Process.StartTime.ToUniversalTime().ToString("o")
    }
    [ordered]@{
        version = 1
        root = $root
        updatedAt = [DateTime]::UtcNow.ToString("o")
        services = $trackedServices
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding utf8
}

function Wait-Endpoint(
    [string]$Url,
    [int]$Seconds = 20,
    [System.Diagnostics.Process]$ManagedProcess = $null
) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return }
        } catch {
            if ($ManagedProcess) {
                $ManagedProcess.Refresh()
                if ($ManagedProcess.HasExited) {
                    throw "Service exited before becoming ready: $Url"
                }
            }
            Start-Sleep -Milliseconds 300
        }
    } while ((Get-Date) -lt $deadline)
    throw "Service did not become ready: $Url"
}

$agent = Get-ListeningProcess $AgentPort
if (-not $agent) {
    $agent = Start-Process -FilePath $python.Source `
        -ArgumentList @("-m", "agent_service.api", "--host", $AgentHost, `
                        "--port", $AgentPort) `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardInput $nullInput `
        -RedirectStandardOutput (Join-Path $logDirectory "agent.out.log") `
        -RedirectStandardError (Join-Path $logDirectory "agent.err.log") -PassThru
} elseif (-not (Test-ProcessCommand $agent @("-m agent_service.api"))) {
    throw "Port $AgentPort is already used by an unmanaged process (PID $($agent.Id))."
}
Register-Service "agent" $agent
# /health stays 200 even when the model is unconfigured, so this only proves the service is up.
Wait-Endpoint "http://${AgentHost}:$AgentPort/health" 30 $agent

$gateway = Get-ListeningProcess 8080
if (-not $gateway) {
    $gateway = Start-Process -FilePath $python.Source `
        -ArgumentList @("-m", "web.gateway.server", "--port", "8080", `
                        "--agent-host", $AgentHost, "--agent-port", $AgentPort) `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardInput $nullInput `
        -RedirectStandardOutput (Join-Path $logDirectory "gateway.out.log") `
        -RedirectStandardError (Join-Path $logDirectory "gateway.err.log") -PassThru
} elseif (-not (Test-ProcessCommand $gateway @("-m web.gateway.server", "--port 8080"))) {
    throw "Port 8080 is already used by an unmanaged process (PID $($gateway.Id))."
}
Register-Service "gateway" $gateway
Wait-Endpoint "http://127.0.0.1:8080/api/health"

$vite = Get-ListeningProcess 5173
if (-not $vite) {
    $vite = Start-Process -FilePath $node.Source `
        -ArgumentList @($viteScript, "--config", $viteConfig, "--host", "127.0.0.1") `
        -WorkingDirectory $webRoot -WindowStyle Hidden `
        -RedirectStandardInput $nullInput `
        -RedirectStandardOutput (Join-Path $logDirectory "vite.out.log") `
        -RedirectStandardError (Join-Path $logDirectory "vite.err.log") -PassThru
} elseif (-not (Test-ProcessCommand $vite @($viteScript, $viteConfig))) {
    throw "Port 5173 is already used by an unmanaged process (PID $($vite.Id))."
}
Register-Service "vite" $vite
Wait-Endpoint "http://127.0.0.1:5173"

Write-Host "WebEditor: http://127.0.0.1:5173"
Write-Host "Gateway:   http://127.0.0.1:8080"
Write-Host "Agent:     http://${AgentHost}:$AgentPort"
Write-Host "Logs:      $logDirectory"
Write-Host "Stop:      .\Stop-WebEditor.ps1"
