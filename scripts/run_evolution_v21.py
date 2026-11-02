"""V2.1 adaptive shadow research; no live execution and no model-supplied code."""
import asyncio, hashlib, json, os, sys
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal as D
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.evolution_v21 import apply_edit,attribute
from scripts.run_evolution import DATA,EXPECTED_DATA,sha,budget_status,run_job,analysis,write_json,boundary
from scripts.evolution_core import acceptance
from scripts.evolution_ai import chart,reservation
from backend.vision_provider import VisionBudget,ConfigurableVisionAdapter,VisionProviderConfig
PREV=ROOT/'docs/reports/abc-evolution-v2/run-20261028T042854266952Z/round-1'
WINDOWS=[{'start':'2023-10-17','train_end':'2025-02-15','validation_end':'2025-06-17'},{'start':'2023-10-17','train_end':'2025-06-17','validation_end':'2025-10-17'}]
def signal_summary(result,agent,start,end):
 rows=[s for s in result['signals'] if s['agent']==agent and start<=s['time']<end]
 counts={}
 for s in rows:counts[s['status']]=counts.get(s['status'],0)+1
 return {'raw_trigger_signals':len(rows),'status_counts':counts,'risk_rejected':counts.get('REJECTED_RISK_OR_TARGET',0)+counts.get('REJECTED_LEDGER',0),'executed':counts.get('OPENED',0),'closed_trades':result['summary'][agent]['closed_trades']}
def propose(path,roundno,agent,feedback,bars,cutoff):
 target=path/f'round-{roundno}'/'candidates'/f'{agent}-model.json';target.parent.mkdir(parents=True,exist_ok=True)
 if target.exists():
  obj=json.loads(target.read_text(encoding='utf8'));return apply_edit(feedback['previous_rule'],obj['response']['edit'])
 prompt=('BTC 15m shadow research only. Review the supplied actual prior candidate rule, filter attribution, realized net, costs, PF, drawdown, trade count, baseline differences and rejection. '
 'Return JSON with fields analysis, reason, prior_closed_trades, edit. edit is {"long_index":integer,"short_index":integer}: remove ONE corresponding redundant filter per side, with indices starting at 0. '
 'Keep all other conditions, stop, target and hold unchanged. A trend-following, B reversal, C Tony trend-following structural risk/reward. Do not force trading or optimize trade count alone. '
 'Explain which condition blocked entries and cite exact previous closed-trade count and results. A candidate with fewer than 40 is not proven. No code. Feedback JSON: '+json.dumps(feedback,ensure_ascii=False))
 image=chart(bars,cutoff);(target.parent/f'{agent}-prompt-chart.png').write_bytes(image);rid=f'v21:{path.name}:{roundno}:{agent}'
 local=VisionBudget(str(path/'run-budget.sqlite3'));local.bind_total_limit(D('1'));amount=reservation(prompt)+D('.0012')
 if amount>D('.05'):raise RuntimeError('CALL_COST_CAP')
 ok,why=local.reserve(rid,amount,D('1'),100,total_limit=D('1'))
 if not ok:raise RuntimeError(why)
 if not os.getenv('DEEPSEEK_API_KEY'):
  for line in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines():
   k,sep,v=line.partition('=')
   if sep and k.strip()=='DEEPSEEK_API_KEY':os.environ[k.strip()]=v.strip().strip('"\'')
 cfg=VisionProviderConfig(enabled=True,calls_authorized=True,model='deepseek-flash',thinking_mode='disabled',max_concurrency=1,max_output_tokens=2000,max_requests_per_day=1000,max_call_cost_usd=D('.05'),daily_budget_usd=D('5'),total_budget_usd=D('5'),input_price_per_million_usd=D('.30'),output_price_per_million_usd=D('1.20'),budget_db_path=str(ROOT/'docs/reports/abc-replay-v1/round1-budget.sqlite3'))
 response=asyncio.run(ConfigurableVisionAdapter(cfg)._send_image_request(image,prompt,rid))
 obj={'request_id':rid,'prompt':prompt,'image_sha256':hashlib.sha256(image).hexdigest(),'response':response,'training_cutoff':cutoff,'reserved_usd':str(amount)};write_json(target,obj);local.mark(rid,'RECORDED',response.get('estimated_cost_usd'))
 if not isinstance(response.get('analysis'),str) or not isinstance(response.get('reason'),str) or response.get('prior_closed_trades')!=feedback['previous_result']['summary']['closed_trades']:raise RuntimeError('INVALID_OR_UNREFERENCED_MODEL_OUTPUT '+str(response.get('status','schema')))
 return apply_edit(feedback['previous_rule'],response['edit'])
def main():
 if sha(DATA)!=EXPECTED_DATA:raise RuntimeError('DATA_MISMATCH')
 if D(budget_status()['unreserved_usd'])<=D('0'):raise RuntimeError('TOTAL_BUDGET_EXHAUSTED')
 run='run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');path=ROOT/'docs/reports/abc-evolution-v21'/run;path.mkdir(parents=True);bars=json.loads(DATA.read_text(encoding='utf8'));none={a:None for a in 'ABC'}
 prev_rules={a:json.loads((PREV/'candidates'/f'{a}-rule.json').read_text(encoding='utf8')) for a in 'ABC'}
 prev_comp=json.loads((PREV/'comparison.json').read_text(encoding='utf8'));prev_result=json.loads((PREV/'candidate-validation'/'results-validation-v30.json').read_text(encoding='utf8'))
 prev_start='2024-10-16T00:00:00+00:00';prev_end='2025-02-14T21:00:00+00:00'
 m={'run_id':run,'data_sha256':EXPECTED_DATA,'model_run_cap':'1','rounds':0,'prior':str(PREV),'windows':WINDOWS,'promoted':{},'validation_status':'PREVIOUSLY_VIEWED_INTERNAL'};write_json(path/'manifest.json',m);print('RUN',path,flush=True)
 for rnd,w in enumerate(WINDOWS,1):
  rd=path/f'round-{rnd}';rd.mkdir();end=boundary(w['train_end']);rules={};feedbacks={}
  for a in 'ABC':
   print('ATTRIBUTION',rnd,a,flush=True)
   stat=attribute(prev_rules[a],bars,prev_start,prev_end);comp=prev_comp[a]
   feedback={'previous_rule':prev_rules[a],'previous_result':comp['candidate'],'baseline_result':comp['baseline'],'failure_reason':comp['reason'],'previous_signal_status':signal_summary(prev_result,a,prev_start,prev_end),'condition_attribution':stat,'suggested_direction':'Consider removing one demonstrably restrictive nonessential paired filter while preserving trading identity; assess net expectancy and costs, never force entries.'}
   feedbacks[a]=feedback;write_json(rd/f'{a}-feedback.json',feedback)
   print('MODEL',rnd,a,flush=True);rules[a]=propose(path,rnd,a,feedback,bars,end);write_json(rd/'candidates'/f'{a}-rule.json',rules[a])
  # Only known data and rules are frozen before the later validation interval.
  pt=run_job(rd/'parent-train','train-v30',w,'ABC',none,prev_rules)
  ct=run_job(rd/'candidate-train','train-v30',w,'ABC',none,rules)
  write_json(rd/'freeze.json',{'window':w,'parent_rules':prev_rules,'candidate_rules':rules,'feedback_hashes':{a:sha(rd/f'{a}-feedback.json') for a in 'ABC'},'candidate_train_sha256':sha(rd/'candidate-train'/'results-train-v30.json'),'frozen_utc':datetime.now(timezone.utc).isoformat()})
  pv=run_job(rd/'parent-validation','validation-v30',w,'ABC',none,prev_rules)
  cv=run_job(rd/'candidate-validation','validation-v30',w,'ABC',none,rules)
  bv=run_job(rd/'v3-validation','validation-v30',w,'ABC',none)
  comparison={};cut_end=(datetime.fromisoformat(boundary(w['validation_end']))-__import__('datetime').timedelta(hours=3)).isoformat()
  for a in 'ABC':
   p=analysis(pv,rd/'parent-validation'/'ledger-validation-v30.sqlite3',a);c=analysis(cv,rd/'candidate-validation'/'ledger-validation-v30.sqlite3',a);base=analysis(bv,rd/'v3-validation'/'ledger-validation-v30.sqlite3',a)
   eligible,reason=acceptance(base['summary'],c['summary'],40)
   comparison[a]={'baseline':base,'parent':p,'candidate':c,'reason':reason,'promoted':eligible,'signal_summary':signal_summary(cv,a,end,cut_end),'condition_attribution':attribute(rules[a],bars,end,cut_end)}
   if eligible:m['promoted'][a]={'round':rnd,'rule':rules[a]}
  write_json(rd/'comparison.json',comparison);m['rounds']=rnd;write_json(path/'manifest.json',m);print('ROUND_DONE',rnd,json.dumps({a:{'n':comparison[a]['candidate']['summary']['closed_trades'],'net':comparison[a]['candidate']['summary']['net_pnl'],'reason':comparison[a]['reason']} for a in 'ABC'}),flush=True)
  prev_rules=rules;prev_comp={a:{'baseline':comparison[a]['parent'],'candidate':comparison[a]['candidate'],'reason':comparison[a]['reason']} for a in 'ABC'};prev_result=cv;prev_start=end;prev_end=cut_end
 print('COMPLETE',path,flush=True)
if __name__=='__main__':main()
