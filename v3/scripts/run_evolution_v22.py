"""V2.2 causal-data cutoff and isolated one-round edge research."""
import asyncio,json,os,sqlite3,sys,hashlib,statistics
from pathlib import Path
from collections import defaultdict
from decimal import Decimal as D
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.evolution_edge_v22 import excursions,one_mechanism
from scripts.run_evolution import DATA,EXPECTED_DATA,sha,budget_status,run_job,analysis,write_json
from scripts.evolution_core import acceptance
from scripts.evolution_ai import chart,reservation
from scripts.run_evolution_v2 import SCHEMA
from scripts.rule_evolution_v2 import validate
from backend.vision_provider import VisionBudget,ConfigurableVisionAdapter,VisionProviderConfig
CUT='2025-06-17T00:00:00+00:00'; BASE=ROOT/'docs/reports/abc-evolution-v21/run-20261028T071633501657Z'; V3=ROOT/'docs/reports/abc-v30'
H=(4,8,12,24)
def regime(bars,i):
 if i<20 or bars[i-20]['open_at'][:7]!=bars[i]['open_at'][:7]:return None
 if any(bars[j]['close_at']!=bars[j+1]['open_at'] for j in range(i-20,i)):return None
 delta=float(bars[i-1]['close'])/float(bars[i-20]['close'])-1
 return 'up' if delta>.002 else 'down' if delta<-.002 else 'flat'
def stats(trades,fee_map,summary):
 net=sum(float(t['net_pnl']) for t in trades);fees=sum(fee_map.get(t['trade_id'],(0,0))[0] for t in trades);slip=sum(fee_map.get(t['trade_id'],(0,0))[1] for t in trades)
 win=[float(t['net_pnl']) for t in trades if float(t['net_pnl'])>0];loss=[float(t['net_pnl']) for t in trades if float(t['net_pnl'])<0]
 groups={}
 for key in ('setup','exit_reason','direction'):
  groups[key]={}
  for t in trades:
   label=t[key];g=groups[key].setdefault(label,{'count':0,'net_pnl':0});g['count']+=1;g['net_pnl']+=float(t['net_pnl'])
 return {'count':len(trades),'net_pnl':round(net,2),'fees':round(fees,2),'slippage':round(slip,2),'same_fill_gross_proxy':round(net+fees+slip,2),'win_rate':len(win)/len(trades) if trades else None,'average_win':statistics.mean(win) if win else None,'average_loss':statistics.mean(loss) if loss else None,'avg_win_loss_ratio':statistics.mean(win)/abs(statistics.mean(loss)) if win and loss else None,'profit_factor':sum(win)/abs(sum(loss)) if loss else None,'max_drawdown_percent':summary['max_drawdown_percent'],'groups':groups}
def load(path,agent):
 result=json.loads((path/'results-train-v30.json').read_text(encoding='utf8'));trades=[t for t in result['trades'] if t['agent']==agent and t['exit_bar_close']<CUT]
 with sqlite3.connect((path/'ledger-train-v30.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as c:
  rows=c.execute('select trade_id,fee,slippage from fills where agent=?',(agent,)).fetchall()
 cost=defaultdict(lambda:[0.,0.])
 for tid,fee,slip in rows:cost[tid][0]+=float(fee);cost[tid][1]+=float(slip)
 return trades,stats(trades,cost,result['summary'][agent])
def build_edge(bars):
 byopen={b['open_at']:i for i,b in enumerate(bars)};baseline=defaultdict(list)
 for i in range(287,len(bars),8):
  if bars[i]['open_at']>=CUT:break
  if i+24>len(bars) or bars[i+23]['close_at']>=CUT:continue
  reg=regime(bars,i)
  if reg is None:continue
  for side in ('LONG','SHORT'):
   es=[excursions(bars,i,side,h) for h in H]
   if all(es):baseline[(bars[i]['open_at'][:7],reg,side)].append(es)
 print('BASELINE_GROUPS',len(baseline),flush=True);out={}
 for a in 'ABC':
  candidate,cs=load(BASE/'round-2/candidate-train',a);v3,vs=load(V3,a);alltr=[];pools=defaultdict(list);matched=defaultdict(list);typical={}
  for name,trades in [('v3',v3),('v21',candidate)]:
   typical[name]={}
   for t in trades:
    i=byopen.get(t['entry_time']);reg=regime(bars,i) if i is not None else None
    if i is None or i+24>len(bars) or bars[i+23]['close_at']>=CUT or reg is None:continue
    ex=[excursions(bars,i,t['direction'],h) for h in H]
    pool=baseline.get((t['entry_time'][:7],reg,t['direction']),[])
    if not all(ex):continue
    for n,h in enumerate(H):
     pools[(name,h)].append(ex[n])
     if pool:
      matched[(name,h)].append({'trade':ex[n],'baseline':{k:statistics.mean(x[n][k] for x in pool) for k in ('mfe_pct','mae_pct','close_return_pct')}})
   ordered=sorted(trades,key=lambda t:float(t['net_pnl']));typical[name]={'worst':ordered[:2],'best':ordered[-2:]}
  paths={}
  for (name,h),arr in pools.items():
   m=matched[(name,h)];keys=('mfe_pct','mae_pct','close_return_pct');paths[f'{name}_{h}']={'trade_count':len(arr),'trade_mean':{k:statistics.mean(x[k] for x in arr) for k in keys},'matched_trade_count':len(m),'matched_trade_mean':{k:statistics.mean(x['trade'][k] for x in m) for k in keys} if m else None,'baseline_mean':{k:statistics.mean(x['baseline'][k] for x in m) for k in keys} if m else None}
  prior_rule=json.loads((BASE/'round-2/candidates'/f'{a}-rule.json').read_text(encoding='utf8'))
  prior_validation=json.loads((BASE/'round-1/comparison.json').read_text(encoding='utf8'))[a]
  out[a]={'training_cutoff':CUT,'v3_stats':vs,'v21_stats':cs,'excursion_and_matched_benchmark':paths,'examples':typical,'previous_rule':prior_rule,'previous_completed_validation':prior_validation,'method':'post-entry OHLC path with open as common assumed midpoint; all horizons require 24 contiguous future bars fully before training cutoff. Control: every 8th bar from same UTC month, same prior 20-bar trend bucket and direction, trade-weighted control group means; not a causal counterfactual; no costs in path returns. Gross proxy adds actual fees and slippage back to realized net on same fills.'}
 return out
def ask(path,a,evidence,bars):
 target=path/'candidates'/f'{a}-model.json';target.parent.mkdir(exist_ok=True);rid=f'edge22:{path.name}:{a}'
 prompt=('Research simulated BTC 15m only. Analyse the actual edge, costs, typical winners and losers, entry shapes and exits; compare 4/8/12/24-bar post-entry paths with matched controls. The analysis is observational; do not claim causality. '
 'Return JSON analysis, reason, rule. Modify exactly ONE principal mechanism from previous_rule: either entry conditions (long/short together), stop_bars, target_r, OR max_hold, leaving other mechanisms identical. Retain '+{'A':'trend trading','B':'reversal','C':'Tony trend-following structural risk'}[a]+'. Do not force trading. '+SCHEMA+' Evidence: '+json.dumps(evidence,ensure_ascii=False,separators=(',',':')))
 image=chart(bars,CUT);(target.parent/f'{a}-chart.png').write_bytes(image)
 local=VisionBudget(str(path/'run-budget.sqlite3'));local.bind_total_limit(D('1'));amount=reservation(prompt)+D('.0024')
 if amount>D('.05'):raise RuntimeError('REQUEST_CAP_EXCEEDED')
 ok,why=local.reserve(rid,amount,D('1'),100,total_limit=D('1'))
 if not ok:raise RuntimeError(why)
 if not os.getenv('DEEPSEEK_API_KEY'):
  for line in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines():
   k,sep,v=line.partition('=')
   if sep and k.strip()=='DEEPSEEK_API_KEY':os.environ[k.strip()]=v.strip().strip('"\'')
 cfg=VisionProviderConfig(enabled=True,calls_authorized=True,model='deepseek-flash',thinking_mode='disabled',max_concurrency=1,max_output_tokens=3000,max_requests_per_day=1000,max_call_cost_usd=D('.05'),daily_budget_usd=D('5'),total_budget_usd=D('5'),input_price_per_million_usd=D('.30'),output_price_per_million_usd=D('1.20'),budget_db_path=str(ROOT/'docs/reports/abc-replay-v1/round1-budget.sqlite3'))
 result=asyncio.run(ConfigurableVisionAdapter(cfg)._send_image_request(image,prompt,rid));write_json(target,{'request_id':rid,'prompt':prompt,'image_sha256':hashlib.sha256(image).hexdigest(),'response':result,'reserved_usd':str(amount)});local.mark(rid,'RESPONSE_RECORDED',result.get('estimated_cost_usd'))
 try:
  rule=validate(result['rule']);change=one_mechanism(evidence['previous_rule'],rule)
  if not isinstance(result.get('analysis'),str) or not isinstance(result.get('reason'),str):raise ValueError('missing analysis')
  return rule,change
 except (ValueError,KeyError,TypeError) as e:raise RuntimeError('UNSUPPORTED_OR_INVALID_MODEL_RULE; response retained: '+str(e)) from e
def main():
 if sha(DATA)!=EXPECTED_DATA or D(budget_status()['unreserved_usd'])<=0:raise RuntimeError('DATA_OR_BUDGET_INVALID')
 path=ROOT/'docs/reports/abc-evolution-v22'/('run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'));path.mkdir(parents=True);bars=json.loads(DATA.read_text(encoding='utf8'));print('RUN',path,flush=True)
 evidence=build_edge(bars);write_json(path/'edge-analysis.json',evidence);print('EDGE_ANALYSIS_SAVED',flush=True)
 rules={};changes={}
 for a in 'ABC':
  print('DEEPSEEK',a,flush=True);rules[a],changes[a]=ask(path,a,evidence[a],bars);write_json(path/'candidates'/f'{a}-rule.json',rules[a])
 w={'start':'2023-10-17','train_end':'2025-06-17','validation_end':'2025-10-17'};none={a:None for a in 'ABC'};prev={a:evidence[a]['previous_rule'] for a in 'ABC'}
 train=run_job(path/'candidate-train','train-v30',w,'ABC',none,rules)
 write_json(path/'freeze.json',{'cutoff':CUT,'candidates':rules,'mechanism_changes':changes,'train_sha256':sha(path/'candidate-train'/'results-train-v30.json'),'data_sha256':EXPECTED_DATA,'at_utc':datetime.now(timezone.utc).isoformat()})
 baseline=run_job(path/'baseline-validation','validation-v30',w,'ABC',none)
 parent=run_job(path/'parent-validation','validation-v30',w,'ABC',none,prev)
 cand=run_job(path/'candidate-validation','validation-v30',w,'ABC',none,rules)
 comp={}
 for a in 'ABC':
  b=analysis(baseline,path/'baseline-validation'/'ledger-validation-v30.sqlite3',a);p=analysis(parent,path/'parent-validation'/'ledger-validation-v30.sqlite3',a);c=analysis(cand,path/'candidate-validation'/'ledger-validation-v30.sqlite3',a);ok,reason=acceptance(b['summary'],c['summary'],40);comp[a]={'baseline':b,'parent':p,'candidate':c,'promoted':ok,'reason':reason,'change':changes[a]}
 write_json(path/'comparison.json',comp);print('COMPLETE',json.dumps({a:{'reason':comp[a]['reason'],'candidate':comp[a]['candidate']['summary']} for a in 'ABC'},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
