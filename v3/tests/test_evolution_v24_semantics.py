from scripts.evolution_v24_semantics import describe_rule, v2_a_rule

def test_description_from_predicates_never_asserts_double_test():
 rule=v2_a_rule(); text=describe_rule(rule)
 assert '此前20根' in text and '收盘价' in text and '双次失败' not in text
 assert '止损' in text and '12根' in text

def test_v2_a_conversion():
 r=v2_a_rule()
 assert r['long'][0]['right']=={'kind':'rolling_high','period':20,'offset':1}
 assert r['stop_bars']==5 and r['target_r']==1.5

def test_retest_claim_requires_same_reference():
 from scripts.evolution_v24_semantics import claim_warnings
 text='Retest the broken level after the breakout.'
 r={'long':[{'left':{'kind':'close','offset':1},'op':'>','right':{'kind':'rolling_high','offset':2,'period':12}}, {'left':{'kind':'low','offset':0},'op':'>=','right':{'kind':'rolling_high','offset':1,'period':12}}], 'short':[{'left':{'kind':'high','offset':0},'op':'<=','right':{'kind':'rolling_low','offset':1,'period':12}}], 'stop_bars':6,'target_r':1.8,'max_hold':8}
 assert 'RETEST_REFERENCE_UNPROVEN' in claim_warnings(text,r)
