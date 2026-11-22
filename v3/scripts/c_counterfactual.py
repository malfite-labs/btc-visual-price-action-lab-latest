"""Isolated counterfactual using separately recorded contemporaneous shadow fills; never calls broker."""
import json,sqlite3,sys
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal as D
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.ledger import Ledger
from backend.domain import CostModel
IDS=['c4148bf65c941d6ee385745dcf3f8788f3bb86cbac0d3456b899020a18d7b577','51f811343186b1fae858fc26a47919b14c6cfe27042d2cd00cf7bbd0c60be07c']
def dt(s):return datetime.fromisoformat(s.replace('Z','+00:00')).astimezone(timezone.utc)
def second_signal_status(entry,exit,signal):return 'NOT_OPENED_SINGLE_POSITION' if entry<=signal<exit else 'ELIGIBLE_FOR_PREFLIGHT'
def ro(p):
 c=sqlite3.connect(Path(p).resolve().as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;return c
def run():
 folder=ROOT/'docs/reports/c-counterfactual-v30';folder.mkdir(parents=True,exist_ok=True);out=folder/'scenario.sqlite3'
 if out.exists():raise RuntimeError('SCENARIO_EXISTS_DO_NOT_OVERWRITE')
 with ro(ROOT/'data/paper.sqlite3') as f,ro(ROOT/'docs/reports/abc-forward-v26/shadow-ledger.sqlite3') as s:
  rows=[dict(f.execute('select * from decisions where decision_id=?',(i,)).fetchone()) for i in IDS]
  assert all(r['agent']=='C' and r['mode']=='RULE_DRIVEN' and r['action']=='OPEN_SHORT' and r['md_version']=='V3.0' and json.loads(r['raw_response'])['execution']['reason']=='TARGET_OR_OBSTACLE_REJECTED' for r in rows)
  shadow=s.execute("select detail from forward_decisions where agent='C' and close_at=? and status='OPENED'",(rows[0]['candle_close'],)).fetchone();detail=json.loads(shadow[0]);pre=detail['preflight'];trade=detail['trade_id'];sig=detail['signal']
  op=dict(s.execute("select * from fills where trade_id=? and position_effect='OPEN'",(trade,)).fetchone());cl=dict(s.execute("select * from fills where trade_id=? and position_effect='CLOSE'",(trade,)).fetchone());trace=dict(s.execute('select * from forward_trades where trade_id=?',(trade,)).fetchone())
  assert sig['stop']==json.loads(rows[0]['raw_response'])['stop_loss'] and op['quantity']==pre['quantity'] and op['price']==pre['fill_price'] and D(s.execute("select cash_after from ledger_events where reference=?",(op['fill_id'],)).fetchone()[0])+D(op['fee'])==D('10000')
  assert dt(detail['quote_event_at'])>=dt(rows[0]['candle_close']) and dt(detail['quote_event_at'])<=dt(op['occurred_at'])
  status=second_signal_status(dt(op['occurred_at']),dt(cl['occurred_at']),dt(rows[1]['candle_close']));assert status=='NOT_OPENED_SINGLE_POSITION'
  l=Ledger(out); opened=l.open_position(agent='C',direction='SHORT',quantity=D(op['quantity']),price=D(op['price']),stop=D(sig['stop']),fee=D(op['fee']),slippage=D(op['slippage']),idempotency_key='COUNTERFACTUAL:'+IDS[0],expected_version=0,decision_id=IDS[0],strategy_version='COUNTERFACTUAL_C_V3.0',rationale='COUNTERFACTUAL shadow fill reference',quote_id=op['quote_id'],research_c_5x=True,cost_model=CostModel())
  if not opened.get('accepted'):raise RuntimeError('PREFLIGHT_LEDGER_REJECTED:'+str(opened))
  closed=l.close_position(agent='C',price=D(cl['price']),fee=D(cl['fee']),slippage=D(cl['slippage']),idempotency_key='COUNTERFACTUAL_CLOSE:'+IDS[0],expected_version=1,decision_id=IDS[0],strategy_version='COUNTERFACTUAL_C_V3.0',rationale='COUNTERFACTUAL shadow TARGET reference',reason='TARGET',cost_model=CostModel())
  if not closed.get('accepted'):raise RuntimeError('CLOSE_REJECTED:'+str(closed))
  pnl=D(l.account('C')['cash'])-D('10000');assert pnl==D(trace['net_pnl'])
  report={'type':'COUNTERFACTUAL_REPLAY','not_formal_history':True,'strategy':'C V3.0','initial_equity_usd':'10000.00','as_of_utc':cl['occurred_at'],'quote_time_utc':detail['quote_event_at'],'limitations':'Historical formal quote and full order book were not persisted. This isolated scenario uses the independently recorded shadow execution quote/preflight and shadow exit, not a reconstruction of the formal fill. Shadow L1 stream was sampled; historical decision-time equity curve marks between events are unavailable.','original_formal_equity_at_scenario_end':'10000.00','counterfactual_equity_at_scenario_end':str(l.account('C')['cash']),'realized_pnl':str(pnl),'open_position':None,'decisions':[{'id':IDS[0],'close_utc':rows[0]['candle_close'],'status':'COUNTERFACTUAL_CLOSED','direction':'SHORT','entry_time_utc':op['occurred_at'],'entry_price':op['price'],'quantity':op['quantity'],'stop':sig['stop'],'target':detail['target'],'entry_fee':op['fee'],'entry_slippage':op['slippage'],'exit_time_utc':cl['occurred_at'],'exit_price':cl['price'],'exit_fee':cl['fee'],'exit_slippage':cl['slippage'],'exit_reason':trace['reason'],'planned_loss':pre['planned_loss'],'net_pnl':str(pnl)},{'id':IDS[1],'close_utc':rows[1]['candle_close'],'status':status,'reason':'第一笔在该信号K线收盘时仍持仓；未追加或再次开仓。'}],'equity_events':[{'time':rows[0]['candle_close'],'formal':'10000.00','scenario':'10000.00','description':'信号前'}, {'time':op['occurred_at'],'formal':'10000.00','scenario':'9999.95','description':'模拟开仓后现金；浮动权益缺少同期完整报价'}, {'time':cl['occurred_at'],'formal':'10000.00','scenario':str(l.account('C')['cash']),'description':'模拟平仓后已实现权益'}],'shadow_source_trade_id':trade}
  with sqlite3.connect(out) as c:
   c.execute('create table scenario_record(id integer primary key check(id=1),json text not null)');c.execute('insert into scenario_record values(1,?)',(json.dumps(report,ensure_ascii=False),))
  (folder/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
  public=ROOT/'frontend/public/research';public.mkdir(parents=True,exist_ok=True);(public/'c-counterfactual-v30.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
  print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':run()
