"""Active-session local HTTP shadow runner. No external Alpaca or order endpoints."""
import json,os,sys,time,urllib.request
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.market import _StreamLease
from scripts.forward_v26_engine import Forward
from scripts.forward_v26_review import dispatch
FOLDER=ROOT/'docs/reports/abc-forward-v26';OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(path):
 if path not in ('/api/v1/market/candles?limit=672','/api/v1/market/quote'):raise ValueError('NONLOCAL_OR_UNAPPROVED_ENDPOINT')
 with OPENER.open('http://127.0.0.1:8011'+path,timeout=10) as response:return json.load(response)
def status(state,**kw):
 out={'state':state,'pid':os.getpid(),'updated_utc':datetime.now(timezone.utc).isoformat(),**kw};tmp=FOLDER/'status.json.tmp';tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf8');tmp.replace(FOLDER/'status.json')
def main():
 lease=_StreamLease(FOLDER/'forward-worker.lock')
 if not lease.acquire():raise RuntimeError('FORWARD_WORKER_ALREADY_RUNNING')
 try:
  engine=Forward(ROOT,FOLDER);thread=None;bars=[];last_bar_poll=0;last_error=None
  status('RUNNING',note='local-only research; sampled quotes; 40-trade review automatically generates future candidate')
  while True:
   try:
    q=get('/api/v1/market/quote');now=time.monotonic()
    if now-last_bar_poll>=15:
     data=get('/api/v1/market/candles?limit=672')
     if data.get('source')!='ALPACA_US_BTCUSD_SPOT' or data.get('currency')!='USD_RESEARCH':raise RuntimeError('LOCAL_FEED_SOURCE_MISMATCH')
     bars=data['items'];last_bar_poll=now
    engine.process(bars,q);thread=dispatch(engine,bars,thread);last_error=None
    status('HALTED_POSITION_GAP' if engine.is_halted() else 'RUNNING',last_bar_close=bars[-1]['close_at'] if bars else None,last_quote_at=q.get('event_at'),quote_age_ms=q.get('age_ms'),review_thread_active=bool(thread and thread.is_alive()))
    if '--once' in sys.argv:
     status('CHECK_COMPLETED',last_bar_close=bars[-1]['close_at'] if bars else None,last_quote_at=q.get('event_at'));break
   except (OSError,urllib.error.URLError,TimeoutError) as exc:
    last_error=type(exc).__name__;status('FEED_UNAVAILABLE',error=last_error,note='No new entries; retry local feed')
   except Exception as exc:
    status('BLOCKED',error=type(exc).__name__+':'+str(exc),note='Fail closed; reconcile before restart');raise
   time.sleep(5)
 finally:lease.release()
if __name__=='__main__':main()
