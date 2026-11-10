param(
  [int]$GatewayPort = 8099,
  [int]$FrontendPort = 5173,
  [int]$LibraryPort = 8000,
  [string]$LibraryDir = $env:OLS_DIR
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$previousGatewayUrl = $env:WEB_GATEWAY_URL
$env:WEB_GATEWAY_URL = "http://127.0.0.1:$GatewayPort"

# The browser learns where OLS lives from GET /api/runtimeConfig, so a non-default
# -LibraryPort has to reach the gateway through its config file. Patch a copy of the
# repository config instead of replacing it, so other runtime values still apply.
$configPath = Join-Path $root "config\constant_config.json"
$config = if (Test-Path $configPath) { Get-Content $configPath -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
if (-not ($config.PSObject.Properties.Name -contains "library_server_url")) {
  $config | Add-Member -NotePropertyName library_server_url -NotePropertyValue "http://127.0.0.1:$LibraryPort"
}
$config.library_server_url = "http://127.0.0.1:$LibraryPort"
$runtimeConfig = Join-Path $env:TEMP "web-frontend-gateway-$GatewayPort.json"
[System.IO.File]::WriteAllText($runtimeConfig, ($config | ConvertTo-Json -Depth 5))

$library = $null
$gateway = $null
try {
  # OLS is a separate checkout (ObjectLibraryServer); -LibraryDir or $env:OLS_DIR points at it.
  # A server already listening on the library port is left alone, so this script never
  # stops a process it did not start.
  $olsEntry = if ($LibraryDir) { Join-Path $LibraryDir "server.py" } else { $null }
  $listening = $false
  try {
    $probe = New-Object System.Net.Sockets.TcpClient
    $probe.Connect("127.0.0.1", $LibraryPort)
    $probe.Close()
    $listening = $true
  } catch { }
  if ($listening) {
    Write-Host "OLS already answers on http://127.0.0.1:$LibraryPort - reusing it"
  } elseif ($olsEntry -and (Test-Path $olsEntry)) {
    $library = Start-Process -FilePath "python" -ArgumentList @("server.py", "--port", $LibraryPort) `
      -WorkingDirectory (Split-Path -Parent $olsEntry) -WindowStyle Hidden -PassThru
  } else {
    Write-Warning "no OLS on port $LibraryPort and none to start: pass -LibraryDir <ObjectLibraryServer> or set OLS_DIR, or start it yourself"
  }
  $gateway = Start-Process -FilePath "python" -ArgumentList @("local_gateway.py", "--port", $GatewayPort, "--config", $runtimeConfig) -WorkingDirectory $root -WindowStyle Hidden -PassThru
  npm --prefix $root run dev -- --host 127.0.0.1 --port $FrontendPort
}
finally {
  if ($gateway -and -not $gateway.HasExited) { Stop-Process -Id $gateway.Id -Force }
  if ($library -and -not $library.HasExited) { Stop-Process -Id $library.Id -Force }
  Remove-Item $runtimeConfig -ErrorAction SilentlyContinue
  $env:WEB_GATEWAY_URL = $previousGatewayUrl
}
