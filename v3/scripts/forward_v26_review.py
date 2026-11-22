"""One 40-trade model review; never connected to an exchange execution client."""
import asyncio,hashlib,json,os,sqlite3,threading
from datetime import datetime,timezone
from decimal import Decimal as D
from scripts.evolution_ai import chart,reservation
from scripts.run_evolution_v2 import SCHEMA
from scripts.rule_evolution_v2 import validate
from scripts.forward_v26_core import next_open
from scripts.evolution_v24_semantics import describe_rule
from scripts.evolution_v23_guard import fingerprint
from backend.vision_provider import VisionBudget,ConfigurableVisionAdapter,VisionProviderConfig

def execute_review(engine,agent,batch,bars):
 root,folder=engine.root,engine.folder;request=f'forward-v26:{agent}:{batch}';file=folder/f'{agent}-review-{batch}.json'
 try:
  with sqlite3.connect(engine.db) as c:
   rows=c.execute('''select t.trade_id,t.signal_close,t.closed_at,t.net_pnl,t.reason,o.position_direction,o.quantity,o.price,o.fee,o.slippage,z.price,z.fee,z.slippage,o.strategy_version from forward_trades t join fills o on o.trade_id=t.trade_id and o.position_effect='OPEN' join fills z on z.trade_id=t.trade_id and z.position_effect='CLOSE' where t.agent=? order by t.rowid desc limit 40''',(agent,)).fetchall()
  if len(rows)!=40:raise RuntimeError('FORWARD_40_CLOSED_RECORDS_REQUIRED')
  keys=('trade_id','signal_close','closed_at','net_pnl','reason','direction','quantity','entry_price','entry_fee','entry_slippage','exit_price','exit_fee','exit_slippage','strategy_version');trades=[dict(zip(keys,row)) for row in reversed(rows)]
  now=datetime.now(timezone.utc);previous_rule,previous_version=engine.active_rule(agent,now.isoformat());evidence={'agent':agent,'closed_forward_trades':trades,'previous_version':previous_version,'previous_structured_rule':previous_rule,'policy':'review uses only completed forward trades; version may start only on a future fully new bar','research_only':True}
  if previous_rule is None:evidence['previous_v3_signal_source']=(folder/'v3-signals.py').read_text(encoding='utf8')
  prompt='Analyze these 40 real forward shadow closed trades including losses, costs, stops and exits. Propose a next independent 15-minute research candidate for '+{'A':'trend','B':'reversal','C':'Tony trend-following structural risk'}[agent]+'. No real orders. Return JSON with analysis, reason, rule. '+SCHEMA+' Evidence: '+json.dumps(evidence,ensure_ascii=False,separators=(',',':'))
  local=VisionBudget(str(folder/'forward-model-budget.sqlite3'));local.bind_total_limit(D('1'));amount=reservation(prompt)+D('.0024')
  if amount>D('.05'):raise RuntimeError('MODEL_CALL_CAP')
  ok,why=local.reserve(request,amount,D('1'),1000,total_limit=D('1'))
  if not ok:raise RuntimeError(why)
  if not os.getenv('DEEPSEEK_API_KEY'):
   env=root/'.env'
   if not env.exists():raise RuntimeError('MODEL_CREDENTIAL_MISSING')
   for line in env.read_text(encoding='utf-8-sig').splitlines():
    k,s,v=line.partition('=')
    if s and k.strip()=='DEEPSEEK_API_KEY':os.environ[k.strip()]=v.strip().strip('"\'')
  cfg=VisionProviderConfig(enabled=True,calls_authorized=True,model='deepseek-flash',thinking_mode='disabled',max_concurrency=1,max_output_tokens=3000,max_requests_per_day=1000,max_call_cost_usd=D('.05'),daily_budget_usd=D('5'),total_budget_usd=D('5'),input_price_per_million_usd=D('.30'),output_price_per_million_usd=D('1.20'),budget_db_path=str(root/'docs/reports/abc-replay-v1/round1-budget.sqlite3'))
  image=chart(bars,now.isoformat());(folder/f'{agent}-review-{batch}-chart.png').write_bytes(image);response=asyncio.run(ConfigurableVisionAdapter(cfg)._send_image_request(image,prompt,request))
  file.write_text(json.dumps({'request_id':request,'prompt':prompt,'image_sha256':hashlib.sha256(image).hexdigest(),'response':response,'reservation_usd':str(amount)},ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf8');local.mark(request,'RECORDED',response.get('estimated_cost_usd'))
  if not isinstance(response.get('analysis'),str) or not isinstance(response.get('reason'),str):raise RuntimeError('MODEL_RESPONSE_INVALID')
  rule=validate(response.get('rule'));
  if previous_rule is not None and fingerprint(rule)==fingerprint(previous_rule):raise RuntimeError('DUPLICATE_CURRENT_RULE')
  (folder/f'{agent}-review-{batch}-conditions-zh.md').write_text(describe_rule(rule)+'\n',encoding='utf8');encoded=json.dumps(rule,ensure_ascii=False,sort_keys=True,separators=(',',':'));digest=hashlib.sha256(encoded.encode('utf8')).hexdigest();activation=next_open(datetime.now(timezone.utc)).isoformat()
  with sqlite3.connect(engine.db) as c:
   c.execute('insert into forward_versions values(?,?,?,?,?,?)',(agent,batch,activation,encoded,digest,datetime.now(timezone.utc).isoformat()))
   c.execute("update forward_reviews set status='COMPLETED',details=? where agent=? and batch=?",(json.dumps({'file':file.name,'activation_open':activation,'rule_sha256':digest}),agent,batch))
 except Exception as exc:
  with sqlite3.connect(engine.db) as c:c.execute("update forward_reviews set status='FAILED',details=? where agent=? and batch=?",(type(exc).__name__+':'+str(exc),agent,batch))

def dispatch(engine,bars,active):
 if active and active.is_alive():return active
 with sqlite3.connect(engine.db) as c:
  row=c.execute("select agent,batch from forward_reviews where status='REVIEW_DUE' order by batch,agent limit 1").fetchone()
  if not row:return None
  changed=c.execute("update forward_reviews set status='PROCESSING',details='bounded DeepSeek request; do not retry uncertain outcome' where agent=? and batch=? and status='REVIEW_DUE'",row).rowcount
 if not changed:return None
 worker=threading.Thread(target=execute_review,args=(engine,row[0],row[1],list(bars)),daemon=True);worker.start();return worker
