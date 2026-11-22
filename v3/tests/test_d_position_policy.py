from datetime import datetime, timedelta, timezone
from backend.d_position_policy import plan_position_event

UTC=timezone.utc
BASE=datetime(2026,11,5,12,0,tzinfo=UTC)

def pos(**kw):
    return {'direction':'LONG','entry_price':'100','stop_price':'98','tp1':'102','tp2':'104',
            'initial_quantity':'1','quantity':'1','entry_bar_close':(BASE+timedelta(minutes=15)).isoformat(),
            'tp1_filled':False,'pending_stop':None, **kw}

def decide(p,bid,minute=1,seconds=0):
    at=BASE+timedelta(minutes=minute,seconds=seconds)
    q={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
       'bid':str(bid),'ask':str(float(bid)+.01),'event_at':at.isoformat()}
    return plan_position_event(p,q,at)

def test_stop_first_and_no_same_bar_breakeven():
    p=pos(tp1_filled=True,quantity='.5',pending_stop='100',pending_stop_after=(BASE+timedelta(minutes=15)).isoformat())
    assert decide(p,99,minute=10)['action']=='HOLD'
    assert decide(p,99,minute=15)['action']=='UPDATE_STOP'
    assert decide(pos(stop_price='100'),99,minute=15)['action']=='STOP'

def test_one_quote_above_both_targets_must_process_tp1_first():
    first=decide(pos(),105)
    assert first['action']=='TP1' and first['quantity']=='0.5'
    assert decide(pos(tp1_filled=True,quantity='.5'),105)['action']=='TP2'

def test_time_limit_and_stop_have_priority_over_target():
    assert decide(pos(),100,minute=119)['action']=='HOLD'
    assert decide(pos(),103,minute=120)['reason']=='NO_TP1_8_BARS'
    assert decide(pos(),97,minute=120)['action']=='STOP'
    assert decide(pos(tp1_filled=True,quantity='.5'),100,minute=240)['reason']=='MAX_HOLD_16_BARS'

def test_unfresh_quote_never_triggers_exit():
    at=BASE+timedelta(minutes=10)
    q={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
       'bid':'90','ask':'91','event_at':BASE.isoformat()}
    assert plan_position_event(pos(),q,at)['reason']=='QUOTE_STALE'

