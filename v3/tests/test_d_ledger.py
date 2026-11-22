from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import sqlite3
import pytest
from backend.d_ledger import DLedger

BAR = '2026-11-05T10:15:00+00:00'
NEXT = '2026-11-05T10:30:00+00:00'


def open_order(ledger, **changes):
    ledger.enqueue_quote(event_id='quote1', event_at='2026-11-05T10:15:01+00:00', bid='80000', ask='80001')
    args = dict(quantity='0.10000000', price='80000', stop='79000', atr='500',
                leverage='1', idempotency_key='open-one', expected_version=0,
                signal_id='signal-one', entry_bar_close=BAR, quote_event_id='quote1')
    args.update(changes)
    return ledger.open_position(**args)


def test_partial_closes_fee_accounting_and_restart(tmp_path):
    path = tmp_path/'trading.sqlite3'; ledger = DLedger(path)
    opened = open_order(ledger)
    assert opened['accepted'] and opened['fee'] == '4.00'
    assert open_order(ledger)['duplicate']
    assert ledger.account()['cash'] == '9996.00'
    pos = ledger.account()['position']
    assert pos['tp1'] == '81000.00' and pos['tp2'] == '82000.00'
    tp1 = ledger.close_position(quantity='.05', price='81000', reason='TP1', bar_close=NEXT,
                                trade_id=opened['trade_id'], quote_event_id='quote1', idempotency_key='tp1', expected_version=1)
    assert tp1['accepted'] and tp1['net_pnl'] == '45.97' and tp1['remaining_quantity'] == '0.05000000'
    restored = DLedger(path); pos = restored.account()['position']
    assert pos['tp1_filled'] and pos['pending_stop'] == '80000'
    too_early = restored.update_stop(stop='80000', trade_id=opened['trade_id'], quote_event_id='quote1', idempotency_key='raise-stop',
                                     current_bar_open=BAR, expected_version=2)
    assert too_early['reason'] == 'STOP_NOT_EFFECTIVE_YET'
    raised = restored.update_stop(stop='80000', trade_id=opened['trade_id'], quote_event_id='quote1', idempotency_key='raise-stop',
                                   current_bar_open=NEXT, expected_version=2)
    assert raised['accepted'] and restored.account()['position']['stop_price'] == '80000'
    tp2 = restored.close_position(quantity='.05', price='82000', reason='TP2',
                                  trade_id=opened['trade_id'], quote_event_id='quote1', idempotency_key='tp2', expected_version=3)
    assert tp2['accepted'] and tp2['net_pnl'] == '95.95'
    assert restored.account()['cash'] == '10141.92'
    assert restored.account()['realized_pnl'] == '141.92'
    assert restored.account()['state'] == 'FLAT'
    assert len(restored.fills()) == 3


def test_concurrent_duplicate_open_and_conflicting_reuse(tmp_path):
    ledger = DLedger(tmp_path/'trading.sqlite3')
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: open_order(ledger), range(2)))
    assert all(r['accepted'] for r in outcomes)
    assert sum(bool(r['duplicate']) for r in outcomes) == 1
    assert len(ledger.fills()) == 1
    assert open_order(ledger, quantity='.2')['reason'] == 'IDEMPOTENCY_KEY_CONFLICT'


def test_concurrent_close_has_no_double_fill(tmp_path):
    ledger = DLedger(tmp_path/'trading.sqlite3'); opened = open_order(ledger)
    def close(_):
        return ledger.close_position(quantity='.10', price='79000', reason='STOP',
                                     trade_id=opened['trade_id'], quote_event_id='quote1', idempotency_key='stop', expected_version=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(close, range(2)))
    assert all(r['accepted'] for r in outcomes)
    assert sum(bool(r['duplicate']) for r in outcomes) == 1
    assert len(ledger.fills()) == 2 and ledger.account()['state'] == 'FLAT'


def test_rejected_close_and_stop_do_not_mutate(tmp_path):
    ledger = DLedger(tmp_path/'trading.sqlite3'); opened = open_order(ledger)
    before = ledger.account()
    invalid = ledger.close_position(quantity='.1', price='81000', reason='TP1', bar_close=NEXT,
                                    trade_id=opened['trade_id'], quote_event_id='quote1', idempotency_key='bad', expected_version=1)
    assert invalid['reason'] == 'INVALID_TP1_QUANTITY_OR_STATE'
    assert ledger.account() == before
    mismatch = ledger.close_position(quantity='.1', price='79000', reason='STOP',
                                     trade_id='wrong', quote_event_id='quote1', idempotency_key='bad2', expected_version=1)
    assert mismatch['reason'] == 'TRADE_ID_MISMATCH'
    assert ledger.account() == before and len(ledger.fills()) == 1


def test_refuses_other_database_without_modifying_it(tmp_path):
    path = tmp_path/'existing.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('create table accounts (name text)')
        db.execute("insert into accounts values ('preserve')")
    with pytest.raises(RuntimeError, match='D_LEDGER_IDENTITY_MISMATCH'):
        DLedger(path)
    with sqlite3.connect(path) as db:
        assert db.execute('select * from accounts').fetchall() == [('preserve',)]
        assert db.execute("select count(*) from sqlite_master where name='d_account'").fetchone()[0] == 0



def test_same_signal_cannot_open_after_close_and_restart(tmp_path):
    path = tmp_path/'trading.sqlite3'
    ledger = DLedger(path); opened = open_order(ledger)
    assert ledger.close_position(quantity='.1', price='79000', reason='STOP', trade_id=opened['trade_id'], quote_event_id='quote1', idempotency_key='stop', expected_version=1)['accepted']
    restarted = DLedger(path)
    retry = open_order(restarted, idempotency_key='new-key', expected_version=2)
    assert retry == {'accepted': False, 'reason': 'SIGNAL_ALREADY_USED'}
    assert restarted.account()['state'] == 'FLAT'
    assert len(restarted.fills()) == 2


def test_nonempty_legacy_ledger_without_signal_history_is_refused(tmp_path):
    path = tmp_path/'legacy.sqlite3'
    ledger = DLedger(path); assert open_order(ledger)['accepted']
    with sqlite3.connect(path) as db:
        db.execute('drop table d_used_signals')
    with pytest.raises(RuntimeError, match='D_LEDGER_SIGNAL_HISTORY_UNRECOVERABLE'):
        DLedger(path)
    with sqlite3.connect(path) as db:
        assert db.execute('select count(*) from d_fills').fetchone()[0] == 1
        assert db.execute("select count(*) from sqlite_master where name='d_used_signals'").fetchone()[0] == 0


def test_quotes_fenced_and_partial_stages_recover_after_restart(tmp_path):
    path=tmp_path/'trading.sqlite3'; ledger=DLedger(path); opened=open_order(ledger)
    assert not ledger.enqueue_quote(event_id='old', event_at='2026-11-05T10:15:00+00:00', bid='1', ask='2')['accepted']
    assert ledger.enqueue_quote(event_id='q2', event_at='2026-11-05T10:15:02+00:00', bid='82000', ask='82001')['accepted']
    assert ledger.complete_quote('quote1')['accepted']
    first=ledger.close_position(quantity='.05',price='81000',reason='TP1',bar_close=NEXT,trade_id=opened['trade_id'],quote_event_id='q2',idempotency_key='tp1',expected_version=1)
    assert first['accepted']
    restored=DLedger(path)
    assert restored.pending_quote()['event_id']=='q2'
    assert restored.close_position(quantity='.05',price='81000',reason='TP1',bar_close=NEXT,trade_id=opened['trade_id'],quote_event_id='q2',idempotency_key='tp1',expected_version=1)['duplicate']
    second=restored.close_position(quantity='.05',price='82000',reason='TP2',trade_id=opened['trade_id'],quote_event_id='q2',idempotency_key='tp2',expected_version=2)
    assert second['accepted'] and restored.complete_quote('q2')['accepted']
    assert restored.account()['state']=='FLAT' and len(restored.fills())==3
    assert not restored.enqueue_quote(event_id='late',event_at='2026-11-05T10:15:01+00:00',bid='1',ask='2')['accepted']


def test_stale_stop_trigger_must_be_persisted_before_event_complete(tmp_path):
    ledger=DLedger(tmp_path/'trading.sqlite3'); opened=open_order(ledger)
    assert ledger.complete_quote('quote1')['accepted']
    assert ledger.enqueue_quote(event_id='stop_quote',event_at='2026-11-05T10:15:02+00:00',bid='78999',ask='79000')['accepted']
    assert ledger.complete_quote('stop_quote')['reason']=='STOP_TRIGGER_UNHANDLED'
    due=ledger.mark_stop_due(event_id='stop_quote',expected_version=1,trade_id=opened['trade_id'])
    assert due['accepted'] and due['stop_due']['bid']=='78999'
    assert ledger.complete_quote('stop_quote')['accepted']
    restored=DLedger(tmp_path/'trading.sqlite3')
    assert restored.account()['position']['stop_due']['event_id']=='stop_quote'
    assert restored.enqueue_quote(event_id='fresh',event_at='2026-11-05T10:15:03+00:00',bid='79010',ask='79011')['accepted']
    bad=restored.close_position(quantity='.05',price='81000',reason='TP1',bar_close=NEXT,quote_event_id='fresh',trade_id=opened['trade_id'],idempotency_key='bad-tp',expected_version=2)
    assert bad['reason']=='STOP_EXIT_PENDING'
    closed=restored.close_position(quantity='.1',price='79010',reason='STOP_DUE',quote_event_id='fresh',trade_id=opened['trade_id'],idempotency_key='stop',expected_version=2)
    assert closed['accepted'] and restored.complete_quote('fresh')['accepted']


def test_open_failure_before_commit_rolls_back_all_writes(tmp_path, monkeypatch):
    ledger = DLedger(tmp_path/'trading.sqlite3')
    def fail_record(*args):
        raise RuntimeError('simulated crash')
    monkeypatch.setattr(ledger, '_record', fail_record)
    with pytest.raises(RuntimeError, match='simulated crash'):
        open_order(ledger)
    assert ledger.account()['state'] == 'FLAT'
    assert ledger.account()['cash'] == '10000.00'
    assert ledger.fills() == []
    with sqlite3.connect(ledger.path) as db:
        assert db.execute('select count(*) from d_used_signals').fetchone()[0] == 0
    assert DLedger(ledger.path).pending_quote()['event_id'] == 'quote1'


def test_fifo_blocks_later_event_until_previous_completed(tmp_path):
    ledger = DLedger(tmp_path/'trading.sqlite3'); opened = open_order(ledger)
    assert ledger.enqueue_quote(event_id='q2',event_at='2026-11-05T10:15:02+00:00',bid='78900',ask='78901')['accepted']
    early = ledger.mark_stop_due(event_id='q2',expected_version=1,trade_id=opened['trade_id'])
    assert early['reason'] == 'QUOTE_NOT_PENDING'
    assert ledger.complete_quote('quote1')['accepted']
    assert DLedger(ledger.path).pending_quote()['event_id'] == 'q2'
    assert DLedger(ledger.path).mark_stop_due(event_id='q2',expected_version=1,trade_id=opened['trade_id'])['accepted']
