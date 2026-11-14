from backend.stats_panel import closed_summary

def test_closed_net_drawdown_and_costs_not_double_charged():
 records=[{'net_pnl':'-63.11','trade_id':'1'},{'net_pnl':'10.00','trade_id':'2'},{'net_pnl':'-45.19','trade_id':'3'}]
 x=closed_summary(records);assert x['realized_net_pnl']=='-98.30';assert x['closed_trades']==3;assert x['profit_factor']=='0.0923';assert x['max_drawdown_pct']=='0.9830';assert x['win_rate_pct']=='33.33'

def test_current_open_fee_not_added_to_closed_realized(tmp_path):
 from backend.stats_panel import build_stats
 from backend.ledger import Ledger
 from backend.domain import CostModel
 from decimal import Decimal as D
 l=Ledger(tmp_path/'ledger.sqlite3')
 from backend.vision import AgentOrchestrator
 AgentOrchestrator(str(l.path))
 o=l.open_position(agent='B',direction='LONG',quantity=D('1'),price=D('100'),stop=D('99'),fee=D('.05'),slippage=D('0'),idempotency_key='open1',expected_version=0);assert o['accepted']
 z=l.close_position(agent='B',price=D('101'),fee=D('.0505'),slippage=D('0'),idempotency_key='close1',expected_version=1);assert z['accepted']
 o=l.open_position(agent='B',direction='LONG',quantity=D('1'),price=D('100'),stop=D('99'),fee=D('.05'),slippage=D('0'),idempotency_key='open2',expected_version=2);assert o['accepted']
 q={'source':'ALPACA_US_BTCUSD_SPOT','timestamp_valid':True,'age_ms':0,'bid':'100','ask':'100.1'};x=build_stats(l.path,tmp_path,l,q,CostModel(),True)['agents']['B']
 assert x['realized_net_pnl']=='0.89' and x['closed_trades']==1 and x['fees']=='0.16' and x['state']=='OPEN_LONG'

def test_formal_mode_accepts_powershell_utf8_bom(tmp_path,monkeypatch):
 import json
 import backend.app as app
 (tmp_path/'data').mkdir();(tmp_path/'data'/'formal-mode.json').write_text(json.dumps({'mode':'RULE_DRIVEN','activated_at_utc':'2026-10-28T14:00:00+00:00'}),encoding='utf-8-sig');monkeypatch.setattr(app,'ROOT',tmp_path);monkeypatch.setattr(app,'DATA_ROOT',tmp_path/'data')
 assert app._formal_mode()=='RULE_DRIVEN'

def test_shadow_status_requires_fresh_heartbeat_and_held_worker_lock(tmp_path):
 import subprocess,sys
 from datetime import datetime,timezone,timedelta
 from backend.stats_panel import shadow_state
 fresh={'state':'RUNNING','updated_utc':datetime.now(timezone.utc).isoformat(),'pid':99999999}
 assert shadow_state(tmp_path,fresh)=='OFFLINE'
 stale={'state':'RUNNING','updated_utc':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()}
 assert shadow_state(tmp_path,stale)=='STALE'
 code="from backend.market import _StreamLease; import sys; l=_StreamLease(sys.argv[1]); assert l.acquire(); print('ready',flush=True); sys.stdin.readline(); l.release()"
 script=tmp_path/'run_forward_v26.py';script.write_text(code)
 p=subprocess.Popen([sys.executable,str(script),str(tmp_path/'forward-worker.lock')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,env={**__import__('os').environ,'PYTHONPATH':str(__import__('pathlib').Path(__file__).resolve().parents[1])})
 try:
  assert p.stdout.readline().strip()=='ready'
  fresh['pid']=p.pid
  assert shadow_state(tmp_path,fresh)=='RUNNING'
 finally:
  p.stdin.write('\n');p.stdin.flush();p.wait(timeout=10)
 assert shadow_state(tmp_path,fresh)=='OFFLINE'
