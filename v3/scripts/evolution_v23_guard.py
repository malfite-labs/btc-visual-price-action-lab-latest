"""Conservative version keys and observable replay-behavior comparison."""
import hashlib,json
from decimal import Decimal

def fingerprint(rule):
 def norm(x):
  if isinstance(x,dict):return {k:norm(v) for k,v in sorted(x.items())}
  if isinstance(x,list):return sorted([norm(v) for v in x],key=lambda v:json.dumps(v,sort_keys=True,separators=(',',':')))
  if type(x) in (float,int):return {'$number':str(Decimal(str(x)).normalize())}
  return x
 return hashlib.sha256(json.dumps(norm(rule),sort_keys=True,separators=(',',':')).encode()).hexdigest()
def cache_eligible(a,b):
 keys=('rule','data','cost','window','engine','agent','mode')
 return all(k in a and k in b and a[k]==b[k] for k in keys)
def behavior_diff(parent,candidate,agent):
 def sig(r):return {(x['time'],x['direction']) for x in r['signals'] if x['agent']==agent and x.get('status') not in ('OUTSIDE_VALIDATION_WINDOW','OUTSIDE_RESEARCH_WINDOW','AFTER_TRAINING_CUTOFF','TRAIN_PERIOD_EXCLUDED')}
 def trade(r):return {(x['signal_time'],x['direction']):x for x in r['trades'] if x['agent']==agent}
 s1,s2=sig(parent),sig(candidate);t1,t2=trade(parent),trade(candidate);common=t1.keys()&t2.keys()
 entry=sum(t1[k]['entry_time']!=t2[k]['entry_time'] for k in common);stop=sum(Decimal(str(t1[k]['stop']))!=Decimal(str(t2[k]['stop'])) for k in common)
 ex=sum((t1[k]['exit_bar_close'],t1[k]['exit_reason'])!=(t2[k]['exit_bar_close'],t2[k]['exit_reason']) for k in common)
 d={'raw_signal_symdiff':len(s1^s2),'trade_set_symdiff':len(t1.keys()^t2.keys()),'matched_entries_changed':entry,'matched_stop_prices_changed':stop,'matched_exits_changed':ex,'parent_trades':len(t1),'candidate_trades':len(t2),'matched_trades':len(common)}
 d['classification']='EFFECTIVE' if any(d[k] for k in ('raw_signal_symdiff','trade_set_symdiff','matched_entries_changed','matched_stop_prices_changed','matched_exits_changed')) else 'NO_EFFECT'
 return d

def find_cache(root,key):
 from pathlib import Path
 for metadata in Path(root).rglob('v23-cache.json'):
  try:
   data=json.loads(metadata.read_text(encoding='utf8'))
   if not cache_eligible(data['key'],key):continue
   mode=key['mode'];result=metadata.parent/('results-'+mode+'.json');ledger=metadata.parent/('ledger-'+mode+'.sqlite3')
   if hashlib.sha256(result.read_bytes()).hexdigest()!=data['result_sha256'] or hashlib.sha256(ledger.read_bytes()).hexdigest()!=data['ledger_sha256']:continue
   return metadata.parent
  except (ValueError,KeyError,OSError,TypeError):continue
 return None
