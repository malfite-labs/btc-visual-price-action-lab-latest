from datetime import datetime, timedelta, timezone
from backend.d_executor import validate_quote


def test_quote_validation_rejects_untrusted_stale_future_and_bad_prices():
    now = datetime.now(timezone.utc)
    quote = {'symbol':'BTC/USD', 'source': 'BINANCE_USDM_PERP', 'currency': 'USD_RESEARCH',
             'timestamp_valid': True, 'event_at': now.isoformat(), 'received_at': now.isoformat(),
             'bid': '100', 'ask': '101', 'event_id': 'q1'}
    assert validate_quote(quote, now)
    for change in ({'symbol': 'ETH/USD'}, {'symbol': None}, {'source': 'OTHER'}, {'timestamp_valid': False}, {'bid': '102'},
                   {'event_id': ''}, {'event_at': (now-timedelta(seconds=61)).isoformat()},
                   {'received_at': (now+timedelta(seconds=1)).isoformat()}):
        assert not validate_quote({**quote, **change}, now)


def test_intent_capture_persists_first_quote_and_is_idempotent(tmp_path):
    import sqlite3
    from backend.d_executor import IntentStore
    now = datetime.now(timezone.utc)
    signal = {'signal_close': (now-timedelta(seconds=4)).isoformat(), 'setup': 'D_CONFIRM'}
    q = {'symbol':'BTC/USD', 'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
         'event_at':now.isoformat(),'received_at':now.isoformat(),'bid':'100','ask':'101','event_id':'first'}
    path = tmp_path/'intent.sqlite3'
    store = IntentStore(path)
    assert store.capture(signal,q,now)['accepted']
    assert IntentStore(path).capture(signal,{**q,'event_id':'later'},now)['duplicate']
    with sqlite3.connect(path) as db:
        assert db.execute('select count(*) from d_execution_intents').fetchone()[0] == 1
        assert 'first' in db.execute('select quote_json from d_execution_intents').fetchone()[0]
    assert not store.capture({**signal,'setup':'DIFFERENT'},q,now)['accepted']


def test_intent_recovery_and_terminal_state_cas(tmp_path):
    from backend.d_executor import IntentStore
    now = datetime.now(timezone.utc)
    signal = {'signal_close': (now-timedelta(seconds=1)).isoformat(), 'setup':'D_CONFIRM'}
    quote = {'symbol':'BTC/USD', 'source':'BINANCE_USDM_PERP', 'currency':'USD_RESEARCH', 'timestamp_valid':True,
             'event_at':now.isoformat(), 'received_at':now.isoformat(), 'bid':'100', 'ask':'101', 'event_id':'event'}
    path = tmp_path/'intent.sqlite3'
    assert IntentStore(path).capture(signal, quote, now)['accepted']
    store = IntentStore(path)
    assert store.pending()[0]['quote'] == quote
    assert store.resolve(signal['signal_close'], 'PENDING', 'FILLED')['accepted']
    assert store.resolve(signal['signal_close'], 'PENDING', 'FILLED')['duplicate']
    assert not store.resolve(signal['signal_close'], 'PENDING', 'EXPIRED')['accepted']
    assert store.pending() == []


def test_reconcile_committed_trade_and_expire_stale_quote(tmp_path):
    import sqlite3
    from backend.d_executor import IntentStore
    now = datetime.now(timezone.utc)
    signal = {'signal_close':(now-timedelta(seconds=1)).isoformat()}
    quote = {'symbol':'BTC/USD', 'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
             'event_at':now.isoformat(),'received_at':now.isoformat(),'bid':'100','ask':'101','event_id':'q'}
    path=tmp_path/'ledger.sqlite3'
    store=IntentStore(path)
    assert store.capture(signal,quote,now)['accepted']
    assert store.reconcile(signal['signal_close'],now)['status']=='BLOCKED'
    with sqlite3.connect(path) as db:
        db.execute('create table d_used_signals(signal_id text primary key, trade_id text)')
        db.execute('insert into d_used_signals values(?,?)',(signal['signal_close'],'trade1'))
    assert IntentStore(path).reconcile(signal['signal_close'],now+timedelta(hours=1)) == {'status':'FILLED','trade_id':'trade1'}
    later={'signal_close':(now+timedelta(seconds=1)).isoformat()}
    laterquote={**quote,'event_at':(now+timedelta(seconds=2)).isoformat(), 'received_at':(now+timedelta(seconds=2)).isoformat(),'event_id':'q2'}
    assert store.capture(later,laterquote,now+timedelta(seconds=2))['accepted']
    assert store.reconcile(later['signal_close'],now+timedelta(minutes=5))['status']=='EXPIRED'


def test_handoff_survives_restart_without_mutating_observation(tmp_path):
    import json
    import sqlite3
    from backend.d_executor import IntentStore, handoff_observed_intent
    from backend.d_paper import initialize_store
    now = datetime.now(timezone.utc)
    signal = {'signal_close': (now-timedelta(seconds=2)).isoformat(),
              'direction':'LONG','setup':'D_CONFIRM'}
    quote = {'symbol':'BTC/USD', 'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
             'event_at':now.isoformat(),'received_at':now.isoformat(),'bid':'100','ask':'101','event_id':'q'}
    observed = tmp_path/'observe.sqlite3'
    intents = tmp_path/'ledger.sqlite3'
    initialize_store(observed)
    with sqlite3.connect(observed) as db:
        db.execute('update d_state set payload=? where id=1', (json.dumps({'phase':'PENDING','signal':signal}),))
    assert handoff_observed_intent(observed,IntentStore(intents),quote,now)['accepted']
    assert handoff_observed_intent(observed,IntentStore(intents),quote,now)['duplicate']
    with sqlite3.connect(observed) as db:
        assert json.loads(db.execute('select payload from d_state').fetchone()[0])['phase']=='PENDING'
    assert IntentStore(intents).pending()[0]['signal']==signal


def test_verified_quote_persistence_and_stale_recovery(tmp_path):
    from backend.d_executor import enqueue_verified_quote, pending_verified_quote
    from backend.d_ledger import DLedger
    now = datetime.now(timezone.utc)
    quote = {'symbol':'BTC/USD', 'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
             'event_at':now.isoformat(),'received_at':now.isoformat(),'bid':'100','ask':'101','event_id':'event'}
    ledger = DLedger(tmp_path/'ledger.sqlite3')
    assert enqueue_verified_quote(ledger,quote,now)['accepted']
    assert enqueue_verified_quote(ledger,quote,now)['duplicate']
    assert not enqueue_verified_quote(ledger,{**quote,'bid':'99'},now)['accepted']
    assert pending_verified_quote(DLedger(ledger.path),now)['quote']==quote
    assert pending_verified_quote(ledger,now+timedelta(seconds=61))['reason']=='QUOTE_STALE_OR_INVALID'


def test_legacy_queue_cannot_be_treated_as_verified(tmp_path):
    from backend.d_executor import pending_verified_quote
    from backend.d_ledger import DLedger
    now = datetime.now(timezone.utc)
    ledger=DLedger(tmp_path/'legacy.sqlite3')
    ledger.enqueue_quote(event_id='legacy',event_at=now.isoformat(),bid='100',ask='101')
    assert pending_verified_quote(ledger,now)['reason']=='EVIDENCE_UNAVAILABLE'


def test_quote_queue_tampering_fails_closed(tmp_path):
    import sqlite3
    from backend.d_executor import enqueue_verified_quote, pending_verified_quote
    from backend.d_ledger import DLedger
    now=datetime.now(timezone.utc)
    quote={'symbol':'BTC/USD', 'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
           'event_at':now.isoformat(),'received_at':now.isoformat(),'bid':'100','ask':'101','event_id':'q'}
    ledger=DLedger(tmp_path/'ledger.sqlite3')
    assert enqueue_verified_quote(ledger,quote,now)['accepted']
    with sqlite3.connect(ledger.path) as db:
        db.execute("update d_quotes set bid='90' where event_id='q'")
    assert pending_verified_quote(ledger,now)['reason']=='EVIDENCE_MISMATCH'


def test_position_protection_uses_fresh_quote_and_persisted_stop_due():
    from backend.d_executor import plan_verified_position_event
    now=datetime.now(timezone.utc)
    quote={'symbol':'BTC/USD', 'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
           'event_at':now.isoformat(),'received_at':now.isoformat(),'bid':'101','ask':'101.1','event_id':'q'}
    pos={'quantity':'1','entry_price':'100','leverage':'5','stop_due':{'event_id':'old'}}
    assert plan_verified_position_event({'cash':'1000','position':pos},quote,now)['action']=='STOP_DUE'
    assert plan_verified_position_event({'cash':'1000','position':pos},quote,now+timedelta(minutes=2))['action']=='BLOCKED'
    assert plan_verified_position_event({'cash':'1','position':pos},{**quote,'bid':'70','ask':'71'},now)['action']=='LIQUIDATION'


def test_verified_quote_drives_both_targets_in_isolated_ledger(tmp_path):
    from backend.d_executor import enqueue_verified_quote, process_verified_quote
    from backend.d_ledger import DLedger
    now=datetime.now(timezone.utc).replace(microsecond=0)
    bar=now.replace(minute=(now.minute//15)*15,second=0)+timedelta(minutes=15)
    def quote(event_id,bid,at):
        return {'symbol':'BTC/USD', 'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
                'event_at':at.isoformat(),'received_at':at.isoformat(),'bid':str(bid),'ask':str(bid+0.01),'event_id':event_id}
    ledger=DLedger(tmp_path/'paper.sqlite3')
    initial=quote('open',100,now-timedelta(seconds=2))
    assert enqueue_verified_quote(ledger,initial,now)['accepted']
    result=ledger.open_position(quantity='1',price='100',stop='90',leverage='5',idempotency_key='open',
          expected_version=0,signal_id='s1',atr='5',entry_bar_close=bar.isoformat(),quote_event_id='open')
    assert result['accepted'] and ledger.complete_quote('open')['accepted']
    rising=quote('both',121,now-timedelta(seconds=1))
    assert enqueue_verified_quote(ledger,rising,now)['accepted']
    assert process_verified_quote(DLedger(ledger.path),now)['status']=='COMPLETE'
    assert ledger.account()['position'] is None
    assert [x['reason'] for x in ledger.fills()]==['D_ENTRY','TP1','TP2']
    assert process_verified_quote(DLedger(ledger.path),now)['status']=='EMPTY'



def test_stale_stop_evidence_survives_restart_and_exits_on_new_quote(tmp_path):
    from backend.d_executor import enqueue_verified_quote, process_verified_quote
    from backend.d_ledger import DLedger
    now = datetime.now(timezone.utc).replace(microsecond=0)
    bar = now.replace(minute=now.minute // 15 * 15, second=0) + timedelta(minutes=15)
    def quote(identifier, bid, at):
        return {'symbol':'BTC/USD', 'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
                'event_at':at.isoformat(),'received_at':at.isoformat(),'bid':str(bid),'ask':str(bid + .01),'event_id':identifier}
    path = tmp_path/'ledger.sqlite3'
    ledger = DLedger(path)
    assert enqueue_verified_quote(ledger, quote('entry',100,now), now)['accepted']
    assert ledger.open_position(quantity='1',price='100',stop='90',leverage='5',idempotency_key='entry',expected_version=0,signal_id='sig',atr='5',entry_bar_close=bar.isoformat(),quote_event_id='entry')['accepted']
    assert ledger.complete_quote('entry')['accepted']
    old_time = now + timedelta(seconds=1)
    assert enqueue_verified_quote(ledger,quote('old-stop',89,old_time),old_time)['accepted']
    recovery = old_time + timedelta(seconds=61)
    assert enqueue_verified_quote(ledger,quote('new',95,recovery),recovery)['accepted']
    result = process_verified_quote(DLedger(path),recovery)
    assert result['status'] == 'COMPLETE'
    assert DLedger(path).account()['position'] is None
    closed = DLedger(path).fills()[-1]
    assert closed['reason'] == 'STOP_DUE'
    assert float(closed['price']) > 90
    assert len(DLedger(path).fills()) == 2


def test_stale_non_trigger_quote_is_discarded_without_filling(tmp_path):
    from backend.d_executor import enqueue_verified_quote, process_verified_quote
    from backend.d_ledger import DLedger
    now = datetime.now(timezone.utc).replace(microsecond=0)
    ledger = DLedger(tmp_path/'ledger.sqlite3')
    q = {'symbol':'BTC/USD', 'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
         'event_at':now.isoformat(),'received_at':now.isoformat(),'bid':'100','ask':'100.01','event_id':'old'}
    assert enqueue_verified_quote(ledger,q,now)['accepted']
    assert process_verified_quote(DLedger(ledger.path),now+timedelta(seconds=61))['status'] == 'EMPTY'
    assert ledger.fills() == []


def test_pending_entry_uses_durable_signal_preflight_and_is_idempotent(tmp_path):
    from backend.d_executor import IntentStore, process_pending_entry
    from backend.d_ledger import DLedger
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    close = now.replace(minute=(now.minute//15)*15)
    signal = {'signal_close':close.isoformat(),'direction':'LONG','setup':'D_CONFIRM',
              'atr':'2','low':'98','neckline':'100','stop':'98.9','close':'100'}
    quote = {'symbol':'BTC/USD', 'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
             'event_at':now.isoformat(),'received_at':now.isoformat(),'bid':'100.08','ask':'100.1','event_id':'entry-q'}
    bar = {'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','symbol':'BTC/USD',
           'interval':'15Min','is_closed':True,'open_at':(close-timedelta(minutes=15)).isoformat(),
           'close_at':close.isoformat(),'close':'100','volume':'4000'}
    ledger = DLedger(tmp_path/'trade.sqlite3')
    intents = IntentStore(ledger.path)
    assert intents.capture(signal,quote,now)['accepted']
    first = process_pending_entry(ledger,intents,close.isoformat(),bar,now)
    assert first['status'] == 'FILLED'
    assert ledger.account()['state'] == 'OPEN_LONG'
    assert ledger.account()['position']['leverage'] == '5'
    assert len(ledger.fills()) == 1
    second = process_pending_entry(DLedger(ledger.path),IntentStore(ledger.path),close.isoformat(),bar,now)
    assert second['status'] == 'FILLED'
    assert len(ledger.fills()) == 1


def test_pending_entry_rejects_untrusted_bar_and_expired_quote(tmp_path):
    from backend.d_executor import IntentStore, process_pending_entry
    from backend.d_ledger import DLedger
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    close = now.replace(minute=now.minute//15*15)
    signal = {'signal_close':close.isoformat(),'direction':'LONG','setup':'D_CONFIRM',
              'atr':'2','low':'98','neckline':'100','stop':'98.9','close':'100'}
    quote = {'symbol':'BTC/USD', 'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
             'event_at':now.isoformat(),'received_at':now.isoformat(),'bid':'100.08','ask':'100.1','event_id':'entry-q'}
    bar = {'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','symbol':'BTC/USD',
           'interval':'15Min','is_closed':True,'open_at':(close-timedelta(minutes=15)).isoformat(),
           'close_at':close.isoformat(),'close':'100','volume':'4000'}
    ledger=DLedger(tmp_path/'trade.sqlite3'); intents=IntentStore(ledger.path)
    assert intents.capture(signal,quote,now)['accepted']
    assert process_pending_entry(ledger,intents,close.isoformat(),{**bar,'source':'OTHER'},now)['status']=='BLOCKED'
    assert ledger.fills() == []
    assert process_pending_entry(ledger,intents,close.isoformat(),bar,now+timedelta(seconds=61))['status']=='EXPIRED'
    assert ledger.fills() == []


def test_verified_quote_redelivery_ignores_received_time_without_mutating_evidence(tmp_path):
    import sqlite3,json
    from backend.d_executor import enqueue_verified_quote
    from backend.d_ledger import DLedger
    now=datetime.now(timezone.utc).replace(microsecond=0)
    q={'symbol':'BTC/USD','source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH',
       'timestamp_valid':True,'event_at':now.isoformat(),'received_at':now.isoformat(),
       'bid':'100','ask':'100.01','event_id':'quote-1'}
    ledger=DLedger(tmp_path/'ledger.sqlite')
    assert enqueue_verified_quote(ledger,q,now)['accepted']
    redelivery={**q,'received_at':(now+timedelta(seconds=1)).isoformat(),'age_ms':1000}
    result=enqueue_verified_quote(ledger,redelivery,now+timedelta(seconds=1))
    assert result['duplicate'] and result['accepted']
    with sqlite3.connect(ledger.path) as db:
        evidence=json.loads(db.execute('select payload from d_quote_evidence where event_id=?',('quote-1',)).fetchone()[0])
    assert evidence==q
    assert not enqueue_verified_quote(ledger,{**redelivery,'bid':'99'},now+timedelta(seconds=1))['accepted']

