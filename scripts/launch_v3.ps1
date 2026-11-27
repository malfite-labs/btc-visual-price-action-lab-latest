# Instant-UI, parallel-boot, proxy-immune, Gemini-independent desktop launcher for BTC Paper Lab V3.
$ErrorActionPreference = 'Stop'
$totalSw = [System.Diagnostics.Stopwatch]::StartNew()
$launchMutex = [System.Threading.Mutex]::new($false, 'Global\BTC_PAPER_LAB_V3_CONSOLE_LAUNCH')
$launchHeld = $false
try {
  try { $launchHeld = $launchMutex.WaitOne([TimeSpan]::FromMilliseconds(300)) }
  catch [System.Threading.AbandonedMutexException] { $launchHeld = $true }
  if (-not $launchHeld) {
    # Another launch is already in progress; avoid duplicate processes or waiting dialog.
    exit 0
  }

  $root = Split-Path -Parent $PSScriptRoot
  Set-Location -LiteralPath $root
  . (Join-Path $PSScriptRoot 'service_lifecycle.ps1')

  # 1. Fast loopback check (~5-15ms, proxy-immune)
  $tCheckStart = $totalSw.ElapsedMilliseconds
  $apiReady = Test-V3Health 'backend'
  $webReady = Test-V3Health 'frontend'
  $tCheckMs = $totalSw.ElapsedMilliseconds - $tCheckStart
  Write-V3LifecycleLog "launch_v3: entry check API_8012=$apiReady Web_5175=$webReady (${tCheckMs}ms)"

  # 2. If both already healthy: immediately focus/open Desktop Console and exit (< 1s!)
  if ($apiReady -and $webReady) {
    Write-V3StartupState @{ state = 'ready'; phase = '前后端均已就绪'; api_ready = $true; web_ready = $true; total_ms = $totalSw.ElapsedMilliseconds }
    $null = Start-V3DesktopConsole
    Write-V3LifecycleLog "launch_v3: warm launch complete in $($totalSw.ElapsedMilliseconds)ms."
    exit 0
  }

  # 3. Partial or cold start: show Desktop Console IMMEDIATELY so user gets < 3s visible feedback!
  $phaseText = if (-not $apiReady -and -not $webReady) { '正在并行启动 8012 后端与 5175 前端…' }
               elseif (-not $apiReady) { '5175 前端已就绪，正在自动恢复 8012 后端…' }
               else { '8012 后端已就绪，正在自动启动 5175 前端…' }
  Write-V3StartupState @{
    state = 'starting'
    phase = $phaseText
    api_ready = $apiReady
    web_ready = $webReady
    started_at_ms = $totalSw.ElapsedMilliseconds
  }
  $null = Start-V3DesktopConsole
  $tConsoleMs = $totalSw.ElapsedMilliseconds
  Write-V3LifecycleLog "launch_v3: desktop console shown at +${tConsoleMs}ms; spawning missing services in parallel."

  try {
    # 4. Spawn missing services IN PARALLEL (detached via Win32_Process.Create under WmiPrvSE)
    $proxy = if (-not $apiReady) { Get-V3UserProxy } else { '' }
    $needApi = -not $apiReady
    $needWeb = -not $webReady
    $apiSpawn = $null
    $webSpawn = $null

    if ($needApi) {
      $apiSpawn = Invoke-V3SpawnService -Service 'backend' -Proxy $proxy
      if ($apiSpawn.Status -eq 'already_running') { $apiReady = $true; $needApi = $false }
    }
    if ($needWeb) {
      $webSpawn = Invoke-V3SpawnService -Service 'frontend'
      if ($webSpawn.Status -eq 'already_running') { $webReady = $true; $needWeb = $false }
    }
    $tSpawnMs = $totalSw.ElapsedMilliseconds
    Write-V3LifecycleLog "launch_v3: detached spawn issued at +${tSpawnMs}ms (needApi=$needApi needWeb=$needWeb)."

    # 5. Parallel health polling loop (250ms cadence, proxy-immune, 2ms fast-fail per closed port)
    $deadline = (Get-Date).AddSeconds(45)
    $apiReadyMs = if ($apiReady) { $tCheckMs } else { -1 }
    $webReadyMs = if ($webReady) { $tCheckMs } else { -1 }
    $apiRetried = $false
    $webRetried = $false

    while ((-not $apiReady -or -not $webReady) -and (Get-Date) -lt $deadline) {
      if (-not $apiReady -and (Test-V3Health 'backend')) {
        $apiReady = $true
        $apiReadyMs = $totalSw.ElapsedMilliseconds
        $live = @(Get-V3ListenerPids 8012)
        if ($live.Count -eq 1) { Set-Content -LiteralPath (Join-Path $script:V3Run 'backend.pid') -Value $live[0] -NoNewline -Encoding ascii }
        Write-V3LifecycleLog "launch_v3: backend 8012 healthy at +${apiReadyMs}ms (PID $($live -join ','))."
        Write-V3StartupState @{ state = 'starting'; phase = '8012 后端已就绪，正在等待 5175 前端…'; api_ready = $true; web_ready = $webReady; api_ms = $apiReadyMs }
      }
      if (-not $webReady -and (Test-V3Health 'frontend')) {
        $webReady = $true
        $webReadyMs = $totalSw.ElapsedMilliseconds
        $live = @(Get-V3ListenerPids 5175)
        if ($live.Count -eq 1) { Set-Content -LiteralPath (Join-Path $script:V3Run 'frontend.pid') -Value $live[0] -NoNewline -Encoding ascii }
        Write-V3LifecycleLog "launch_v3: frontend 5175 healthy at +${webReadyMs}ms (PID $($live -join ','))."
        Write-V3StartupState @{ state = 'starting'; phase = '5175 前端已就绪，正在等待 8012 后端初始化…'; api_ready = $apiReady; web_ready = $true; web_ms = $webReadyMs }
      }
      if ($apiReady -and $webReady) { break }

      # Single finite retry if a service still hasn't come up past 22s
      if (-not $apiReady -and -not $apiRetried -and $totalSw.ElapsedMilliseconds -gt 22000) {
        $apiRetried = $true
        Write-V3LifecycleLog 'launch_v3: backend retry attempt 2/2.'
        $apiSpawn = Invoke-V3SpawnService -Service 'backend' -Proxy $proxy
      }
      if (-not $webReady -and -not $webRetried -and $totalSw.ElapsedMilliseconds -gt 18000) {
        $webRetried = $true
        Write-V3LifecycleLog 'launch_v3: frontend retry attempt 2/2.'
        $webSpawn = Invoke-V3SpawnService -Service 'frontend'
      }
      Start-Sleep -Milliseconds 250
    }

    if (-not $apiReady -or -not $webReady) {
      throw "服务未在时限内通过健康检查（API 8012: $apiReady; Web 5175: $webReady）。"
    }

    Write-V3StartupState @{
      state = 'ready'
      phase = '前后端均已就绪'
      api_ready = $true
      web_ready = $true
      console_ms = $tConsoleMs
      api_ms = $apiReadyMs
      web_ms = $webReadyMs
      total_ms = $totalSw.ElapsedMilliseconds
    }
    Write-V3LifecycleLog "launch_v3: ALL READY in $($totalSw.ElapsedMilliseconds)ms (console=${tConsoleMs}ms, api=${apiReadyMs}ms, web=${webReadyMs}ms)."
  } catch {
    $msg = $_.Exception.Message
    Write-V3StartupState @{ state = 'error'; phase = '启动失败'; error = $msg; api_ready = $apiReady; web_ready = $webReady; total_ms = $totalSw.ElapsedMilliseconds }
    Write-V3LifecycleLog "launch_v3: FAILED at +$($totalSw.ElapsedMilliseconds)ms - $msg"
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
      "BTC Paper Lab V3 启动或恢复失败：`n`n$msg`n`n请查看日志目录：`n$(Join-Path $root 'logs')`n（launcher-lifecycle.log 及最新 backend-error-*.log）",
      'BTC Paper Lab V3 启动异常',
      [System.Windows.Forms.MessageBoxButtons]::OK,
      [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
    exit 1
  }
} finally {
  if ($launchHeld) { $launchMutex.ReleaseMutex() }
  $launchMutex.Dispose()
}
