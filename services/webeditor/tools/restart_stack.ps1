param(
  [switch]$KeepState,
  [switch]$Build,
  [ValidateRange(0, 120)][double]$QwenDelaySeconds = 0,
  [string]$WebRoot = 'F:\web-frontend',
  [string]$BrainRoot = 'F:\robot_agent_brain\robot_agent_brain',
  [string]$BrainEnvRoot = 'F:\robot_agent_brain',
  [string]$OlsRoot = 'F:\ObjectLibraryServer',
  [string]$QwenRoot = 'F:\QwenMock'
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

function Resolve-Root([string]$Path, [string]$Name) {
  $resolved = [System.IO.Path]::GetFullPath($Path)
  if (-not (Test-Path -LiteralPath $resolved -PathType Container)) { throw "$Name does not exist: $resolved" }
  return $resolved.TrimEnd('\')
}

$WebRoot = Resolve-Root $WebRoot 'WebRoot'
$BrainRoot = Resolve-Root $BrainRoot 'BrainRoot'
$BrainEnvRoot = Resolve-Root $BrainEnvRoot 'BrainEnvRoot'
$OlsRoot = Resolve-Root $OlsRoot 'OlsRoot'
$QwenRoot = Resolve-Root $QwenRoot 'QwenRoot'
$python = Join-Path $BrainEnvRoot '.venv\Scripts\python.exe'
$config = Join-Path $BrainRoot 'config\webeditor_local.json'
$inventory = Join-Path $BrainRoot 'var\webeditor\joint_inventory.json'
foreach ($file in @($python, $config, $inventory, (Join-Path $OlsRoot 'server.py'), (Join-Path $QwenRoot 'qwen_mock.py'), (Join-Path $WebRoot 'local_gateway.py'))) {
  if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required file does not exist: $file" }
}

function Assert-UnderRoot([string]$Path, [string]$Root) {
  $full = [System.IO.Path]::GetFullPath($Path)
  $rootFull = ([System.IO.Path]::GetFullPath($Root)).TrimEnd('\') + '\'
  if (-not $full.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase) -and
      $full -ne $rootFull.TrimEnd('\')) { throw "Refusing path outside root: $full" }
}

function Stop-OwnedPort([int]$Port, [string[]]$Needles, [string]$Root) {
  $connections = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
  foreach ($connection in $connections) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($connection.OwningProcess)"
    $command = [string]$process.CommandLine
    if (-not $command -or ($Needles | Where-Object { $command -match [regex]::Escape($_) }).Count -eq 0 -or
        $command.IndexOf($Root, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
      throw "Port $Port is owned by an unexpected process: PID $($connection.OwningProcess) $command"
    }
    Write-Host "Stopping port $Port PID $($connection.OwningProcess)"
    Stop-Process -Id $connection.OwningProcess -Force
  }
}

function Move-State([string]$Path, [string]$Root, [string]$BackupRoot) {
  if (-not (Test-Path -LiteralPath $Path)) { return }
  Assert-UnderRoot $Path $Root
  # PowerShell 5.1 has no [System.IO.Path]::GetRelativePath.
  $rootFull = ([System.IO.Path]::GetFullPath($Root)).TrimEnd('\')
  $relative = ([System.IO.Path]::GetFullPath($Path)).Substring($rootFull.Length).TrimStart('\')
  $destination = Join-Path $BackupRoot $relative
  $parent = Split-Path -Parent $destination
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  Move-Item -LiteralPath $Path -Destination $destination
  Write-Host "Backed up $relative"
}

Stop-OwnedPort 8000 @('server.py') $OlsRoot
Stop-OwnedPort 8080 @('qwen_mock.py') $QwenRoot
Stop-OwnedPort 8099 @('local_gateway.py') $WebRoot
Stop-OwnedPort 8500 @('robot_agent_brain') $BrainRoot

$stackLog = Join-Path $WebRoot 'var\stack'
New-Item -ItemType Directory -Force -Path $stackLog | Out-Null
if (-not $KeepState) {
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  $backupRoot = Join-Path $WebRoot "var\stack\backups\$stamp"
  New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
  Move-State (Join-Path $WebRoot 'var\gateway_state.json') $WebRoot $backupRoot
  Move-State (Join-Path $WebRoot 'var\gateway_transactions') $WebRoot $backupRoot
  Move-State (Join-Path $BrainRoot 'var\webeditor\tasks') $BrainRoot $backupRoot
}

if ($Build) { & (Get-Command npm.cmd -ErrorAction Stop).Source --prefix $WebRoot run build; if ($LASTEXITCODE) { throw "Frontend build failed: $LASTEXITCODE" } }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
New-Item -ItemType Directory -Force -Path (Join-Path $QwenRoot 'logs') | Out-Null
function Start-ServiceProcess([string]$Name, [string]$WorkingDirectory, [string[]]$Arguments) {
  $out = Join-Path $stackLog "$Name.out.log"
  $err = Join-Path $stackLog "$Name.err.log"
  Start-Process -FilePath $python -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err | Out-Null
}
Start-ServiceProcess 'ols' $OlsRoot @((Join-Path $OlsRoot 'server.py'), '--port', '8000')
$qwenArgs = @((Join-Path $QwenRoot 'qwen_mock.py'), '--port', '8080', '--model', 'qwen-mock', '--log-file', (Join-Path $QwenRoot 'logs\requests.jsonl'))
if ($QwenDelaySeconds -gt 0) { $qwenArgs += @('--response-delay-seconds', $QwenDelaySeconds.ToString([System.Globalization.CultureInfo]::InvariantCulture)) }
Start-ServiceProcess 'qwenmock' $QwenRoot $qwenArgs
Start-ServiceProcess 'gateway' $WebRoot @((Join-Path $WebRoot 'local_gateway.py'), '--host', '127.0.0.1', '--port', '8099')
Start-ServiceProcess 'brain' $BrainRoot @('-m', 'robot_agent_brain', 'serve', '--config', $config, '--gateway-url', 'http://127.0.0.1:8099', '--ols-url', 'http://127.0.0.1:8000', '--joint-inventory', $inventory, '--camera-id', '5', '--port', '8500')

function Wait-Http([string]$Name, [string]$Uri, [scriptblock]$Check) {
  for ($i = 0; $i -lt 60; $i++) {
    try { $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 2; if ($response.StatusCode -eq 200) { $json = $response.Content | ConvertFrom-Json; & $Check $json; Write-Host "Ready $Name"; return } } catch { }
    Start-Sleep -Milliseconds 500
  }
  throw "$Name did not become ready: $Uri"
}
Wait-Http 'OLS' 'http://127.0.0.1:8000/list?type=model&uid=0' { param($x) }
Wait-Http 'QwenMock' 'http://127.0.0.1:8080/v1/models' { param($x) if (-not ($x.data | Where-Object id -eq 'qwen-mock')) { throw 'qwen-mock model missing' } }
Wait-Http 'Gateway' 'http://127.0.0.1:8099/api/health' { param($x) }
Wait-Http 'Brain' 'http://127.0.0.1:8500/health' { param($x) if ($x.service -ne 'ready' -or $x.qwen -ne 'ready' -or $x.assets -ne 'ok') { throw ('Brain health not ready: ' + ($x | ConvertTo-Json -Compress)) } }
Write-Host 'Stack ready: http://127.0.0.1:8099'
