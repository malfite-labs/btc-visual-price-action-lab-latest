from datetime import datetime, timedelta, timezone
from backend.d_runtime import DRuntime
from backend.d_ledger import DLedger


class Archive:
    def __init__(self, bar): self.bar = bar
    def read(self, start, end):
        return [self.bar] if start <= datetime.fromisoformat(self.bar['open_at']) < end else []


def fixture(now):
    close = now.replace(minute=now.minute//15*15, second=0, microsecond=0)
    signal = {'signal_close':close.isoformat(),'direction':'LONG','setup':'D_CONFIRM',
              'atr':'2','low':'98','neckline':'100','stop':'98.9','close':'100'}
    bar = {'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','symbol':'BTC/USD',
           'interval':'15Min','is_closed':True,'open_at':(close-timedelta(minutes=15)).isoformat(),
           'close_at':close.isoformat(),'close':'100','volume':'4000'}
    return signal, bar


def quote(at, event_id, bid, ask):
    return {'symbol':'BTC/USD','source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH',
            'timestamp_valid':True,'event_at':at.isoformat(),'received_at':at.isoformat(),
            'bid':str(bid),'ask':str(ask),'event_id':event_id}



def activate(rt, close):
    t=close-timedelta(seconds=2)
    assert rt.confirm_stream_recovered(quote(t,'validation',100,100.01),t,market_ready=True,candles_complete=True)

def test_isolated_entry_exit_restart_and_duplicate(tmp_path, monkeypatch):
    from backend import d_signal
    now=datetime.now(timezone.utc).replace(second=0,microsecond=0)
    signal,bar=fixture(now)
    def candidate(bars,state):
        return {'status':'PENDING','reason':'D_CONFIRM','state':{'phase':'PENDING','last_close':signal['signal_close'],'signal':signal},'signal':signal}
    monkeypatch.setattr(d_signal,'evaluate',candidate)
    archive=Archive(bar)
    obs,ledger=tmp_path/'observation.sqlite',tmp_path/'ledger.sqlite'
    rt=DRuntime(obs,ledger,archive)
    close=datetime.fromisoformat(signal['signal_close'])
    activate(rt,close)
    pre=close-timedelta(seconds=1)
    assert rt.on_quote(quote(pre,'pre',99.9,99.91),pre,market_ready=True)['status']=='IDLE'
    decision_time=close+timedelta(seconds=1)
    assert rt.on_bar(close,decision_time,market_ready=True)['status']=='PENDING'
    now=close+timedelta(seconds=2)
    q=quote(now,'first',100.08,100.10)
    result=rt.on_quote(q,now,market_ready=True)
    assert result['status']=='FILLED',result
    assert rt.ledger.account()['state']=='OPEN_LONG'
    assert rt.status()['state']['phase']=='LONG'
    rt.close()
    rt=DRuntime(obs,ledger,archive)
    assert rt.ledger.account()['state']=='OPEN_LONG'
    # Both take-profit thresholds hit on a single new valid quote.
    exit_time=now+timedelta(seconds=1)
    high=quote(exit_time,'exit',110,110.01)
    result=rt.on_quote(high,exit_time,market_ready=True)
    assert result['status']=='COMPLETE',result
    assert rt.ledger.account()['state']=='FLAT'
    assert [x['reason'] for x in rt.ledger.fills()]==['D_ENTRY','TP1','TP2']
    assert rt.status()['state']['phase']=='COOLDOWN'
    rt.close()
    rt=DRuntime(obs,ledger,archive)
    assert rt.status()['state']['phase']=='COOLDOWN'
    assert rt.on_quote(high,exit_time,market_ready=True)['status'] in ('IDLE','BLOCKED')
    assert len(rt.ledger.fills())==3


def test_market_gate_does_not_disable_position_protection(tmp_path):
    from backend.d_executor import enqueue_verified_quote
    now=datetime.now(timezone.utc).replace(microsecond=0)
    obs,ledger=tmp_path/'observation.sqlite',tmp_path/'ledger.sqlite'
    rt=DRuntime(obs,ledger,Archive({}))
    first=quote(now,'entry',100,100.01)
    assert enqueue_verified_quote(rt.ledger,first,now)['accepted']
    close=(now.replace(minute=now.minute//15*15,second=0)+timedelta(minutes=15)).isoformat()
    assert rt.ledger.open_position(quantity='1',price='100',stop='90',leverage='5',idempotency_key='entry',expected_version=0,signal_id='s',atr='5',entry_bar_close=close,quote_event_id='entry')['accepted']
    assert rt.ledger.complete_quote('entry')['accepted']
    t=now+timedelta(seconds=1)
    result=rt.on_quote(quote(t,'stop',89,89.01),t,market_ready=False)
    assert result['status']=='COMPLETE',result
    assert rt.ledger.account()['state']=='FLAT'


def test_first_quote_after_decision_used_instead_of_predecision_quote(tmp_path, monkeypatch):
    from backend import d_signal
    now=datetime.now(timezone.utc).replace(second=0,microsecond=0)
    signal,bar=fixture(now)
    def candidate(bars,state):
        return {'status':'PENDING','reason':'D_CONFIRM','state':{'phase':'PENDING','last_close':signal['signal_close'],'signal':signal},'signal':signal}
    monkeypatch.setattr(d_signal,'evaluate',candidate)
    rt=DRuntime(tmp_path/'obs.sqlite',tmp_path/'ledger.sqlite',Archive(bar))
    close=datetime.fromisoformat(signal['signal_close'])
    activate(rt,close)
    pre=close-timedelta(seconds=1)
    assert rt.on_quote(quote(pre,'pre',99.9,99.91),pre,market_ready=True)['status']=='IDLE'
    first=close+timedelta(seconds=1)
    assert rt.on_quote(quote(first,'before-decision',100.08,100.10),first,market_ready=True)['status']=='IDLE'
    assert rt.on_bar(close,first+timedelta(seconds=1),market_ready=True)['status']=='PENDING'
    later=first+timedelta(seconds=2)
    result=rt.on_quote(quote(later,'later',100.08,100.10),later,market_ready=True)
    assert result['status']=='FILLED',result
    assert rt.ledger.account()['state']=='OPEN_LONG'
    assert len(rt.ledger.fills())==1


def test_disconnect_is_durable_and_recovery_requires_full_validation(tmp_path):
    now=datetime.now(timezone.utc).replace(microsecond=0)
    obs,ledger=tmp_path/'obs.sqlite',tmp_path/'ledger.sqlite'
    rt=DRuntime(obs,ledger,Archive({}))
    rt.mark_stream_unavailable('D_EVENT_QUEUE_OVERFLOW')
    assert rt.status()['protection']=='UNCERTAIN'
    rt.close()
    rt=DRuntime(obs,ledger,Archive({}))
    assert rt.status()['protection']=='UNCERTAIN'
    q=quote(now,'q',100,100.01)
    assert not rt.confirm_stream_recovered(q,now,market_ready=True,candles_complete=False)
    assert not rt.confirm_stream_recovered(q,now+timedelta(minutes=2),market_ready=True,candles_complete=True)
    assert rt.confirm_stream_recovered(q,now,market_ready=True,candles_complete=True)
    assert rt.status()['protection']=='READY'


def test_restart_expires_unfilled_candidate_without_later_order(tmp_path, monkeypatch):
    from backend import d_signal
    now=datetime.now(timezone.utc).replace(second=0,microsecond=0)
    signal,bar=fixture(now)
    def candidate(bars,state):
        return {'status':'PENDING','reason':'D_CONFIRM','state':{'phase':'PENDING','last_close':signal['signal_close'],'signal':signal},'signal':signal}
    monkeypatch.setattr(d_signal,'evaluate',candidate)
    archive=Archive(bar);obs=tmp_path/'obs.sqlite';ledger=tmp_path/'ledger.sqlite'
    close=datetime.fromisoformat(signal['signal_close'])
    rt=DRuntime(obs,ledger,archive)
    assert rt.on_bar(close,close+timedelta(seconds=1),market_ready=True)['status']=='PENDING'
    rt.close()
    rt=DRuntime(obs,ledger,archive)
    assert rt.status()['state']['phase']=='COOLDOWN'
    t=close+timedelta(seconds=2)
    assert rt.on_quote(quote(t,'late',100.08,100.10),t,market_ready=True)['status']=='BLOCKED'
    assert rt.ledger.fills()==[]


def test_stale_event_after_decision_is_not_used_for_entry(tmp_path, monkeypatch):
    from backend import d_signal
    now=datetime.now(timezone.utc).replace(second=0,microsecond=0)
    signal,bar=fixture(now)
    def candidate(bars,state):
        return {'status':'PENDING','reason':'D_CONFIRM','state':{'phase':'PENDING','last_close':signal['signal_close'],'signal':signal},'signal':signal}
    monkeypatch.setattr(d_signal,'evaluate',candidate)
    rt=DRuntime(tmp_path/'obs.sqlite',tmp_path/'ledger.sqlite',Archive(bar))
    close=datetime.fromisoformat(signal['signal_close'])
    activate(rt,close)
    decision_time=close+timedelta(seconds=4)
    assert rt.on_bar(close,decision_time,market_ready=True)['status']=='PENDING'
    old_time=close+timedelta(seconds=2)
    old_quote=quote(old_time,'old',100.08,100.10)
    old_quote['received_at']=(close+timedelta(seconds=5)).isoformat()
    assert rt.on_quote(old_quote,close+timedelta(seconds=5),market_ready=True)['reason']=='QUOTE_BEFORE_CONFIRMATION'
    fresh=close+timedelta(seconds=6)
    assert rt.on_quote(quote(fresh,'fresh',100.08,100.10),fresh,market_ready=True)['status']=='FILLED'
    assert len(rt.ledger.fills())==1


def test_committed_entry_with_failed_protection_never_reports_success(tmp_path,monkeypatch):
    from backend import d_signal, d_executor
    now=datetime.now(timezone.utc).replace(second=0,microsecond=0)
    signal,bar=fixture(now)
    def candidate(bars,state):
        return {'status':'PENDING','reason':'D_CONFIRM','state':{'phase':'PENDING','last_close':signal['signal_close'],'signal':signal},'signal':signal}
    monkeypatch.setattr(d_signal,'evaluate',candidate)
    monkeypatch.setattr(d_executor,'process_verified_quote',lambda ledger,at:{'status':'BLOCKED','reason':'PROTECTION_ERROR'})
    obs,ledger=tmp_path/'obs.sqlite',tmp_path/'ledger.sqlite'
    rt=DRuntime(obs,ledger,Archive(bar));close=datetime.fromisoformat(signal['signal_close'])
    activate(rt,close)
    assert rt.on_bar(close,close+timedelta(seconds=1),market_ready=True)['status']=='PENDING'
    t=close+timedelta(seconds=2)
    result=rt.on_quote(quote(t,'q',100.08,100.10),t,market_ready=True)
    assert result['status']=='BLOCKED' and result['reason']=='OPEN_PROTECTION_UNCERTAIN'
    assert rt.ledger.account()['state']=='OPEN_LONG'
    rt.close()
    assert DRuntime(obs,ledger,Archive(bar)).status()['protection']=='UNCERTAIN'


def test_restart_drains_old_entry_quote_before_new_stop(tmp_path):
    from backend.d_executor import enqueue_verified_quote
    now=datetime.now(timezone.utc).replace(microsecond=0)
    obs,ledger=tmp_path/'observation.sqlite',tmp_path/'ledger.sqlite'
    rt=DRuntime(obs,ledger,Archive({}))
    first=quote(now,'entry',100,100.01)
    assert enqueue_verified_quote(rt.ledger,first,now)['accepted']
    close=(now.replace(minute=now.minute//15*15,second=0)+timedelta(minutes=15)).isoformat()
    assert rt.ledger.open_position(quantity='1',price='100',stop='90',leverage='5',idempotency_key='entry',expected_version=0,signal_id='s',atr='5',entry_bar_close=close,quote_event_id='entry')['accepted']
    # Crash after open commit but before entry quote completion.
    rt.close()
    rt=DRuntime(obs,ledger,Archive({}))
    assert rt.status()['protection']=='UNCERTAIN'
    t=now+timedelta(seconds=1)
    result=rt.on_quote(quote(t,'stop',89,89.01),t,market_ready=True)
    assert result['status']=='COMPLETE',result
    assert rt.ledger.account()['state']=='FLAT'
    assert rt.ledger.fills()[-1]['reason']=='STOP_DUE'
    assert rt.ledger.pending_quote() is None


def test_open_position_must_consume_quote_before_stream_recovery_can_clear_protection(tmp_path):
    from backend.d_executor import enqueue_verified_quote
    now=datetime.now(timezone.utc).replace(microsecond=0)
    obs,ledger=tmp_path/'obs.sqlite',tmp_path/'ledger.sqlite'
    rt=DRuntime(obs,ledger,Archive({}))
    q=quote(now,'entry',100,100.01)
    assert enqueue_verified_quote(rt.ledger,q,now)['accepted']
    close=(now.replace(minute=now.minute//15*15,second=0)+timedelta(minutes=15)).isoformat()
    assert rt.ledger.open_position(quantity='1',price='100',stop='90',leverage='5',idempotency_key='entry',expected_version=0,signal_id='s',atr='5',entry_bar_close=close,quote_event_id='entry')['accepted']
    assert rt.ledger.complete_quote('entry')['accepted']
    rt.close()
    rt=DRuntime(obs,ledger,Archive({}))
    t=now+timedelta(seconds=1);stop_quote=quote(t,'stop',89,89.01)
    assert not rt.confirm_stream_recovered(stop_quote,t,market_ready=True,candles_complete=True)
    assert rt.status()['protection']=='UNCERTAIN'
    assert rt.on_quote(stop_quote,t,market_ready=True)['status']=='COMPLETE'
    assert rt.ledger.account()['state']=='FLAT'
    assert rt.status()['protection']=='UNCERTAIN'


def test_stop_quote_persisted_before_observation_failure_and_recovered(tmp_path, monkeypatch):
    import pytest
    from backend.d_executor import enqueue_verified_quote
    now=datetime.now(timezone.utc).replace(microsecond=0)
    obs,ledger=tmp_path/'obs.sqlite',tmp_path/'ledger.sqlite'
    rt=DRuntime(obs,ledger,Archive({}))
    entry=quote(now,'entry',100,100.01)
    assert enqueue_verified_quote(rt.ledger,entry,now)['accepted']
    close=(now.replace(minute=now.minute//15*15,second=0)+timedelta(minutes=15)).isoformat()
    assert rt.ledger.open_position(quantity='1',price='100',stop='90',leverage='5',idempotency_key='entry',expected_version=0,signal_id='s',atr='5',entry_bar_close=close,quote_event_id='entry')['accepted']
    assert rt.ledger.complete_quote('entry')['accepted']
    def crash(*args): raise RuntimeError('simulated observation write failure')
    monkeypatch.setattr(rt,'_record_quote',crash)
    trigger=now+timedelta(seconds=1)
    with pytest.raises(RuntimeError):
        rt.on_quote(quote(trigger,'stop-at-89',89,89.01),trigger,market_ready=True)
    assert rt.ledger.pending_quote()['event_id']=='stop-at-89'
    assert rt.ledger.account()['state']=='OPEN_LONG'
    rt.close()
    rt=DRuntime(obs,ledger,Archive({}))
    later=trigger+timedelta(seconds=61)
    result=rt.on_quote(quote(later,'rebound-at-95',95,95.01),later,market_ready=True)
    assert result['status']=='COMPLETE',result
    assert rt.ledger.account()['state']=='FLAT'
    assert rt.ledger.fills()[-1]['reason']=='STOP_DUE'
    assert float(rt.ledger.fills()[-1]['price'])>90
    assert len(rt.ledger.fills())==2
    rt.close()


def test_pending_intent_reconciled_when_open_already_committed(tmp_path):
    from backend.d_executor import enqueue_verified_quote
    now=datetime.now(timezone.utc).replace(microsecond=0)
    obs,ledger=tmp_path/'obs.sqlite',tmp_path/'ledger.sqlite'
    rt=DRuntime(obs,ledger,Archive({}))
    close=now.replace(minute=now.minute//15*15,second=0)
    signal={'signal_close':close.isoformat(),'direction':'LONG','setup':'D_CONFIRM'}
    entry=quote(now,'entry',100,100.01)
    assert rt.intents.capture(signal,entry,now)['accepted']
    assert enqueue_verified_quote(rt.ledger,entry,now)['accepted']
    end=(close+timedelta(minutes=15)).isoformat()
    assert rt.ledger.open_position(quantity='1',price='100',stop='90',leverage='5',idempotency_key='entry',expected_version=0,signal_id=signal['signal_close'],atr='5',entry_bar_close=end,quote_event_id='entry')['accepted']
    assert len(rt.intents.pending())==1
    rt.close()
    rt=DRuntime(obs,ledger,Archive({}))
    assert rt.ledger.account()['state']=='OPEN_LONG'
    assert rt.intents.pending()==[]
    assert rt.status()['state']['phase']=='LONG'
    rt.close()


def test_runtime_lease_excludes_duplicate_instances_until_release(tmp_path):
    import pytest
    obs,ledger=tmp_path/'obs.sqlite',tmp_path/'ledger.sqlite'
    rt=DRuntime(obs,ledger,Archive({}))
    with pytest.raises(RuntimeError,match='D_RUNTIME_LEASE_UNAVAILABLE'):
        DRuntime(obs,ledger,Archive({}))
    rt.close()
    other=DRuntime(obs,ledger,Archive({}))
    assert other.status()['protection']=='UNCERTAIN'
    other.close()


def test_invalid_quote_makes_open_position_protection_uncertain_across_restart(tmp_path):
    from backend.d_executor import enqueue_verified_quote
    now=datetime.now(timezone.utc).replace(microsecond=0)
    obs,ledger=tmp_path/'obs.sqlite',tmp_path/'ledger.sqlite'
    rt=DRuntime(obs,ledger,Archive({}))
    entry=quote(now,'entry',100,100.01)
    assert enqueue_verified_quote(rt.ledger,entry,now)['accepted']
    end=(now.replace(minute=now.minute//15*15,second=0)+timedelta(minutes=15)).isoformat()
    assert rt.ledger.open_position(quantity='1',price='100',stop='90',leverage='5',idempotency_key='entry',expected_version=0,signal_id='s',atr='5',entry_bar_close=end,quote_event_id='entry')['accepted']
    assert rt.ledger.complete_quote('entry')['accepted']
    first=now+timedelta(seconds=1)
    assert rt.on_quote(quote(first,'fresh',100,100.01),first,market_ready=True)['status']=='COMPLETE'
    assert rt.status()['protection']=='READY'
    invalid={**quote(first+timedelta(seconds=1),'invalid',89,89.01),'timestamp_valid':False}
    assert rt.on_quote(invalid,first+timedelta(seconds=1),market_ready=True)['reason']=='INVALID_QUOTE'
    assert rt.status()['protection']=='UNCERTAIN'
    rt.close()
    rt=DRuntime(obs,ledger,Archive({}))
    assert rt.status()['protection']=='UNCERTAIN'
    final=first+timedelta(seconds=2)
    assert rt.on_quote(quote(final,'valid-stop',89,89.01),final,market_ready=True)['status']=='COMPLETE'
    assert rt.ledger.account()['state']=='FLAT'
    assert rt.status()['protection']=='UNCERTAIN'
    rt.close()


def test_runtime_lease_cross_process_released_on_process_exit(tmp_path):
    import subprocess,sys,pytest
    obs,ledger=tmp_path/'obs.sqlite',tmp_path/'ledger.sqlite'
    program = """import sys
from backend.d_runtime import DRuntime
class A:
 def read(self,start,end): return []
r=DRuntime(sys.argv[1],sys.argv[2],A())
print('READY',flush=True)
sys.stdin.readline()
r.close()
"""
    proc=subprocess.Popen([sys.executable,'-c',program,str(obs),str(ledger)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert proc.stdout.readline().strip()=='READY'
        with pytest.raises(RuntimeError,match='D_RUNTIME_LEASE_UNAVAILABLE'):
            DRuntime(obs,ledger,Archive({}))
        proc.stdin.write('stop\n');proc.stdin.flush()
        assert proc.wait(timeout=10)==0
        rt=DRuntime(obs,ledger,Archive({}))
        assert rt.status()['protection']=='UNCERTAIN'
        rt.close()
    finally:
        if proc.poll() is None:
            proc.kill();proc.wait(timeout=10)
        proc.stdin.close();proc.stdout.close();proc.stderr.close()


def test_failed_first_attempt_cannot_open_on_later_quote(tmp_path,monkeypatch):
    from backend import d_signal, d_runtime
    now=datetime.now(timezone.utc).replace(second=0,microsecond=0)
    signal,bar=fixture(now)
    def candidate(bars,state):
        return {'status':'PENDING','reason':'D_CONFIRM','state':{'phase':'PENDING','last_close':signal['signal_close'],'signal':signal},'signal':signal}
    monkeypatch.setattr(d_signal,'evaluate',candidate)
    rt=DRuntime(tmp_path/'obs.sqlite',tmp_path/'ledger.sqlite',Archive(bar))
    close=datetime.fromisoformat(signal['signal_close']);activate(rt,close)
    assert rt.on_bar(close,close+timedelta(seconds=1),market_ready=True)['status']=='PENDING'
    original=d_runtime.handoff_observed_intent
    monkeypatch.setattr(d_runtime,'handoff_observed_intent',lambda *args: (_ for _ in ()).throw(ValueError('transient')))
    first=close+timedelta(seconds=2)
    assert rt.on_quote(quote(first,'first',100.08,100.10),first,market_ready=True)['status']=='BLOCKED'
    monkeypatch.setattr(d_runtime,'handoff_observed_intent',original)
    later=close+timedelta(seconds=3)
    result=rt.on_quote(quote(later,'later',100.08,100.10),later,market_ready=True)
    assert result=={'status':'REJECTED','reason':'FIRST_ATTEMPT_INCOMPLETE'}
    assert rt.ledger.fills()==[]
    assert rt.status()['state']['phase']=='COOLDOWN'
    rt.close()
