$ErrorActionPreference = 'Stop'
$launchMutex = [System.Threading.Mutex]::new($false, 'Global\BTC_PAPER_LAB_V3_CONSOLE_LAUNCH')
$launchHeld = $false
try {
  try { $launchHeld = $launchMutex.WaitOne([TimeSpan]::FromSeconds(45)) }
  catch [System.Threading.AbandonedMutexException] { $launchHeld = $true }
  if (-not $launchHeld) { throw 'V3 console startup is already in progress.' }
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$env:NO_PROXY = 'localhost,127.0.0.1,::1'
$env:no_proxy = $env:NO_PROXY

function Test-LocalHttp([string]$url) {
  try {
    $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
    return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400)
  } catch {
    return $false
  }
}

$apiReady = Test-LocalHttp 'http://127.0.0.1:8012/api/v1/health'
$webReady = Test-LocalHttp 'http://127.0.0.1:5175/'

if (-not $apiReady -and -not $webReady) {
  & (Join-Path $PSScriptRoot 'start_lab.ps1')
} elseif (-not $apiReady -or -not $webReady) {
  Add-Type -AssemblyName System.Windows.Forms
  [System.Windows.Forms.MessageBox]::Show(
    "V3 is only partially running. API 8012: $apiReady; Web 5175: $webReady. Check the V3 logs before restarting.",
    'BTC Paper Lab V3',
    [System.Windows.Forms.MessageBoxButtons]::OK,
    [System.Windows.Forms.MessageBoxIcon]::Warning
  ) | Out-Null
  exit 1
}

$pidFile = Join-Path $root '.run\desktop_console.pid'
$existingPid = 0
if (Test-Path -LiteralPath $pidFile) {
  $parsedPid = 0
  if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw), [ref]$parsedPid) -and
      (Get-Process -Id $parsedPid -ErrorAction SilentlyContinue)) {
    $existingPid = $parsedPid
  }
}

if ($existingPid -eq 0) {
  $pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
  $dashboard = Join-Path $root 'scripts\local_dashboard.py'
  if (-not (Test-Path -LiteralPath $pythonw) -or -not (Test-Path -LiteralPath $dashboard)) {
    throw 'V3 Python environment or BTC paper console entrypoint is missing.'
  }
  $console = Start-Process -FilePath $pythonw -ArgumentList @($dashboard) -WorkingDirectory $root -WindowStyle Hidden -PassThru
  Set-Content -LiteralPath $pidFile -Value $console.Id -NoNewline -Encoding ascii
}
} finally {
  if ($launchHeld) { $launchMutex.ReleaseMutex() }
  $launchMutex.Dispose()
}
