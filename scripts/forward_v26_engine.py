"""Independent local-only quote/bar consumer and simulated shadow accounts.
Never connects to an exchange or the live scheduler. Fail closed after incomplete transactions.
"""
import hashlib,importlib.util,json,sqlite3
from datetime import datetime,timezone,timedelta
from decimal import Decimal as D
from pathlib import Path
from backend.domain import CostModel,ceil_step,floor_step
from backend.ledger import Ledger
from backend.execution import prepare_entry,handle_confirmed_stop,recover_pending_stop,handle_research_liquidation
from scripts.rule_evolution_v2 import signal as json_signal
from scripts.forward_v26_core import eligible,quote_fresh,dt,valid_context

COST=CostModel()
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def position_quote_id(q):return 'FWD_QUOTE:'+str(q['event_at'])
class Forward:
 def __init__(self,root,folder):
  self.root=Path(root).resolve();self.folder=Path(folder).resolve();self.config=json.loads((self.folder/'freeze.json').read_text(encoding='utf8'))
  if self.folder==self.root/'data' or self.root not in self.folder.parents:raise ValueError('INVALID_SHADOW_PATH')
  self.verify_frozen();self.ledger=Ledger(self.folder/'shadow-ledger.sqlite3');self.db=self.folder/'shadow-ledger.sqlite3'
  with sqlite3.connect(self.db) as c:
   c.executescript('''CREATE TABLE IF NOT EXISTS forward_cycles(close_at TEXT PRIMARY KEY,status TEXT NOT NULL,error TEXT);
CREATE TABLE IF NOT EXISTS forward_bars(open_at TEXT PRIMARY KEY,close_at TEXT NOT NULL,bar_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forward_decisions(agent TEXT NOT NULL,close_at TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL,PRIMARY KEY(agent,close_at));
CREATE TABLE IF NOT EXISTS forward_positions(agent TEXT PRIMARY KEY,trade_id TEXT NOT NULL,signal_close TEXT NOT NULL,target TEXT NOT NULL,max_hold INTEGER NOT NULL,bars_held INTEGER NOT NULL DEFAULT 0,initial_cash TEXT NOT NULL,exit_due TEXT);
CREATE TABLE IF NOT EXISTS forward_trades(trade_id TEXT PRIMARY KEY,agent TEXT NOT NULL,signal_close TEXT NOT NULL,closed_at TEXT NOT NULL,net_pnl TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forward_reviews(agent TEXT NOT NULL,batch INTEGER NOT NULL,status TEXT NOT NULL,details TEXT,PRIMARY KEY(agent,batch));
CREATE TABLE IF NOT EXISTS forward_versions(agent TEXT NOT NULL,batch INTEGER NOT NULL,activation_open TEXT NOT NULL,rule_json TEXT NOT NULL,rule_sha256 TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(agent,batch));
CREATE TABLE IF NOT EXISTS forward_halts(id INTEGER PRIMARY KEY CHECK(id=1),reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forward_gap_events(close_at TEXT PRIMARY KEY,reason TEXT NOT NULL,recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forward_gap_resolutions(close_at TEXT PRIMARY KEY,resolved_at TEXT NOT NULL,evidence_json TEXT NOT NULL);
''')
  self._ensure_recovery_schema()
  spec=importlib.util.spec_from_file_location('forward_frozen_v3',self.folder/'v3-signals.py');self.v3=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.v3)
  self.rule=json.loads((self.folder/'A-rule.json').read_text(encoding='utf8'))
  self.check_recovery()
 def _ensure_recovery_schema(self):
  with sqlite3.connect(self.db) as c:
   c.executescript('''CREATE TABLE IF NOT EXISTS forward_halt_resolutions(halt_id INTEGER PRIMARY KEY,original_reason TEXT NOT NULL,resolved_at TEXT NOT NULL,resolution_code TEXT NOT NULL CHECK(resolution_code='RESOLVED'),evidence_json TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS forward_halt_resolutions_no_update BEFORE UPDATE ON forward_halt_resolutions BEGIN SELECT RAISE(ABORT,'HALT_RESOLUTION_APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS forward_halt_resolutions_no_delete BEFORE DELETE ON forward_halt_resolutions BEGIN SELECT RAISE(ABORT,'HALT_RESOLUTION_APPEND_ONLY'); END;
CREATE TABLE IF NOT EXISTS forward_version_approvals(agent TEXT NOT NULL,batch INTEGER NOT NULL,approved_at TEXT NOT NULL,approved_by TEXT NOT NULL,note TEXT NOT NULL,PRIMARY KEY(agent,batch));
CREATE TRIGGER IF NOT EXISTS forward_version_approvals_no_update BEFORE UPDATE ON forward_version_approvals BEGIN SELECT RAISE(ABORT,'VERSION_APPROVAL_APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS forward_version_approvals_no_delete BEFORE DELETE ON forward_version_approvals BEGIN SELECT RAISE(ABORT,'VERSION_APPROVAL_APPEND_ONLY'); END;''')
 def verify_frozen(self):
  for a in 'ABC':
   x=self.config[a]
   if digest(self.folder/x['file'])!=x['sha256']:raise RuntimeError('FROZEN_RULE_CHANGED:'+a)
  if digest(self.folder/'C-v24-archived-rule.json')!=self.config['C']['archive_sha256']:raise RuntimeError('ARCHIVED_RULE_CHANGED')
  for name,sha in self.config['risk_execution_hashes'].items():
   if digest(self.root/name)!=sha:raise RuntimeError('RISK_CODE_CHANGED:'+name)
 def check_recovery(self):
  with sqlite3.connect(self.db) as c:
   if c.execute("select count(*) from forward_cycles where status='IN_PROGRESS'").fetchone()[0]:raise RuntimeError('INCOMPLETE_CYCLE_RECONCILIATION_REQUIRED')
   for a in 'ABC':
    account=self.ledger.account(a);row=c.execute('select trade_id from forward_positions where agent=?',(a,)).fetchone()
    pos=json.loads(account['position_json']) if account.get('position_json') else None
    if bool(row)!=bool(pos) or (row and row[0]!=pos['trade_id']):raise RuntimeError('POSITION_RECONCILIATION_REQUIRED:'+a)
 def is_halted(self):
  with sqlite3.connect(self.db) as c:
   old=c.execute("select 1 from forward_halts h where not exists (select 1 from forward_halt_resolutions r where r.halt_id=h.id and r.original_reason=h.reason and r.resolution_code='RESOLVED')").fetchone()
   has_gaps=c.execute("select 1 from sqlite_master where type='table' and name='forward_gap_events'").fetchone()
   gaps=c.execute('select 1 from forward_gap_events g where not exists (select 1 from forward_gap_resolutions r where r.close_at=g.close_at) limit 1').fetchone() if has_gaps else None
   return bool(old or gaps)
 def review_due(self,a):
  with sqlite3.connect(self.db) as c:return c.execute("select 1 from forward_reviews where agent=? and status!='COMPLETED'",(a,)).fetchone() is not None
 def active_rule(self,a,bar_open):
  with sqlite3.connect(self.db) as c:
   latest=c.execute('''select v.batch,v.activation_open,v.rule_json,v.rule_sha256 from forward_versions v
    join forward_version_approvals p on p.agent=v.agent and p.batch=v.batch
    where v.agent=? and p.approved_at<=? and v.activation_open<=?
    order by v.batch desc limit 1''',(a,bar_open,bar_open)).fetchone()
  if latest:
   if hashlib.sha256(latest[2].encode('utf8')).hexdigest()!=latest[3]:raise RuntimeError('FORWARD_RULE_HASH_MISMATCH:'+a)
   return json.loads(latest[2]),'FORWARD_AI_BATCH_'+str(latest[0])
  return (self.rule,'V25') if a=='A' else (None,'V3')
 def result_close(self,a,result,reason,at):
  if not result.get('accepted'):return
  with sqlite3.connect(self.db) as c:
   row=c.execute('select trade_id,signal_close,initial_cash from forward_positions where agent=?',(a,)).fetchone()
   if not row:return
   account=self.ledger.account(a)
   if account['state']!='FLAT':return
   pnl=D(account['cash'])-D(row[2]);c.execute('insert or ignore into forward_trades values(?,?,?,?,?,?)',(row[0],a,row[1],at,str(pnl),reason));c.execute('delete from forward_positions where agent=?',(a,))
   count=c.execute('select count(*) from forward_trades where agent=?',(a,)).fetchone()[0]
   if count and count%40==0:c.execute('insert or ignore into forward_reviews values(?,?,?,?)',(a,count,'REVIEW_DUE','40 closed forward trades; waiting for bounded review'))
 def monitor(self,q):
  if not quote_fresh(q,'1970-01-01T00:00:00+00:00'):return
  q=dict(q);q['event_id']=position_quote_id(q)
  for a in 'ABC':
   account=self.ledger.account(a)
   if account['state']=='FLAT':continue
   with sqlite3.connect(self.db) as c:meta=c.execute('select target,exit_due from forward_positions where agent=?',(a,)).fetchone()
   if not meta:raise RuntimeError('MISSING_POSITION_META:'+a)
   pos=json.loads(account['position_json']);event=q['event_id']+':'+a
   if account['state']=='STOP_EXIT_PENDING':
    result=recover_pending_stop(ledger=self.ledger,agent=a,quote=q,cost=COST,event_id=event);self.result_close(a,result,'STOP_RECOVERY',q['event_at']);continue
   if a=='C':
    result=handle_research_liquidation(ledger=self.ledger,agent='C',quote=q,cost=COST,event_id=event,maintenance_rate=D('.01'))
    if result.get('accepted') or self.ledger.account(a)['state']=='STOP_EXIT_PENDING':self.result_close(a,result,'LIQUIDATION',q['event_at']);continue
   result=handle_confirmed_stop(ledger=self.ledger,agent=a,trigger_event_id=event,quote=q,cost=COST)
   if result.get('accepted') or self.ledger.account(a)['state']=='STOP_EXIT_PENDING':self.result_close(a,result,'STOP',q['event_at']);continue
   side='bid' if pos['direction']=='LONG' else 'ask';px=D(q[side]);target=D(meta[0]);due=meta[1];trigger=(px>=target if side=='bid' else px<=target)
   if not due and not trigger:continue
   reason=due or 'TARGET';qty=D(pos['quantity'])
   slip=COST.adverse_slippage_bps/D(10000);price=floor_step(px*(1-slip),COST.simulated_tick) if side=='bid' else ceil_step(px*(1+slip),COST.simulated_tick)
   account=self.ledger.account(a);result=self.ledger.close_position(agent=a,price=price,fee=qty*price*COST.fee_rate,slippage=abs(px-price)*qty,idempotency_key='FWD_CLOSE:'+pos['trade_id'],expected_version=account['version'],reason=reason,cost_model=COST,rationale='RULE_DRIVEN_FORWARD '+reason)
   self.result_close(a,result,reason,q['event_at'])
 def process(self,bars,q,now=None):
  now=now or datetime.now(timezone.utc);self.verify_frozen();self.monitor(q)
  bars=[b for b in bars if b.get('is_closed') is True and dt(b['close_at'])<=now];bars.sort(key=lambda x:dt(x['open_at']))
  for i,b in enumerate(bars):
   if not eligible(b,self.config['earliest_forward_open_utc'],now):continue
   close=dt(b['close_at']).astimezone(timezone.utc).isoformat()
   with sqlite3.connect(self.db) as c:
    row=c.execute('select status from forward_cycles where close_at=?',(close,)).fetchone()
    if row:
     if row[0]=='IN_PROGRESS':raise RuntimeError('INCOMPLETE_CYCLE_RECONCILIATION_REQUIRED')
     continue
    c.execute('insert into forward_bars(open_at,close_at,bar_json) values(?,?,?)',(dt(b['open_at']).astimezone(timezone.utc).isoformat(),close,json.dumps(b,ensure_ascii=False,sort_keys=True)))
    c.execute('insert into forward_cycles(close_at,status) values(?,?)',(close,'IN_PROGRESS'))
   try:
    window=bars[max(0,i-287):i+1];valid=valid_context(window)
    stale=(now-dt(close)).total_seconds()>180
    if stale or not valid:
     with sqlite3.connect(self.db) as c:
      for a in 'ABC':c.execute('insert into forward_decisions values(?,?,?,?)',(a,close,'SKIPPED_STALE' if stale else 'SKIPPED_GAP','{}'))
      if c.execute('select count(*) from forward_positions').fetchone()[0]:
       c.execute("insert or ignore into forward_gap_events values(?,'MISSED_BAR_WHILE_POSITION_OPEN',?)",(close,datetime.now(timezone.utc).isoformat()))
       c.execute("update forward_positions set exit_due=coalesce(exit_due,'TIME_EXIT_GAP_RECOVERY')")
      c.execute('update forward_cycles set status=? where close_at=?',('SKIPPED',close))
     self.monitor(q)
     continue
    for a in 'ABC':
     with sqlite3.connect(self.db) as c:
      meta=c.execute('select bars_held,max_hold from forward_positions where agent=?',(a,)).fetchone()
      if meta:
       held=meta[0]+1;c.execute('update forward_positions set bars_held=?,exit_due=case when ?>=max_hold then coalesce(exit_due,\'TIME_EXIT\') else exit_due end where agent=?',(held,held,a))
    self.monitor(q)
    for a in 'ABC':
     active,version=self.active_rule(a,b['open_at'])
     if version=='FUTURE_VERSION_NOT_ACTIVE':s=None
     else:s=(json_signal(active,window,287) if active is not None else (self.v3.signal_b(window,287) if a=='B' else self.v3.signal_c(window,287)))
     account=self.ledger.account(a);detail={'signal':s,'quote_event_at':q.get('event_at'),'decision_close':close,'mode':'RULE_DRIVEN_FORWARD','version':version};status='FUTURE_VERSION_NOT_ACTIVE' if version=='FUTURE_VERSION_NOT_ACTIVE' else 'WAIT'
     if s and account['state']!='FLAT':status='POSITION_ALREADY_OPEN'
     elif s and self.is_halted():status='MISSED_BAR_HALT'
     elif s and self.review_due(a):status='REVIEW_DUE_BLOCKED'
     elif s and not quote_fresh(q,close):status='QUOTE_UNAVAILABLE'
     elif s:
      q2=dict(q);q2['event_id']=position_quote_id(q2);book={};cash=D(account['cash']);direction=s['direction'];stop=D(s['stop'])
      pre=prepare_entry(equity=cash,available_cash=cash,direction=direction,stop=stop,quote=q2,orderbook=book,cost=COST,quote_age_ms=int(q2['age_ms']),book_age_ms=int(q2['age_ms']),leverage=D('5') if a=='C' else D('1'));detail['preflight']=pre
      if not pre.get('accepted'):status='RISK_REJECTED'
      else:
       price=D(pre['fill_price']);rr=D(str(active['target_r'])) if active is not None else D('2') if a=='C' else D('1.5');target=price+(price-stop)*rr if direction=='LONG' else price-(stop-price)*rr
       slip=COST.adverse_slippage_bps/D(10000);ex=target*(1-slip if direction=='LONG' else 1+slip);gross=(ex-price)*(1 if direction=='LONG' else -1)
       obstacle=s.get('obstacle');blocked=target<=0 or gross<=(price+ex)*COST.fee_rate or (a=='C' and obstacle is not None and ((direction=='LONG' and price<D(obstacle)<target) or (direction=='SHORT' and target<D(obstacle)<price)))
       if blocked:status='TARGET_OR_OBSTACLE_REJECTED'
       else:
        qty=D(pre['quantity']);ref=D(q2['ask' if direction=='LONG' else 'bid']);ident='FWD:'+a+':'+close
        opened=self.ledger.open_position(agent=a,direction=direction,quantity=qty,price=price,stop=stop,fee=D(pre['fee']),slippage=abs(price-ref)*qty,idempotency_key=ident,expected_version=account['version'],decision_id=ident,strategy_version=self.config[a]['version']+':'+version,rationale='RULE_DRIVEN_FORWARD '+s['setup'],quote_id=q2['event_id'],cost_model=COST,research_c_5x=(a=='C'))
        if not opened.get('accepted'):status='LEDGER_REJECTED';detail['ledger']=opened
        else:
         pos=json.loads(self.ledger.account(a)['position_json']);hold=int(active['max_hold']) if active is not None else 12
         with sqlite3.connect(self.db) as c:c.execute('insert into forward_positions(agent,trade_id,signal_close,target,max_hold,initial_cash) values(?,?,?,?,?,?)',(a,pos['trade_id'],close,str(target),hold,str(cash)))
         status='OPENED';detail['trade_id']=pos['trade_id'];detail['target']=str(target)
     with sqlite3.connect(self.db) as c:c.execute('insert into forward_decisions values(?,?,?,?)',(a,close,status,json.dumps(detail,ensure_ascii=False,default=str)))
    with sqlite3.connect(self.db) as c:c.execute("update forward_cycles set status='COMPLETE' where close_at=?",(close,))
   except Exception:
    raise
