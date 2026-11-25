from scripts.p0_watchdog import classify

def test_health_failures_and_feed_degrade():
 assert classify(False,None,0,False)=='RECOVERY_PENDING'
 assert classify(True,{'scheduler':'ENABLED_INTERNAL_PAPER','feed_health':'STALE'},0,False)=='DEGRADED'
 assert classify(True,{'scheduler':'ENABLED_INTERNAL_PAPER','feed_health':'LIVE'},0,True)=='RECOVER_PENDING_EXITS'
 assert classify(True,{'scheduler':'ENABLED_INTERNAL_PAPER','feed_health':'LIVE'},1801,False)=='DEGRADED'
 assert classify(True,{'scheduler':'DISABLED','feed_health':'LIVE'},0,False)=='DEGRADED'
 assert classify(True,{'scheduler':'ENABLED_INTERNAL_PAPER','feed_health':'LIVE'},900,False)=='HEALTHY'

def test_market_manager_recovery_never_triggers_backend_restart():
 for state in ('RETRY','RECOVERING','COOLDOWN','VALIDATING','LONG_PROTECTION'):
  status={'scheduler':'ENABLED_INTERNAL_PAPER','feed_health':'LIVE','websocket_recovery_state':state,'websocket':'CONNECTED'}
  assert classify(True,status,3600,False)=='MARKET_RECOVERY_MANAGED'
 assert classify(True,{'scheduler':'ENABLED_INTERNAL_PAPER','feed_health':'LIVE','websocket':'CONNECTED','websocket_recovery_state':'HEALTHY'},3600,False)=='DEGRADED'
 assert classify(True,{'scheduler':'ENABLED_INTERNAL_PAPER','feed_health':'STALE','websocket':'UNKNOWN'},3600,False)=='DEGRADED'

def test_watchdog_singleton_lock_is_exclusive(tmp_path):
 from backend.market import _StreamLease
 a=_StreamLease(tmp_path/'manager.lock');b=_StreamLease(tmp_path/'manager.lock')
 try:
  assert a.acquire();assert not b.acquire()
 finally:a.release();b.release()
 assert b.acquire();b.release()

def test_bounded_step_reports_timeout_and_heartbeats(tmp_path):
 import sys
 from scripts.p0_watchdog import run_bounded
 beats=[]
 result=run_bounded([sys.executable,'-c','import time;time.sleep(5)'],timeout=0.4,interval=0.1,heartbeat=lambda:beats.append(1))
 assert result=='TIMEOUT' and len(beats)>=2

def test_bounded_step_returns_exit_status():
 import sys
 from scripts.p0_watchdog import run_bounded
 assert run_bounded([sys.executable,'-c','pass'],timeout=5,interval=.05)=='SUCCESS'
 assert run_bounded([sys.executable,'-c','raise SystemExit(7)'],timeout=5,interval=.05)=='FAILED:7'

def test_powershell_timeout_is_bounded():
 from scripts.p0_watchdog import run_bounded
 assert run_bounded(['powershell.exe','-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 5'],timeout=.5,interval=.1)=='TIMEOUT'

def test_pending_stop_survives_ledger_reopen_and_closes_once(tmp_path):
 from decimal import Decimal as D
 from backend.ledger import Ledger
 from backend.domain import CostModel
 from backend.execution import recover_pending_stop
 path=tmp_path/'restarted.sqlite3'; ledger=Ledger(path)
 opened=ledger.open_position(agent='B',direction='LONG',quantity=D('.01'),price=D('10000'),stop=D('9900'),fee=D('.05'),slippage=D('0'),idempotency_key='open',expected_version=0)
 assert opened['accepted']
 ledger.request_stop_exit(agent='B',trigger_price=D('9900'),event_key='stop',quote_event_id="confirmed-quote",expected_version=1)
 ledger=Ledger(path); assert ledger.account('B')['state']=='STOP_EXIT_PENDING'
 q={'source':'BINANCE_USDM_PERP','timestamp_valid':True,'age_ms':1,'bid':'9850','ask':'9851','bid_qty':'1','ask_qty':'1'}
 assert recover_pending_stop(ledger=ledger,agent='B',quote=q,cost=CostModel(),event_id='event')['accepted']
 ledger=Ledger(path); recover_pending_stop(ledger=ledger,agent='B',quote=q,cost=CostModel(),event_id='event')
 assert ledger.account('B')['state']=='FLAT'
 assert len([x for x in ledger.fills('B') if x['position_effect']=='CLOSE'])==1

def test_self_recovered_backend_skips_recovery_process(tmp_path,monkeypatch):
 import json
 from scripts import p0_watchdog as w
 class Lease:
  def __init__(self,*a):pass
  def acquire(self):return True
  def release(self):pass
 monkeypatch.setattr(w,'_StreamLease',Lease);monkeypatch.setattr(w,'OUT',tmp_path/'status.json')
 probes=iter([{'classification':'RECOVERY_PENDING','backend':False,'status':{},'pending':False,'frontend':True,'cycle_age_seconds':0}]*3+[{'backend':True}])
 monkeypatch.setattr(w,'probe',lambda:next(probes))
 calls=[]
 def step(cmd,*args,**kwargs):
  calls.append(cmd);return 'SUCCESS'
 monkeypatch.setattr(w,'run_bounded',step)
 class Finished(BaseException):pass
 counter=[0]
 def sleep(_):
  counter[0]+=1
  if counter[0]==3:raise Finished()
 monkeypatch.setattr(w.time,'sleep',sleep)
 try:w.main()
 except Finished:pass
 assert len(calls)==1 and '--backup-only' in calls[0]
 assert json.loads((tmp_path/'status.json').read_text())['last_recovery_result']=='ALREADY_RECOVERED'

def test_timeout_can_leave_live_powershell_descendant(tmp_path):
 import ctypes,os,signal
 from scripts.p0_watchdog import run_bounded
 pidfile=tmp_path/'descendant.pid'; script=tmp_path/'parent.ps1'
 script.write_text("$p=Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 20' -WindowStyle Hidden -PassThru\nSet-Content -LiteralPath '"+str(pidfile)+"' -Value $p.Id\nStart-Sleep -Seconds 20\n")
 pid=None
 try:
  assert run_bounded(['powershell.exe','-NoProfile','-File',str(script)],timeout=3,interval=.1)=='TIMEOUT'
  pid=int(pidfile.read_text().strip());handle=ctypes.windll.kernel32.OpenProcess(0x100000,False,pid)
  assert handle and ctypes.windll.kernel32.WaitForSingleObject(handle,0)==258
  ctypes.windll.kernel32.CloseHandle(handle)
 finally:
  if pid:os.kill(pid,signal.SIGTERM)

def test_timeout_latches_manual_and_prevents_repeat(tmp_path,monkeypatch):
 import json
 from scripts import p0_watchdog as w
 class Lease:
  def __init__(self,*a):pass
  def acquire(self):return True
  def release(self):pass
 monkeypatch.setattr(w,'_StreamLease',Lease);monkeypatch.setattr(w,'OUT',tmp_path/'status.json')
 monkeypatch.setattr(w,'probe',lambda:{'classification':'RECOVERY_PENDING','backend':False,'status':{},'pending':False,'frontend':True,'cycle_age_seconds':0})
 calls=[]
 def bounded(cmd,*args,**kwargs):
  calls.append(cmd)
  return 'SUCCESS' if '--backup-only' in cmd else 'TIMEOUT'
 monkeypatch.setattr(w,'run_bounded',bounded)
 class Finished(BaseException):pass
 count=[0]
 def sleep(_):
  count[0]+=1
  if count[0]==7:raise Finished()
 monkeypatch.setattr(w.time,'sleep',sleep)
 try:w.main()
 except Finished:pass
 status=json.loads((tmp_path/'status.json').read_text())
 assert len(calls)==2 and status['state']=='MANUAL_INTERVENTION_REQUIRED' and status['last_recovery_result']=='TIMEOUT'

def test_pending_stop_survives_process_restart_and_duplicate(tmp_path):
 import subprocess,sys,sqlite3
 from pathlib import Path
 db=tmp_path/'pending-service.sqlite3'
 setup='''from backend.ledger import Ledger\nfrom decimal import Decimal as D\nimport sys\nl=Ledger(sys.argv[1]);assert l.open_position(agent="B",direction="LONG",quantity=D(".01"),price=D("10000"),stop=D("9900"),fee=D(".05"),slippage=D("0"),idempotency_key="open",expected_version=0)["accepted"];l.request_stop_exit(agent="B",trigger_price=D("9900"),event_key="stop",quote_event_id="confirmed-quote",expected_version=1)'''
 resume='''from backend.ledger import Ledger\nfrom backend.execution import recover_pending_stop\nfrom backend.domain import CostModel\nimport sys\nl=Ledger(sys.argv[1]);assert l.account("B")["state"]=="STOP_EXIT_PENDING"\nq={"source":"BINANCE_USDM_PERP","timestamp_valid":True,"age_ms":1,"bid":"9850","ask":"9851","bid_qty":"1","ask_qty":"1"}\nassert recover_pending_stop(ledger=l,agent="B",quote=q,cost=CostModel(),event_id="event")["accepted"];recover_pending_stop(ledger=l,agent="B",quote=q,cost=CostModel(),event_id="event")'''
 for code in (setup,resume):subprocess.run([sys.executable,'-c',code,str(db)],cwd=Path(__file__).resolve().parents[1],check=True,timeout=15)
 with sqlite3.connect(db) as c:
  assert c.execute("select state from accounts where agent='B'").fetchone()[0]=='FLAT'
  assert c.execute("select count(*) from fills where agent='B' and position_effect='CLOSE'").fetchone()[0]==1

