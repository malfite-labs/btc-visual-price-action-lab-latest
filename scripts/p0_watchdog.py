"""Single-instance local paper-service watchdog; never touches a broker API."""
import os
import json,os,sqlite3,subprocess,sys,time,urllib.request
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.market import _StreamLease
OUT=ROOT/'frontend/public/research/watchdog-status.json'
DB=ROOT/'data/paper.sqlite3'
def classify(reachable,status,cycle_age,pending):
 if not reachable:return 'RECOVERY_PENDING'
 if pending:return 'RECOVER_PENDING_EXITS'
 # The in-process Alpaca market manager owns bounded WebSocket recovery. A stale
 # scheduler timestamp during that protected state must not restart the backend.
 if status.get('websocket_recovery_state') in ('RETRY','RECOVERING','COOLDOWN','VALIDATING','LONG_PROTECTION') or status.get('websocket') in ('RETRY','RECOVERING','RECOVERY_LIMIT_BLOCKED','CONNECTION_LIMIT_BLOCKED','AUTH_FAILED','SERVICE_REJECTED','COOLDOWN','LONG_PROTECTION'):
  return 'MARKET_RECOVERY_MANAGED'
 if status.get('feed_health')!='LIVE' or status.get('quote_health') in ('STALE','MISSING','INVALID'):return 'DEGRADED'
 if status.get('scheduler')!='ENABLED_INTERNAL_PAPER' or cycle_age>1800:return 'DEGRADED'
 return 'HEALTHY'
def probe():
 with sqlite3.connect(DB.resolve().as_uri()+'?mode=ro',uri=True,timeout=3) as c:
  if c.execute('pragma quick_check').fetchone()[0]!='ok':raise RuntimeError('DB_INTEGRITY')
  pending=bool(c.execute("select count(*) from accounts where state in ('STOP_EXIT_PENDING','RECOVERY_REQUIRED','CLOSING')").fetchone()[0]);last=c.execute("select max(cycle_close) from scheduler_cycles where status='DONE'").fetchone()[0]
 now=datetime.now(timezone.utc);activated=json.loads((ROOT/'data/formal-mode.json').read_text(encoding='utf-8-sig'))['activated_at_utc'];base=max(datetime.fromisoformat(activated),datetime.fromisoformat(last) if last else datetime.fromisoformat(activated));age=max(0,(now-base).total_seconds())
 try:
  with urllib.request.urlopen('http://127.0.0.1:8011/api/v1/system/status',timeout=4) as r:status=json.load(r)
  reachable=True
 except Exception:status={};reachable=False
 try:
  with urllib.request.urlopen('http://127.0.0.1:5174/',timeout=2) as r:frontend=r.status==200
 except Exception:frontend=False
 return {'backend':reachable,'status':status,'pending':pending,'cycle_age_seconds':age,'frontend':frontend,'classification':classify(reachable,status,age,pending)}
def save(x):
 OUT.parent.mkdir(parents=True,exist_ok=True);tmp=OUT.with_suffix('.tmp');tmp.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf8');os.replace(tmp,OUT)
def backup():
 folder=ROOT/'docs/reports/p0-recovery';folder.mkdir(parents=True,exist_ok=True);stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
 for name,path in [('formal',DB),('shadow',ROOT/'docs/reports/abc-forward-v26/shadow-ledger.sqlite3')]:
  with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(folder/(stamp+'-watchdog-'+name+'.sqlite3')) as dst:
   src.backup(dst)
   if dst.execute('pragma quick_check').fetchone()[0]!='ok':raise RuntimeError('BACKUP_FAILED')
def run_bounded(command,timeout,interval=5,heartbeat=lambda:None):
 """Bound each step; do not capture pipes inherited by detached descendants."""
 p=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
 deadline=time.monotonic()+timeout
 while True:
  code=p.poll()
  if code is not None:return 'SUCCESS' if code==0 else 'FAILED:'+str(code)
  if time.monotonic()>=deadline:
   p.kill()
   try:p.wait(timeout=3)
   except subprocess.TimeoutExpired:pass
   return 'TIMEOUT'
  heartbeat();time.sleep(min(interval,max(0,deadline-time.monotonic())))
def main():
 lease=_StreamLease(ROOT/'.run/p0-watchdog.lock')
 if not lease.acquire():raise SystemExit('WATCHDOG_ALREADY_RUNNING')
 try:
  try:old=json.loads(OUT.read_text(encoding='utf8'));attempts=int(old.get('attempts',0));last_attempt=float(old.get('last_attempt_epoch',0));last_result=old.get('last_recovery_result')
  except Exception:attempts=0;last_attempt=0;last_result=None
  failures=0;manual=old.get('last_recovery_result')=='TIMEOUT' if isinstance(locals().get('old'),dict) else False
  while True:
   try:obs=probe();state=obs['classification']
   except Exception as exc:obs={'backend':False,'pending':None,'status':{},'frontend':None,'error':type(exc).__name__};state='DEGRADED'
   if state=='RECOVERY_PENDING':failures+=1
   else:failures=0
   if state in ('HEALTHY','RECOVER_PENDING_EXITS') and time.time()-last_attempt>120:attempts=0
   if failures>=3 and attempts<3 and time.time()-last_attempt>=120 and not manual:
    state='STOP_OLD_INSTANCE';attempts+=1;last_attempt=time.time();save({'state':state,'attempts':attempts,'last_attempt_epoch':last_attempt,'last_recovery_result':last_result,'updated_utc':datetime.now(timezone.utc).isoformat(),'automatic':True,'pid':os.getpid()})
    try:
     def heartbeat():save({'state':state,'attempts':attempts,'last_attempt_epoch':last_attempt,'last_recovery_result':last_result,'updated_utc':datetime.now(timezone.utc).isoformat(),'automatic':True,'pid':os.getpid()})
     state='BACKUP_IN_PROGRESS';last_result=run_bounded([sys.executable,str(Path(__file__).resolve()),'--backup-only'],30,heartbeat=heartbeat)
     if last_result=='SUCCESS':
      # A recovered backend needs no process replacement.
      if probe()['backend']:last_result='ALREADY_RECOVERED';state='HEALTHY'
      else:
       state='STOP_OLD_INSTANCE';last_result=(run_bounded(['powershell.exe','-NoProfile','-NonInteractive','-File',str(ROOT/'scripts/recover_backend.ps1')],90,heartbeat=heartbeat) if os.name=='nt' else run_bounded(['bash',str(ROOT/'scripts/linux/recover_backend.sh')],90,heartbeat=heartbeat))
       state='RECOVER_PENDING_EXITS' if last_result=='SUCCESS' else 'RECOVERY_FAILED'
     else:state='RECOVERY_FAILED'
     if last_result=='TIMEOUT':manual=True;state='MANUAL_INTERVENTION_REQUIRED'
    except Exception as exc:last_result='FAILED:'+type(exc).__name__;state='RECOVERY_FAILED'
    failures=0
   elif (attempts>=3 or manual) and state=='RECOVERY_PENDING':state='MANUAL_INTERVENTION_REQUIRED'
   save({'state':state,'attempts':attempts,'last_attempt_epoch':last_attempt,'last_recovery_result':last_result,'updated_utc':datetime.now(timezone.utc).isoformat(),'automatic':True,'pid':os.getpid(),'backend':obs.get('backend'),'feed':obs.get('status',{}).get('feed_health'),'scheduler':obs.get('status',{}).get('scheduler'),'shadow':obs.get('status',{}).get('shadow_evolution',{}).get('state'),'pending_exit':obs.get('pending'),'frontend':obs.get('frontend'),'cycle_age_seconds':obs.get('cycle_age_seconds')})
   time.sleep(30)
 finally:lease.release()
if __name__=='__main__':
 if '--backup-only' in sys.argv:backup()
 else:main()
