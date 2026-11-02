import pytest
from scripts.rule_evolution_v2 import validate, signal

def rule():
 return {'long':[{'left':{'kind':'close','offset':0},'op':'>','right':{'kind':'rolling_high','period':5,'offset':1}}], 'short':[{'left':{'kind':'close','offset':0},'op':'<','right':{'kind':'rolling_low','period':5,'offset':1}}], 'stop_bars':4,'target_r':1.5,'max_hold':12}
def test_causal_breakout_and_stop():
 bars=[{'open':'10','high':'11','low':'9','close':'10'} for _ in range(9)]+[{'open':'10','high':'13','low':'9.5','close':'12'}]
 assert signal(rule(),bars,9)['direction']=='LONG'
 assert signal(rule(),bars,8) is None
 assert signal(rule(),bars+[{'open':'1','high':'1000','low':'.1','close':'900'}],9)==signal(rule(),bars,9)
def test_reject_invalid_or_future_data():
 x=rule(); x['long'][0]['right']['offset']=-1
 with pytest.raises(ValueError):validate(x)
 x=rule();x['long'][0]['op']='__import__'
 with pytest.raises(ValueError):validate(x)
 x=rule();x['max_hold']=13
 with pytest.raises(ValueError):validate(x)

def test_prior_candidate_feedback_is_included(tmp_path):
 from scripts.run_evolution_v2 import add_previous_feedback
 import json
 r=tmp_path/'round-1';r.mkdir();(r/'comparison.json').write_text(json.dumps({'A':{'candidate':{'summary':{'net_pnl':'-7'}},'reason':'INSUFFICIENT_TRADES'}}))
 (r/'candidates').mkdir();(r/'candidates'/'A-rule.json').write_text(json.dumps(rule()))
 result=add_previous_feedback(tmp_path,2,'A',{'summary':{}})
 assert result['previous_candidate']['result']['summary']['net_pnl']=='-7'
 assert result['previous_candidate']['rule']==rule()
 assert 'previous_candidate' not in add_previous_feedback(tmp_path,1,'A',{})

def test_reject_unconditional_entry():
 x=rule();x['long']=[{'left':1,'op':'>','right':0}]
 with pytest.raises(ValueError):validate(x)
