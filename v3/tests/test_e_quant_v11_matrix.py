"""Predeclared OHLC rule cases; these synthetic variants are not independent market samples."""
import copy
import pytest
from test_e_quant_v11 import range_window,e2_window
from backend.e_quant_v11 import detect_setups


def positive(kind,n):
    d=e2_window() if kind=='E2' else range_window()
    if kind=='E1':
        d[-2].update(open=99.8,high=100,low=99.4,close=99.5)
        d[-1].update(open=99.5,high=100.2,low=99.4,close=100)
    elif kind=='E3':
        d[-4].update(open=100,high=101.8,low=99.9,close=101.5)
        d[-3].update(open=101.5,high=101.6,low=100.4,close=100.6)
        d[-2].update(open=100.6,high=100.9,low=100.5,close=100.7)
        d[-1].update(open=100.7,high=101.3,low=100.6,close=101.2)
    elif kind=='E4':
        d[-4].update(open=100,high=100.1,low=98.2,close=98.5)
        d[-3].update(open=98.5,high=99.6,low=98.4,close=99.4)
        d[-2].update(open=99.4,high=99.5,low=99.1,close=99.2)
        d[-1].update(open=99.2,high=99.3,low=98.7,close=98.8)
    # Vary a pre-signal closed candle without changing the setup; all OHLC sequences differ.
    d[-40-n]['open']=100-(n+1)*.01
    return d


def has(result,kind):return any(x['setup']==kind for x in result['candidates'])


@pytest.mark.parametrize('kind',['E1','E2','E3','E4'])
@pytest.mark.parametrize('n',range(5))
def test_positive_rule_variants(kind,n):
    assert has(detect_setups(positive(kind,n)),kind)


@pytest.mark.parametrize('kind',['E1','E2','E3','E4'])
@pytest.mark.parametrize('n',range(5))
def test_no_confirmation_variants_are_not_candidates(kind,n):
    d=positive(kind,n);last=d[-1]
    if n==0:
        last['open']=last['close']
    elif kind=='E1':
        if n==1:
            d[-2].update(open=100,high=100.1,low=99.9,close=99.95);last.update(open=99.95,high=100.3,low=99.9,close=100.2)
        elif n==2:
            d[-2].update(open=99.8,high=100.1,low=99.4,close=99.9);last.update(open=99.9,high=100.2,low=99.8,close=100.1)
        elif n==3:last.update(open=99.5,high=99.7,low=99.45,close=99.6)
        else:last.update(open=99.2,high=99.5,low=99.1,close=99.4)
    elif kind=='E2':
        if n==1:last['low']=97.4
        elif n==2:last.update(open=98,high=98.3,low=97.9,close=98.2)
        elif n==3:
            for b in d[-8:]:b.update(open=100,high=100.2,low=99.8,close=100)
        else:last['open']=last['close']+.01;last['high']=max(last['high'],last['open'])
    elif kind=='E3':
        if n==1:
            d[-3].update(open=101.5,high=101.6,low=101.2,close=101.3);d[-2].update(open=101.3,high=101.5,low=101.2,close=101.4);last.update(open=101.4,high=101.6,low=101.35,close=101.5)
        elif n==2:d[-4].update(open=100,high=100.5,low=99.9,close=100.4)
        elif n==3:last.update(open=100.55,high=100.7,low=100.5,close=100.6)
        else:last['low']=99.4
    else:
        if n==1:
            d[-3].update(open=98.5,high=98.8,low=98.4,close=98.7);d[-2].update(open=98.7,high=98.8,low=98.5,close=98.6);last.update(open=98.6,high=98.7,low=98.3,close=98.4)
        elif n==2:d[-4].update(open=100,high=100.1,low=99.5,close=99.6)
        elif n==3:last.update(open=99.45,high=99.5,low=99.3,close=99.4)
        else:last['open']=last['close']-.01;last['low']=min(last['low'],last['open'])
    assert not has(detect_setups(d),kind)


@pytest.mark.parametrize('kind',['E1','E2','E3','E4'])
@pytest.mark.parametrize('n',range(3))
def test_missing_closed_bar_boundary_rejects_snapshot(kind,n):
    d=positive(kind,n);d[-1-n]['is_closed']=False
    with pytest.raises(ValueError):detect_setups(d)


@pytest.mark.parametrize('close,expected',[(99.65,True),(99.649,False)])
def test_e1_reclaim_eps_boundary(close,expected):
    d=positive('E1',0);d[-1].update(open=99.5,high=99.8,low=99.4,close=close)
    assert has(detect_setups(d),'E1') is expected


@pytest.mark.parametrize('close,expected',[(98.44,True),(98.439,False)])
def test_e2_recovery_fraction_boundary(close,expected):
    d=positive('E2',0);d[-1].update(open=98,high=98.6,low=97.9,close=close)
    assert has(detect_setups(d),'E2') is expected


@pytest.mark.parametrize('close,expected',[(100.651,True),(100.65,False)])
def test_e3_breakout_buffer_strict_boundary(close,expected):
    d=positive('E3',0);d[-1].update(open=100.6,high=100.8,low=100.55,close=close)
    assert has(detect_setups(d),'E3') is expected


@pytest.mark.parametrize('close,expected',[(99.349,True),(99.35,False)])
def test_e4_breakdown_buffer_strict_boundary(close,expected):
    d=positive('E4',0);d[-1].update(open=99.5,high=99.6,low=99.2,close=close)
    assert has(detect_setups(d),'E4') is expected
