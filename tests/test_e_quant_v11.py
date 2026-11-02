import pytest
from test_e_visual import bars
from backend.e_quant_v11 import detect_setups


def e2_window(scale=1.0):
    data=bars()
    for b in data:
        b.update(open=100*scale,high=100.2*scale,low=99.8*scale,close=100*scale)
    closes=[99.6,99.2,98.8,98.4,97.8,97.9,98.0,99.2]
    previous=100
    for b,close in zip(data[-8:],closes):
        op=previous; b.update(open=op*scale,high=(max(op,close)+.2)*scale,low=(min(op,close)-.2)*scale,close=close*scale);previous=close
    return data


@pytest.mark.parametrize('scale',[.5,.8,1,1.2,2])
def test_e2_five_independent_scale_positive_cases(scale):
    result=detect_setups(e2_window(scale))
    assert any(x['setup']=='E2' and x['side']=='LONG' for x in result['candidates'])


def test_e2_unconfirmed_falling_market_does_not_trigger():
    window=e2_window(); window[-1].update(open=98.2,high=98.3,low=97.4,close=97.5)
    assert not any(x['setup']=='E2' for x in detect_setups(window)['candidates'])


def range_window():
    data=bars()
    for b in data: b.update(open=100,high=100.5,low=99.5,close=100)
    return data


def test_e1_reclaim_is_detected():
    data=range_window()
    data[-2].update(open=99.8,high=100,low=99.4,close=99.5)
    data[-1].update(open=99.5,high=100.2,low=99.4,close=100)
    assert any(x['setup']=='E1' for x in detect_setups(data)['candidates'])


def test_e3_break_retest_confirm_is_detected():
    data=range_window()
    data[-4].update(open=100,high=101.8,low=99.9,close=101.5)
    data[-3].update(open=101.5,high=101.6,low=100.4,close=100.6)
    data[-2].update(open=100.6,high=100.9,low=100.5,close=100.7)
    data[-1].update(open=100.7,high=101.3,low=100.6,close=101.2)
    assert any(x['setup']=='E3' for x in detect_setups(data)['candidates'])


def test_e4_break_retest_confirm_is_detected():
    data=range_window()
    data[-4].update(open=100,high=100.1,low=98.2,close=98.5)
    data[-3].update(open=98.5,high=99.6,low=98.4,close=99.4)
    data[-2].update(open=99.4,high=99.5,low=99.1,close=99.2)
    data[-1].update(open=99.2,high=99.3,low=98.7,close=98.8)
    assert any(x['setup']=='E4' for x in detect_setups(data)['candidates'])


def test_e3_without_prior_retest_does_not_signal():
    data=range_window()
    data[-3].update(open=100,high=101.8,low=99.9,close=101.5)
    data[-2].update(open=101.5,high=102.2,low=101.4,close=102.0)
    data[-1].update(open=102,high=102.5,low=101.8,close=102.4)
    result=detect_setups(data)
    assert not any(x['setup']=='E3' for x in result['candidates'])
    assert 'NOT_RETESTED' in result['diagnostics']


def test_detector_rejects_incomplete_snapshot():
    with pytest.raises(ValueError):detect_setups(range_window()[:-1])


def test_detector_is_deterministic_and_never_authorizes_order():
    data=e2_window()
    one=detect_setups(data);two=detect_setups(data)
    assert one==two
    assert one['order_eligible'] is False


def test_e3_uses_nearer_preexisting_swing_resistance_than_projection():
    data=range_window()
    data[100]['high']=101.3
    data[-4].update(open=100,high=101.8,low=99.9,close=101.5)
    data[-3].update(open=101.5,high=101.6,low=100.4,close=100.6)
    data[-2].update(open=100.6,high=100.9,low=100.5,close=100.7)
    data[-1].update(open=100.7,high=101.3,low=100.6,close=101.2)
    setup=next(x for x in detect_setups(data)['candidates'] if x['setup']=='E3')
    assert float(setup['reward'])==101.3


def test_result_carries_snapshot_identity_and_order_gate():
    result=detect_setups(e2_window())
    assert len(result['snapshot_sha256'])==64
    assert result['strategy_version']=='E-V1.1-QUANT'
    assert result['closed_bar_time']==e2_window()[-1]['close_at']
    assert result['order_eligible'] is False
