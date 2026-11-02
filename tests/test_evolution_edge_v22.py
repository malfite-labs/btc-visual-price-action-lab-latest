from scripts.evolution_edge_v22 import excursions, one_mechanism
import pytest

def test_excursion_no_gap_and_no_leak():
 bars=[{'open_at':str(i),'close_at':str(i+1),'open':'100','high':str(101+i),'low':str(99-i),'close':'100'} for i in range(5)]
 x=excursions(bars,0,'LONG',4)
 assert x['mfe_pct']==4 and x['mae_pct']==4
 bars[2]['open_at']='gap'
 assert excursions(bars,0,'LONG',4) is None

def test_one_mechanism_rejects_multi():
 b={'long':[1],'short':[2],'stop_bars':4,'target_r':2,'max_hold':12}
 a={**b,'stop_bars':5};assert one_mechanism(b,a)=='stop_bars'
 with pytest.raises(ValueError):one_mechanism(b,{**b,'stop_bars':5,'max_hold':6})
