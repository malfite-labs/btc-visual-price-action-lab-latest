"""Validated causal candle-rule interpreter. No eval, exec or live interfaces."""
from decimal import Decimal as D
import operator
OPS={'>':operator.gt,'<':operator.lt,'>=':operator.ge,'<=':operator.le}
KINDS={'open','high','low','close','rolling_high','rolling_low','body_ratio','upper_wick_ratio','lower_wick_ratio','trend_pct','range_pct'}
def validate(rule):
 if not isinstance(rule,dict) or set(rule)!={'long','short','stop_bars','target_r','max_hold'}: raise ValueError('RULE_SCHEMA')
 if type(rule['stop_bars']) is not int or not 2<=rule['stop_bars']<=30:raise ValueError('STOP_RANGE')
 if type(rule['max_hold']) is not int or not 1<=rule['max_hold']<=12:raise ValueError('HOLD_RANGE')
 if type(rule['target_r']) not in (int,float) or not .5<=rule['target_r']<=5:raise ValueError('TARGET_RANGE')
 for side in ('long','short'):
  if not isinstance(rule[side],list) or not 1<=len(rule[side])<=8:raise ValueError('CONDITION_COUNT')
  for cond in rule[side]:
   if not isinstance(cond,dict) or set(cond)!={'left','op','right'} or cond['op'] not in OPS:raise ValueError('CONDITION_SCHEMA')
   if not isinstance(cond['left'],dict) and not isinstance(cond['right'],dict):raise ValueError('UNCONDITIONAL_CONSTANT')
   if cond['left']==cond['right']:raise ValueError('IDENTICAL_OPERANDS')
   for v in (cond['left'],cond['right']):
    if type(v) in (int,float):
     if not -1000000<=v<=1000000:raise ValueError('NUMBER_RANGE')
    elif isinstance(v,dict):
     if set(v)-{'kind','offset','period'} or v.get('kind') not in KINDS:raise ValueError('FEATURE_SCHEMA')
     off=v.get('offset',0);period=v.get('period',2)
     if type(off) is not int or not 0<=off<=96 or type(period) is not int or not 2<=period<=96:raise ValueError('FUTURE_OR_WINDOW')
    else:raise ValueError('OPERAND_SCHEMA')
 return rule
def value(spec,bars,i):
 if type(spec) in (int,float):return D(str(spec))
 j=i-spec.get('offset',0);kind=spec['kind'];period=spec.get('period',2)
 if j<0:raise IndexError()
 b=bars[j];n=lambda x,k:D(str(x[k]))
 if kind in ('open','high','low','close'):return n(b,kind)
 if kind in ('rolling_high','rolling_low','trend_pct','range_pct'):
  if j-period+1<0:raise IndexError()
  w=bars[j-period+1:j+1]
  if kind=='rolling_high':return max(n(x,'high') for x in w)
  if kind=='rolling_low':return min(n(x,'low') for x in w)
  if kind=='trend_pct':return (n(w[-1],'close')/n(w[0],'close')-1)*100
  return (max(n(x,'high') for x in w)-min(n(x,'low') for x in w))/n(w[-1],'close')*100
 h=n(b,'high');l=n(b,'low');o=n(b,'open');c=n(b,'close');r=h-l
 if r<=0:raise IndexError()
 if kind=='body_ratio':return abs(c-o)/r
 if kind=='upper_wick_ratio':return (h-max(o,c))/r
 return (min(o,c)-l)/r
def signal(rule,bars,i):
 try:
  matches=[side for side in ('long','short') if all(OPS[c['op']](value(c['left'],bars,i),value(c['right'],bars,i)) for c in rule[side])]
  if len(matches)!=1 or i+1<rule['stop_bars']:return None
  side=matches[0];w=bars[i-rule['stop_bars']+1:i+1];stop=(min(D(str(x['low'])) for x in w)-D('.01')) if side=='long' else (max(D(str(x['high'])) for x in w)+D('.01'))
  return {'direction':side.upper(),'stop':stop,'target':None,'setup':'AI_RULE_V2'}
 except (IndexError,ZeroDivisionError):return None
