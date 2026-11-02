$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$global:started = @()
$global:written = @()
$global:scenario = 'healthy'
$global:healthCalls = 0
$global:stopped = @()
$global:backendGate = $null
function global:Get-NetTCPConnection {
  [CmdletBinding()] param([string]$State,[int]$LocalPort)
  if ($LocalPort -eq 8011 -and $global:scenario -ne 'no_api') { [pscustomobject]@{LocalAddress='127.0.0.1';OwningProcess=42} }
}
function global:Get-CimInstance { [CmdletBinding()] param([string]$ClassName,[string]$Filter); [pscustomobject]@{CommandLine= $(if ($global:scenario -eq 'wrong_process') {'unrelated.exe'} else {'python -m uvicorn backend.app:app'})} }
function global:Invoke-RestMethod {
  [CmdletBinding()] param([string]$Uri,[int]$TimeoutSec)
  $global:healthCalls++
  [pscustomobject]@{status=$(if ($global:scenario -eq 'lost_after_adoption' -and $global:healthCalls -gt 1) {'fail'} else {'ok'});live_trading_disabled=($global:scenario -ne 'unsafe_health')}
}
function global:Start-Process {
  [CmdletBinding()] param([string]$FilePath,[object[]]$ArgumentList,[string]$WorkingDirectory,[string]$WindowStyle,[switch]$PassThru,[string]$RedirectStandardOutput,[string]$RedirectStandardError)
  $global:started += $FilePath
  if ($FilePath -match 'python') { $global:backendGate = $env:BTC_REQUIRE_STARTUP_MONITOR }
  if ($PassThru) { [pscustomobject]@{Id=1001} }
}
function global:Set-Content { [CmdletBinding()] param([string]$LiteralPath,[object]$Value,[switch]$NoNewline); $global:written += $LiteralPath }
function global:New-Item { [CmdletBinding()] param([string]$ItemType,[string]$Path,[switch]$Force) }
function global:Start-Sleep { [CmdletBinding()] param([int]$Seconds) }
function global:Stop-Process { [CmdletBinding()] param([int]$Id); $global:stopped += $Id }
& (Join-Path $root 'scripts/start_lab.ps1')
if (@($global:started | Where-Object { $_ -match 'python' }).Count -ne 0) { throw 'Started a duplicate backend' }
if (@($global:started | Where-Object { $_ -match 'node' }).Count -ne 1) { throw 'Expected one frontend' }
if (@($global:written | Where-Object { $_ -match 'backend\.pid$' }).Count -ne 0) { throw 'Overwrote adopted backend identity' }
if (@($global:written | Where-Object { $_ -match 'frontend\.pid$' }).Count -ne 1) { throw 'Frontend PID not recorded' }
foreach ($scenario in 'wrong_process','unsafe_health') {
  $global:scenario=$scenario; $global:started=@(); $rejected=$false
  try { & (Join-Path $root 'scripts/start_lab.ps1') } catch { $rejected=$true }
  if (-not $rejected -or $global:started.Count -ne 0) { throw "Unsafe adoption was accepted: $scenario" }
}
$global:scenario='lost_after_adoption'; $global:healthCalls=0; $global:started=@(); $global:stopped=@(); $rejected=$false
try { & (Join-Path $root 'scripts/start_lab.ps1') } catch { $rejected=$true }
if (-not $rejected -or $global:stopped.Count -ne 1 -or $global:stopped[0] -ne 1001) { throw 'Failed to stop only the new frontend after adopted backend became unhealthy' }
$global:scenario='no_api'; $global:healthCalls=0; $global:started=@(); $global:written=@(); $env:BTC_REQUIRE_STARTUP_MONITOR=$null
& (Join-Path $root 'scripts/start_lab.ps1')
if ($global:backendGate -ne 'true') { throw 'New backend did not receive startup position-monitor gate' }
if (@($global:started | Where-Object { $_ -match 'python' }).Count -ne 1) { throw 'Expected one newly started backend' }
Write-Output 'ADOPT_RUNNING_API_AND_START_FRONTEND=PASS'
