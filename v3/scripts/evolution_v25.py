"""One-bar breakout retest: touch and close on favorable side of pre-break level."""
from copy import deepcopy
from scripts.rule_evolution_v2 import validate

def fix_a(original):
 r=deepcopy(validate(original))
 for side,field,kind,touch,reclaim in [('long','low','rolling_high','<=','>'),('short','high','rolling_low','>=','<')]:
  old={'left':{'kind':field,'offset':0},'op':'>=' if side=='long' else '<=','right':{'kind':kind,'offset':1,'period':12}}
  if old not in r[side]:raise ValueError('EXPECTED_INCORRECT_REFERENCE_NOT_FOUND')
  idx=r[side].index(old);level={'kind':kind,'offset':2,'period':12}
  r[side][idx]={'left':{'kind':field,'offset':0},'op':touch,'right':level}
  r[side].insert(idx+1,{'left':{'kind':'close','offset':0},'op':reclaim,'right':level})
 return validate(r)
