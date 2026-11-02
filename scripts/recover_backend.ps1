$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$py=Join-Path $root '.venv\Scripts\python.exe'
$pidPath=Join-Path $root '.run\backend.pid'
if(Test-Path -LiteralPath $pidPath){$oldId=[int](Get-Content -LiteralPath $pidPath -Raw);if(Get-Process -Id $oldId -ErrorAction SilentlyContinue){throw 'OLD_BACKEND_PROCESS_PRESENT'}}
$other=@(Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -match 'uvicorn backend\.app:app'})
if($other.Count -gt 0){throw 'UNIDENTIFIED_BACKEND_PROCESS_PRESENT'}
if(Get-NetTCPConnection -LocalPort 8011 -State Listen -ErrorAction SilentlyContinue){throw 'PORT_8011_IN_USE'}
@'
from pathlib import Path
from backend.market import _StreamLease
import sqlite3
for path,sql in [('data/paper.sqlite3',"select count(*) from scheduler_cycles where status='RUNNING'"),('docs/reports/abc-forward-v26/shadow-ledger.sqlite3',"select count(*) from forward_cycles where status not in ('COMPLETE','SKIPPED')")]:
 with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
  if c.execute('pragma quick_check').fetchone()[0]!='ok' or c.execute(sql).fetchone()[0]:raise SystemExit('INCOMPLETE_CYCLE_OR_BAD_DB')
items=[_StreamLease(Path('.run')/n) for n in ('paper-scheduler.lock','alpaca-stream.lock')];held=[]
try:
 for x in items:
  if not x.acquire():raise SystemExit('OLD_LEASE_HELD')
  held.append(x)
finally:
 for x in reversed(held):x.release()
'@ | & $py -
if($LASTEXITCODE -ne 0){throw 'OLD_LEASE_NOT_RELEASED'}
$mode=Get-Content -LiteralPath (Join-Path $root 'data\formal-mode.json') -Raw | ConvertFrom-Json
if($mode.mode -ne 'RULE_DRIVEN'){throw 'FORMAL_MODE_NOT_RULE_DRIVEN'}
$env:BTC_REQUIRE_STARTUP_MONITOR='true'
$env:PAPER_AUTOMATION_ENABLED='false';$env:ALPACA_CREDENTIALS_FILE=Join-Path $root 'data\alpaca-market-new.env'
$stamp=[DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssfffZ');$logs=Join-Path $root 'logs'
$new=Start-Process -FilePath $py -ArgumentList @('-m','uvicorn','backend.app:app','--host','127.0.0.1','--port','8011') -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs "backend-recovery-$stamp.log") -RedirectStandardError (Join-Path $logs "backend-recovery-error-$stamp.log")
Set-Content -LiteralPath $pidPath -Value $new.Id -NoNewline
for($i=0;$i -lt 40;$i++){try{$s=Invoke-RestMethod 'http://127.0.0.1:8011/api/v1/system/status' -TimeoutSec 2;if($s.scheduler -eq 'DISABLED_NOT_ARMED'){Write-Output "READY PID=$($new.Id)";exit 0}}catch{};Start-Sleep -Milliseconds 500}
throw 'STARTED_BUT_NOT_READY'
