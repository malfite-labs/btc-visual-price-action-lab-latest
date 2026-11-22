"""V2.4 predicate-grounded explanations and isolated historical research."""
import hashlib,importlib.util,json,sys
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal as D
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.run_evolution_v23 import catalog,propose,raw_precheck,cached_replay,c_modes,CUT,WINDOW
from scripts.run_evolution import DATA,EXPECTED_DATA,sha,budget_status,analysis,write_json
from scripts.evolution_v23_guard import fingerprint,behavior_diff
from scripts.evolution_v24_semantics import describe_rule,v2_a_rule,claim_warnings
from scripts.evolution_core import acceptance
from scripts.rule_evolution_v2 import signal
PREV=ROOT/'docs/reports/abc-evolution-v23/run-20261028T081122989251Z'
BASE=ROOT/'docs/reports/abc-evolution-v22-followup/run-20261028T075958339147Z'
def cross_version(bars):
 f=ROOT/'docs/reports/ab-rule-v2/rule_replay.py';sp=importlib.util.spec_from_file_location('legacy_v2_replay',f);mod=importlib.util.module_from_spec(sp);sp.loader.exec_module(mod)
 rule=v2_a_rule();seg=[];total=0;diff=[]
 def check(seq):
  nonlocal total
  for i in range(20,len(seq)):
   old=mod.signal_a(seq,i);new=signal(rule,seq,i);total+=1
   a=(old['direction'],str(old['stop'])) if old else None;b=(new['direction'],str(new['stop'])) if new else None
   if a!=b and len(diff)<10:diff.append({'time':seq[i]['close_at'],'legacy':a,'normalized':b})
 for b in bars:
  if seg and seg[-1]['close_at']!=b['open_at']:check(seg);seg=[]
  seg.append(b)
 if seg:check(seg)
 return {'A_V2_PYTHON':{'source':str(f.relative_to(ROOT)),'source_sha256':sha(f),'normalized_rule':rule,'rule_fingerprint':fingerprint(rule),'tested_candles':total,'first_mismatches':diff,'entry_stop_on_observed_candles':'MATCH' if not diff else 'MISMATCH','full_execution_equivalence':'UNKNOWN','limitations':'Observed closed candle domain and entry/stop only. Full execution costs/data/cutoffs not proven equivalent. Source has structurally matching breakout and 5-bar stop.'},'B_V2_BOLLINGER':{'status':'UNKNOWN','reason':'rolling mean/std Bollinger and dynamic midline target not represented by the current rule language'},'ABC_V3_PYTHON':{'status':'UNKNOWN','reason':'OR pattern priority, C obstacle filtering, and other semantics not generally expressible or proven by current JSON schema'}}
def main():
 if sha(DATA)!=EXPECTED_DATA or D(budget_status()['unreserved_usd'])<=0:raise RuntimeError('DATA_OR_BUDGET')
 p=ROOT/'docs/reports/abc-evolution-v24'/('run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'));p.mkdir(parents=True);print('RUN',p,flush=True);bars=json.loads(DATA.read_text(encoding='utf8'));known=catalog();converted=cross_version(bars);write_json(p/'cross-version-identity.json',converted);print('CROSS_VERSION',converted['A_V2_PYTHON']['entry_stop_on_observed_candles'],converted['A_V2_PYTHON']['tested_candles'],flush=True)
 known.setdefault(fingerprint(v2_a_rule()),[]).append('docs/reports/ab-rule-v2/rule_replay.py::signal_a normalized entry/stop, full execution UNKNOWN')
 cmodes=json.loads((PREV/'c-mode-training-analysis.json').read_text(encoding='utf8'));write_json(p/'c-mode-training-analysis.json',cmodes)
 old_b=json.loads((PREV/'candidates/B-1-model.json').read_text(encoding='utf8'));mismatch={'source':'V2.3 B','model_claim':'second failed test','evidence':'Rule has prior breach and current return but no explicit independent second breach after first reclaim.','corrected_rule_description':describe_rule(old_b['response']['rule'])};write_json(p/'previous-semantic-mismatch.json',mismatch)
 rules={};pre={};parents={};results={}
 for a in 'ABC':
  ev=json.loads((BASE/f'{a}-analysis.json').read_text(encoding='utf8'));ev['previous_rule']=json.loads((PREV/'candidates'/f'{a}-rule.json').read_text(encoding='utf8'));parents[a]=ev['previous_rule'];priortrain=json.loads((PREV/f'{a}-candidate-train/results-train-v30.json').read_text(encoding='utf8'));ev['previous_training_analysis']=analysis(priortrain,PREV/f'{a}-candidate-train/ledger-train-v30.sqlite3',a);ev['research_requirements']='Use executed conditions as authoritative; do not claim two tests unless explicit two independent breaches and reclaims exist. Favor an entry direction or exit structure grounded in causal indicators; avoid simple filter count or repeat of known rules.'
  if a=='C':ev['tony_t1_t2_t3_t4_stats']=cmodes
  print('MODEL',a,flush=True);rules[a],fp=propose(p,a,ev,bars,known);known.setdefault(fp,[]).append(str(p/'candidates'/f'{a}-rule.json'));(p/'candidates'/f'{a}-conditions-zh.md').write_text(describe_rule(rules[a])+'\n',encoding='utf8')
  response=next(json.loads(f.read_text(encoding='utf8'))['response'] for f in sorted((p/'candidates').glob(f'{a}-*-model.json')) if json.loads(f.read_text(encoding='utf8')).get('response',{}).get('rule')==rules[a]);text=' '.join(str(response.get(k,'')) for k in ('analysis','reason','problem','expected_behavior_change','hypothesis'))
  double=('second fail' in text.lower() or 'second test' in text.lower() or 'double test' in text.lower() or '两次失败' in text)
  write_json(p/'candidates'/f'{a}-semantic-review.json',{'human_description_source':'EXECUTABLE_PREDICATES_ONLY','double_test_claim':'UNVERIFIED_REQUIRES_MANUAL_PREDICATE_PROOF' if double else 'NOT_ASSERTED','claim_warnings':claim_warnings(text,rules[a]),'rule_sha256':sha(p/'candidates'/f'{a}-rule.json')})
  pre[a]=raw_precheck(rules[a],bars,priortrain,a);write_json(p/f'{a}-precheck.json',pre[a]);print('PRECHECK',a,pre[a]['symmetric_difference'],flush=True)
 for a in 'ABC':
  train,train_dir=cached_replay(p,a,rules[a],'train-v30');parenttrain=json.loads((PREV/f'{a}-candidate-train/results-train-v30.json').read_text(encoding='utf8'));effect=behavior_diff(parenttrain,train,a);write_json(p/f'{a}-train-behavior.json',effect)
  if effect['classification']=='NO_EFFECT':results[a]={'status':'NO_EFFECT_TRAIN','train':effect};continue
  write_json(p/f'{a}-freeze.json',{'rule_sha256':sha(p/'candidates'/f'{a}-rule.json'),'train_sha256':sha(train_dir/'results-train-v30.json'),'data_sha256':EXPECTED_DATA,'window':WINDOW,'frozen_utc':datetime.now(timezone.utc).isoformat()})
  val,val_dir=cached_replay(p,a,rules[a],'validation-v30');parentval=json.loads((PREV/f'{a}-candidate-validation/results-validation-v30.json').read_text(encoding='utf8'));effectval=behavior_diff(parentval,val,a);base=json.loads((PREV/f'{a}-result.json').read_text(encoding='utf8'))['baseline'];parent=analysis(parentval,PREV/f'{a}-candidate-validation/ledger-validation-v30.sqlite3',a);cand=analysis(val,val_dir/'ledger-validation-v30.sqlite3',a);ok,why=acceptance(base['summary'],cand['summary'],40)
  if effectval['classification']=='NO_EFFECT':ok=False;why='NO_EFFECT_VALIDATION'
  results[a]={'status':'COMPLETE','train_behavior':effect,'validation_behavior':effectval,'baseline':base,'parent':parent,'candidate':cand,'promoted':ok,'reason':why};write_json(p/f'{a}-result.json',results[a]);print('AGENT_COMPLETE',a,effectval['classification'],why,cand['summary']['closed_trades'],cand['summary']['net_pnl'],flush=True)
 write_json(p/'results.json',results);print('COMPLETE',p,flush=True)
if __name__=='__main__':main()
