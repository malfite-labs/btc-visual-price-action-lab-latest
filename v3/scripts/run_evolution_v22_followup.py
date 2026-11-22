"""Incremental V2.2 research: causal training feedback and prior isolated replay reuse."""
import json,sys,statistics
from pathlib import Path
from datetime import datetime,timezone
from collections import defaultdict
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.run_evolution_v22 import ask,regime,CUT
from scripts.run_evolution import DATA,EXPECTED_DATA,sha,budget_status,run_job,analysis,write_json
from scripts.evolution_core import acceptance
from scripts.evolution_v22_followup import c_exposure,group_metrics
from decimal import Decimal
PREV=ROOT/'docs/reports/abc-evolution-v22/run-20261028T074014818337Z'
def pct(values,p):
 x=sorted(values);return x[round((len(x)-1)*p)] if x else None
def main():
 if sha(DATA)!=EXPECTED_DATA or Decimal(budget_status()['unreserved_usd'])<=0:raise RuntimeError('DATA_OR_BUDGET_INVALID')
 path=ROOT/'docs/reports/abc-evolution-v22-followup'/('run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'));path.mkdir(parents=True);print('RUN',path,flush=True)
 bars=json.loads(DATA.read_text(encoding='utf8'));indices={b['open_at']:i for i,b in enumerate(bars)};evidence=json.loads((PREV/'edge-analysis.json').read_text(encoding='utf8'))
 train=json.loads((PREV/'candidate-train/results-train-v30.json').read_text(encoding='utf8'))
 for a in 'ABC':
  tr=[t for t in train['trades'] if t['agent']==a and t['exit_bar_close']<CUT];by={k:defaultdict(list) for k in ('setup','direction','regime','exit_reason')}
  for t in tr:
   i=indices.get(t['entry_time']);r=regime(bars,i) if i is not None else None
   for k,v in {'setup':t['setup'],'direction':t['direction'],'regime':r or 'unknown','exit_reason':t['exit_reason']}.items():by[k][v].append(t)
  groups={k:{v:group_metrics(rows) for v,rows in d.items()} for k,d in by.items()}
  entry_stats=analysis(train,PREV/'candidate-train/ledger-train-v30.sqlite3',a)
  prior_rule=json.loads((PREV/'candidates'/f'{a}-rule.json').read_text(encoding='utf8'))
  losses=sorted([t for t in tr if Decimal(t['net_pnl'])<0],key=lambda t:Decimal(t['net_pnl']));wins=sorted([t for t in tr if Decimal(t['net_pnl'])>0],key=lambda t:Decimal(t['net_pnl']))
  extra={'latest_prior_rule':prior_rule,'latest_prior_training_analysis':entry_stats,'latest_prior_training_groups':groups,'latest_prior_training_closed':len(tr),'typical_closed_examples':{'median_winner':wins[len(wins)//2] if wins else None,'median_loser':losses[len(losses)//2] if losses else None},'previous_cutoff_eligible_validation':evidence[a]['previous_completed_validation'],'failure_context':'prior candidate not promoted; training statistics are not validation; original v21 earlier validation available before cutoff','temporal_exposure':'Entire historical period previously viewed. Post-cutoff prior validation metrics intentionally excluded from model input.'}
  if a=='C':
   items=[c_exposure(t) for t in tr];extra['c_exposure']={'count':len(items),'notional_mean':statistics.mean(x['notional'] for x in items) if items else None,'notional_median':pct([x['notional'] for x in items],.5),'notional_p90':pct([x['notional'] for x in items],.9),'effective_leverage_mean':statistics.mean(x['effective_leverage'] for x in items) if items else None,'effective_leverage_median':pct([x['effective_leverage'] for x in items],.5),'effective_leverage_max':max((x['effective_leverage'] for x in items),default=None),'initial_margin_mean':statistics.mean(x['initial_margin'] for x in items) if items else None,'maintenance_at_entry_mean':statistics.mean(x['maintenance_requirement_entry'] for x in items) if items else None,'fees_and_slippage':entry_stats['costs'],'definition':'notional=actual quantity*actual fill; effective leverage=notional/pre-entry cash; initial margin=notional/5; maintenance proxy=notional*1% at entry, not liquidation price'}
  evidence[a]['previous_rule']=prior_rule;evidence[a]['current_research_feedback']=extra
  write_json(path/f'{a}-analysis.json',evidence[a]);print('ANALYSIS',a,len(tr),flush=True)
 rules={};changes={}
 for a in 'ABC':
  print('MODEL',a,flush=True);rules[a],changes[a]=ask(path,a,evidence[a],bars);write_json(path/'candidates'/f'{a}-rule.json',rules[a])
 w={'start':'2023-10-17','train_end':'2025-06-17','validation_end':'2025-10-17'};none={a:None for a in 'ABC'}
 trainnew=run_job(path/'candidate-train','train-v30',w,'ABC',none,rules)
 write_json(path/'freeze.json',{'cutoff':CUT,'changes':changes,'rule_hashes':{a:sha(path/'candidates'/f'{a}-rule.json') for a in 'ABC'},'train_hash':sha(path/'candidate-train/results-train-v30.json'),'source_hash':EXPECTED_DATA,'at_utc':datetime.now(timezone.utc).isoformat()})
 candidate=run_job(path/'candidate-validation','validation-v30',w,'ABC',none,rules)
 baseline=json.loads((PREV/'baseline-validation/results-validation-v30.json').read_text(encoding='utf8'));parent=json.loads((PREV/'candidate-validation/results-validation-v30.json').read_text(encoding='utf8'))
 comp={}
 for a in 'ABC':
  b=analysis(baseline,PREV/'baseline-validation/ledger-validation-v30.sqlite3',a);p=analysis(parent,PREV/'candidate-validation/ledger-validation-v30.sqlite3',a);c=analysis(candidate,path/'candidate-validation/ledger-validation-v30.sqlite3',a);ok,why=acceptance(b['summary'],c['summary'],40);comp[a]={'baseline':b,'parent':p,'candidate':c,'promoted':ok,'reason':why,'mechanism':changes[a]}
 write_json(path/'comparison.json',comp);print('COMPLETE',json.dumps({a:{'reason':comp[a]['reason'],'candidate':comp[a]['candidate']['summary']} for a in 'ABC'},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
