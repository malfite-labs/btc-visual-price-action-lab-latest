"""V2.3 bounded model research with pre-replay version and signal checks."""
import asyncio,hashlib,json,os,sqlite3,statistics,sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime,timezone
from decimal import Decimal as D
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.run_evolution import DATA,EXPECTED_DATA,sha,budget_status,run_job,analysis,write_json,validate_job
from scripts.evolution_core import acceptance
from scripts.evolution_ai import chart,reservation
from scripts.run_evolution_v2 import SCHEMA
from scripts.rule_evolution_v2 import validate,signal
from scripts.evolution_v23_guard import fingerprint,behavior_diff,find_cache
from scripts.evolution_edge_v22 import excursions
from scripts.evolution_v22_followup import group_metrics,c_exposure
from backend.vision_provider import VisionBudget,ConfigurableVisionAdapter,VisionProviderConfig
PARENT=ROOT/'docs/reports/abc-evolution-v22-followup/run-20261028T075958339147Z';V3=ROOT/'docs/reports/abc-v30';CUT='2025-06-17T00:00:00+00:00';WINDOW={'start':'2023-10-17','train_end':'2025-06-17','validation_end':'2025-10-17'}
def catalog():
 result=defaultdict(list);root=ROOT/'docs/reports'
 def walk(x,file):
  if isinstance(x,dict):
   if set(x)=={'long','short','stop_bars','target_r','max_hold'}:
    try:validate(x);result[fingerprint(x)].append(str(file.relative_to(ROOT)))
    except ValueError:pass
   else:
    for v in x.values():walk(v,file)
  elif isinstance(x,list):
   for v in x:walk(v,file)
 for pattern in ('*rule.json','freeze.json','round.json','*-model.json','*-ai.json','manifest.json'):
  for f in root.rglob(pattern):
   try:walk(json.loads(f.read_text(encoding='utf8')),f)
   except (ValueError,OSError,UnicodeDecodeError):continue
 return dict(result)
def cache_key(agent,rule,mode):
 engine_files=['scripts/run_evolution.py','scripts/rule_evolution_v2.py','docs/reports/abc-v30/run_v30.py']
 cost_files=['backend/domain.py','backend/execution.py','backend/ledger.py']
 digest=lambda files:hashlib.sha256(''.join(sha(ROOT/f) for f in files).encode()).hexdigest()
 return {'agent':agent,'mode':mode,'rule':fingerprint(rule),'data':EXPECTED_DATA,'window':json.dumps(WINDOW,sort_keys=True,separators=(',',':')),'engine':digest(engine_files),'cost':digest(cost_files)}
def cache_record(directory,key):
 mode=key['mode'];write_json(directory/'v23-cache.json',{'key':key,'result_sha256':sha(directory/('results-'+mode+'.json')),'ledger_sha256':sha(directory/('ledger-'+mode+'.sqlite3'))})
def cached_replay(path,agent,rule,mode):
 key=cache_key(agent,rule,mode);hit=find_cache(ROOT/'docs/reports',key)
 if hit is not None:
  result=validate_job(hit,mode,agent)
  if result is None:raise RuntimeError('CACHE_INCOMPLETE')
  write_json(path/f'{agent}-{mode}-cache-reference.json',{'cache':str(hit),'key':key})
  print('REUSED_VERIFIED_CACHE',agent,mode,flush=True);return result,hit
 directory=path/(f'{agent}-candidate-'+('train' if mode=='train-v30' else 'validation'))
 result=run_job(directory,mode,WINDOW,agent,{agent:None},{agent:rule});cache_record(directory,key)
 return result,directory
def c_modes(bars):
 r=json.loads((V3/'results-train-v30.json').read_text(encoding='utf8'));t=[x for x in r['trades'] if x['agent']=='C' and x['exit_bar_close']<CUT];ix={b['open_at']:i for i,b in enumerate(bars)}
 with sqlite3.connect((V3/'ledger-train-v30.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as db:rows=db.execute("select trade_id,fee,slippage from fills where agent='C'").fetchall()
 fees=defaultdict(lambda:[0.,0.])
 for tid,fee,slip in rows:fees[tid][0]+=float(fee);fees[tid][1]+=float(slip)
 out={}
 for setup in ('T1','T2','T3','T4'):
  ts=[v for v in t if v['setup']==setup];s=group_metrics(ts);s['fees']=sum(fees[v['trade_id']][0] for v in ts);s['slippage']=sum(fees[v['trade_id']][1] for v in ts);s['gross_same_fills_proxy']=s['net_pnl']+s['fees']+s['slippage'];s['effective_leverage_mean']=statistics.mean(c_exposure(v)['effective_leverage'] for v in ts) if ts else None;s['paths']={}
  for h in (4,8,12,24):
   vals=[]
   for v in ts:
    i=ix.get(v['entry_time']);
    if i is None or i+h>len(bars) or bars[i+h-1]['close_at']>=CUT:continue
    z=excursions(bars,i,v['direction'],h)
    if z:vals.append(z)
   s['paths'][str(h)]={'count':len(vals),'mean_directional_close_pct':statistics.mean(x['close_return_pct'] for x in vals) if vals else None,'mfe_pct':statistics.mean(x['mfe_pct'] for x in vals) if vals else None,'mae_pct':statistics.mean(x['mae_pct'] for x in vals) if vals else None}
  s['directions']={d:group_metrics([v for v in ts if v['direction']==d]) for d in ('LONG','SHORT')};out[setup]=s
 return out
def propose(path,a,evidence,bars,known):
 d=path/'candidates';d.mkdir(exist_ok=True);style={'A':'trend continuation and break quality','B':'reversal false break versus ordinary touch','C':'Tony trend following, structural price action and bounded risk'}[a]
 for n in (1,2,3):
  prompt=('Simulated BTC 15-minute research only. Analyze actual training losses, costs, exits, prior closed-only path results and market states. Propose a structurally distinct testable '+style+' rule, not merely changed window length or a recycled rule. '
   'Return JSON with analysis, reason, problem, expected_behavior_change, hypothesis, test_method, rule. Explain concrete loss and how behavior differs. '
   'Modify the entry_conditions mechanism; leave stop_bars, target_r, max_hold exactly as previous_rule. Do not invent signals or force trades. '+SCHEMA+' Evidence (all before cutoff): '+json.dumps(evidence,ensure_ascii=False,separators=(',',':')))
  rid=f'v23:{path.name}:{a}:{n}';image=chart(bars,CUT);(d/f'{a}-{n}-chart.png').write_bytes(image);local=VisionBudget(str(path/'run-budget.sqlite3'));local.bind_total_limit(D(1));amount=reservation(prompt)+D('.0024')
  if amount>D('.05'):raise RuntimeError('CALL_COST_CAP')
  ok,why=local.reserve(rid,amount,D(1),100,total_limit=D(1))
  if not ok:raise RuntimeError(why)
  if not os.getenv('DEEPSEEK_API_KEY'):
   for line in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines():
    k,sep,v=line.partition('=')
    if sep and k.strip()=='DEEPSEEK_API_KEY':os.environ[k.strip()]=v.strip().strip('"\'')
  cfg=VisionProviderConfig(enabled=True,calls_authorized=True,model='deepseek-flash',thinking_mode='disabled',max_concurrency=1,max_output_tokens=3000,max_requests_per_day=1000,max_call_cost_usd=D('.05'),daily_budget_usd=D('5'),total_budget_usd=D('5'),input_price_per_million_usd=D('.30'),output_price_per_million_usd=D('1.20'),budget_db_path=str(ROOT/'docs/reports/abc-replay-v1/round1-budget.sqlite3'))
  response=asyncio.run(ConfigurableVisionAdapter(cfg)._send_image_request(image,prompt,rid));record={'request_id':rid,'prompt':prompt,'response':response,'image_sha256':hashlib.sha256(image).hexdigest(),'reserve':str(amount)};write_json(d/f'{a}-{n}-model.json',record);local.mark(rid,'RESPONSE_RECORDED',response.get('estimated_cost_usd'))
  try:
   rule=validate(response['rule']);base=evidence['previous_rule'];
   if any(rule[k]!=base[k] for k in ('stop_bars','target_r','max_hold')) or (rule['long']==base['long'] and rule['short']==base['short']):raise ValueError('NOT_ENTRY_STRUCTURE_CHANGE')
   if any(not isinstance(response.get(k),str) or not response[k].strip() for k in ('analysis','reason','problem','expected_behavior_change','hypothesis','test_method')):raise ValueError('MISSING_RATIONALE')
   fp=fingerprint(rule)
   if fp in known:
    train_cache=find_cache(ROOT/'docs/reports',cache_key(a,rule,'train-v30'));val_cache=find_cache(ROOT/'docs/reports',cache_key(a,rule,'validation-v30'))
    write_json(d/f'{a}-{n}-duplicate.json',{'fingerprint':fp,'prior_versions':known[fp],'verified_train_cache':str(train_cache) if train_cache else None,'verified_validation_cache':str(val_cache) if val_cache else None,'status':'DUPLICATE_REUSABLE' if train_cache and val_cache else 'RULE_DUPLICATE_NO_VERIFIED_EQUIVALENT_CACHE'})
    if not (train_cache and val_cache):continue
   write_json(d/f'{a}-rule.json',rule);return rule,fp
  except (KeyError,ValueError,TypeError) as e:write_json(d/f'{a}-{n}-rejected.json',{'reason':str(e),'status':'UNSUPPORTED_OR_INVALID_STRUCTURAL_RULE'})
 raise RuntimeError('NO_VALID_UNIQUE_CANDIDATE:'+a)
def raw_precheck(rule,bars,parent,a):
 actual=set();segments=[];seg=[]
 for b in bars:
  if seg and seg[-1]['close_at']!=b['open_at']:segments.append(seg);seg=[]
  seg.append(b)
 if seg:segments.append(seg)
 for seg in segments:
  for i in range(287,len(seg)):
   b=seg[i]
   if b['close_at']>='2025-06-16T21:00:00+00:00':break
   x=signal(rule,seg,i)
   if x:actual.add((b['close_at'],x['direction']))
 old={(s['time'],s['direction']) for s in parent['signals'] if s['agent']==a and s['status']!='OUTSIDE_RESEARCH_WINDOW'}
 return {'old_raw_signals':len(old),'new_raw_signals':len(actual),'symmetric_difference':len(old^actual),'scope':'training timestamps only, independently of position/risk; no future prices'}
def main():
 if sha(DATA)!=EXPECTED_DATA or D(budget_status()['unreserved_usd'])<=0:raise RuntimeError('DATA_OR_BUDGET')
 path=ROOT/'docs/reports/abc-evolution-v23'/('run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'));path.mkdir(parents=True);print('RUN',path,flush=True);bars=json.loads(DATA.read_text(encoding='utf8'));known=catalog();print('CATALOG_UNIQUE_RULES',len(known),flush=True)
 parent=json.loads((PARENT/'candidate-train/results-train-v30.json').read_text(encoding='utf8'));parent_val=json.loads((PARENT/'candidate-validation/results-validation-v30.json').read_text(encoding='utf8'));pastcomp=json.loads((PARENT/'comparison.json').read_text(encoding='utf8'));cstats=c_modes(bars);write_json(path/'c-mode-training-analysis.json',cstats)
 rules={};fps={}
 for a in 'ABC':
  ev=json.loads((PARENT/f'{a}-analysis.json').read_text(encoding='utf8'));prior=json.loads((PARENT/'candidates'/f'{a}-rule.json').read_text(encoding='utf8'));ev['previous_rule']=prior;ev['previous_training_analysis']=analysis(parent,PARENT/'candidate-train/ledger-train-v30.sqlite3',a);ev['research_requirements']='Do not repeat earlier rules; provide a structural hypothesis, distinguish false breakout vs touch, account for costs and actual exits; time-only parameter edits are insufficient.'
  if a=='C':ev['tony_t1_t2_t3_t4_stats']=cstats
  print('MODEL',a,flush=True);rules[a],fps[a]=propose(path,a,ev,bars,known);known.setdefault(fps[a],[]).append(str(path/'candidates'/f'{a}-rule.json'));check=raw_precheck(rules[a],bars,parent,a);write_json(path/f'{a}-precheck.json',check);print('PRECHECK',a,json.dumps(check),flush=True)
 results={}
 for a in 'ABC':
  train,train_dir=cached_replay(path,a,rules[a],'train-v30');effect=behavior_diff(parent,train,a);write_json(path/f'{a}-train-behavior.json',effect)
  if effect['classification']=='NO_EFFECT':results[a]={'status':'NO_EFFECT_TRAINING','train':effect,'validation':None};continue
  write_json(path/f'{a}-freeze.json',{'rule_sha256':sha(path/'candidates'/f'{a}-rule.json'),'train_sha256':sha(train_dir/'results-train-v30.json'),'data_sha256':EXPECTED_DATA,'window':WINDOW,'frozen_utc':datetime.now(timezone.utc).isoformat()})
  val,val_dir=cached_replay(path,a,rules[a],'validation-v30');behavior=behavior_diff(parent_val,val,a);base=pastcomp[a]['baseline'];prior=pastcomp[a]['candidate'];candidate=analysis(val,val_dir/'ledger-validation-v30.sqlite3',a);ok,why=acceptance(base['summary'],candidate['summary'],40)
  if behavior['classification']=='NO_EFFECT':ok=False;why='NO_EFFECT_VALIDATION'
  results[a]={'status':'COMPLETE','train':effect,'validation_behavior':behavior,'baseline':base,'parent':prior,'candidate':candidate,'promoted':ok,'reason':why}
  write_json(path/f'{a}-result.json',results[a]);print('AGENT_COMPLETE',a,behavior['classification'],why,candidate['summary']['closed_trades'],candidate['summary']['net_pnl'],flush=True)
 write_json(path/'results.json',results);print('COMPLETE',path,flush=True)
if __name__=='__main__':main()
