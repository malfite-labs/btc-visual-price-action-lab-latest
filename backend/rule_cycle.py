"""Rule-based execution in the existing internal paper scheduler; no broker client."""
import hashlib,importlib.util,json,sqlite3
from datetime import timezone
from decimal import Decimal as D
from pathlib import Path
from backend.domain import CostModel,ceil_step,floor_step
from backend.execution import prepare_entry
from scripts.rule_evolution_v2 import signal as json_signal

ROOT=Path(__file__).resolve().parents[1]
def hold_due(entry_close,current_close,max_hold):
 from datetime import datetime
 t=datetime.fromisoformat(entry_close)
 return (current_close.astimezone(timezone.utc)-t.astimezone(timezone.utc)).total_seconds()>=900*max_hold

def describe_signal(agent,s):
 if s is None:return {'A':'未满足突破后回踩并收回及趋势条件','B':'未满足关键位置假突破、影线拒绝或吞没条件','C':'未满足顺势回踩、局部突破、连续动量或反弹结束条件'}[agent]
 return f"RULE_DRIVEN {s['setup']}：{s['direction']}，结构止损 {s['stop']}"

def strategy_target_rejection(config, price, exit_price, gross, fee_rate):
 """Optional strategy-owned target policy, not a public execution constraint."""
 if config.get('require_target_cover_costs',False) and gross <= (price+exit_price)*fee_rate:
  return 'TARGET_CANNOT_COVER_COSTS'
 return None

def describe_no_signal(agent,bars,rule):
 from scripts.rule_evolution_v2 import value, OPS
 from scripts.evolution_v24_semantics import feature
 i=len(bars)-1;b=bars[i];c=D(str(b['close']));o=D(str(b['open']));h=D(str(b['high']));l=D(str(b['low']))
 if agent=='A':
  parts=[]
  for side,label in [('long','多'),('short','空')]:
   failed=[]
   for cond in rule[side]:
    try:
     left=value(cond['left'],bars,i);right=value(cond['right'],bars,i)
     if not OPS[cond['op']](left,right):failed.append(f"{feature(cond['left'])}={left} 不满足 {cond['op']} {feature(cond['right'])}={right}")
    except (IndexError,ZeroDivisionError):failed.append('指标上下文不足')
   parts.append(label+'：'+('；'.join(failed[:2]) if failed else '全部条件通过'))
  return 'RULE_DRIVEN WAIT：'+'；'.join(parts)
 prior=bars[i-10:i];hi=max(D(str(x['high'])) for x in prior);lo=min(D(str(x['low'])) for x in prior)
 if agent=='B':return f'RULE_DRIVEN WAIT：收盘{c}，本根低/高{l}/{h}，此前10根支撑/阻力{lo}/{hi}；未出现符合规则的假突破收回、边界影线拒绝或吞没。'
 prior8=bars[i-8:i];h8=max(D(str(x['high'])) for x in prior8);l8=min(D(str(x['low'])) for x in prior8);trend=D(str(bars[i-1]['close']))-D(str(bars[i-12]['close']))
 return f'RULE_DRIVEN WAIT：收盘{c}，前12根收盘差{trend}，前8根结构高/低{h8}/{l8}；未出现规则规定的T1/T2/T3/T4入场形态。'

def load_profile():
 p=ROOT/'config/rule_v26';profile=json.loads((p/'profile.json').read_text(encoding='utf8'))
 for a in 'ABC':
  item=profile[a];f=p/item['file']
  if hashlib.sha256(f.read_bytes()).hexdigest()!=item['sha256']:raise RuntimeError('RULE_PROFILE_HASH_MISMATCH:'+a)
 spec=importlib.util.spec_from_file_location('formal_v3_frozen_signals',p/'v3-signals.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
 rule=json.loads((p/'A-rule.json').read_text(encoding='utf8'))
 from scripts.rule_evolution_v2 import validate
 validate(rule)
 return profile,rule,mod

class RuleCycleRunner:
 audit_version='rule-rejection-audit-v1'
 def __init__(self,ledger,db_path,cost=CostModel(),enabled=True):
  self.ledger=ledger;self.db_path=str(db_path);self.cost=cost;self.enabled=enabled;self.profile,self.rule,self.v3=load_profile()
  with sqlite3.connect(self.db_path) as c:c.execute('''create table if not exists rule_position_meta(agent text primary key,decision_id text not null,trade_id text,target text not null,entry_close text not null,max_hold integer not null,version text not null,exit_due text)''')
 def check_profile(self):
  p=ROOT/'config/rule_v26'
  for a in 'ABC':
   item=self.profile[a]
   if hashlib.sha256((p/item['file']).read_bytes()).hexdigest()!=item['sha256']:raise RuntimeError('FROZEN_RULE_CHANGED:'+a)
 def _meta(self,a):
  with sqlite3.connect(self.db_path) as c:return c.execute('select decision_id,trade_id,target,entry_close,max_hold,version,exit_due from rule_position_meta where agent=?',(a,)).fetchone()
 def _reconcile(self,a):
  acc=self.ledger.account(a);meta=self._meta(a)
  if not meta:return
  if acc['state']=='FLAT':
   with sqlite3.connect(self.db_path) as c:c.execute('delete from rule_position_meta where agent=?',(a,))
   return
  pos=json.loads(acc['position_json']) if acc['position_json'] else None
  if not pos:raise RuntimeError('RULE_POSITION_STATE_INCONSISTENT:'+a)
  if meta[1] is None:
   with sqlite3.connect(self.db_path) as c:
    fill=c.execute("select trade_id from fills where decision_id=? and agent=? and position_effect='OPEN'",(meta[0],a)).fetchone()
    if not fill or fill[0]!=pos['trade_id']:raise RuntimeError('RULE_POSITION_PENDING_RECONCILE:'+a)
    c.execute('update rule_position_meta set trade_id=? where agent=?',(fill[0],a))
  elif meta[1]!=pos['trade_id']:raise RuntimeError('RULE_POSITION_ID_MISMATCH:'+a)
 def mark_time_exits(self,candle_close):
  """Register exits from a validated closed bar, independently of entry permission."""
  from datetime import datetime,timedelta
  for a in 'ABC':
   self._reconcile(a);meta=self._meta(a);acc=self.ledger.account(a)
   if not meta or acc['state'] not in ('OPEN_LONG','OPEN_SHORT') or meta[6] is not None:continue
   if not hold_due(meta[3],candle_close,meta[4]):continue
   due_at=datetime.fromisoformat(meta[3])+timedelta(minutes=15*meta[4])
   from uuid import uuid4
   with sqlite3.connect(self.db_path) as c:
    changed=c.execute("update rule_position_meta set exit_due='RULE_TIME_EXIT' where agent=? and trade_id=? and exit_due is null",(a,meta[1])).rowcount
    if changed:
     observed=datetime.now(timezone.utc).isoformat()
     c.execute('insert into audit_events values(?,?,?,?,?)',(str(uuid4()),a,'RULE_TIME_EXIT_DUE',json.dumps({'trade_id':meta[1],'due_at':due_at.isoformat(),'observed_closed_bar':candle_close.isoformat(),'recorded_at':observed}),observed))
 def monitor_targets(self,quote):
  outcomes={}
  if not quote or quote.get('source')!='ALPACA_US_BTCUSD_SPOT' or quote.get('currency')!='USD_RESEARCH' or quote.get('timestamp_valid') is not True or not 0<=int(quote.get('age_ms',10**9))<=self.cost.max_quote_age_ms:return outcomes
  for a in 'ABC':
   self._reconcile(a);meta=self._meta(a);acc=self.ledger.account(a)
   if not meta or acc['state'] not in ('OPEN_LONG','OPEN_SHORT'):continue
   pos=json.loads(acc['position_json']);side='bid' if pos['direction']=='LONG' else 'ask';px=D(str(quote.get(side,'0')));target=D(meta[2]);due=meta[6]
   if px<=0:continue
   if not due and not (px>=target if side=='bid' else px<=target):continue
   reason=due or 'RULE_TARGET';qty=D(pos['quantity'])
   slip=self.cost.adverse_slippage_bps/D(10000);price=floor_step(px*(1-slip),self.cost.simulated_tick) if side=='bid' else ceil_step(px*(1+slip),self.cost.simulated_tick)
   out=self.ledger.close_position(agent=a,price=price,fee=qty*price*self.cost.fee_rate,slippage=abs(px-price)*qty,idempotency_key='rule-exit:'+pos['trade_id'],expected_version=acc['version'],reason=reason,decision_id=meta[0],strategy_version=meta[5],rationale='RULE_DRIVEN '+reason,cost_model=self.cost)
   outcomes[a]=out
   if out.get('accepted'):self._reconcile(a)
  return outcomes
 async def run_cycle(self,*,candles,candle_close,quote,orderbook,now=None,**kwargs):
  if not self.enabled:return {a:{'status':'PAPER_EXECUTION_DISABLED'} for a in 'ABC'}
  self.check_profile();key=candle_close.astimezone(timezone.utc).isoformat();results={}
  if len(candles)!=288:return {a:{'status':'INSUFFICIENT_CANDLES'} for a in 'ABC'}
  self.mark_time_exits(candle_close)
  for a in 'ABC':
   self._reconcile(a);acc=self.ledger.account(a);meta=self._meta(a);closed_for_time=False;close_execution=None;closed_direction='LONG' if acc['state']=='OPEN_LONG' else 'SHORT'
   if meta and acc['state'] in ('OPEN_LONG','OPEN_SHORT') and meta[6]=='RULE_TIME_EXIT':
    close_execution=self.monitor_targets(quote).get(a);closed_for_time=self.ledger.account(a)['state']=='FLAT';acc=self.ledger.account(a)
   s=json_signal(self.rule,candles,287) if a=='A' else self.v3.signal_b(candles,287) if a=='B' else self.v3.signal_c(candles,287)
   action=('CLOSE_'+closed_direction if acc['state']=='FLAT' and closed_for_time else 'HOLD' if acc['state']!='FLAT' else 'OPEN_'+s['direction'] if s else 'WAIT')
   reason='达到最长持仓周期并模拟平仓' if closed_for_time else ('已有持仓，继续持有；'+describe_signal(a,s) if acc['state']!='FLAT' else describe_signal(a,s) if s else describe_no_signal(a,candles,self.rule))
   version=self.profile[a]['version'];ident=hashlib.sha256(('RULE_DRIVEN|'+a+'|'+key).encode()).hexdigest();payload={'rationale':reason,'stop_loss':str(s['stop']) if s else None,'setup':s['setup'] if s else None,'decision_type':'RULE_DRIVEN','execution':close_execution if closed_for_time else None}
   with sqlite3.connect(self.db_path,timeout=10) as c:
    c.execute('begin immediate');old=c.execute('select decision_id from decisions where agent=? and candle_close=?',(a,key)).fetchone()
    if old:c.rollback();results[a]={'agent':a,'status':'DUPLICATE_CYCLE','decision_id':old[0]};continue
    c.execute('insert into decisions values(?,?,?,?,?,?,?,?,?,?)',(ident,a,key,version,hashlib.sha256(json.dumps(candles,sort_keys=True,default=str).encode()).hexdigest(),None,'RULE_DRIVEN',action,json.dumps(payload,ensure_ascii=False),'PENDING_EXECUTION' if action.startswith('OPEN_') else 'VALID'));c.commit()
   execution=close_execution if closed_for_time and close_execution else {'accepted':True,'no_fill':True,'reason':action}
   if action.startswith('OPEN_'):
    direction=s['direction'];stop=D(s['stop']);metrics=self.ledger.account_metrics(a,quote,self.cost)
    audit={'quote':quote,'orderbook':orderbook,'account_equity':metrics.get('equity'),'available_margin':metrics.get('available_cash'),'stop':str(stop),'direction':direction,'leverage':'5' if a=='C' else '1'}
    try:
     if not quote or quote.get('timestamp_valid') is not True or quote.get('source')!='ALPACA_US_BTCUSD_SPOT' or quote.get('currency')!='USD_RESEARCH' or D(str(quote['bid']))<=0 or D(str(quote['ask']))<D(str(quote['bid'])):raise ValueError('INVALID_QUOTE_OR_BOOK')
     from datetime import datetime
     if datetime.fromisoformat(quote['event_at'].replace('Z','+00:00')).astimezone(timezone.utc)<candle_close.astimezone(timezone.utc):raise ValueError('QUOTE_BEFORE_CANDLE_CLOSE')
     pre=prepare_entry(equity=D(metrics['equity']),available_cash=D(metrics['available_cash']),direction=direction,stop=stop,quote=quote,orderbook=orderbook,cost=self.cost,quote_age_ms=int(quote['age_ms']),book_age_ms=0,leverage=D(self.profile[a]['max_leverage']),risk_fraction=D(self.profile[a]['risk_fraction']))
    except (KeyError,ValueError,TypeError,ArithmeticError) as exc:pre={'accepted':False,'reason':'INVALID_CONTEXT','error_type':type(exc).__name__,'error_detail':str(exc)}
    audit['preflight']=dict(pre)
    if pre.get('accepted'):
     price=D(pre['fill_price']);rr=D(str(self.rule['target_r'])) if a=='A' else D(self.profile[a]['target_r']);target=price+(price-stop)*rr if direction=='LONG' else price-(stop-price)*rr;slip=self.cost.adverse_slippage_bps/D(10000);exitp=target*(1-slip if direction=='LONG' else 1+slip);gross=(exitp-price)*(1 if direction=='LONG' else -1);obstacle=s.get('obstacle')
     audit.update({'target':str(target),'obstacle':str(obstacle) if obstacle is not None else None,'estimated_target_gross_per_unit':str(gross),'estimated_target_fees_per_unit':str((price+exitp)*self.cost.fee_rate),'required_margin':str(D(pre['notional'])/(D('5') if a=='C' else D('1')))})
     failures=[]
     if target<=0:failures.append('INVALID_TARGET_PRICE')
     # A/B retain their pre-existing strategy-level cost filter; C V3.1 does not.
     strategy_rejection=strategy_target_rejection(self.profile[a],price,exitp,gross,self.cost.fee_rate)
     if strategy_rejection:failures.append(strategy_rejection)
     if failures:pre={'accepted':False,'reason':failures[0],'all_reasons':failures}
    if pre.get('accepted'):
     maxhold=int(self.rule['max_hold']) if a=='A' else int(self.profile[a]['max_hold'])
     with sqlite3.connect(self.db_path) as c:c.execute('insert into rule_position_meta values(?,?,?,?,?,?,?,?)',(a,ident,None,str(target),key,maxhold,version,None))
     qty=D(pre['quantity']);ref=D(str(quote['ask' if direction=='LONG' else 'bid']))
     execution=self.ledger.open_position(agent=a,direction=direction,quantity=qty,price=D(pre['fill_price']),stop=stop,fee=D(pre['fee']),slippage=abs(D(pre['fill_price'])-ref)*qty,idempotency_key='rule-open:'+ident,expected_version=acc['version'],decision_id=ident,strategy_version=version,rationale=reason,quote_id=pre.get('quote_id'),cost_model=self.cost,risk_fraction=D(self.profile[a]['risk_fraction']),research_c_5x=(a=='C'))
     if execution.get('accepted'):self._reconcile(a)
     else:
      with sqlite3.connect(self.db_path) as c:c.execute('delete from rule_position_meta where agent=?',(a,))
    else:execution=pre
    execution['audit']=audit
    payload['execution']=execution
    with sqlite3.connect(self.db_path) as c:c.execute('update decisions set outcome=?,raw_response=? where decision_id=?',('VALID' if execution.get('accepted') else 'RISK_REJECTED',json.dumps(payload,ensure_ascii=False,default=str),ident))
   results[a]={'agent':a,'status':'VALID','mode':'RULE_DRIVEN','action':action,'decision_id':ident,'execution':execution}
  return results
