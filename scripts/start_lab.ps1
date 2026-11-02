$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
if (-not (Test-Path -LiteralPath "$root\.venv\Scripts\python.exe")) { throw 'Python environment missing. Follow README setup.' }
if (-not (Test-Path -LiteralPath "$root\frontend\node_modules\vite\bin\vite.js")) { throw 'Frontend dependencies missing. Follow README setup.' }
$apiPort = if ($env:BTC_LAB_API_PORT) { [int]$env:BTC_LAB_API_PORT } else { 8011 }
$uiPort = if ($env:BTC_LAB_UI_PORT) { [int]$env:BTC_LAB_UI_PORT } else { 5174 }
$listener = @(Get-NetTCPConnection -State Listen -LocalPort $apiPort -ErrorAction SilentlyContinue)
$existingApi = $listener.Count -gt 0
if ($existingApi) {
  if ($listener.Count -ne 1 -or $listener[0].LocalAddress -ne '127.0.0.1') { throw 'Unrecognized API listener.' }
  $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener[0].OwningProcess)"
  if ($process.CommandLine -notmatch 'uvicorn backend\.app:app') { throw 'Unrecognized API process.' }
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/api/v1/health" -TimeoutSec 3
  if ($health.status -ne 'ok' -or $health.live_trading_disabled -ne $true) { throw 'Existing API is not a healthy BTC paper service.' }
}
if (Get-NetTCPConnection -State Listen -LocalPort $uiPort -ErrorAction SilentlyContinue) { throw "Port $uiPort is already in use. Stop the existing frontend or change its port." }
$run = Join-Path $root '.run'
$null = New-Item -ItemType Directory -Force -Path $run
$logs = Join-Path $root 'logs'
$null = New-Item -ItemType Directory -Force -Path $logs
$runStamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ') + '-' + $PID
$env:ALPACA_CREDENTIALS_FILE = Join-Path $root 'data\alpaca-market-new.env'
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not $existingApi) {
  $env:BTC_REQUIRE_STARTUP_MONITOR = 'true'
  $api = Start-Process -FilePath $py -ArgumentList @('-m','uvicorn','backend.app:app','--host','127.0.0.1','--port',"$apiPort") -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs "backend-$runStamp.log") -RedirectStandardError (Join-Path $logs "backend-error-$runStamp.log")
}
$vite = Join-Path $root 'frontend\node_modules\vite\bin\vite.js'
$node = (Get-Command node.exe).Source
$env:VITE_API_BASE = "http://127.0.0.1:$apiPort/api/v1"
$ui = Start-Process -FilePath $node -ArgumentList @($vite,'--host','127.0.0.1','--port',"$uiPort") -WorkingDirectory (Join-Path $root 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs "frontend-$runStamp.log") -RedirectStandardError (Join-Path $logs "frontend-error-$runStamp.log")
if (-not $existingApi) { Set-Content -LiteralPath (Join-Path $run 'backend.pid') -Value $api.Id -NoNewline }
Set-Content -LiteralPath (Join-Path $run 'frontend.pid') -Value $ui.Id -NoNewline
$ready = $false
for ($i=0; $i -lt 30; $i++) {
  try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/api/v1/health" -TimeoutSec 2; if ($health.status -eq 'ok') { $ready=$true; break } } catch {}
  Start-Sleep -Seconds 1
}
if (-not $ready) {
  if ($existingApi) { Stop-Process -Id $ui.Id -ErrorAction SilentlyContinue } else { & (Join-Path $PSScriptRoot 'stop_lab.ps1') }
  throw "Backend failed to become healthy. Read logs/backend-error-$runStamp.log."
}
Start-Process "http://127.0.0.1:$uiPort"
Write-Host "BTC Visual Price Action Lab started at http://127.0.0.1:$uiPort"
Write-Host 'Local-only. LIVE TRADING DISABLED. See System Status for data/model gates.'
