"""Causal filter attribution and single-mechanism rule edits."""
from copy import deepcopy
from scripts.rule_evolution_v2 import validate, value, OPS

def apply_edit(rule,edit):
 validate(rule)
 if not isinstance(edit,dict) or set(edit)!={'long_index','short_index'}:raise ValueError('EDIT_SCHEMA')
 result=deepcopy(rule)
 for side in ('long','short'):
  index=edit[side+'_index']
  if type(index) is not int or len(result[side])<=1 or not 0<=index<len(result[side]):raise ValueError('EDIT_INDEX')
  result[side].pop(index)
 return validate(result)
def funnel(rule,bars,i):
 validate(rule);out={}
 for side in ('long','short'):
  passed=[]
  for c in rule[side]:
   try: passed.append(bool(OPS[c['op']](value(c['left'],bars,i),value(c['right'],bars,i))))
   except (IndexError,ZeroDivisionError):passed.append(False)
  running=True;seq=[]
  for p in passed:running=running and p;seq.append(int(running))
  out[side]={'failed':[int(not p) for p in passed],'sequential_remaining':seq,'all_pass':int(all(passed))}
 return out

def attribute(rule,bars,start,end):
 result={s:{'eligible_bars':0,'failed':[0]*len(rule[s]),'sequential_remaining':[0]*len(rule[s]),'all_pass':0} for s in ('long','short')};seg=[]
 def process(seg):
  for i in range(287,len(seg)-12):
   if not start<=seg[i]['close_at']<end:continue
   f=funnel(rule,seg,i)
   for side in ('long','short'):
    r=result[side];r['eligible_bars']+=1;r['all_pass']+=f[side]['all_pass']
    for j in range(len(r['failed'])):r['failed'][j]+=f[side]['failed'][j];r['sequential_remaining'][j]+=f[side]['sequential_remaining'][j]
 for b in bars:
  if seg and seg[-1]['close_at']!=b['open_at']:process(seg);seg=[]
  seg.append(b)
 if seg:process(seg)
 return result
