from decimal import Decimal
from scripts.evolution_core import choose_setup, acceptance, walk_forward_windows, eligible_coverage

def test_candidate_selection_uses_only_training_set_and_minimum_sample():
    trades=[{'agent':'A','setup':'X','net_pnl':'-2'}]*40+[{'agent':'A','setup':'Y','net_pnl':'1'}]*39
    assert choose_setup(trades,'A',40)=='X'
    trades.append({'agent':'A','setup':'Y','net_pnl':'1'})
    assert choose_setup(trades,'A',40)=='Y'

def test_no_promotion_from_higher_trade_count_or_small_sample():
    base={'closed_trades':100,'net_pnl':'-100','profit_factor':'0.8','max_drawdown_percent':'10'}
    noisy={'closed_trades':10,'net_pnl':'100','profit_factor':'2','max_drawdown_percent':'1'}
    assert not acceptance(base,noisy,40)[0]
    poor={'closed_trades':200,'net_pnl':'-200','profit_factor':'0.7','max_drawdown_percent':'12'}
    assert not acceptance(base,poor,40)[0]

def test_small_statistical_improvement_is_not_meaningful():
    base={'closed_trades':100,'net_pnl':'-1000','profit_factor':'0.5','max_drawdown_percent':'20'}
    slight={'closed_trades':90,'net_pnl':'-890','profit_factor':'0.51','max_drawdown_percent':'19'}
    assert not acceptance(base,slight,40)[0]

def test_walk_forward_intervals_are_disjoint():
    windows=walk_forward_windows('2023-10-17','2025-10-17',3)
    assert len(windows)==3
    for w in windows: assert w['start']<w['train_end']<w['validation_end']
    assert all(a['validation_end']==b['train_end'] for a,b in zip(windows,windows[1:]))

def test_frequency_uses_continuous_bars_and_partial_utc_days():
    from datetime import datetime, timedelta, timezone
    start=datetime(2025,1,1,tzinfo=timezone.utc)
    bars=[{'open_at':(start+timedelta(minutes=15*i)).isoformat(),'close_at':(start+timedelta(minutes=15*(i+1))).isoformat()} for i in range(310)]
    coverage=eligible_coverage(bars,bars[0]['open_at'],bars[-1]['close_at'])
    assert coverage['eligible_bars']==11 and coverage['valid_days']==1
