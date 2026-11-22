import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.run_evolution import run_job,write_json,DATA,EXPECTED_DATA,sha
from scripts.evolution_v25 import fix_a
from scripts.rule_evolution_v2 import signal
from scripts.evolution_v24_semantics import describe_rule
p=Path('docs/reports/abc-evolution-v25/run-20261028T084916681422Z').resolve();prev=Path('docs/reports/abc-evolution-v24/run-20261028T084424148512Z').resolve();job=sys.argv[1]
if sha(DATA)!=EXPECTED_DATA:raise RuntimeError('DATA_CHANGED')
if job=='A':
 rule=fix_a(json.loads((prev/'candidates/A-rule.json').read_text(encoding='utf8')));write_json(p/'A-rule.json',rule);(p/'A-conditions-zh.md').write_text(describe_rule(rule)+'\n',encoding='utf8');bars=json.loads(DATA.read_text(encoding='utf8'));segment=[];examples=[]
 for b in bars:
  if segment and segment[-1]['close_at']!=b['open_at']:segment=[]
  segment.append(b);i=len(segment)-1
  if i>=287:
   s=signal(rule,segment,i)
   if s and len(examples)<6:
    examples.append({'signal_time':b['close_at'],'direction':s['direction'],'pre_break_level':str(max(float(x['high']) for x in segment[i-13:i-1]) if s['direction']=='LONG' else min(float(x['low']) for x in segment[i-13:i-1])),'previous_closed_break_bar':segment[i-1],'signal_bar':b,'stop':str(s['stop'])})
 write_json(p/'A-actual-signal-examples.json',examples);print('ACTUAL_SIGNALS_SHOWN',len(examples),flush=True)
 w={'start':'2023-10-17','train_end':'2025-06-17','validation_end':'2025-10-17'};run_job(p/'A-validation','validation-v30',w,'A',{'A':None},{'A':rule})
elif job in ('C','CB'):
 f=json.loads((p/'C-freeze.json').read_text(encoding='utf8'));rule=json.loads((p/'C-frozen-rule.json').read_text(encoding='utf8'))
 if sha(p/'C-frozen-rule.json')!=f['frozen_copy_sha256'] or sha(Path(f['source_rule']))!=f['source_sha256']:raise RuntimeError('C_FREEZE_MISMATCH')
 w=f['extended_window'];run_job(p/('C-extended' if job=='C' else 'C-baseline'),'train-v30',w,'C',{'C':None},{'C':rule} if job=='C' else None)
else:raise ValueError(job)
