# Shared, ownership-aware, Job-Object-decoupled lifecycle helpers for standalone BTC Paper Lab V3.
$ErrorActionPreference = 'Stop'
$script:V3Root = Split-Path -Parent $PSScriptRoot
$script:V3Run = Join-Path $script:V3Root '.run'
$script:V3Logs = Join-Path $script:V3Root 'logs'
$script:V3LifecycleLog = Join-Path $script:V3Logs 'launcher-lifecycle.log'

function Write-V3LifecycleLog([string]$Message) {
  try {
    $null = New-Item -ItemType Directory -Force -Path $script:V3Logs
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss.fff')
    Add-Content -LiteralPath $script:V3LifecycleLog -Value "[$ts] $Message" -Encoding utf8
  } catch {}
}

function Get-V3VenvHome() {
  $cfg = Join-Path $script:V3Root '.venv\pyvenv.cfg'
  if (Test-Path -LiteralPath $cfg) {
    foreach ($line in Get-Content -LiteralPath $cfg -ErrorAction SilentlyContinue) {
      if ($line -match '^\s*home\s*=\s*(.+?)\s*$') {
        return $Matches[1].Trim()
      }
    }
  }
  return ''
}

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
  $venvHome = Get-V3VenvHome
  $exeIsVenvBase = $exe -and $venvHome -and $exe.StartsWith($venvHome, [StringComparison]::OrdinalIgnoreCase)
  $cmdIsV3 = ($cmd -match '(?i)uvicorn\s+backend\.app:app') -and ($cmd -match '(?i)(--port\s+8012|8012)')
  $parent = Get-V3ProcessInfo ([int]$proc.ParentProcessId)
  $parentIsVenv = $parent -and ([string]$parent.ExecutablePath).Equals((Join-Path $root '.venv\Scripts\python.exe'), [StringComparison]::OrdinalIgnoreCase) -and ([string]$parent.CommandLine -match '(?i)uvicorn\s+backend\.app:app.*--port\s+8012')
  return (($exeIsVenv -and $cmdIsV3) -or (($cmd -match [regex]::Escape($root)) -and $cmdIsV3) -or ($cmdIsV3 -and $parentIsVenv) -or ($exeIsVenvBase -and $cmdIsV3))
}

function Get-V3ConsolePids() {
  $root = [IO.Path]::GetFullPath($script:V3Root).TrimEnd('\')
  $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    ($_.Name -match '(?i)^pythonw?\.exe$') -and
    ([string]$_.CommandLine -match '(?i)local_dashboard\.py') -and
    ([string]$_.CommandLine -match [regex]::Escape($root))
  })
  return @($procs | ForEach-Object { [int]$_.ProcessId } | Sort-Object -Unique)
}

function Get-V3UserProxy() {
  try {
    $reg = Get-ItemProperty -Path 'HKCU:\Environment' -ErrorAction Stop
    foreach ($k in @('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY')) {
      $val = $reg.$k
      if ($val -and ([string]$val).Trim()) { return ([string]$val).Trim() }
    }
  } catch {}
  return ''
}

function Get-V3DAutoEnabled() {
  $cfgPath = Join-Path $script:V3Root 'data\d\auto-paper.json'
  if (Test-Path -LiteralPath $cfgPath) {
    try {
      $obj = Get-Content -LiteralPath $cfgPath -Raw -Encoding utf8 | ConvertFrom-Json
      if ($null -ne $obj.enabled) { return ([bool]$obj.enabled).ToString().ToLowerInvariant() }
    } catch {}
  }
  return 'true'
}

function Start-V3DetachedCommand([string]$CommandLine, [string]$WorkingDirectory, [uint16]$ShowWindow = 0) {
  # Win32_Process.Create spawns under WmiPrvSE, completely detached from any caller terminal
  # or AI IDE Job Object (Gemini/Codex), ensuring V3 survives closing Gemini or terminals.
  $startup = New-CimInstance -ClassName Win32_ProcessStartup -Property @{ ShowWindow = $ShowWindow } -ClientOnly
  $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $CommandLine
    CurrentDirectory = $WorkingDirectory
    ProcessStartupInformation = $startup
  }
  if ($res.ReturnValue -ne 0) {
    throw "Win32_Process.Create failed with code $($res.ReturnValue)"
  }
  return [int]$res.ProcessId
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
  if (-not [V3ConsoleSignal]::AttachConsole([uint32]$Listener)) {
    throw "NO_CONSOLE"
  }
  try {
    $ids = New-Object 'uint32[]' 32
    $count = [V3ConsoleSignal]::GetConsoleProcessList($ids,32)
    $others = @($ids | Select-Object -First $count | Where-Object { $_ -notin @([uint32]$PID,[uint32]$Listener,[uint32]$parentId) })
    if ($count -ne 3 -or $others.Count) { throw 'CONSOLE_SHARED' }
    if (-not [V3ConsoleSignal]::SetConsoleCtrlHandler([IntPtr]::Zero,$true)) { throw 'HANDLER_FAILED' }
    if (-not [V3ConsoleSignal]::GenerateConsoleCtrlEvent(0,0)) { throw 'CTRL_FAILED' }
  } finally { [void][V3ConsoleSignal]::FreeConsole() }
}

function Test-V3Health([string]$Service) {
  $env:NO_PROXY = 'localhost,127.0.0.1,::1'
  $env:no_proxy = $env:NO_PROXY
  try {
    if ($Service -eq 'backend') {
      $h = Invoke-RestMethod 'http://127.0.0.1:8012/api/v1/health' -TimeoutSec 3
      return ($h.status -eq 'ok' -and $h.live_trading_disabled -eq $true)
    }
    $w = Invoke-WebRequest 'http://127.0.0.1:5175/' -UseBasicParsing -TimeoutSec 3
    return ($w.StatusCode -eq 200)
  } catch { return $false }
}

function Stop-V3Service([string]$Service, [switch]$AllowUnhealthy) {
  $port = if ($Service -eq 'backend') { 8012 } else { 5175 }
  $pidFile = Join-Path $script:V3Run "$Service.pid"
  $pids = @(Get-V3ListenerPids $port)
  if (-not $pids.Count) {
    if (Test-V3Health $Service) {
      throw "Port $port responds over HTTP, but no local Windows listener PID is visible; service state is ambiguous and nothing was stopped."
    }
    Write-V3LifecycleLog "Stop-V3Service($Service): already stopped (port $port has no listener)."
    Write-Host "V3 $Service is already stopped (port $port has no listener)."
    return
  }
  if ($pids.Count -ne 1) { throw "Port $port has multiple listener PIDs ($($pids -join ', ')); nothing was stopped." }
  $listener = [int]$pids[0]
  if (-not (Test-V3OwnedListener $Service $listener)) {
    throw "Port $port is held by PID $listener, which cannot be verified as this V3 $Service process; nothing was stopped."
  }
  if (-not $AllowUnhealthy -and -not (Test-V3Health $Service)) {
    throw "PID $listener matches V3 $Service but its service health check failed; use -AllowUnhealthy to clean up."
  }
  $proc = Get-V3ProcessInfo $listener
  $parentId = if ($proc) { [int]$proc.ParentProcessId } else { 0 }
  Write-V3LifecycleLog "Stop-V3Service($Service): stopping verified V3 PID $listener (AllowUnhealthy=$AllowUnhealthy)."
  if ($Service -eq 'backend') {
    try {
      Stop-V3BackendGracefully $listener
    } catch {
      # Detached hidden backend has no attached console; terminate verified V3 listener cleanly.
      Stop-Process -Id $listener -Force -ErrorAction SilentlyContinue
      if ($parentId -gt 0 -and (Test-V3OwnedListener 'backend' $parentId)) {
        Stop-Process -Id $parentId -Force -ErrorAction SilentlyContinue
      }
    }
  } else {
    Stop-Process -Id $listener -Force -ErrorAction SilentlyContinue
  }
  $deadline = (Get-Date).AddSeconds(10)
  do { Start-Sleep -Milliseconds 250; $remaining = @(Get-V3ListenerPids $port); $stillRunning = [bool](Get-V3ProcessInfo $listener) } while (($remaining.Count -or $stillRunning) -and (Get-Date) -lt $deadline)
  if ($remaining.Count -or $stillRunning) {
    Stop-Process -Id $listener -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    $remaining = @(Get-V3ListenerPids $port)
    $stillRunning = [bool](Get-V3ProcessInfo $listener)
  }
  if ($remaining.Count) { throw "V3 $Service PID $($remaining -join ',') did not release port $port." }
  if ($stillRunning) { throw "V3 $Service process $listener has not exited; do not start another instance." }
  if (Test-Path -LiteralPath $pidFile) {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
  }
  Write-V3LifecycleLog "Stop-V3Service($Service): stopped cleanly; ledgers untouched."
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
        Set-Content -LiteralPath (Join-Path $script:V3Run "$Service.pid") -Value $pids[0] -NoNewline -Encoding ascii
        Write-V3LifecycleLog "Start-V3Service($Service): already healthy (PID $($pids[0]))."
        Write-Host "V3 $Service is already running (PID $($pids[0]))."
        return
      }
      if ($pids.Count -eq 1 -and (Test-V3OwnedListener $Service ([int]$pids[0]))) {
        Write-V3LifecycleLog "Start-V3Service($Service): found unhealthy V3-owned PID $($pids[0]) on port $port; cleaning up before restart."
        Stop-V3Service $Service -AllowUnhealthy
      } else {
        throw "Port $port is occupied by an unverified non-V3 process ($($pids -join ',')); V3 $Service was not started."
      }
    }
    if (Test-V3Health $Service) {
      throw "Port $port responds over HTTP, but no local Windows listener PID is visible; refusing to start a duplicate V3 $Service."
    }

    if ($Service -eq 'backend') {
      $pyPath = Join-Path $script:V3Root '.venv\Scripts\python.exe'
      $residual = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -eq $pyPath -and $_.CommandLine -match '(?i)uvicorn\s+backend\.app:app.*--port\s+8012'
      })
      foreach ($r in $residual) {
        Write-V3LifecycleLog "Start-V3Service(backend): stopping residual non-listening V3 backend PID $($r.ProcessId)."
        Stop-Process -Id $r.ProcessId -Force -ErrorAction SilentlyContinue
      }
      if ($residual.Count) { Start-Sleep -Milliseconds 500 }
    }

    $null = New-Item -ItemType Directory -Force -Path $script:V3Run
    $null = New-Item -ItemType Directory -Force -Path $script:V3Logs
    $proxy = Get-V3UserProxy
    $maxAttempts = 2

    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
      $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
      $outLog = Join-Path $script:V3Logs "$Service-$stamp.log"
      $errLog = Join-Path $script:V3Logs "$Service-error-$stamp.log"

      if ($Service -eq 'backend') {
        $py = Join-Path $script:V3Root '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $py)) { throw 'V3 Python environment is missing.' }
        $dAuto = Get-V3DAutoEnabled
        $proxyPrefix = if ($proxy) { "set `"HTTP_PROXY=$proxy`" && set `"HTTPS_PROXY=$proxy`" && set `"ALL_PROXY=$proxy`" && " } else { '' }
        $cmdLine = "cmd.exe /d /c `"${proxyPrefix}set `"NO_PROXY=localhost,127.0.0.1,::1`" && set `"no_proxy=localhost,127.0.0.1,::1`" && set `"PAPER_AUTOMATION_ENABLED=true`" && set `"BTC_REQUIRE_STARTUP_MONITOR=true`" && set `"D_AUTO_PAPER_ENABLED=$dAuto`" && `"$py`" -m uvicorn backend.app:app --host 127.0.0.1 --port 8012 >> `"$outLog`" 2>> `"$errLog`"`""
        Write-V3LifecycleLog "Start-V3Service(backend): attempt $attempt/$maxAttempts via Win32_Process.Create (detached)."
        $spawnPid = Start-V3DetachedCommand -CommandLine $cmdLine -WorkingDirectory $script:V3Root
        $waitSec = 45
      } else {
        $vite = Join-Path $script:V3Root 'frontend\node_modules\vite\bin\vite.js'
        if (-not (Test-Path -LiteralPath $vite)) { throw 'V3 frontend dependencies are missing.' }
        $node = (Get-Command node.exe -ErrorAction Stop).Source
        $feDir = Join-Path $script:V3Root 'frontend'
        $cmdLine = "cmd.exe /d /c `"set `"NO_PROXY=localhost,127.0.0.1,::1`" && set `"VITE_API_BASE=http://127.0.0.1:8012/api/v1`" && `"$node`" `"$vite`" --host 127.0.0.1 --port 5175 >> `"$outLog`" 2>> `"$errLog`"`""
        Write-V3LifecycleLog "Start-V3Service(frontend): attempt $attempt/$maxAttempts via Win32_Process.Create (detached)."
        $spawnPid = Start-V3DetachedCommand -CommandLine $cmdLine -WorkingDirectory $feDir
        $waitSec = 35
      }

      $ok = $false
      for ($i = 0; $i -lt $waitSec; $i++) {
        if (Test-V3Health $Service) { $ok = $true; break }
        Start-Sleep -Seconds 1
      }
      if ($ok) {
        $livePids = @(Get-V3ListenerPids $port)
        $recordedPid = if ($livePids.Count -eq 1) { $livePids[0] } else { $spawnPid }
        Set-Content -LiteralPath (Join-Path $script:V3Run "$Service.pid") -Value $recordedPid -NoNewline -Encoding ascii
        Write-V3LifecycleLog "Start-V3Service($Service): healthy on port $port (PID $recordedPid)."
        Write-Host "V3 $Service started (PID $recordedPid)."
        return
      }
      Write-V3LifecycleLog "Start-V3Service($Service): attempt $attempt did not become healthy within ${waitSec}s (errLog=$errLog)."
      $leftover = @(Get-V3ListenerPids $port)
      foreach ($lp in $leftover) {
        if (Test-V3OwnedListener $Service $lp) { Stop-Process -Id $lp -Force -ErrorAction SilentlyContinue }
      }
    }
    throw "V3 $Service failed to become healthy after $maxAttempts attempts. Check logs: $script:V3Logs"
  } finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
  }
}

function Start-V3DesktopConsole() {
  $existing = @(Get-V3ConsolePids)
  if ($existing.Count -gt 0) {
    Write-V3LifecycleLog "Start-V3DesktopConsole: already running (PID $($existing -join ',')); skipping duplicate launch."
    # Bring existing console window to foreground if possible
    try {
      Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class V3WinFocus {
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
'@ -ErrorAction SilentlyContinue
      foreach ($cpid in $existing) {
        $gp = Get-Process -Id $cpid -ErrorAction SilentlyContinue
        if ($gp -and $gp.MainWindowHandle -ne [IntPtr]::Zero) {
          [void][V3WinFocus]::ShowWindowAsync($gp.MainWindowHandle, 9)
          [void][V3WinFocus]::SetForegroundWindow($gp.MainWindowHandle)
        }
      }
    } catch {}
    return $existing[0]
  }
  $pythonw = Join-Path $script:V3Root '.venv\Scripts\pythonw.exe'
  $dashboard = Join-Path $script:V3Root 'scripts\local_dashboard.py'
  if (-not (Test-Path -LiteralPath $pythonw) -or -not (Test-Path -LiteralPath $dashboard)) {
    throw 'V3 Python environment or BTC paper console entrypoint is missing.'
  }
  # Launch desktop GUI in the caller's interactive Windows session (never Session 0 WMI)
  # so the Tkinter console window is immediately visible on the user's desktop.
  $console = Start-Process -FilePath $pythonw -ArgumentList @($dashboard) -WorkingDirectory $script:V3Root -PassThru
  $consolePid = [int]$console.Id
  $null = New-Item -ItemType Directory -Force -Path $script:V3Run
  Set-Content -LiteralPath (Join-Path $script:V3Run 'desktop_console.pid') -Value $consolePid -NoNewline -Encoding ascii
  Write-V3LifecycleLog "Start-V3DesktopConsole: launched interactive desktop console (PID $consolePid)."
  return $consolePid
}
