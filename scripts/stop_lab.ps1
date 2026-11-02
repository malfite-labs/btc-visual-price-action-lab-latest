$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$run = Join-Path $root '.run'
foreach ($name in 'frontend','backend') {
  $pidFile = Join-Path $run "$name.pid"
  if (-not (Test-Path -LiteralPath $pidFile)) { continue }
  $id = 0; [void][int]::TryParse((Get-Content -LiteralPath $pidFile -Raw), [ref]$id)
  if ($id -gt 0) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue
    if ($proc) {
      $cmd = [string]$proc.CommandLine
      $owned = if ($name -eq 'backend') { $cmd.Contains('backend.app:app') } else { $cmd.Contains((Join-Path $root 'frontend\node_modules\vite\bin\vite.js')) }
      if ($owned) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    }
  }
  Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}
Write-Host 'BTC Visual Price Action Lab stopped. No trading positions or orders were created.'
