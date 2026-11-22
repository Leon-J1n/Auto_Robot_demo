[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateRange(1, 65535)][int]$AgentPort = 8765
)

$ErrorActionPreference = "Stop"
$requestedWhatIf = $WhatIfPreference
try {
    $WhatIfPreference = $false
    Import-Module CimCmdlets -ErrorAction Stop
} finally {
    $WhatIfPreference = $requestedWhatIf
}
$root = $PSScriptRoot
$webRoot = Join-Path $root "web"
$viteScript = Join-Path $webRoot "node_modules\vite\bin\vite.js"
$viteConfig = Join-Path $webRoot "editor\vite.config.ts"
$logDirectory = Join-Path $env:TEMP "robotsim-web"
$statePath = Join-Path $logDirectory "processes.json"

function Get-ListeningProcess([int]$Port) {
    $processIds = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    if ($processIds.Count -gt 1) {
        Write-Warning "Port $Port has multiple listening processes; none will be stopped."
        return $null
    }
    if ($processIds.Count -eq 0) { return $null }
    return Get-Process -Id $processIds[0] -ErrorAction SilentlyContinue
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

function Test-TrackedStartTime([System.Diagnostics.Process]$Process, $Entry) {
    if (-not $Entry -or -not $Entry.startedAt) { return $false }
    try {
        $recorded = [DateTime]::Parse($Entry.startedAt).ToUniversalTime()
        return [Math]::Abs(($Process.StartTime.ToUniversalTime() - $recorded).TotalSeconds) -lt 1
    } catch {
        return $false
    }
}

$state = $null
if (Test-Path -LiteralPath $statePath) {
    try {
        $candidate = Get-Content -Raw -Encoding utf8 -LiteralPath $statePath | ConvertFrom-Json
        if ($candidate.version -eq 1 -and $candidate.root -eq $root) {
            $state = $candidate
        } else {
            Write-Warning "Ignoring a WebEditor process file for another workspace or version."
        }
    } catch {
        Write-Warning "Ignoring an unreadable WebEditor process file: $statePath"
    }
}

# Gateway first: it is the only client of the agent service, so stopping it first avoids a
# burst of failed turn polls while the agent goes down.
$specifications = @(
    [pscustomobject]@{ Name = "Gateway"; Key = "gateway"; Port = 8080; Markers = @("-m web.gateway.server", "--port 8080"); TrackedOnly = $false },
    [pscustomobject]@{ Name = "Agent"; Key = "agent"; Port = $AgentPort; Markers = @("-m agent_service.api"); TrackedOnly = $false },
    [pscustomobject]@{ Name = "Vite"; Key = "vite"; Port = 5173; Markers = @($viteScript, $viteConfig); TrackedOnly = $false }
)
$unresolved = $false

foreach ($specification in $specifications) {
    $process = $null
    $entry = $null
    if ($state -and $state.services) {
        $property = $state.services.PSObject.Properties[$specification.Key]
        if ($property) { $entry = $property.Value }
    }
    if ($entry -and $entry.pid) {
        $tracked = Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue
        if ($tracked -and (Test-TrackedStartTime $tracked $entry) -and
            (Test-ProcessCommand $tracked $specification.Markers)) {
            $process = $tracked
        }
    }
    if (-not $process -and -not $specification.TrackedOnly) {
        $listener = Get-ListeningProcess $specification.Port
        if ($listener -and (Test-ProcessCommand $listener $specification.Markers)) {
            $process = $listener
        } elseif ($listener) {
            Write-Warning "$($specification.Name) port $($specification.Port) belongs to an unmanaged process (PID $($listener.Id)); it was not stopped."
            $unresolved = $true
        }
    }
    if (-not $process) {
        Write-Host "$($specification.Name): not running"
        continue
    }
    if ($PSCmdlet.ShouldProcess("$($specification.Name) process $($process.Id)", "Stop")) {
        Stop-Process -Id $process.Id -ErrorAction Stop
        $null = $process.WaitForExit(5000)
        Write-Host "$($specification.Name): stopped (PID $($process.Id))"
    }
}

if (-not $WhatIfPreference -and -not $unresolved -and (Test-Path -LiteralPath $statePath)) {
    Remove-Item -LiteralPath $statePath -Force
}
