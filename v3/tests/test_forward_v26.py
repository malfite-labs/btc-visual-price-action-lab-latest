from datetime import datetime,timezone
from scripts.forward_v26_core import eligible, quote_fresh

def test_forward_uses_only_whole_bars_after_freeze():
 a={'open_at':'2026-10-28T09:00:00+00:00','close_at':'2026-10-28T09:15:00+00:00','is_closed':True}
 assert not eligible(a,'2026-10-28T09:15:00+00:00',datetime(2026,10,28,9,30,tzinfo=timezone.utc))
 a['open_at']='2026-10-28T09:15:00+00:00';a['close_at']='2026-10-28T09:30:00+00:00'
 assert eligible(a,'2026-10-28T09:15:00+00:00',datetime(2026,10,28,9,30,tzinfo=timezone.utc))

def test_fresh_quote_must_be_after_signal():
 q={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':200,'bid':'100','ask':'101','bid_qty':'1','ask_qty':'1','event_at':'2026-10-28T09:29:59+00:00'}
 assert not quote_fresh(q,'2026-10-28T09:30:00+00:00')
 q['event_at']='2026-10-28T09:30:02+00:00';assert quote_fresh(q,'2026-10-28T09:30:00+00:00')

def test_new_full_bar_generates_three_isolated_decisions_once(tmp_path):
 import json,hashlib,shutil,sqlite3
 from datetime import timedelta
 from pathlib import Path
 from scripts.forward_v26_engine import Forward
 project=tmp_path/'fake-root';folder=project/'research';folder.mkdir(parents=True)
 real=Path(__file__).resolve().parents[1];names=['backend/domain.py','backend/ledger.py','backend/execution.py','backend/margin.py','scripts/rule_evolution_v2.py']
 for name in names:
  out=project/name;out.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(real/name,out)
 shutil.copyfile(real/'docs/reports/abc-v30/signals.py',folder/'v3-signals.py')
 shutil.copyfile(real/'docs/reports/abc-forward-v26/A-rule.json',folder/'A-rule.json')
 shutil.copyfile(real/'docs/reports/abc-forward-v26/C-v24-archived-rule.json',folder/'C-v24-archived-rule.json')
 h=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
 start=datetime(2026,10,28,9,15,tzinfo=timezone.utc);config={'earliest_forward_open_utc':start.isoformat(),'risk_execution_hashes':{n:h(project/n) for n in names},'A':{'file':'A-rule.json','sha256':h(folder/'A-rule.json'),'version':'A'},'B':{'file':'v3-signals.py','sha256':h(folder/'v3-signals.py'),'version':'B'},'C':{'file':'v3-signals.py','sha256':h(folder/'v3-signals.py'),'version':'C','archive_sha256':h(folder/'C-v24-archived-rule.json')}}
 (folder/'freeze.json').write_text(json.dumps(config),encoding='utf8');bars=[]
 for i in range(288):
  opening=start-timedelta(minutes=15*(287-i));bars.append({'open_at':opening.isoformat(),'close_at':(opening+timedelta(minutes=15)).isoformat(),'is_closed':True,'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','open':'100','high':'101','low':'99','close':'100'})
 q={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':100,'bid':'100','ask':'101','bid_qty':'1','ask_qty':'1','event_at':(start+timedelta(minutes=15,seconds=1)).isoformat()};engine=Forward(project,folder);now=start+timedelta(minutes=15,seconds=3);engine.process(bars,q,now);engine.process(bars,q,now)
 with sqlite3.connect(folder/'shadow-ledger.sqlite3') as db:
  assert db.execute('select count(*) from forward_cycles').fetchone()[0]==1
  assert db.execute('select count(*) from forward_bars').fetchone()[0]==1
  assert db.execute('select count(*) from forward_decisions').fetchone()[0]==3
  assert db.execute('select count(*) from fills').fetchone()[0]==0
 assert not (project/'data/paper.sqlite3').exists()

def test_open_and_stop_use_isolated_simulation(tmp_path):
 import json,hashlib,shutil,sqlite3
 from datetime import timedelta
 from pathlib import Path
 from scripts.forward_v26_engine import Forward
 project=tmp_path/'root';folder=project/'shadow';folder.mkdir(parents=True);real=Path(__file__).resolve().parents[1];names=['backend/domain.py','backend/ledger.py','backend/execution.py','backend/margin.py','scripts/rule_evolution_v2.py']
 for name in names:
  out=project/name;out.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(real/name,out)
 for name,source in [('v3-signals.py','docs/reports/abc-v30/signals.py'),('A-rule.json','docs/reports/abc-forward-v26/A-rule.json'),('C-v24-archived-rule.json','docs/reports/abc-forward-v26/C-v24-archived-rule.json')]:shutil.copyfile(real/source,folder/name)
 h=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();start=datetime(2026,10,28,9,15,tzinfo=timezone.utc);cfg={'earliest_forward_open_utc':start.isoformat(),'risk_execution_hashes':{n:h(project/n) for n in names},'A':{'file':'A-rule.json','sha256':h(folder/'A-rule.json'),'version':'A'},'B':{'file':'v3-signals.py','sha256':h(folder/'v3-signals.py'),'version':'B'},'C':{'file':'v3-signals.py','sha256':h(folder/'v3-signals.py'),'version':'C','archive_sha256':h(folder/'C-v24-archived-rule.json')}};(folder/'freeze.json').write_text(json.dumps(cfg),encoding='utf8')
 bars=[]
 for i in range(288):
  op=start-timedelta(minutes=15*(287-i));vals=('98','100','95','99') if i<286 else ('99','103','98','102') if i==286 else ('100','104','99','103');bars.append({'open_at':op.isoformat(),'close_at':(op+timedelta(minutes=15)).isoformat(),'is_closed':True,'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH',**dict(zip(('open','high','low','close'),vals))})
 q={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':100,'bid':'103','ask':'104','bid_qty':'1','ask_qty':'1','event_at':(start+timedelta(minutes=15,seconds=1)).isoformat()};e=Forward(project,folder);e.process(bars,q,start+timedelta(minutes=15,seconds=3));assert e.ledger.account('A')['state']=='OPEN_LONG'
 q.update({'bid':'90','ask':'91','event_at':(start+timedelta(minutes=15,seconds=5)).isoformat()});e.monitor(q);assert e.ledger.account('A')['state']=='FLAT'
 with sqlite3.connect(folder/'shadow-ledger.sqlite3') as db:
  assert db.execute("select count(*) from fills where agent='A'").fetchone()[0]==2
  assert db.execute("select count(*) from forward_trades where agent='A'").fetchone()[0]==1

def test_next_activation_requires_full_new_bar():
 from scripts.forward_v26_core import next_open
 from datetime import datetime,timezone
 assert next_open(datetime(2026,10,28,9,30,0,tzinfo=timezone.utc)).isoformat()=='2026-10-28T09:45:00+00:00'
 assert next_open(datetime(2026,10,28,9,31,0,tzinfo=timezone.utc)).isoformat()=='2026-10-28T09:45:00+00:00'

def test_bad_source_and_broken_ohlc_are_rejected():
 from scripts.forward_v26_core import valid_context
 b={'open_at':'2026-10-28T09:15:00+00:00','close_at':'2026-10-28T09:30:00+00:00','is_closed':True,'open':'100','high':'101','low':'99','close':'100','source':'OTHER','currency':'USD_RESEARCH'}
 assert not valid_context([b],1)
 b['source']='BINANCE_USDM_PERP';b['high']='98';assert not valid_context([b],1)

def test_review_uses_40_closed_records_and_schedules_future_rule(tmp_path,monkeypatch):
 import json,sqlite3,types
 from datetime import datetime,timezone
 from scripts import forward_v26_review as review
 folder=tmp_path/'shadow';folder.mkdir();(folder/'v3-signals.py').write_text('SOURCE_FOR_PRIOR_V3');db=folder/'paper.sqlite3';c=sqlite3.connect(db)
 c.executescript('''create table forward_trades(trade_id text,agent text,signal_close text,closed_at text,net_pnl text,reason text);create table fills(trade_id text,position_effect text,position_direction text,quantity text,price text,fee text,slippage text,strategy_version text);create table forward_reviews(agent text,batch integer,status text,details text);create table forward_versions(agent text,batch integer,activation_open text,rule_json text,rule_sha256 text,created_at text);''')
 for i in range(40):
  t=str(i);c.execute('insert into forward_trades values(?,?,?,?,?,?)',(t,'A','2026-10-28T09:30:00+00:00','2026-10-28T09:31:00+00:00','-1','STOP'))
  for effect in ('OPEN','CLOSE'):c.execute('insert into fills values(?,?,?,?,?,?,?,?)',(t,effect,'LONG','0.1','100','0.01','0.01','V25'))
 c.execute('insert into forward_reviews values(?,?,?,?)',('A',40,'PROCESSING',''));c.commit();c.close()
 rule={'long':[{'left':{'kind':'close'},'op':'>','right':{'kind':'open'}}],'short':[{'left':{'kind':'close'},'op':'<','right':{'kind':'open'}}],'stop_bars':4,'target_r':2,'max_hold':8}
 captured={}
 class FakeAdapter:
  def __init__(self,*args,**kwargs):pass
  async def _send_image_request(self,image,prompt,request):
   captured['prompt']=prompt
   return {'analysis':'analysis','reason':'reason','rule':rule,'estimated_cost_usd':'0.001'}
 monkeypatch.setenv('DEEPSEEK_API_KEY','test-value');monkeypatch.setattr(review,'ConfigurableVisionAdapter',FakeAdapter);monkeypatch.setattr(review,'chart',lambda bars,cutoff:b'fake')
 engine=types.SimpleNamespace(root=tmp_path,folder=folder,db=db,config={'A':{'version':'V25'}},active_rule=lambda a,t:(None,'V25'))
 before=datetime.now(timezone.utc);review.execute_review(engine,'A',40,[])
 with sqlite3.connect(db) as c:
  row=c.execute('select status from forward_reviews').fetchone();v=c.execute('select activation_open,rule_json from forward_versions').fetchone()
 assert row[0]=='COMPLETED' and datetime.fromisoformat(v[0])>before and json.loads(v[1])==rule
 assert 'SOURCE_FOR_PRIOR_V3' in captured['prompt']

def test_resolved_halt_is_append_only_and_no_longer_blocks_entries(tmp_path):
 import sqlite3
 from scripts.forward_v26_engine import Forward
 db=tmp_path/'shadow.sqlite3'
 with sqlite3.connect(db) as c:
  c.executescript('''create table forward_halts(id integer primary key,reason text not null,created_at text not null);
  create table forward_halt_resolutions(halt_id integer primary key,original_reason text not null,resolved_at text not null,resolution_code text not null,evidence_json text not null);
  insert into forward_halts values(1,'MISSED_BAR_WHILE_POSITION_OPEN','2026-10-28T16:30:00+00:00');''')
 e=Forward.__new__(Forward);e.db=db
 assert e.is_halted()
 with sqlite3.connect(db) as c:
  c.execute('insert into forward_halt_resolutions values(?,?,?,?,?)',(1,'MISSED_BAR_WHILE_POSITION_OPEN','2026-10-29T04:00:00+00:00','RESOLVED','{}'))
 assert not e.is_halted()
 with sqlite3.connect(db) as c:
  assert c.execute('select reason,created_at from forward_halts where id=1').fetchone()==('MISSED_BAR_WHILE_POSITION_OPEN','2026-10-28T16:30:00+00:00')

def test_candidate_version_requires_explicit_approval(tmp_path):
 import sqlite3
 from scripts.forward_v26_engine import Forward
 db=tmp_path/'shadow.sqlite3'
 with sqlite3.connect(db) as c:
  c.executescript('''create table forward_versions(agent text,batch integer,activation_open text,rule_json text,rule_sha256 text,created_at text);
  create table forward_version_approvals(agent text,batch integer,approved_at text,approved_by text,note text);
  insert into forward_versions values('A',40,'2026-10-28T09:45:00+00:00','{"candidate":true}','a'*64,'2026-10-28T09:30:00+00:00');''')
 e=Forward.__new__(Forward);e.db=db;e.rule={'baseline':True}
 assert e.active_rule('A','2026-10-28T10:00:00+00:00')==({'baseline':True},'V25')
 with sqlite3.connect(db) as c:c.execute("insert into forward_version_approvals values('A',40,'2026-10-28T09:50:00+00:00','user','approved')")
 # Candidate hash is invalid, so approval cannot bypass integrity validation.
 import pytest
 with pytest.raises(RuntimeError,match='FORWARD_RULE_HASH_MISMATCH:A'):
  e.active_rule('A','2026-10-28T10:00:00+00:00')

def test_halt_resolutions_and_version_approvals_are_append_only(tmp_path):
 import sqlite3,pytest
 from scripts.forward_v26_engine import Forward
 db=tmp_path/'shadow.sqlite3'
 with sqlite3.connect(db) as c:
  c.executescript('''create table forward_halt_resolutions(halt_id integer primary key,original_reason text,resolved_at text,resolution_code text,evidence_json text);
  create table forward_version_approvals(agent text,batch integer,approved_at text,approved_by text,note text,primary key(agent,batch));
  insert into forward_halt_resolutions values(1,'MISSED_BAR_WHILE_POSITION_OPEN','2026-10-29T04:00:00+00:00','RESOLVED','{}');
  insert into forward_version_approvals values('A',40,'2026-10-29T04:00:00+00:00','admin','confirmed');''')
 e=Forward.__new__(Forward);e.db=db;e._ensure_recovery_schema()
 with sqlite3.connect(db) as c:
  with pytest.raises(sqlite3.IntegrityError,match='HALT_RESOLUTION_APPEND_ONLY'):
   c.execute("update forward_halt_resolutions set evidence_json='{}'")
  with pytest.raises(sqlite3.IntegrityError,match='HALT_RESOLUTION_APPEND_ONLY'):
   c.execute('delete from forward_halt_resolutions')
  with pytest.raises(sqlite3.IntegrityError,match='VERSION_APPROVAL_APPEND_ONLY'):
   c.execute("update forward_version_approvals set note='changed'")
  with pytest.raises(sqlite3.IntegrityError,match='VERSION_APPROVAL_APPEND_ONLY'):
   c.execute('delete from forward_version_approvals')

def test_multiple_missing_bars_are_distinct_after_legacy_halt_resolved(tmp_path):
 import sqlite3
 from scripts.forward_v26_engine import Forward
 db=tmp_path/'shadow.sqlite3'
 with sqlite3.connect(db) as c:
  c.executescript('''create table forward_halts(id integer primary key,reason text,created_at text);
  create table forward_halt_resolutions(halt_id integer primary key,original_reason text,resolution_code text);
  create table forward_gap_events(close_at text primary key,reason text,recorded_at text);
  create table forward_gap_resolutions(close_at text primary key,resolved_at text,evidence_json text);
  insert into forward_halts values(1,'MISSED_BAR_WHILE_POSITION_OPEN','old');
  insert into forward_halt_resolutions values(1,'MISSED_BAR_WHILE_POSITION_OPEN','RESOLVED');''')
 e=Forward.__new__(Forward);e.db=db
 assert not e.is_halted()
 with sqlite3.connect(db) as c:
  for close in ('2026-10-30T07:00:00+00:00','2026-10-30T07:15:00+00:00'):
   c.execute("insert or ignore into forward_gap_events values(?,'MISSED_BAR_WHILE_POSITION_OPEN','now')",(close,))
  c.execute("insert into forward_gap_resolutions values('2026-10-30T07:00:00+00:00','now','{}')")
  assert c.execute('select count(*) from forward_gap_events').fetchone()[0]==2
 assert e.is_halted()

