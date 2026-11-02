param([Parameter(Mandatory=$true)][ValidateSet('RULE_DRIVEN','VISUAL')][string]$Mode,[switch]$Recovery)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$py=Join-Path $root '.venv\Scripts\python.exe'
$pidPath=Join-Path $root '.run\backend.pid'
if (-not (Test-Path -LiteralPath $pidPath)) { throw 'BACKEND_PID_FILE_MISSING' }
$parentId=[int](Get-Content -LiteralPath $pidPath -Raw)
$parent=Get-CimInstance Win32_Process -Filter "ProcessId=$parentId"
if (-not $parent -or $parent.CommandLine -notmatch 'uvicorn backend\.app:app' -or $parent.ExecutablePath -ne $py) { throw 'BACKEND_PARENT_IDENTITY_MISMATCH' }
$children=@(Get-CimInstance Win32_Process -Filter "ParentProcessId=$parentId")
$workers=@($children | Where-Object { $_.CommandLine -match 'uvicorn backend\.app:app' -and $_.Name -match '^python(?:w)?\.exe$' })
$helpers=@($children | Where-Object { $_.Name -eq 'conhost.exe' -and $_.ExecutablePath -ieq (Join-Path $env:SystemRoot 'System32\conhost.exe') })
if ($workers.Count -ne 1 -or ($workers.Count + $helpers.Count) -ne $children.Count) { throw 'BACKEND_CHILD_IDENTITY_MISMATCH' }
$childId=[int]$workers[0].ProcessId
if (@(Get-CimInstance Win32_Process -Filter "ParentProcessId=$childId").Count -ne 0) { throw 'UNEXPECTED_BACKEND_DESCENDANT' }
$oldProcessIds=@($parentId,$childId)+@($helpers | ForEach-Object { [int]$_.ProcessId })
$listener=@(Get-NetTCPConnection -LocalPort 8011 -State Listen -ErrorAction SilentlyContinue)
if (($listener.Count -eq 0 -and -not $Recovery) -or $listener.Count -gt 1 -or ($listener.Count -eq 1 -and $listener[0].OwningProcess -ne $childId)) { throw 'BACKEND_LISTENER_IDENTITY_MISMATCH' }
$env:BTC_RECOVERY_MODE=if($Recovery){'true'}else{'false'}
@'
import pathlib,sqlite3,os
for name in ('data/paper.sqlite3','docs/reports/abc-forward-v26/shadow-ledger.sqlite3'):
 p=pathlib.Path(name).resolve();c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
 if os.environ.get('BTC_RECOVERY_MODE')!='true' and c.execute("select count(*) from accounts where state!='FLAT' or position_json is not null or pending_stop_json is not null").fetchone()[0]:raise SystemExit('POSITION_OR_STOP_PENDING:'+name)
 if name.startswith('docs/') and c.execute("select count(*) from forward_cycles where status not in ('COMPLETE','SKIPPED')").fetchone()[0]:raise SystemExit('SHADOW_CYCLE_IN_PROGRESS')
 if name.startswith('data/') and c.execute("select count(*) from scheduler_cycles where status='RUNNING'").fetchone()[0]:raise SystemExit('FORMAL_CYCLE_IN_PROGRESS')
 if name.startswith('data/') and c.execute("select count(*) from decisions where outcome in ('PENDING','PENDING_EXECUTION')").fetchone()[0]:raise SystemExit('FORMAL_DECISION_PENDING')
 print('ACCOUNT_STATE_CHECKED',name,'recovery='+os.environ.get('BTC_RECOVERY_MODE','false'))
'@ | & $py -
if ($LASTEXITCODE -ne 0) { throw 'PRE_STOP_SAFETY_CHECK_FAILED' }
Stop-Process -Id $childId -Force -ErrorAction Stop
for ($i=0; $i -lt 20; $i++) { if (@(Get-Process -Id $oldProcessIds -ErrorAction SilentlyContinue).Count -eq 0) { break }; Start-Sleep -Milliseconds 500 }
if (Get-Process -Id $parentId -ErrorAction SilentlyContinue) { throw 'BACKEND_PARENT_STILL_RUNNING' }
if (Get-Process -Id $childId -ErrorAction SilentlyContinue) { throw 'BACKEND_CHILD_STILL_RUNNING' }
foreach ($helper in $helpers) { if (Get-Process -Id ([int]$helper.ProcessId) -ErrorAction SilentlyContinue) { throw 'BACKEND_CONSOLE_HELPER_STILL_RUNNING' } }
if (Get-NetTCPConnection -LocalPort 8011 -State Listen -ErrorAction SilentlyContinue) { throw 'PORT_8011_STILL_LISTENING' }
@'
from pathlib import Path
from backend.market import _StreamLease
leases=[_StreamLease(Path('.run')/x) for x in ('paper-scheduler.lock','alpaca-stream.lock')];held=[]
try:
 for item in leases:
  if not item.acquire():raise SystemExit('LEASE_STILL_HELD:'+str(item.path))
  held.append(item)
 print('OLD_SCHEDULER_AND_STREAM_LEASES_FREE')
finally:
 for item in reversed(held):item.release()
'@ | & $py -
if ($LASTEXITCODE -ne 0) { throw 'OLD_LEASE_NOT_RELEASED' }
$modePath=Join-Path $root 'data\formal-mode.json';$tempPath=$modePath+'.tmp'
$record=@{ mode=$Mode; activated_at_utc=[DateTimeOffset]::UtcNow.ToString('o'); source='authorized_formal_paper_switch' }
$record | ConvertTo-Json | Set-Content -LiteralPath $tempPath -Encoding utf8
Move-Item -LiteralPath $tempPath -Destination $modePath -Force
$env:BTC_REQUIRE_STARTUP_MONITOR='true'
$env:PAPER_AUTOMATION_ENABLED='true';$env:VISION_ENABLED='true';$env:VISION_CALLS_AUTHORIZED='true';$env:VISION_BASE_URL='https://api.deepseek.com';$env:VISION_ENDPOINT_PATH='/chat/completions';$env:VISION_MODEL='deepseek-flash';$env:VISION_THINKING_MODE='disabled';$env:VISION_PROMPT_VARIANT='wait_example';$env:VISION_MAX_CONCURRENCY='1';$env:VISION_TIMEOUT_SECONDS='60';$env:VISION_MAX_OUTPUT_TOKENS='1000';$env:VISION_MAX_CALLS_PER_UTC_DAY='200';$env:VISION_MAX_CALL_COST_USD='0.05';$env:VISION_DAILY_BUDGET_USD='5';$env:VISION_TOTAL_BUDGET_USD='5';$env:VISION_INPUT_PRICE_PER_MILLION_USD='0.30';$env:VISION_OUTPUT_PRICE_PER_MILLION_USD='1.20';$env:VISION_BUDGET_DB=Join-Path $root 'data\vision-ab-paper-budget.sqlite3';$env:ALPACA_CREDENTIALS_FILE=Join-Path $root 'data\alpaca-market-new.env'
$stamp=[DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssfffZ');$logs=Join-Path $root 'logs'
$new=Start-Process -FilePath $py -ArgumentList @('-m','uvicorn','backend.app:app','--host','127.0.0.1','--port','8011') -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs "backend-mode-$stamp.log") -RedirectStandardError (Join-Path $logs "backend-mode-error-$stamp.log")
Set-Content -LiteralPath $pidPath -Value $new.Id -NoNewline
$ready=$false
for ($i=0; $i -lt 40; $i++) { try { $status=Invoke-RestMethod 'http://127.0.0.1:8011/api/v1/system/status' -TimeoutSec 2; if ($status.formal_trading_mode -eq $Mode -and $status.scheduler -eq 'ENABLED_INTERNAL_PAPER') { $ready=$true;break } } catch {}; Start-Sleep -Milliseconds 500 }
if (-not $ready) { throw "BACKEND_NOT_READY_CHECK_LOGS:$stamp" }
Write-Output "BACKEND_MODE=$Mode PARENT_PID=$($new.Id) STATUS=$($status.scheduler)"
