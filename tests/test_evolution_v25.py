from scripts.evolution_v25 import fix_a
from scripts.rule_evolution_v2 import signal

def test_breakout_retest_uses_pre_break_extreme():
 r={'long':[{'left':{'kind':'close','offset':1},'op':'>','right':{'kind':'rolling_high','offset':2,'period':12}},{'left':{'kind':'low','offset':0},'op':'>=','right':{'kind':'rolling_high','offset':1,'period':12}}], 'short':[{'left':{'kind':'close','offset':1},'op':'<','right':{'kind':'rolling_low','offset':2,'period':12}},{'left':{'kind':'high','offset':0},'op':'<=','right':{'kind':'rolling_low','offset':1,'period':12}}], 'stop_bars':6,'target_r':1.8,'max_hold':8}
 r=fix_a(r)
 bars=[{'open':'98','high':'100','low':'95','close':'99'} for _ in range(12)]+[{'open':'99','high':'103','low':'98','close':'102'},{'open':'101','high':'104','low':'99','close':'103'}]
 assert signal(r,bars,13)['direction']=='LONG'
 bars[-1]['low']='101'
 assert signal(r,bars,13) is None
