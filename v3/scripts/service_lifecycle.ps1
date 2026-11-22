# Shared, ownership-aware lifecycle helpers for the standalone V3 deployment.
$ErrorActionPreference = 'Stop'
$script:V3Root = Split-Path -Parent $PSScriptRoot
$script:V3Run = Join-Path $script:V3Root '.run'

function Get-V3ListenerPids([int]$Port) {
  $found = @()
  try {
    $found += @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq $Port } |
      ForEach-Object { [int]$_.OwningProcess })
  } catch {
    $found += @(& netstat.exe -ano -p tcp 2>$null | ForEach-Object {
      $line = (($_ -replace '\s+', ' ').Trim())
      if ($line -match ('^TCP\s+\S+:' + [regex]::Escape([string]$Port) + '\s+\S+\s+LISTENING\s+(\d+)$')) {
        [int]$Matches[1]
      }
    })
  }
  return @($found | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
}

function Get-V3ProcessInfo([int]$ProcessId) {
  try { return Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop } catch { return $null }
}

function Test-V3OwnedListener([string]$Service, [int]$ProcessId) {
  $proc = Get-V3ProcessInfo $ProcessId
  if (-not $proc) { return $false }
  $root = [IO.Path]::GetFullPath($script:V3Root).TrimEnd('\')
  $exe = [string]$proc.ExecutablePath
  $cmd = [string]$proc.CommandLine
  if ($Service -eq 'frontend') {
    return (($cmd -match '(?i)node_modules[\\/]vite[\\/]bin[\\/]vite\.js') -and
      ($cmd -match '(?i)(--port\s+5175|5175)') -and ($cmd -match [regex]::Escape($root)))
  }
  $exeIsVenv = $exe -and $exe.StartsWith((Join-Path $root '.venv\Scripts\'), [StringComparison]::OrdinalIgnoreCase)
  $cmdIsV3 = ($cmd -match '(?i)uvicorn\s+backend\.app:app') -and ($cmd -match '(?i)(--port\s+8012|8012)')
  $parent = Get-V3ProcessInfo ([int]$proc.ParentProcessId)
  $parentIsVenv = $parent -and ([string]$parent.ExecutablePath).Equals((Join-Path $root '.venv\Scripts\python.exe'), [StringComparison]::OrdinalIgnoreCase) -and ([string]$parent.CommandLine -match '(?i)uvicorn\s+backend\.app:app.*--port\s+8012')
  return (($exeIsVenv -and $cmdIsV3) -or (($cmd -match [regex]::Escape($root)) -and $cmdIsV3) -or ($cmdIsV3 -and $parentIsVenv))
}

function Stop-V3BackendGracefully([int]$Listener) {
  $proc = Get-V3ProcessInfo $Listener
  if (-not $proc -or -not (Test-V3OwnedListener 'backend' $Listener)) { throw 'Unverified V3 backend.' }
  $parentId = [int]$proc.ParentProcessId
  if (-not ('V3ConsoleSignal' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class V3ConsoleSignal {
 [DllImport("kernel32.dll", SetLastError=true)] public static extern bool FreeConsole();
 [DllImport("kernel32.dll", SetLastError=true)] public static extern bool AttachConsole(uint pid);
 [DllImport("kernel32.dll", SetLastError=true)] public static extern uint GetConsoleProcessList(uint[] ids,uint count);
 [DllImport("kernel32.dll", SetLastError=true)] public static extern bool SetConsoleCtrlHandler(IntPtr handler,bool add);
 [DllImport("kernel32.dll", SetLastError=true)] public static extern bool GenerateConsoleCtrlEvent(uint evt,uint group);
}
'@
  }
  [void][V3ConsoleSignal]::FreeConsole()
  if (-not [V3ConsoleSignal]::AttachConsole([uint32]$Listener)) { throw "Cannot attach V3 console: $([Runtime.InteropServices.Marshal]::GetLastWin32Error())" }
  try {
    $ids = New-Object 'uint32[]' 32
    $count = [V3ConsoleSignal]::GetConsoleProcessList($ids,32)
    $others = @($ids | Select-Object -First $count | Where-Object { $_ -notin @([uint32]$PID,[uint32]$Listener,[uint32]$parentId) })
    if ($count -ne 3 -or $others.Count) { throw 'Unexpected V3 console participants; no signal sent.' }
    if (-not [V3ConsoleSignal]::SetConsoleCtrlHandler([IntPtr]::Zero,$true)) { throw 'Cannot protect maintenance shell.' }
    if (-not [V3ConsoleSignal]::GenerateConsoleCtrlEvent(0,0)) { throw "V3 interrupt failed: $([Runtime.InteropServices.Marshal]::GetLastWin32Error())" }
  } finally { [void][V3ConsoleSignal]::FreeConsole() }
}

function Test-V3Health([string]$Service) {
  try {
    if ($Service -eq 'backend') {
      $h = Invoke-RestMethod 'http://127.0.0.1:8012/api/v1/health' -TimeoutSec 3
      return ($h.status -eq 'ok' -and $h.live_trading_disabled -eq $true)
    }
    $w = Invoke-WebRequest 'http://127.0.0.1:5175/' -UseBasicParsing -TimeoutSec 3
    return ($w.StatusCode -eq 200)
  } catch { return $false }
}

function Stop-V3Service([string]$Service) {
  $port = if ($Service -eq 'backend') { 8012 } else { 5175 }
  $pidFile = Join-Path $script:V3Run "$Service.pid"
  $pids = @(Get-V3ListenerPids $port)
  if (-not $pids.Count) {
    if (Test-V3Health $Service) {
      throw "Port $port responds over HTTP, but no local Windows listener PID is visible; service state is ambiguous and nothing was stopped."
    }
    # No OS listener and no service response means stopped. A PID file alone is not authority.
    Write-Host "V3 $Service is already stopped (port $port has no listener)."
    return
  }
  if ($pids.Count -ne 1) { throw "Port $port has multiple listener PIDs ($($pids -join ', ')); nothing was stopped." }
  $listener = [int]$pids[0]
  if (-not (Test-V3OwnedListener $Service $listener)) {
    throw "Port $port is held by PID $listener, which cannot be verified as this V3 $Service process; nothing was stopped."
  }
  if (-not (Test-V3Health $Service)) { throw "PID $listener matches V3 $Service but its service health check failed; nothing was stopped." }
  if ($Service -eq 'backend') { Stop-V3BackendGracefully $listener }
  else { Stop-Process -Id $listener -Force -ErrorAction Stop }
  $deadline = (Get-Date).AddSeconds(10)
  do { Start-Sleep -Milliseconds 250; $remaining = @(Get-V3ListenerPids $port); $stillRunning = [bool](Get-V3ProcessInfo $listener) } while (($remaining.Count -or $stillRunning) -and (Get-Date) -lt $deadline)
  if ($remaining.Count) { throw "V3 $Service PID $($remaining -join ',') did not release port $port." }
  if ($stillRunning) { throw "V3 $Service process $listener has not exited; do not start another instance." }
  if (Test-Path -LiteralPath $pidFile) {
    $filePid = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw), [ref]$filePid) -and $filePid -eq $listener) {
      Remove-Item -LiteralPath $pidFile -Force
    }
  }
  Write-Host "V3 $Service stopped; ledger files were not changed."
}

function Start-V3Service([string]$Service) {
  if ($Service -notin @('backend','frontend')) { throw 'Invalid V3 service.' }
  $mutex = [System.Threading.Mutex]::new($false, "Global\BTC_PAPER_LAB_V3_START_$Service")
  $held = $false
  try {
    try { $held = $mutex.WaitOne([TimeSpan]::FromSeconds(45)) }
    catch [System.Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) { throw "V3 $Service startup is already in progress." }
  $port = if ($Service -eq 'backend') { 8012 } else { 5175 }
  $pids = @(Get-V3ListenerPids $port)
  if ($pids.Count) {
    if ($pids.Count -eq 1 -and (Test-V3OwnedListener $Service ([int]$pids[0])) -and (Test-V3Health $Service)) {
      Write-Host "V3 $Service is already running (PID $($pids[0]))."
      return
    }
    throw "Port $port is occupied by an unverified process; V3 $Service was not started."
  }
  if (Test-V3Health $Service) {
    throw "Port $port responds over HTTP, but no local Windows listener PID is visible; refusing to start a duplicate V3 $Service."
  }
  if ($Service -eq 'backend') {
    $pyPath = Join-Path $script:V3Root '.venv\Scripts\python.exe'
    $residual = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
      $_.ExecutablePath -eq $pyPath -and $_.CommandLine -match '(?i)uvicorn\s+backend\.app:app.*--port\s+8012'
    })
    if ($residual.Count) { throw 'An earlier V3 backend is still running; wait for it to exit before starting.' }
  }
  $null = New-Item -ItemType Directory -Force -Path $script:V3Run
  $logs = Join-Path $script:V3Root 'logs'
  $null = New-Item -ItemType Directory -Force -Path $logs
  $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
  if ($Service -eq 'backend') {
    $py = Join-Path $script:V3Root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $py)) { throw 'V3 Python environment is missing.' }
    $env:PAPER_AUTOMATION_ENABLED = 'true'
    $env:BTC_REQUIRE_STARTUP_MONITOR = 'true'
    $p = Start-Process $py -ArgumentList @('-m','uvicorn','backend.app:app','--host','127.0.0.1','--port','8012') -WorkingDirectory $script:V3Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs "backend-$stamp.log") -RedirectStandardError (Join-Path $logs "backend-error-$stamp.log")
  } else {
    $vite = Join-Path $script:V3Root 'frontend\node_modules\vite\bin\vite.js'
    if (-not (Test-Path -LiteralPath $vite)) { throw 'V3 frontend dependencies are missing.' }
    $node = (Get-Command node.exe -ErrorAction Stop).Source
    $env:VITE_API_BASE = 'http://127.0.0.1:8012/api/v1'
    $p = Start-Process $node -ArgumentList @($vite,'--host','127.0.0.1','--port','5175') -WorkingDirectory (Join-Path $script:V3Root 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs "frontend-$stamp.log") -RedirectStandardError (Join-Path $logs "frontend-error-$stamp.log")
  }
  Set-Content -LiteralPath (Join-Path $script:V3Run "$Service.pid") -Value $p.Id -NoNewline
  $ok = $false
  for ($i=0; $i -lt 30; $i++) { if (Test-V3Health $Service) { $ok = $true; break }; Start-Sleep -Seconds 1 }
  if (-not $ok) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; throw "V3 $Service did not become healthy; inspect logs." }
  Write-Host "V3 $Service started (PID $($p.Id))."
  } finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
  }
}
