from datetime import datetime, timedelta, timezone
from backend.d_circuit import DCircuit
from backend.d_circuit_replay import assess_from_committed_ledger, proven_flat_midnight_equity, replay_closed_trades
from backend.d_executor import enqueue_verified_quote
from backend.d_ledger import DLedger
from backend.d_runtime import DRuntime
import pytest
import sqlite3


def test_closed_trades_replay_after_cross_database_interruption(tmp_path):
    ledger = DLedger(tmp_path/'ledger.sqlite')
    circuit = DCircuit(str(ledger.path)+'.circuit.sqlite')
    now = datetime.now(timezone.utc).replace(microsecond=0)
    midnight = now.replace(hour=0,minute=0,second=0)
    circuit.record_day_start(midnight=midnight,equity='10000',provenance='D_LEDGER_FLAT_MIDNIGHT_V1')
    assert str(proven_flat_midnight_equity(ledger,now)[1]) == '10000.00'
    def event(at, ident, price):
        return {'symbol':'BTC/USD','source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH',
                'timestamp_valid':True,'event_at':at.isoformat(),'received_at':at.isoformat(),
                'bid':str(price),'ask':str(price),'event_id':ident}
    for i in range(3):
        at=now+timedelta(seconds=i*2)
        q=event(at,f'entry{i}',100)
        assert enqueue_verified_quote(ledger,q,at)['accepted']
        a=ledger.account()
        opened=ledger.open_position(quantity='1',price='100',stop='90',leverage='5',
                idempotency_key=f'entry{i}',expected_version=a['version'],signal_id=f'signal{i}',
                atr='5',entry_bar_close=(at.replace(minute=at.minute//15*15,second=0)+timedelta(minutes=15)).isoformat(),quote_event_id=q['event_id'])
        assert opened['accepted']
        assert ledger.complete_quote(q['event_id'])['accepted']
        at += timedelta(seconds=1)
        q=event(at,f'exit{i}',99)
        assert enqueue_verified_quote(ledger,q,at)['accepted']
        a=ledger.account()
        assert ledger.close_position(quantity='1',price='99',reason='STOP',
            idempotency_key=f'exit{i}',expected_version=a['version'],trade_id=a['position']['trade_id'],
            quote_event_id=q['event_id'])['accepted']
        assert ledger.complete_quote(q['event_id'])['accepted']
        # Simulate a crash before the final trade reaches the circuit database.
        if i == 1:
            assert replay_closed_trades(ledger,circuit)==2
    circuit=DCircuit(str(ledger.path)+'.circuit.sqlite')
    assert replay_closed_trades(ledger,circuit)==3
    assert replay_closed_trades(ledger,circuit)==3
    status=circuit.assess(now=datetime.now(timezone.utc),equity=ledger.account()['cash'])
    assert status['loss_streak']==3
    assert status['reason']=='LOSS_STREAK_PAUSE'
    class Archive:
        def read(self, start, end): return []
    runtime=DRuntime(tmp_path/'obs.sqlite',ledger.path,Archive())
    when=datetime.now(timezone.utc)+timedelta(seconds=10)
    live=event(when,'after-streak',100)
    assert runtime.confirm_stream_recovered(live,when,market_ready=True,candles_complete=True)
    assert runtime.on_quote(live,when,market_ready=True)['reason']=='LOSS_STREAK_PAUSE'
    runtime.close()
    assert str(proven_flat_midnight_equity(ledger,datetime.now(timezone.utc))[1]) == '10000.00'
    with sqlite3.connect(ledger.path) as db:
        ids = [row[0] for row in db.execute('SELECT fill_id FROM d_fills ORDER BY rowid')]
        midnight = now.replace(hour=0,minute=0,second=0)
        for index, ident in enumerate(ids):
            stamp = midnight + timedelta(seconds=index-3)
            db.execute('UPDATE d_fills SET occurred_at=? WHERE fill_id=?', (stamp.isoformat(),ident))
    # A trade spans midnight, so no unverified mark can be substituted.
    with pytest.raises(RuntimeError,match='D_MIDNIGHT_POSITION_UNVALUED'):
        proven_flat_midnight_equity(ledger,now)


def test_guard_denies_invalid_quote_and_proves_new_account_baseline(tmp_path):
    ledger=DLedger(tmp_path/'ledger.sqlite')
    circuit=DCircuit(tmp_path/'circuit.sqlite')
    now=datetime.now(timezone.utc)
    quote={'symbol':'BTC/USD','source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH',
           'timestamp_valid':True,'event_at':now.isoformat(),'received_at':now.isoformat(),
           'bid':'100','ask':'100.01','event_id':'q'}
    assert assess_from_committed_ledger(ledger,circuit,quote,now)['reason']=='READY'
    assert assess_from_committed_ledger(ledger,circuit,{**quote,'timestamp_valid':False},now)['block_entries']


@pytest.mark.parametrize(('bid','expected'), [('100','DAILY_LOSS'),('89','STOP_DUE')])
def test_runtime_daily_flatten_and_stop_priority(tmp_path,bid,expected):
    class Archive:
        def read(self,start,end): return []
    rt=DRuntime(tmp_path/'obs.sqlite',tmp_path/'ledger.sqlite',Archive())
    now=datetime.now(timezone.utc).replace(microsecond=0)
    midnight=now.replace(hour=0,minute=0,second=0)
    assert rt.circuit.record_day_start(midnight=midnight,equity='10150',provenance='D_LEDGER_FLAT_MIDNIGHT_V1')['accepted']
    def event(at,event_id,price):
        return {'symbol':'BTC/USD','source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH',
                'timestamp_valid':True,'event_at':at.isoformat(),'received_at':at.isoformat(),
                'bid':price,'ask':price,'event_id':event_id}
    entry=event(now,'entry','100')
    assert enqueue_verified_quote(rt.ledger,entry,now)['accepted']
    close=(now.replace(minute=now.minute//15*15,second=0)+timedelta(minutes=15)).isoformat()
    assert rt.ledger.open_position(quantity='1',price='100',stop='90',leverage='5',
        idempotency_key='entry',expected_version=0,signal_id='s',atr='5',entry_bar_close=close,quote_event_id='entry')['accepted']
    assert rt.ledger.complete_quote('entry')['accepted']
    at=now+timedelta(seconds=1)
    assert rt.on_quote(event(at,'risk',bid),at,market_ready=True)['status']=='COMPLETE'
    assert rt.ledger.account()['state']=='FLAT'
    assert rt.ledger.fills()[-1]['reason']==expected
    assert rt.circuit.assess(now=at,equity='10000')['reason']=='DAILY_LOSS_LIMIT'
    rt.close()
