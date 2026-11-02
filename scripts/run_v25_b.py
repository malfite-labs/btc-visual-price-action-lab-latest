"""B V2.5 structural post-trade review and bounded DeepSeek generation."""
import asyncio,hashlib,json,os,sqlite3,sys
from pathlib import Path
from collections import defaultdict
from decimal import Decimal as D
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.run_evolution import DATA,sha,write_json,run_job
from scripts.evolution_v22_followup import group_metrics
from scripts.evolution_v24_semantics import describe_rule,claim_warnings
from scripts.rule_evolution_v2 import validate
from scripts.run_evolution_v2 import SCHEMA
from scripts.evolution_ai import chart,reservation
from backend.vision_provider import VisionBudget,ConfigurableVisionAdapter,VisionProviderConfig
P=ROOT/'docs/reports/abc-evolution-v25/run-20261028T084916681422Z';OLD=ROOT/'docs/reports/abc-evolution-v24/run-20261028T084424148512Z';WINDOW={'start':'2023-10-17','train_end':'2025-06-17','validation_end':'2025-10-17'}
def main():
 bars=json.loads(DATA.read_text(encoding='utf8'));idx={b['open_at']:i for i,b in enumerate(bars)};result=json.loads((OLD/'B-candidate-validation/results-validation-v30.json').read_text(encoding='utf8'));trades=[t for t in result['trades'] if t['agent']=='B'];out=[];groups={k:defaultdict(list) for k in ('direction','exit','trend','target_space','post4')}
 for t in trades:
  i=idx[t['entry_time']];prior=bars[i-20:i];s=bars[i-1];close=float(s['close']);first=float(prior[0]['close']);trend=(close/first-1)*100;sign=1 if t['direction']=='LONG' else -1;aligned=trend*sign
  regime='countertrend' if aligned<-.3 else 'with_trend' if aligned>.3 else 'neutral';distance=abs(float(t['entry'])-float(t['stop']))/float(t['entry'])*100;target=abs(float(t['target'])-float(t['entry']))/float(t['entry'])*100
  future=bars[i:i+4];future_ok=len(future)==4 and all(x['close_at']==y['open_at'] for x,y in zip(future,future[1:]));p4=((float(future[-1]['close'])/float(bars[i]['open'])-1)*100*sign) if future_ok else None
  categories={'direction':t['direction'],'exit':t['exit_reason'],'trend':regime,'target_space':'target_under_0.5_pct' if target<.5 else 'target_at_least_0.5_pct','post4':'adverse' if p4 is not None and p4<0 else 'favorable' if p4 is not None else 'unavailable'}
  for k,v in categories.items():groups[k][v].append(t)
  out.append({'trade_id':t['trade_id'],'direction':t['direction'],'signal_time':t['signal_time'],'entry_time':t['entry_time'],'entry':t['entry'],'stop':t['stop'],'target':t['target'],'stop_distance_pct':distance,'target_space_pct':target,'exit_reason':t['exit_reason'],'bars_held':t['bars_held'],'net_pnl':t['net_pnl'],'entry_known_prior20_trend_pct':trend,'signal_bar':s,'two_prior_bars':bars[i-3:i-1],'post_entry_4bar_direction_return_pct_posthoc_only':p4,'classifications':categories})
 evidence={'historical_exposure':'These 41 trades are from the same later history previously examined and previously validated. This is internal in-sample research; historical future return is post-hoc scoring only and cannot be used by a signal at its time.', 'previous_rule':json.loads((OLD/'candidates/B-rule.json').read_text(encoding='utf8')),'previous_summary':result['summary']['B'],'trade_count':len(trades),'groups':{k:{v:group_metrics(ts) for v,ts in group.items()} for k,group in groups.items()},'individual_trades':out,'research_focus':'false-break failure, countertrend failures, continuation after rejection, stop/target room, costs; do not demand trade quota'};write_json(P/'B-41-trade-analysis.json',evidence)
 prompt=('Research-only BTC/USD 15m simulated. Analyze all 41 real closed trades and posthoc features; training and evaluation period previously viewed, never use post-entry outcomes as entry features. Design one new B reversal candidate. '
 'Return JSON with nonempty analysis, reason, problem, expected_behavior_change, hypothesis, test_method and rule. Do not claim unencoded patterns. The executable conditions alone are authoritative. Retain fixed fees, 1x and risk. No arbitrary code. '+SCHEMA+' Evidence: '+json.dumps(evidence,ensure_ascii=False,separators=(',',':')))
 img=chart(bars,'2025-10-17T00:00:00+00:00');(P/'B-prompt-chart.png').write_bytes(img);request='v25:'+P.name+':B:1';local=VisionBudget(str(P/'run-budget.sqlite3'));local.bind_total_limit(D(1));amount=reservation(prompt)+D('.0024')
 if amount>D('.05'):raise RuntimeError('CALL_CAP')
 ok,why=local.reserve(request,amount,D(1),100,total_limit=D(1))
 if not ok:raise RuntimeError(why)
 if not os.getenv('DEEPSEEK_API_KEY'):
  for line in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines():
   k,sep,v=line.partition('=')
   if sep and k.strip()=='DEEPSEEK_API_KEY':os.environ[k.strip()]=v.strip().strip('"\'')
 cfg=VisionProviderConfig(enabled=True,calls_authorized=True,model='deepseek-flash',thinking_mode='disabled',max_concurrency=1,max_output_tokens=3000,max_requests_per_day=1000,max_call_cost_usd=D('.05'),daily_budget_usd=D('5'),total_budget_usd=D('5'),input_price_per_million_usd=D('.30'),output_price_per_million_usd=D('1.20'),budget_db_path=str(ROOT/'docs/reports/abc-replay-v1/round1-budget.sqlite3'));response=asyncio.run(ConfigurableVisionAdapter(cfg)._send_image_request(img,prompt,request));write_json(P/'B-model.json',{'request_id':request,'prompt':prompt,'image_sha256':hashlib.sha256(img).hexdigest(),'response':response,'reserved_usd':str(amount)});local.mark(request,'RESPONSE_RECORDED',response.get('estimated_cost_usd'))
 rule=validate(response.get('rule'))
 if any(not isinstance(response.get(k),str) or not response[k].strip() for k in ('analysis','reason','problem','expected_behavior_change','hypothesis','test_method')):raise RuntimeError('MODEL_RESPONSE_INCOMPLETE')
 write_json(P/'B-rule.json',rule);(P/'B-conditions-zh.md').write_text(describe_rule(rule)+'\n',encoding='utf8');text=' '.join(response[k] for k in ('analysis','reason','problem','expected_behavior_change','hypothesis'));write_json(P/'B-semantic-review.json',{'authoritative_description':'B-conditions-zh.md generated from executable predicates','model_claim_warnings':claim_warnings(text,rule),'scope':'pattern names unproven unless predicates explicitly establish them','rule_sha256':sha(P/'B-rule.json')})
 print('B_MODEL_OK',response.get('estimated_cost_usd'),flush=True);run_job(P/'B-validation','validation-v30',WINDOW,'B',{'B':None},{'B':rule});print('B_REPLAY_COMPLETE',flush=True)
if __name__=='__main__':main()
