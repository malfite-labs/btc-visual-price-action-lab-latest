# Idempotent, self-healing, Gemini-independent desktop launcher for BTC Paper Lab V3.
$ErrorActionPreference = 'Stop'
$launchMutex = [System.Threading.Mutex]::new($false, 'Global\BTC_PAPER_LAB_V3_CONSOLE_LAUNCH')
$launchHeld = $false
try {
  try { $launchHeld = $launchMutex.WaitOne([TimeSpan]::FromSeconds(45)) }
  catch [System.Threading.AbandonedMutexException] { $launchHeld = $true }
  if (-not $launchHeld) {
    exit 0
  }

  $root = Split-Path -Parent $PSScriptRoot
  Set-Location -LiteralPath $root
  $env:NO_PROXY = 'localhost,127.0.0.1,::1'
  $env:no_proxy = $env:NO_PROXY

  . (Join-Path $PSScriptRoot 'service_lifecycle.ps1')

  $apiReady = Test-V3Health 'backend'
  $webReady = Test-V3Health 'frontend'
  Write-V3LifecycleLog "launch_v3: entry state API_8012=$apiReady Web_5175=$webReady"

  try {
    if (-not $apiReady -and -not $webReady) {
      Write-V3LifecycleLog 'launch_v3: both services stopped -> starting backend then frontend.'
      Start-V3Service 'backend'
      Start-V3Service 'frontend'
    } elseif (-not $apiReady -and $webReady) {
      Write-V3LifecycleLog 'launch_v3: partial state (8012 down, 5175 up) -> auto-recovering backend and verifying frontend.'
      $fePids = @(Get-V3ListenerPids 5175)
      if ($fePids.Count -eq 1 -and -not (Test-V3OwnedListener 'frontend' ([int]$fePids[0]))) {
        throw "端口 5175 被非 V3 进程 (PID $($fePids[0])) 占用，拒绝继续启动。"
      }
      Start-V3Service 'backend'
      if (-not (Test-V3Health 'frontend')) {
        Start-V3Service 'frontend'
      }
    } elseif ($apiReady -and -not $webReady) {
      Write-V3LifecycleLog 'launch_v3: partial state (8012 up, 5175 down) -> auto-starting frontend.'
      Start-V3Service 'frontend'
    } else {
      Write-V3LifecycleLog 'launch_v3: both services healthy -> opening desktop console directly.'
    }

    # Final health gate before opening console
    $finalApi = Test-V3Health 'backend'
    $finalWeb = Test-V3Health 'frontend'
    if (-not $finalApi -or -not $finalWeb) {
      throw "服务恢复后健康检查未通过（API 8012: $finalApi; Web 5175: $finalWeb）。"
    }

    $null = Start-V3DesktopConsole
  } catch {
    $msg = $_.Exception.Message
    Write-V3LifecycleLog "launch_v3: FAILED - $msg"
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
