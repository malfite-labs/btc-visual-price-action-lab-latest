param([switch]$RearmAfterWebsocket)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$py = Join-Path $root '.venv\Scripts\python.exe'
$pidPath = Join-Path $root '.run\backend.pid'
$modePath = Join-Path $root 'data\formal-mode.json'
$freezePath = Join-Path $root 'docs\reports\abc-forward-v26\freeze.json'
$marketPath = Join-Path $root 'backend\market.py'
$stamp = [DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$evidence = Join-Path $root "docs\reports\backend-recovery-$stamp"
$logs = Join-Path $root 'logs'
$null = New-Item -ItemType Directory -Force -Path $evidence
$statusUri = 'http://127.0.0.1:8011/api/v1/system/status'

if (-not (Test-Path -LiteralPath $pidPath)) { throw 'BACKEND_PID_FILE_MISSING' }
if (-not (Test-Path -LiteralPath $py)) { throw 'PROJECT_PYTHON_MISSING' }
$mode = Get-Content -LiteralPath $modePath -Raw | ConvertFrom-Json
if ($mode.mode -ne 'RULE_DRIVEN') { throw 'FORMAL_MODE_NOT_RULE_DRIVEN' }
$freeze = Get-Content -LiteralPath $freezePath -Raw | ConvertFrom-Json
$marketHash = (Get-FileHash -LiteralPath $marketPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($freeze.risk_execution_hashes.'backend/market.py' -ne $marketHash) { throw 'ACTIVE_FREEZE_MARKET_HASH_MISMATCH' }

if ($RearmAfterWebsocket) {
  $before = Invoke-RestMethod -Uri $statusUri -TimeoutSec 4
  if ($before.websocket -ne 'CONNECTED' -or $before.websocket_recovery_state -ne 'LONG_PROTECTION' -or $before.websocket_quote_health -ne 'FRESH' -or $before.feed_health -ne 'LIVE' -or [int]$before.gap_count_288 -ne 0) { throw 'FRESH_CONNECTED_WS_AND_COMPLETE_MARKET_REQUIRED_BEFORE_REARM' }
  $received = [DateTimeOffset]::Parse($before.websocket_last_quote_received_at)
  if (([DateTimeOffset]::UtcNow - $received.ToUniversalTime()).TotalSeconds -gt 30) { throw 'WEBSOCKET_QUOTE_TOO_OLD_FOR_REARM' }
  if ($before.formal_trading_mode -ne 'RULE_DRIVEN' -or $before.scheduler -ne 'ENABLED_INTERNAL_PAPER' -or $before.vision.enabled -or $before.vision.calls_authorized -or [decimal]$before.vision.daily_budget_usd -ne 0) { throw 'LIVE_CONFIGURATION_CHANGED' }
@'
import sqlite3
from pathlib import Path
p=Path('data/paper.sqlite3').resolve();c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
if c.execute('pragma quick_check').fetchone()[0]!='ok':raise SystemExit('FORMAL_LEDGER_INTEGRITY_FAILED')
if c.execute("select count(*) from accounts where state!='FLAT' or position_json is not null or pending_stop_json is not null").fetchone()[0]:raise SystemExit('FORMAL_POSITION_OR_PENDING_EXIT_PRESENT')
if c.execute("select count(*) from scheduler_cycles where status='RUNNING'").fetchone()[0]:raise SystemExit('FORMAL_CYCLE_IN_PROGRESS')
c.close();print('FORMAL_LEDGER_CLEAR_FOR_REARM')
'@ | & $py
  if ($LASTEXITCODE -ne 0) { throw 'FORMAL_LEDGER_REARM_GATE_FAILED' }
  $before | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $evidence 'pre-rearm-status.json') -Encoding utf8
} else {
  @'
from pathlib import Path
import sqlite3,shutil,sys
root=Path.cwd();out=Path(sys.argv[1])
for rel,name in [('data/paper.sqlite3','formal.sqlite3'),('docs/reports/abc-forward-v26/shadow-ledger.sqlite3','shadow.sqlite3')]:
 src=sqlite3.connect((root/rel).resolve().as_uri()+'?mode=ro',uri=True);dst=sqlite3.connect(out/name)
 src.backup(dst);dst.close();src.close()
 c=sqlite3.connect(out/name);result=c.execute('pragma quick_check').fetchone()[0];c.close()
 if result!='ok':raise SystemExit('BACKUP_QUICK_CHECK_FAILED:'+name)
 print('BACKUP_OK',name)
for rel,name in [('data/formal-mode.json','formal-mode.json'),('docs/reports/abc-forward-v26/freeze.json','freeze.json'),('docs/reports/abc-forward-v26/status.json','shadow-status.json')]:
 shutil.copy2(root/rel,out/name)
'@ | & $py - $evidence
  if ($LASTEXITCODE -ne 0) { throw 'PRE_RESTART_BACKUP_FAILED' }
}

$parentId = [int](Get-Content -LiteralPath $pidPath -Raw)
$parent = Get-CimInstance Win32_Process -Filter "ProcessId=$parentId"
$listeners = @(Get-NetTCPConnection -LocalPort 8011 -State Listen -ErrorAction SilentlyContinue)
$childId = $null
if ($parent -or $listeners.Count) {
  if (-not $parent -or $parent.CommandLine -notmatch 'uvicorn backend\.app:app' -or $parent.ExecutablePath -ne $py) { throw 'BACKEND_PARENT_IDENTITY_MISMATCH' }
  $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$parentId")
  $workers = @($children | Where-Object { $_.CommandLine -match 'uvicorn backend\.app:app' -and $_.Name -match '^python(?:w)?\.exe$' })
  $helpers = @($children | Where-Object { $_.Name -eq 'conhost.exe' -and $_.ExecutablePath -ieq (Join-Path $env:SystemRoot 'System32\conhost.exe') })
  if ($workers.Count -ne 1 -or ($workers.Count + $helpers.Count) -ne $children.Count) { throw 'BACKEND_CHILD_IDENTITY_MISMATCH' }
  $childId = [int]$workers[0].ProcessId
  if ($listeners.Count -ne 1 -or $listeners[0].OwningProcess -ne $childId -or $listeners[0].LocalAddress -ne '127.0.0.1') { throw 'BACKEND_LISTENER_IDENTITY_MISMATCH' }
}
$allBackend = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'uvicorn backend\.app:app' })
if ($allBackend.Count -gt 2 -or ($allBackend | Where-Object { $_.ProcessId -notin @($parentId,$childId) }).Count) { throw 'UNIDENTIFIED_BACKEND_PROCESS_PRESENT' }
if ($childId) {
  Stop-Process -Id $childId -Force
  for ($i=0; $i -lt 40; $i++) {
    if (-not (Get-Process -Id $childId -ErrorAction SilentlyContinue) -and -not (Get-NetTCPConnection -LocalPort 8011 -State Listen -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 250
  }
  if (Get-Process -Id $childId -ErrorAction SilentlyContinue) { throw 'BACKEND_LISTENER_PROCESS_STILL_RUNNING' }
  if (Get-NetTCPConnection -LocalPort 8011 -State Listen -ErrorAction SilentlyContinue) { throw 'PORT_8011_STILL_LISTENING' }
  if (Get-Process -Id $parentId -ErrorAction SilentlyContinue) { Stop-Process -Id $parentId -Force }
  for ($i=0; $i -lt 20; $i++) { if (-not (Get-Process -Id $parentId -ErrorAction SilentlyContinue)) { break }; Start-Sleep -Milliseconds 250 }
  if (Get-Process -Id $parentId -ErrorAction SilentlyContinue) { throw 'BACKEND_PARENT_STILL_RUNNING' }
} elseif ($listeners.Count) { throw 'PORT_8011_IN_USE' }

@'
from pathlib import Path
from backend.market import _StreamLease
leases=[_StreamLease(Path('.run')/n) for n in ('paper-scheduler.lock','alpaca-stream.lock')];held=[]
try:
 for x in leases:
  if not x.acquire():raise SystemExit('OLD_LEASE_HELD:'+str(x.path))
  held.append(x)
 print('OLD_SCHEDULER_AND_STREAM_LEASES_FREE')
finally:
 for x in reversed(held):x.release()
'@ | & $py
if ($LASTEXITCODE -ne 0) { throw 'OLD_LEASE_NOT_RELEASED' }

if ($RearmAfterWebsocket) {
@'
from pathlib import Path
from backend.market import AlpacaMarketData
m=AlpacaMarketData(recovery_history_path=Path('.run')/'alpaca-stream-recovery.jsonl')
m.clear_recovery_protection('Fresh connected WebSocket quote and complete market data verified before controlled offline re-arm')
print('OPERATOR_REARM_RECORDED')
'@ | & $py
  if ($LASTEXITCODE -ne 0) { throw 'OFFLINE_REARM_FAILED' }
}

$env:BTC_REQUIRE_STARTUP_MONITOR = 'true'
$env:PAPER_AUTOMATION_ENABLED = 'true'
$env:VISION_ENABLED = 'false'
$env:VISION_CALLS_AUTHORIZED = 'false'
$env:VISION_MAX_CALLS_PER_UTC_DAY = '0'
$env:VISION_MAX_CALL_COST_USD = '0'
$env:VISION_DAILY_BUDGET_USD = '0'
$env:ALPACA_CREDENTIALS_FILE = Join-Path $root 'data\alpaca-market-new.env'
$std = Join-Path $logs "backend-recovery-$stamp.log"
$err = Join-Path $logs "backend-recovery-error-$stamp.log"
$new = Start-Process -FilePath $py -ArgumentList @('-m','uvicorn','backend.app:app','--host','127.0.0.1','--port','8011') -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput $std -RedirectStandardError $err
Set-Content -LiteralPath $pidPath -Value $new.Id -NoNewline
$ready = $false
$deadline = [DateTimeOffset]::UtcNow.AddSeconds(120)
while ([DateTimeOffset]::UtcNow -lt $deadline) {
  try {
    $s = Invoke-RestMethod -Uri $statusUri -TimeoutSec 3
    if ($s.scheduler -eq 'ENABLED_INTERNAL_PAPER' -and $s.formal_trading_mode -eq 'RULE_DRIVEN' -and -not $s.vision.enabled -and -not $s.vision.calls_authorized -and [decimal]$s.vision.daily_budget_usd -eq 0) {
      if (-not $RearmAfterWebsocket -or ($s.websocket -eq 'CONNECTED' -and $s.websocket_recovery_state -eq 'HEALTHY' -and $s.websocket_quote_health -eq 'FRESH')) { $ready = $true; break }
    }
  } catch {}
  Start-Sleep -Seconds 2
}
if (-not $ready) { throw "BACKEND_STARTED_BUT_ACCEPTANCE_PENDING:PID=$($new.Id);LOG=$std;ERR=$err" }
$started = Get-Process -Id $new.Id -ErrorAction Stop
$listener = Get-NetTCPConnection -LocalPort 8011 -State Listen -ErrorAction Stop
if ($listener.OwningProcess -eq $new.Id) { $listenerPid = $new.Id } else { $listenerPid = $listener.OwningProcess }
if ($started.StartTime.ToUniversalTime() -le (Get-Item -LiteralPath $marketPath).LastWriteTimeUtc) { throw 'BACKEND_START_PRECEDES_MARKET_SOURCE' }
@{
  at_utc=[DateTimeOffset]::UtcNow.ToString('o'); parent_pid=$new.Id; listener_pid=$listenerPid
  market_sha256=$marketHash; formal_mode=$s.formal_trading_mode; scheduler=$s.scheduler
  vision_enabled=$s.vision.enabled; vision_calls_authorized=$s.vision.calls_authorized
  websocket=$s.websocket; recovery_state=$s.websocket_recovery_state; websocket_quote_health=$s.websocket_quote_health
  websocket_quote_received_at=$s.websocket_last_quote_received_at; stdout=$std; stderr=$err
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $evidence 'deployment.json') -Encoding utf8
Write-Output "BACKEND_RECOVERED PID=$($new.Id) LISTENER=$listenerPid MODE=$($s.formal_trading_mode) SCHEDULER=$($s.scheduler) WS=$($s.websocket) RECOVERY=$($s.websocket_recovery_state) LOG=$std ERR=$err EVIDENCE=$evidence"
