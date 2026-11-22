"""Bounded, offline ABC research evolution. Never imports or runs the live app."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.evolution_core import acceptance, choose_setup, walk_forward_windows, eligible_coverage
from scripts.evolution_ai import generate

SOURCE=ROOT/'docs/reports/abc-v30'
STORE=ROOT/'docs/reports/abc-evolution-v1'
BUDGET=ROOT/'docs/reports/abc-replay-v1/round1-budget.sqlite3'
DATA=SOURCE/'historical-bars.json'
EXPECTED_DATA='adfc5318d995b690778d484ab21bac768a645980d76c48f66531b8b9f03f506c'

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def write_json(path,value):
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf8');tmp.replace(path)
def boundary(day): return day+'T00:00:00+00:00'
def safe_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',value):raise ValueError('invalid run id')
    return value

def budget_status():
    if not BUDGET.is_file():raise RuntimeError('Independent research budget database missing')
    with sqlite3.connect(BUDGET.resolve().as_uri()+'?mode=ro',uri=True) as db:
        row=db.execute('select total_limit_usd from vision_budget_scope where id=1').fetchone()
        if row is None:raise RuntimeError('Independent budget cap missing')
        cap=Decimal(row[0]);rows=db.execute('select reserved_usd,actual_cost_usd from vision_budget_calls').fetchall()
    reserved=sum((Decimal(r[0]) for r in rows),Decimal(0));known=sum((Decimal(r[1]) for r in rows if r[1] is not None),Decimal(0));unknown=sum((Decimal(r[0]) for r in rows if r[1] is None),Decimal(0))
    return {'cap_usd':str(cap),'reserved_usd':str(reserved),'unreserved_usd':str(max(Decimal(0),cap-reserved)),'known_peak_usage_estimate_usd':str(known),'unknown_usage_reserve_usd':str(unknown),'calls':len(rows)}

def analysis(result,ledger_path,agent):
    trades=[t for t in result['trades'] if t['agent']==agent]; groups={k:defaultdict(list) for k in ('setup','direction','exit_reason','hold','month','regime')}
    bars=json.loads(DATA.read_text(encoding='utf8')); indices={b['close_at']:i for i,b in enumerate(bars)}
    for t in trades:
        i=indices[t['signal_time']];w=bars[i-19:i+1];cl=[Decimal(x['close']) for x in w];movement=sum((abs(y-x) for x,y in zip(cl,cl[1:])),Decimal(0));er=abs(cl[-1]-cl[0])/movement if movement else Decimal(0)
        cats={'setup':t['setup'],'direction':t['direction'],'exit_reason':t['exit_reason'],'hold':str(t['bars_held']),'month':t['exit_bar_close'][:7],'regime':'trend' if er>=Decimal('.5') else 'mixed' if er>=Decimal('.25') else 'range'}
        for k,v in cats.items():groups[k][v].append(Decimal(t['net_pnl']))
    with sqlite3.connect(ledger_path.resolve().as_uri()+'?mode=ro',uri=True) as c:
        fee,slip=c.execute('select coalesce(sum(cast(fee as real)),0),coalesce(sum(cast(slippage as real)),0) from fills where agent=?',(agent,)).fetchone()
    def stats(vals):
        wins=sum((x for x in vals if x>0),Decimal(0));loss=-sum((x for x in vals if x<0),Decimal(0))
        return {'count':len(vals),'net_pnl':str(sum(vals,Decimal(0))),'mean_net':str(sum(vals,Decimal(0))/len(vals)) if vals else None,'win_rate':sum(v>0 for v in vals)/len(vals) if vals else None,'profit_factor':str(wins/loss) if loss else None}
    summary=result['summary'][agent]; n=len(trades); net=Decimal(summary['net_pnl']); extra=Decimal(str(fee))+Decimal(str(slip)); cut=result['cutoffs']
    begin=cut['window_start'] if result['comparison']=='train-v30' else cut['validation_start']
    end=cut['train_signal_end'] if result['comparison']=='train-v30' else cut['validation_signal_end']
    coverage=eligible_coverage(bars,begin,end)
    coverage['trades_per_valid_day']=n/coverage['valid_days'] if coverage['valid_days'] else None
    coverage['trades_per_96_bars']=n*96/coverage['eligible_bars'] if coverage['eligible_bars'] else None
    return {'summary':summary,'mean_net':str(net/n) if n else None,'coverage_and_frequency':coverage,'costs':{'fees':fee,'slippage':slip,'same_fill_gross_proxy':str(net+extra),'same_fill_150pct_cost_proxy':str(net-extra/2),'note':'arithmetic on same fills; not a repriced execution'},'groups':{k:{v:stats(vals) for v,vals in d.items()} for k,d in groups.items()}}

def validate_job(directory,mode,agents):
    result_path=directory/('results-'+mode+'.json');ledger=directory/('ledger-'+mode+'.sqlite3')
    if not result_path.exists() or not ledger.exists():return None
    result=json.loads(result_path.read_text(encoding='utf8'))
    if result['source_sha256']!=EXPECTED_DATA:raise RuntimeError('replay source mismatch')
    with sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro',uri=True) as c:
        if c.execute('pragma integrity_check').fetchone()[0]!='ok':raise RuntimeError('research ledger corrupt')
        for a in agents:
            trades=[x for x in result['trades'] if x['agent']==a]; rows=c.execute('select trade_id,position_effect from fills where agent=?',(a,)).fetchall()
            if len(rows)!=2*len(trades) or any(rows.count((x['trade_id'],'OPEN'))!=1 or rows.count((x['trade_id'],'CLOSE'))!=1 for x in trades):raise RuntimeError('unpaired research trade')
            if any(Decimal(t['preflight']['planned_loss'])>Decimal(t['initial_cash'])*Decimal('.02') for t in trades):raise RuntimeError('risk bound violated')
            if sum((Decimal(t['net_pnl']) for t in trades),Decimal(0))!=Decimal(result['summary'][a]['net_pnl']):raise RuntimeError('pnl mismatch')
    return result

def run_job(directory, mode, window, agents, filters, rules=None):
    directory.mkdir(parents=True,exist_ok=True); existing=validate_job(directory,mode,agents)
    if existing is not None:return existing
    if (directory/('ledger-'+mode+'.sqlite3')).exists():raise RuntimeError('Interrupted replay has a partial ledger; preserve it and start a new run id')
    src=(SOURCE/'run_v30.py').read_text(encoding='utf8');shutil.copyfile(SOURCE/'signals.py',directory/'signals.py')
    src=src.replace('ROOT=HERE.parents[2]', 'ROOT=Path('+repr(str(ROOT))+')')
    src=src.replace("DATA=HERE/'historical-bars.json'", 'DATA=Path('+repr(str(DATA))+')')
    old="for a,fn in [('A',v3_a),('B',v3_b),('C',v3_c)]:"
    new='for a,fn in '+repr([(a,'v3_'+a.lower()) for a in agents])+':'
    new=new.replace("'v3_a'",'v3_a').replace("'v3_b'",'v3_b').replace("'v3_c'",'v3_c')
    assert old in src;src=src.replace(old,new)
    old="sig=fn(seg,i) if i>=287 else None; ident="
    replacement="sig=fn(seg,i) if i>=287 else None\n        if sig is not None and "+repr(filters)+".get(a) is not None and sig['setup'] != "+repr(filters)+".get(a): sig=None\n        ident="
    assert old in src;src=src.replace(old,replacement)
    old="strategy_version='RULE_DRIVEN_'+a+'_V30'"
    new="strategy_version='RULE_DRIVEN_'+a+'_V30'+('_SETUP_'+str("+repr(filters)+".get(a)) if "+repr(filters)+".get(a) else '')"
    assert old in src;src=src.replace(old,new)
    start=boundary(window['start']);end=boundary(window['validation_end']);split=boundary(window['train_end'])
    train_limit=(datetime.fromisoformat(split)-timedelta(hours=3)).isoformat();val_limit=(datetime.fromisoformat(end)-timedelta(hours=3)).isoformat()
    old="if mode=='train-v30' and b['close_at']>='2025-02-28T21:00:00+00:00':order['status']='AFTER_TRAINING_CUTOFF'\n            elif mode.startswith('validation-') and b['close_at']<'2025-03-01T00:00:00+00:00':order['status']='TRAIN_PERIOD_EXCLUDED'"
    new="if mode=='train-v30' and not ("+repr(start)+" <= b['close_at'] < "+repr(train_limit)+"):order['status']='OUTSIDE_RESEARCH_WINDOW'\n            elif mode.startswith('validation-') and not ("+repr(split)+" <= b['close_at'] < "+repr(val_limit)+"):order['status']='OUTSIDE_VALIDATION_WINDOW'"
    assert old in src;src=src.replace(old,new)
    old="'cutoffs':{'train_signal_end':'2025-02-28T21:00:00+00:00','validation_start':'2025-03-01T00:00:00+00:00'}"
    new="'cutoffs':"+repr({'window_start':start,'train_signal_end':train_limit,'validation_start':split,'validation_signal_end':val_limit,'validation_end_exclusive':end})
    assert old in src;src=src.replace(old,new)
    # The original runner initializes A/B/C safely in an isolated ledger. Limit
    # its reporting loop to agents actually selected; no live process is touched.
    src=src.replace("    for a in 'ABC':\n        ts=[t for t in trades", "    for a in "+repr(agents)+":\n        ts=[t for t in trades")
    if rules:
        from scripts.rule_evolution_v2 import validate
        for rule in rules.values(): validate(rule)
        src=src.replace('from signals import signal_a as v3_a', 'from scripts.rule_evolution_v2 import signal as ai_signal\nfrom signals import signal_a as v3_a')
        src=src.replace('sig=fn(seg,i) if i>=287 else None', 'sig=(ai_signal('+repr(rules)+'[a],seg,i) if a in '+repr(rules)+' else fn(seg,i)) if i>=287 else None')
        src=src.replace("(D('2') if a=='C' else D('1.5'))", "(D(str("+repr({a:r['target_r'] for a,r in rules.items()})+"[a])) if a in "+repr(rules)+" else (D('2') if a=='C' else D('1.5')))")
        src=src.replace("pos['bars_held']>=12", "pos['bars_held']>=("+repr({a:r['max_hold'] for a,r in rules.items()})+".get(a,12))")
        src=src.replace("'TIME_12_BARS'","'TIME_EXIT_BARS'")
        original_assumption="all signals require 288 consecutive closed bars; A/B target 1.5R; C target 2R and 5x maximum with 1% maintenance; time exit 12 bars"
        revised_assumption="288 closed bars; AI rules specify target R and max hold; C 5x maximum with 1% maintenance; rules="+json.dumps(rules,sort_keys=True)
        if original_assumption not in src: raise RuntimeError('WORKER_ASSUMPTION_ANCHOR_MISSING')
        src=src.replace(repr(original_assumption),repr(revised_assumption))
        src=src.replace("strategy_version='RULE_DRIVEN_'+a+'_V30'","strategy_version='RULE_DRIVEN_'+a+'_AI_V2'")
    worker=directory/'worker.py';worker.write_text(src,encoding='utf8');
    print('REPLAY',directory.name,mode,flush=True)
    result=subprocess.run([sys.executable,str(worker),mode],cwd=ROOT,text=True,capture_output=True,encoding='utf8',errors='replace')
    (directory/'worker.log').write_text(result.stdout+'\n'+result.stderr,encoding='utf8')
    if result.returncode:raise RuntimeError('Research replay failed; see '+str(directory/'worker.log'))
    validated=validate_job(directory,mode,agents)
    if validated is None:raise RuntimeError('Replay output missing')
    return validated

def execute(run_path, manifest):
    agents=manifest['agents']; current=manifest.get('current_filters',{a:None for a in agents})
    for index,window in enumerate(manifest['windows'],1):
        rounddir=run_path/('round-'+str(index));rounddir.mkdir(exist_ok=True);meta_path=rounddir/'round.json'
        meta=json.loads(meta_path.read_text(encoding='utf8')) if meta_path.exists() else {'window':window,'baseline_filters':current.copy(),'status':'STARTED'}
        if meta.get('status')=='COMPLETE':
            current=meta['next_filters'];manifest['current_filters']=current;manifest['completed_rounds']=max(index,manifest.get('completed_rounds',0));write_json(run_path/'manifest.json',manifest);continue
        base_filters=meta['baseline_filters'];write_json(meta_path,meta)
        base_train=run_job(rounddir/'baseline-train','train-v30',window,agents,base_filters)
        train_ledger=rounddir/'baseline-train'/'ledger-train-v30.sqlite3'
        if 'proposals' not in meta:
            meta['analysis']={a:analysis(base_train,train_ledger,a) for a in agents}
            if manifest.get('candidate_method')=='DEEPSEEK_SETUP_SELECTION':
                bars=json.loads(DATA.read_text(encoding='utf8'));proposed={}
                for a in agents:
                    print('AI_PROPOSAL',index,a,flush=True)
                    proposed[a]=generate(ROOT,run_path,a,index,meta['analysis'][a],base_filters.get(a),bars,boundary(window['train_end']))
                meta['proposal_basis']='DeepSeek analysis of training evidence selects one existing V3 setup; no arbitrary model-generated code'
            else:
                proposed={a:choose_setup(base_train['trades'],a,40) for a in agents}
                meta['proposal_basis']='deterministic training-set setup grouping, not AI'
            proposed={a:(v if v!=base_filters.get(a) else None) for a,v in proposed.items()}
            meta['proposals']=proposed;write_json(meta_path,meta)
            candidate_dir=rounddir/'candidates';candidate_dir.mkdir(exist_ok=True)
            for a in agents:
                proposal={'agent':a,'round':index,'parent_setup_filter':base_filters.get(a),'proposed_setup_filter':proposed[a],'source':meta['proposal_basis'],'risk_cost_and_exit':'unchanged V3; isolated research only','generation_reason':meta['proposal_basis'],'training_setup_stats':meta['analysis'][a]['groups']['setup']}
                write_json(candidate_dir/(a+'.json'),proposal)
        cand_filters={a:(meta['proposals'][a] or base_filters.get(a)) for a in agents}
        candidate_train=run_job(rounddir/'candidate-train','train-v30',window,agents,cand_filters)
        # Freeze proposals and training evidence before invoking either validation.
        if 'freeze' not in meta:
            meta['candidate_train']={a:analysis(candidate_train,rounddir/'candidate-train'/'ledger-train-v30.sqlite3',a) for a in agents};meta['freeze']={'at_utc':datetime.now(timezone.utc).isoformat(),'proposals':meta['proposals'],'baseline_train_sha256':sha(rounddir/'baseline-train'/'results-train-v30.json'),'candidate_train_sha256':sha(rounddir/'candidate-train'/'results-train-v30.json')};write_json(meta_path,meta)
        else:
            if meta['proposals']!=meta['freeze']['proposals'] or sha(rounddir/'baseline-train'/'results-train-v30.json')!=meta['freeze']['baseline_train_sha256'] or sha(rounddir/'candidate-train'/'results-train-v30.json')!=meta['freeze']['candidate_train_sha256']:raise RuntimeError('frozen training evidence changed')
        base_val=run_job(rounddir/'baseline-validation','validation-v30',window,agents,base_filters)
        cand_val=run_job(rounddir/'candidate-validation','validation-v30',window,agents,cand_filters)
        next_filters=base_filters.copy();out={}
        for a in agents:
            b=base_val['summary'][a];v=cand_val['summary'][a];ok,reason=acceptance(b,v,40)
            if not meta['proposals'][a]:ok=False;reason='NO_DISTINCT_PROPOSAL'
            if ok:next_filters[a]=meta['proposals'][a]
            out[a]={'baseline':analysis(base_val,rounddir/'baseline-validation'/'ledger-validation-v30.sqlite3',a),'candidate':analysis(cand_val,rounddir/'candidate-validation'/'ledger-validation-v30.sqlite3',a),'promoted':ok,'reason':reason,'next_filter':next_filters[a]}
        meta.update({'comparison':out,'next_filters':next_filters,'status':'COMPLETE','finished_utc':datetime.now(timezone.utc).isoformat(),'validation_status':'PREVIOUSLY_VIEWED_INTERNAL'});write_json(meta_path,meta);current=next_filters
        manifest['current_filters']=current;manifest['completed_rounds']=index;write_json(run_path/'manifest.json',manifest)
        print('ROUND',index,'COMPLETE',json.dumps({a:{'proposal':meta['proposals'][a],'promoted':out[a]['promoted'],'reason':out[a]['reason']} for a in agents},ensure_ascii=False),flush=True)
    make_report(run_path,manifest)

def make_report(path,manifest):
    lines=['# ABC automatic research evolution','', 'RULE_DRIVEN executions only. Candidate method: '+manifest['candidate_method']+'. All historical data was previously viewed; comparisons are internal. No formal deployment, live scheduler or real orders.','',f"Run model budget cap: {manifest['model_budget_usd']} USD. AI request records are in each candidates directory.",'', '| Round | Agent | Proposal | Baseline n / net / PF / DD | Candidate n / net / PF / DD | Selection |','|---:|---|---|---|---|---|']
    for idx in range(1,manifest.get('completed_rounds',0)+1):
        r=json.loads((path/f'round-{idx}'/'round.json').read_text(encoding='utf8'))
        for a,x in r['comparison'].items():
            def fmt(obj):
                s=obj['summary'];return f"{s['closed_trades']} / {s['net_pnl']} / {s['profit_factor']} / {s['max_drawdown_percent']}%"
            lines.append(f"| {idx} | {a} | {r['proposals'][a]} | {fmt(x['baseline'])} | {fmt(x['candidate'])} | {x['reason']} |")
    lines+=['','| Round | Agent | Baseline trades / valid day | Candidate trades / valid day | Eligible 15m bars |','|---:|---|---:|---:|---:|']
    for idx in range(1,manifest.get('completed_rounds',0)+1):
        r=json.loads((path/f'round-{idx}'/'round.json').read_text(encoding='utf8'))
        for a,x in r['comparison'].items():
            b=x['baseline'].get('coverage_and_frequency');v=x['candidate'].get('coverage_and_frequency')
            if b and v:lines.append(f"| {idx} | {a} | {b['trades_per_valid_day']:.3f} | {v['trades_per_valid_day']:.3f} | {v['eligible_bars']} |")
    lines+=['','Promotion requires >=40 candidate trades, higher total net, mean net gain >= max($1, 10% of absolute baseline mean), profit factor improvement >=0.05, and no larger conservative drawdown. These research thresholds are not proof of a stable edge.','', 'Per-agent setup, direction, exits, holding, month, causal regime and same-fill cost sensitivity are recorded in each round.json. Individual rules, logs and SQLite trade ledgers are preserved in each job folder. Profit factor and return include fees and adverse slippage. Drawdown uses conservative OHLC bar extremes, possibly after exits; missing data periods are censored by availability only.','', 'AI selects among existing executable setups only, not arbitrary novel code. Each research run requires an authorized active session.']
    (path/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf8')

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--agents',default='ABC');parser.add_argument('--rounds',type=int,default=3);parser.add_argument('--budget',type=Decimal,default=Decimal(0),help='maximum model spend for this run; model calls currently disabled');parser.add_argument('--start',default='2023-10-17');parser.add_argument('--end',default='2025-10-17',help='exclusive UTC date');parser.add_argument('--ai',action='store_true');parser.add_argument('--resume');parser.add_argument('--latest',action='store_true');args=parser.parse_args()
    STORE.mkdir(parents=True,exist_ok=True)
    if args.latest:
        runs=sorted((p for p in STORE.iterdir() if p.is_dir() and (p/'manifest.json').exists()),key=lambda p:p.stat().st_mtime,reverse=True)
        if not runs:raise SystemExit('No research run found')
        report=runs[0]/'REPORT.md'
        print(report.read_text(encoding='utf8') if report.exists() else (runs[0]/'manifest.json').read_text(encoding='utf8'));return
    if sha(DATA)!=EXPECTED_DATA:raise RuntimeError('Frozen dataset hash mismatch')
    v3freeze=json.loads((SOURCE/'prevalidation-freeze.json').read_text(encoding='utf8'))
    for name in ['signals.py','run_v30.py']:
        if sha(SOURCE/name)!=v3freeze['files'][name]:raise RuntimeError('V3 source changed since freeze')
    budget=budget_status()
    if not args.budget.is_finite() or args.budget<0 or args.budget>Decimal(budget['unreserved_usd']):raise ValueError('run model cap exceeds available independent budget')
    if args.resume:
        path=STORE/safe_id(args.resume);manifest=json.loads((path/'manifest.json').read_text(encoding='utf8'))
        if manifest['data_sha256']!=EXPECTED_DATA:raise RuntimeError('resume data mismatch')
        if manifest.get('completed_rounds',0)<len(manifest['windows']) and any(sha(ROOT/name)!=digest for name,digest in manifest['engine_hashes'].items()):raise RuntimeError('resume engine or risk code changed; preserve this run')
    else:
        agents=''.join(a for a in 'ABC' if a in args.agents.upper())
        if not agents or set(args.agents.upper())-set('ABC') or len(set(args.agents.upper()))!=len(args.agents):raise ValueError('agents must be unique letters A, B, C')
        windows=walk_forward_windows(args.start,args.end,args.rounds);run_id='run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');path=STORE/run_id;path.mkdir()
        engine_files=['scripts/run_evolution.py','scripts/evolution_core.py','scripts/evolution_ai.py','backend/domain.py','backend/execution.py','backend/ledger.py']
        manifest={'run_id':run_id,'agents':agents,'windows':windows,'data_sha256':EXPECTED_DATA,'source_hashes':{n:sha(SOURCE/n) for n in ['signals.py','run_v30.py']},'engine_hashes':{n:sha(ROOT/n) for n in engine_files},'model_budget_usd':str(args.budget),'model_calls':0,'budget_at_start':budget,'candidate_method':'DEEPSEEK_SETUP_SELECTION' if args.ai else 'DETERMINISTIC_SETUP_FILTER_NOT_AI','validation_status':'PREVIOUSLY_VIEWED_INTERNAL','completed_rounds':0,'current_filters':{a:None for a in agents}}
        write_json(path/'manifest.json',manifest)
    if manifest.get('candidate_method')=='DEEPSEEK_SETUP_SELECTION' and Decimal(manifest['model_budget_usd'])<=0: raise ValueError('AI requires a positive run budget')
    print('RESEARCH_RUN',path,flush=True);execute(path,manifest)
    print('REPORT',path/'REPORT.md',flush=True)

if __name__=='__main__':main()
