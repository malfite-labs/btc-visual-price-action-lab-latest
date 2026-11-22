import pytest
from scripts.evolution_v21 import apply_edit, funnel
from tests.test_rule_evolution_v2 import rule

def test_only_one_filter_pair_can_be_removed():
 r=rule();r['long'].append({'left':{'kind':'body_ratio'},'op':'>','right':.5});r['short'].append({'left':{'kind':'body_ratio'},'op':'>','right':.5})
 out=apply_edit(r,{'long_index':1,'short_index':1})
 assert len(out['long'])==len(out['short'])==1 and out['target_r']==r['target_r']
 with pytest.raises(ValueError):apply_edit(r,{'long_index':0,'short_index':4})

def test_funnel_counts_individual_and_sequential_filters():
 r=rule();bars=[{'open':'10','high':'11','low':'9','close':'10'} for _ in range(10)]
 stats=funnel(r,bars,9)
 assert stats['long']['failed']==[1] and stats['long']['sequential_remaining']==[0]
