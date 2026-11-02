"""Active-session, isolated DeepSeek rule evolution. No generated Python is executed."""
import asyncio, json, os, sys
from pathlib import Path
from decimal import Decimal as D
from datetime import datetime, timezone
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.run_evolution import DATA, EXPECTED_DATA, sha, budget_status, run_job, analysis, write_json, boundary
from scripts.evolution_core import walk_forward_windows, acceptance
from scripts.evolution_ai import chart, reservation
from scripts.rule_evolution_v2 import validate
from backend.vision_provider import VisionBudget,ConfigurableVisionAdapter,VisionProviderConfig

SCHEMA='''Return only JSON with fields analysis, reason, rule. rule is an object with exactly long, short, stop_bars, target_r, max_hold. long and short are each arrays of 1-8 AND conditions. Each condition is {"left":FEATURE_OR_NUMBER,"op":">|<|>=|<=","right":FEATURE_OR_NUMBER}. A feature object is {"kind":NAME,"offset":INTEGER,"period":INTEGER}. Valid NAME: open, high, low, close, rolling_high, rolling_low, body_ratio, upper_wick_ratio, lower_wick_ratio, trend_pct, range_pct. Offset 0 is current closed signal bar, 1 is previous, never negative. period 2-96 for rolling_high, rolling_low, trend_pct, range_pct; offset/period may be omitted where not needed. Rolling values include bar at offset; trend_pct is percentage change over period; range_pct is high-low divided by close times 100; ratio features are fractions of candle range. Prices are absolute USD, other features have units above. stop_bars integer 2-30 uses recent structure extreme and 0.01 buffer. target_r number 0.5-5; max_hold integer 1-12. Entry next bar. No trailing stop available. Invent a new coherent rule using these features and parameters; do not output Python. Avoid future data and do not force trading. Both directions must be specified.''' 
def request(path,agent,index,evidence,bars,cutoff):
 target=path/f'round-{index}'/'candidates'/f'{agent}-model.json';target.parent.mkdir(parents=True,exist_ok=True)
 if target.exists():
  x=json.loads(target.read_text(encoding='utf8'));return validate(x['response']['rule'])
 prior=list(target.parent.glob(f'{agent}-failed-*.json'));attempt=len(prior)+1
 style={'A':'trend continuation, trend break and pullback','B':'price-action reversal, rejection or false break','C':'Tony trend-aligned naked-candle, small structural risk and reward'}[agent]
 prompt='Research-only simulated BTC USD 15m. Analyze the supplied real training-only net losses, direction, exit, month, regime, fees and drawdown. Design an original causal '+style+' rule. No live action. '+SCHEMA+' Training evidence: '+json.dumps(evidence,ensure_ascii=False)
 image=chart(bars,cutoff);rid=f'v2:{path.name}:{index}:{agent}:{attempt}'
 local=VisionBudget(str(path/'run-budget.sqlite3'));local.bind_total_limit(D('1'));amount=reservation(prompt)+D('0.0012')
 if amount>D('.05'):raise RuntimeError('CALL_COST_CAP')
 ok,reason=local.reserve(rid,amount,D('1'),100,total_limit=D('1'))
 if not ok:raise RuntimeError(reason)
 if not os.getenv('DEEPSEEK_API_KEY'):
  for line in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines():
   key,sep,val=line.partition('=')
   if sep and key.strip()=='DEEPSEEK_API_KEY':os.environ[key.strip()]=val.strip().strip('"\'')
 cfg=VisionProviderConfig(enabled=True,calls_authorized=True,model='deepseek-flash',thinking_mode='disabled',max_concurrency=1,max_output_tokens=2000,max_requests_per_day=1000,max_call_cost_usd=D('.05'),daily_budget_usd=D('5'),total_budget_usd=D('5'),input_price_per_million_usd=D('.30'),output_price_per_million_usd=D('1.20'),budget_db_path=str(ROOT/'docs/reports/abc-replay-v1/round1-budget.sqlite3'))
 response=asyncio.run(ConfigurableVisionAdapter(cfg)._send_image_request(image,prompt,rid))
 write_json(target,{'request_id':rid,'training_cutoff':cutoff,'prompt':prompt,'response':response,'reservation_usd':str(amount)})
 local.mark(rid,'RESPONSE_RECORDED',response.get('estimated_cost_usd'))
 try:
  if not isinstance(response.get('analysis'),str) or not isinstance(response.get('reason'),str):raise ValueError('INVALID_AI_ANALYSIS '+str(response.get('status','schema')))
  return validate(response.get('rule'))
 except ValueError:
  target.replace(target.parent/f'{agent}-failed-{attempt}.json')
  if attempt>=3:raise
  return request(path,agent,index,evidence,bars,cutoff)

def add_previous_feedback(path,index,agent,evidence):
 out=dict(evidence)
 if index>1:
  rd=path/f'round-{index-1}'
  comparison=json.loads((rd/'comparison.json').read_text(encoding='utf8'))[agent]
  rule=json.loads((rd/'candidates'/f'{agent}-rule.json').read_text(encoding='utf8'))
  out['previous_candidate']={'rule':rule,'result':comparison['candidate'],'selection_reason':comparison['reason']}
 return out

def main():
 if sha(DATA)!=EXPECTED_DATA:raise RuntimeError('DATA_HASH_MISMATCH')
 budget=budget_status()
 if D(budget['unreserved_usd'])<D('1'):raise RuntimeError('INSUFFICIENT_AVAILABLE_LIFETIME_BUDGET')
 run=sys.argv[1] if len(sys.argv)>1 else 'run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');path=ROOT/'docs/reports/abc-evolution-v2'/run;path.mkdir(parents=True,exist_ok=True)
 windows=walk_forward_windows('2023-10-17','2025-10-17',3);bars=json.loads(DATA.read_text(encoding='utf8'));current={};print('RUN',path,flush=True)
 manifest_path=path/'manifest.json'
 if manifest_path.exists():
  manifest=json.loads(manifest_path.read_text(encoding='utf8'))
  if manifest['data_sha256']!=EXPECTED_DATA or manifest['windows']!=windows:raise RuntimeError('RESUME_CONFIG_MISMATCH')
  current=manifest['current_rules']
 else:
  manifest={'run_id':run,'data_sha256':EXPECTED_DATA,'model_run_limit':'1','candidate_method':'AI_STRUCTURED_RULE','windows':windows,'rounds_completed':0,'current_rules':current,'validation_status':'PREVIOUSLY_VIEWED_INTERNAL'};write_json(manifest_path,manifest)
 for index,w in enumerate(windows,1):
  if index<=manifest['rounds_completed']:continue
  rd=path/f'round-{index}';rd.mkdir(exist_ok=True);none={a:None for a in 'ABC'}
  base=run_job(rd/'baseline-train','train-v30',w,'ABC',none,current)
  evidence={a:analysis(base,rd/'baseline-train'/'ledger-train-v30.sqlite3',a) for a in 'ABC'}
  rules={}
  for a in 'ABC':
   print('AI_GENERATING',index,a,flush=True)
   rules[a]=request(path,a,index,add_previous_feedback(path,index,a,evidence[a]),bars,boundary(w['train_end']))
   write_json(rd/'candidates'/f'{a}-rule.json',rules[a])
  train=run_job(rd/'candidate-train','train-v30',w,'ABC',none,rules)
  freeze={'window':w,'baseline_rules':current,'candidate_rules':rules,'training_analysis':evidence,'train_result_hash':sha(rd/'candidate-train'/'results-train-v30.json'),'candidate_hashes':{a:sha(rd/'candidates'/f'{a}-rule.json') for a in 'ABC'},'time':datetime.now(timezone.utc).isoformat()};write_json(rd/'freeze.json',freeze)
  bv=run_job(rd/'baseline-validation','validation-v30',w,'ABC',none,current)
  cv=run_job(rd/'candidate-validation','validation-v30',w,'ABC',none,rules)
  comparison={};next_rules=dict(current)
  for a in 'ABC':
   ok,reason=acceptance(bv['summary'][a],cv['summary'][a],40)
   if ok:next_rules[a]=rules[a]
   comparison[a]={'baseline':analysis(bv,rd/'baseline-validation'/'ledger-validation-v30.sqlite3',a),'candidate':analysis(cv,rd/'candidate-validation'/'ledger-validation-v30.sqlite3',a),'promoted':ok,'reason':reason}
  write_json(rd/'comparison.json',comparison);current=next_rules;manifest.update({'rounds_completed':index,'current_rules':current});write_json(path/'manifest.json',manifest)
  print('ROUND_COMPLETE',index,json.dumps({a:{'reason':comparison[a]['reason'],'candidate':comparison[a]['candidate']['summary']} for a in 'ABC'},ensure_ascii=False),flush=True)
 print('COMPLETE',path,flush=True)
if __name__=='__main__':main()
