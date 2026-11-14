from backend.rule_cycle import describe_signal, hold_due
from datetime import datetime,timezone

def test_hold_exit_from_closed_bar_counts():
 assert hold_due('2026-10-28T09:30:00+00:00',datetime(2026,10,28,11,30,tzinfo=timezone.utc),8)
 assert not hold_due('2026-10-28T09:30:00+00:00',datetime(2026,10,28,11,15,tzinfo=timezone.utc),8)
def test_wait_has_concrete_rule_reason():
 assert '未满足' in describe_signal('B',None)
 assert 'B_FALSE_BREAK' in describe_signal('B',{'direction':'LONG','setup':'B_FALSE_BREAK','stop':'100'})

def test_real_rule_cycle_opens_targets_and_deduplicates(tmp_path,monkeypatch):
 import asyncio,types,sqlite3
 from backend.ledger import Ledger
 from backend.vision import AgentOrchestrator
 from backend.rule_cycle import RuleCycleRunner
 from backend.domain import CostModel
 import backend.rule_cycle as module
 db=tmp_path/'paper.sqlite3';ledger=Ledger(db);AgentOrchestrator(str(db));runner=RuleCycleRunner(ledger,db,CostModel());runner.v3=types.SimpleNamespace(signal_b=lambda bars,i:None,signal_c=lambda bars,i:None)
 monkeypatch.setattr(module,'json_signal',lambda rule,bars,i:{'direction':'LONG','stop':'90','target':None,'setup':'TEST_BREAK'})
 quote={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'event_at':'2026-10-28T09:30:01+00:00','bid':'100','ask':'100.1','bid_qty':'1','ask_qty':'1','event_id':'q'}
 book={'source':'ALPACA_US_BTCUSD_SPOT','timestamp_valid':True,'age_ms':0,'bids':[{'p':'100','s':'1'}],'asks':[{'p':'100.1','s':'1'}]};close=datetime(2026,10,28,9,30,tzinfo=timezone.utc)
 first=asyncio.run(runner.run_cycle(candles=[{'open':'100','high':'101','low':'99','close':'100'}]*288,candle_close=close,quote=quote,orderbook=book));assert first['A']['execution']['accepted'];assert ledger.account('A')['state']=='OPEN_LONG'
 second=asyncio.run(runner.run_cycle(candles=[{'open':'100','high':'101','low':'99','close':'100'}]*288,candle_close=close,quote=quote,orderbook=book));assert second['A']['status']=='DUPLICATE_CYCLE'
 target=runner._meta('A')[2];quote.update(bid=str(float(target)+1),ask=str(float(target)+2));runner.monitor_targets(quote);assert ledger.account('A')['state']=='FLAT'
 with sqlite3.connect(db) as c:
  assert c.execute("select count(*) from fills where agent='A'").fetchone()[0]==2
  assert c.execute("select count(*) from decisions where agent='A'").fetchone()[0]==1
  assert c.execute("select mode from decisions where agent='A'").fetchone()[0]=='RULE_DRIVEN'

def test_actual_rule_cycle_rejects_entry_when_paused(tmp_path, monkeypatch):
 import asyncio,types
 from pathlib import Path
 from backend.ledger import Ledger
 from backend.vision import AgentOrchestrator
 from backend.rule_cycle import RuleCycleRunner
 from backend.strategy_control import StrategyControl
 import backend.rule_cycle as module
 db=tmp_path/'paper.sqlite3';ledger=Ledger(db);AgentOrchestrator(str(db));runner=RuleCycleRunner(ledger,db)
 runner.v3=types.SimpleNamespace(signal_b=lambda bars,i:None,signal_c=lambda bars,i:None)
 monkeypatch.setattr(module,'ROOT',tmp_path)
 monkeypatch.setattr(runner,'check_profile',lambda:None)
 monkeypatch.setattr(module,'json_signal',lambda rule,bars,i:{'direction':'LONG','stop':'90','target':None,'setup':'TEST_BREAK'})
 control=StrategyControl(tmp_path/'data/strategy-control.sqlite3');control.request_pause('A')
 quote={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'event_at':'2026-10-28T09:30:01+00:00','bid':'100','ask':'100.1','bid_qty':'1','ask_qty':'1','event_id':'q'}
 close=datetime(2026,10,28,9,30,tzinfo=timezone.utc)
 result=asyncio.run(runner.run_cycle(candles=[{'open':'100','high':'101','low':'99','close':'100'}]*288,candle_close=close,quote=quote,orderbook={}))
 assert result['A']['execution']['reason']=='STRATEGY_PAUSED'
 assert ledger.account('A')['state']=='FLAT'
 assert runner._meta('A') is None
def test_existing_visual_decision_blocks_rule_duplicate(tmp_path,monkeypatch):
 import asyncio,types,sqlite3
 from backend.ledger import Ledger
 from backend.vision import AgentOrchestrator
 from backend.rule_cycle import RuleCycleRunner
 import backend.rule_cycle as module
 db=tmp_path/'p.sqlite3';l=Ledger(db);AgentOrchestrator(str(db));runner=RuleCycleRunner(l,db);runner.v3=types.SimpleNamespace(signal_b=lambda x,i:None,signal_c=lambda x,i:None);monkeypatch.setattr(module,'json_signal',lambda *args:None)
 close=datetime(2026,10,28,9,30,tzinfo=timezone.utc);key=close.isoformat()
 with sqlite3.connect(db) as c:c.execute('insert into decisions values(?,?,?,?,?,?,?,?,?,?)',('old','A',key,'v1','abc','deepseek','REAL_PROVIDER','WAIT','{}','VALID'))
 result=asyncio.run(runner.run_cycle(candles=[{'open':'100','high':'101','low':'99','close':'100'}]*288,candle_close=close,quote=None,orderbook=None));assert result['A']['status']=='DUPLICATE_CYCLE'
 with sqlite3.connect(db) as c:assert c.execute("select count(*) from decisions where agent='A'").fetchone()[0]==1

def test_c_uses_leverage_and_time_closes_short(tmp_path,monkeypatch):
 import asyncio,types,sqlite3
 from datetime import timedelta
 from backend.ledger import Ledger
 from backend.vision import AgentOrchestrator
 from backend.rule_cycle import RuleCycleRunner
 from backend.domain import CostModel
 import backend.rule_cycle as module
 db=tmp_path/'p.sqlite3';l=Ledger(db);AgentOrchestrator(str(db));runner=RuleCycleRunner(l,db,CostModel());runner.v3=types.SimpleNamespace(signal_b=lambda bars,i:None,signal_c=lambda bars,i:{'direction':'SHORT','stop':'101','target':None,'setup':'T2','obstacle':None});monkeypatch.setattr(module,'json_signal',lambda *args:None)
 q={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'event_at':'2026-10-28T09:30:01+00:00','bid':'100','ask':'100.1','bid_qty':'1000','ask_qty':'1000','event_id':'q'};book={'source':q['source'],'timestamp_valid':True,'age_ms':0,'bids':[{'p':'100','s':'1000'}],'asks':[{'p':'100.1','s':'1000'}]};start=datetime(2026,10,28,9,30,tzinfo=timezone.utc)
 r=asyncio.run(runner.run_cycle(candles=[{'open':'100','high':'101','low':'99','close':'100'}]*288,candle_close=start,quote=q,orderbook=book));assert r['C']['execution']['accepted'];pos=__import__('json').loads(l.account('C')['position_json']);assert pos['leverage']=='5' and float(pos['planned_loss'])<=200
 later=start+timedelta(minutes=180);q['event_at']=(later+timedelta(seconds=1)).isoformat();r=asyncio.run(runner.run_cycle(candles=[{'open':'100','high':'101','low':'99','close':'100'}]*288,candle_close=later,quote=q,orderbook=book));assert r['C']['action']=='CLOSE_SHORT';assert l.account('C')['state']=='FLAT'
 with sqlite3.connect(db) as c:assert c.execute("select count(*) from fills where agent='C'").fetchone()[0]==2

def test_scheduler_rule_runner_once_and_no_model(tmp_path,monkeypatch):
 import asyncio,types,sqlite3
 from backend.ledger import Ledger
 from backend.vision import AgentOrchestrator
 from backend.scheduler import PaperCycleScheduler
 from backend.rule_cycle import RuleCycleRunner
 import backend.rule_cycle as module
 db=tmp_path/'p.sqlite3';l=Ledger(db);AgentOrchestrator(str(db));runner=RuleCycleRunner(l,db);runner.v3=types.SimpleNamespace(signal_b=lambda bars,i:None,signal_c=lambda bars,i:None);monkeypatch.setattr(module,'json_signal',lambda *args:None);scheduler=PaperCycleScheduler(db,runner,enabled=True)
 bars=[{'open':'100','high':'101','low':'99','close':'100'}]*288;start=datetime(2026,10,28,9,30,tzinfo=timezone.utc)
 args={'candles':bars,'candle_close':start,'candle_is_closed':True,'now':start,'quote':None,'orderbook':None}
 first=asyncio.run(scheduler.run_closed_cycle(**args));second=asyncio.run(scheduler.run_closed_cycle(**args))
 assert all(first[a]['action']=='WAIT' and first[a]['mode']=='RULE_DRIVEN' for a in 'ABC')
 assert all(second[a]['status']=='DUPLICATE_CYCLE' for a in 'ABC')
 with sqlite3.connect(db) as c:assert c.execute("select count(*) from decisions where mode='RULE_DRIVEN'").fetchone()[0]==3

def test_rule_scheduler_c_liquidation_uses_original_margin(tmp_path,monkeypatch):
 import asyncio,types
 from decimal import Decimal as D
 from backend.ledger import Ledger
 from backend.vision import AgentOrchestrator
 from backend.scheduler import PaperCycleScheduler
 from backend.rule_cycle import RuleCycleRunner
 from backend.domain import CostModel
 import backend.rule_cycle as module
 db=tmp_path/'c.sqlite3';ledger=Ledger(db);AgentOrchestrator(str(db));runner=RuleCycleRunner(ledger,db);runner.v3=types.SimpleNamespace(signal_b=lambda b,i:None,signal_c=lambda b,i:{'direction':'LONG','stop':'99','target':None,'setup':'T2','obstacle':None});monkeypatch.setattr(module,'json_signal',lambda *args:None)
 q={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'event_at':'2026-10-28T09:30:01+00:00','bid':'100','ask':'100','bid_qty':'1000','ask_qty':'1000','event_id':'q'};book={'source':q['source'],'timestamp_valid':True,'age_ms':0,'bids':[{'p':'100','s':'1000'}],'asks':[{'p':'100','s':'1000'}]};start=datetime(2026,10,28,9,30,tzinfo=timezone.utc);bars=[{'open':'100','high':'101','low':'99','close':'100'}]*288
 r=asyncio.run(runner.run_cycle(candles=bars,candle_close=start,quote=q,orderbook=book));assert r['C']['execution']['accepted'];q.update({'bid':'1','ask':'1','event_id':'gap'});scheduler=PaperCycleScheduler(db,runner,enabled=True,c_maintenance_rate=D('.01'));result=scheduler.monitor_stops(event_ids={'C':'test-gap'},quote=q,cost=CostModel());assert result['C']['accepted'];assert ledger.account('C')['state']=='FLAT'

def test_v31_short_records_context_without_obstacle_rejection(tmp_path,monkeypatch):
 import asyncio,types,sqlite3,json
 from backend.ledger import Ledger
 from backend.vision import AgentOrchestrator
 from backend.rule_cycle import RuleCycleRunner
 import backend.rule_cycle as module
 db=tmp_path/'audit.sqlite3';l=Ledger(db);AgentOrchestrator(str(db));r=RuleCycleRunner(l,db);r.v3=types.SimpleNamespace(signal_b=lambda b,i:None,signal_c=lambda b,i:{'direction':'SHORT','stop':'101','setup':'T2','obstacle':'99'});monkeypatch.setattr(module,'json_signal',lambda *a:None)
 q={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'event_at':'2026-10-28T09:30:01+00:00','bid':'100','ask':'100.1','bid_qty':'1000','ask_qty':'1000'};book={'source':q['source'],'timestamp_valid':True,'age_ms':0,'bids':[{'p':'100','s':'1000'}],'asks':[{'p':'100.1','s':'1000'}]}
 result=asyncio.run(r.run_cycle(candles=[{'open':'100','high':'101','low':'99','close':'100'}]*288,candle_close=datetime(2026,10,28,9,30,tzinfo=timezone.utc),quote=q,orderbook=book))
 assert result['C']['execution']['accepted']
 with sqlite3.connect(db) as c: payload=json.loads(c.execute("select raw_response from decisions where agent='C'").fetchone()[0])
 audit=payload['execution']['audit'];assert audit['quote']['bid']=='100';assert audit['account_equity']=='10000.00';assert audit['preflight']['accepted'];assert audit['required_margin'];assert audit['target'];assert audit['obstacle']=='99'

def test_short_safety_boundaries_on_isolated_ledger(tmp_path):
 import pytest
 from decimal import Decimal as D
 from backend.ledger import Ledger
 from backend.execution import prepare_entry
 from backend.domain import CostModel
 l=Ledger(tmp_path/'limits.sqlite3')
 def open_(q,stop,fee):
  return l.open_position(agent='C',direction='SHORT',quantity=D(q),price=D('100'),stop=D(stop),fee=D(fee),slippage=D('0'),idempotency_key=q+stop,expected_version=0,research_c_5x=True)
 with pytest.raises(ValueError,match='stop must be on loss side'):open_('1','99','0.05')
 assert not open_('200','102','10')['accepted'] # exceeds 2% including costs
 assert not open_('501','100.01','25.05')['accepted'] # notional > 5x
 assert not open_('500','100.01','25')['accepted'] # margin plus fee > available
 q={'source':'ALPACA_US_BTCUSD_SPOT','bid':'100','ask':'100.1','timestamp_valid':True};book={'bids':[{'p':'100','s':'1000'}],'asks':[{'p':'100.1','s':'1000'}]}
 args=dict(equity=D('10000'),available_cash=D('10000'),direction='SHORT',stop=D('101'),quote=q,orderbook=book,cost=CostModel(),quote_age_ms=60001,book_age_ms=0,leverage=D('5'))
 assert prepare_entry(**args)['reason']=='QUOTE_STALE'
 args['quote_age_ms']=0;args['stop']=D('99');assert prepare_entry(**args)['reason']=='STOP_ON_WRONG_SIDE'
 assert l.account('C')['state']=='FLAT' and not l.fills('C')

def test_rule_audit_version_marker(tmp_path):
 from backend.ledger import Ledger
 from backend.rule_cycle import RuleCycleRunner
 r=RuleCycleRunner(Ledger(tmp_path/'p.sqlite3'),tmp_path/'p.sqlite3')
 assert r.audit_version=='rule-rejection-audit-v1'

def test_specific_target_rejections_persist_snapshot(tmp_path,monkeypatch):
 import asyncio,types,sqlite3,json
 from backend.ledger import Ledger
 from backend.vision import AgentOrchestrator
 from backend.rule_cycle import RuleCycleRunner
 import backend.rule_cycle as module
 for idx,(stop,obstacle,expected) in enumerate([('180',None,'INVALID_TARGET_PRICE')]):
  db=tmp_path/f'case{idx}.sqlite3';l=Ledger(db);AgentOrchestrator(str(db));runner=RuleCycleRunner(l,db);runner.v3=types.SimpleNamespace(signal_b=lambda b,i:None,signal_c=lambda b,i:{'direction':'SHORT','stop':stop,'setup':'T2','obstacle':obstacle});monkeypatch.setattr(module,'json_signal',lambda *a:None)
  q={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'event_at':'2026-10-28T09:30:01+00:00','bid':'100','ask':'100.1','bid_qty':'1000','ask_qty':'1000'};book={'source':q['source'],'timestamp_valid':True,'age_ms':0,'bids':[{'p':'100','s':'1000'}],'asks':[{'p':'100.1','s':'1000'}]}
  result=asyncio.run(runner.run_cycle(candles=[{'open':'100','high':'101','low':'99','close':'100'}]*288,candle_close=datetime(2026,10,28,9,30,tzinfo=timezone.utc),quote=q,orderbook=book))
  assert result['C']['execution']['reason']==expected
  with sqlite3.connect(db) as c: audit=json.loads(c.execute("select raw_response from decisions where agent='C'").fetchone()[0])['execution']['audit']
  assert audit['quote']['event_at'] and audit['quote']['age_ms']==0 and audit['stop']==stop
  assert all(k in audit for k in ('target','obstacle','estimated_target_gross_per_unit','estimated_target_fees_per_unit','required_margin','available_margin'))
  assert all(k in audit['preflight'] for k in ('fill_price','quantity','notional','planned_loss','fee'))

def test_c_audit_stats_since_rollout(tmp_path):
 import sqlite3,json
 from scripts.c_rule_audit_stats import stats
 db=tmp_path/'stats.sqlite3'
 with sqlite3.connect(db) as c:
  c.execute('create table decisions(decision_id text,candle_close text,outcome text,raw_response text,agent text,mode text,action text)')
  c.execute('create table fills(agent text,decision_id text,position_effect text)')
  rows=[('old','2026-10-28T09:00:00+00:00','RISK_REJECTED',json.dumps({'execution':{'reason':'OLD'}}),'C','RULE_DRIVEN','OPEN_SHORT'),('new','2026-10-28T10:00:00+00:00','RISK_REJECTED',json.dumps({'execution':{'reason':'STRUCTURE_OBSTACLE_BEFORE_TARGET'}}),'C','RULE_DRIVEN','OPEN_SHORT'),('fill','2026-10-28T10:00:00+00:00','VALID','{}','C','RULE_DRIVEN','OPEN_LONG'),('wait','2026-10-28T10:00:00+00:00','VALID','{}','C','RULE_DRIVEN','WAIT')]
  c.executemany('insert into decisions values(?,?,?,?,?,?,?)',rows);c.execute("insert into fills values('C','fill','OPEN')")
 result=stats(db,datetime(2026,10,28,9,30,tzinfo=timezone.utc));assert result['signal_count']==2 and result['filled_count']==1 and result['rejections']=={'STRUCTURE_OBSTACLE_BEFORE_TARGET':1}

def test_b_hold_and_duplicate_cycle_do_not_add_position(tmp_path,monkeypatch):
 import asyncio,types,sqlite3,json
 from datetime import timedelta
 from backend.ledger import Ledger
 from backend.vision import AgentOrchestrator
 from backend.rule_cycle import RuleCycleRunner
 import backend.rule_cycle as module
 db=tmp_path/'b-one-position.sqlite3';ledger=Ledger(db);AgentOrchestrator(str(db));runner=RuleCycleRunner(ledger,db);runner.v3=types.SimpleNamespace(signal_b=lambda b,i:{'direction':'LONG','stop':'99','setup':'B_TEST'},signal_c=lambda b,i:None);monkeypatch.setattr(module,'json_signal',lambda *a:None)
 start=datetime(2026,10,28,9,30,tzinfo=timezone.utc);q={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'event_at':'2026-10-28T09:30:01+00:00','bid':'100','ask':'100.1','bid_qty':'100','ask_qty':'100'};book={'source':q['source'],'timestamp_valid':True,'age_ms':0,'bids':[{'p':'100','s':'100'}],'asks':[{'p':'100.1','s':'100'}]};bars=[{'open':'100','high':'101','low':'99','close':'100'}]*288
 one=asyncio.run(runner.run_cycle(candles=bars,candle_close=start,quote=q,orderbook=book));assert one['B']['execution']['accepted'];initial=json.loads(ledger.account('B')['position_json']);again=asyncio.run(runner.run_cycle(candles=bars,candle_close=start,quote=q,orderbook=book));assert again['B']['status']=='DUPLICATE_CYCLE'
 later=asyncio.run(runner.run_cycle(candles=bars,candle_close=start+timedelta(minutes=15),quote=q,orderbook=book));assert later['B']['action']=='HOLD';assert json.loads(ledger.account('B')['position_json'])['quantity']==initial['quantity'];assert len(ledger.fills('B'))==1

def test_isolated_guard_keeps_b_pending_and_a_c_decisions_running(tmp_path,monkeypatch):
 import asyncio,types,sqlite3
 from decimal import Decimal as D
 from backend.ledger import Ledger
 from backend.vision import AgentOrchestrator
 from backend.rule_cycle import RuleCycleRunner
 from backend.recovery_guard import recovery_blocked
 import backend.rule_cycle as module
 db=tmp_path/'independent.sqlite3';l=Ledger(db);AgentOrchestrator(str(db));runner=RuleCycleRunner(l,db)
 o=l.open_position(agent='B',direction='LONG',quantity=D('1'),price=D('100'),stop=D('99'),fee=D('.05'),slippage=D('0'),idempotency_key='open-b',expected_version=0);assert o['accepted'];l.request_stop_exit(agent='B',trigger_price=D('98'),event_key='stop',quote_event_id="confirmed-quote",expected_version=1)
 runner.v3=types.SimpleNamespace(signal_b=lambda b,i:{'direction':'LONG','stop':'99','setup':'B_TEST'},signal_c=lambda b,i:None);monkeypatch.setattr(module,'json_signal',lambda *a:None)
 assert not recovery_blocked(l,runner)
 q={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'event_at':'2026-10-28T09:30:01+00:00','bid':'100','ask':'100.1'};book={'source':q['source'],'age_ms':0,'bids':[{'p':'100','s':'10'}],'asks':[{'p':'100.1','s':'10'}]};bars=[{'open':'100','high':'101','low':'99','close':'100'}]*288
 r=asyncio.run(runner.run_cycle(candles=bars,candle_close=datetime(2026,10,28,9,30,tzinfo=timezone.utc),quote=q,orderbook=book));assert r['A']['action']=='WAIT' and r['B']['action']=='HOLD' and r['C']['action']=='WAIT';assert l.account('B')['state']=='STOP_EXIT_PENDING' and len(l.fills('B'))==1

def test_b_pending_allows_consistent_c_short_to_hold(tmp_path,monkeypatch):
 import asyncio,types,sqlite3,json
 from decimal import Decimal as D
 from backend.ledger import Ledger
 from backend.vision import AgentOrchestrator
 from backend.rule_cycle import RuleCycleRunner
 from backend.recovery_guard import recovery_blocked
 import backend.rule_cycle as module
 db=tmp_path/'independent-c.sqlite3';l=Ledger(db);AgentOrchestrator(str(db));r=RuleCycleRunner(l,db)
 for a,d,stop,extra in [('B','LONG','99',{}),('C','SHORT','101',{'research_c_5x':True})]:
  out=l.open_position(agent=a,direction=d,quantity=D('1'),price=D('100'),stop=D(stop),fee=D('.05'),slippage=D('0'),idempotency_key='open-'+a,expected_version=0,**extra);assert out['accepted']
 l.request_stop_exit(agent='B',trigger_price=D('98'),event_key='stop',quote_event_id="confirmed-quote",expected_version=1)
 pos=json.loads(l.account('C')['position_json'])
 with sqlite3.connect(db) as c:c.execute('insert into rule_position_meta values(?,?,?,?,?,?,?,?)',('C','cid',pos['trade_id'],'98','2026-10-28T09:15:00+00:00',12,'V3.0',None))
 assert not recovery_blocked(l,r);r.v3=types.SimpleNamespace(signal_b=lambda b,i:None,signal_c=lambda b,i:None);monkeypatch.setattr(module,'json_signal',lambda *a:None)
 bars=[{'open':'100','high':'101','low':'99','close':'100'}]*288
 out=asyncio.run(r.run_cycle(candles=bars,candle_close=datetime(2026,10,28,9,30,tzinfo=timezone.utc),quote=None,orderbook=None));assert out['A']['action']=='WAIT' and out['B']['action']=='HOLD' and out['C']['action']=='HOLD';assert json.loads(l.account('C')['position_json'])['trade_id']==pos['trade_id'] and l.account('B')['state']=='STOP_EXIT_PENDING'

