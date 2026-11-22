"""Contract tests for the paper-only strategy control state machine."""
import threading
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from backend.strategy_control import StrategyControl
from backend.strategy_control import close_paper_account
from backend.domain import CostModel
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from backend.e_paper_store import EPaperStore
from backend.ledger import Ledger


def test_pause_blocks_entries_and_requires_both_accounts_flat(tmp_path):
    control = StrategyControl(tmp_path / 'control.sqlite3')
    assert control.is_entry_allowed('A')
    control.request_pause('A')
    assert not control.is_entry_allowed('A')
    control.record_account_result('A', 'formal', 'FLAT')
    control.record_account_result('A', 'shadow', 'UNAVAILABLE')
    assert control.status('A')['state'] != 'PAUSED'
    assert not control.is_entry_allowed('A')
    control.record_account_result('A', 'shadow', 'FLAT')
    assert control.status('A')['state'] == 'PAUSED'


def test_incomplete_pause_survives_restart_and_cannot_resume(tmp_path):
    path = tmp_path / 'control.sqlite3'
    first = StrategyControl(path)
    first.request_pause('C')
    first.record_account_result('C', 'formal', 'ERROR')
    second = StrategyControl(path)
    assert not second.is_entry_allowed('C')
    assert not second.resume('C')
    assert second.is_entry_allowed('A')


def test_pause_waits_for_in_flight_entry_before_returning(tmp_path):
    control = StrategyControl(tmp_path / 'control.sqlite3')
    entered = threading.Event()
    release = threading.Event()
    def entry():
        with control.entry_guard('B') as allowed:
            assert allowed
            entered.set()
            assert release.wait(5)
    with ThreadPoolExecutor(max_workers=2) as executor:
        open_task = executor.submit(entry)
        assert entered.wait(5)
        pause_task = executor.submit(control.request_pause, 'B')
        assert not pause_task.done()
        release.set()
        open_task.result(timeout=5)
        pause_task.result(timeout=5)
    with control.entry_guard('B') as allowed:
        assert not allowed


def test_pause_waits_for_another_process_entry(tmp_path):
    path = tmp_path / 'control.sqlite3'
    control = StrategyControl(path)
    ready = tmp_path / 'ready'
    release = tmp_path / 'release'
    code = '''
import sys,time
from pathlib import Path
from backend.strategy_control import StrategyControl
control=StrategyControl(sys.argv[1])
with control.entry_guard('E') as allowed:
 assert allowed
 Path(sys.argv[2]).write_text('ready')
 while not Path(sys.argv[3]).exists(): time.sleep(.02)
'''
    proc = subprocess.Popen([sys.executable, '-c', code, str(path), str(ready), str(release)])
    try:
        for _ in range(100):
            if ready.exists(): break
            time.sleep(.05)
        assert ready.exists()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pause = executor.submit(control.request_pause, 'E')
            time.sleep(.2)
            assert not pause.done()
            release.write_text('go')
            pause.result(timeout=5)
        assert proc.wait(timeout=5) == 0
        assert not control.is_entry_allowed('E')
    finally:
        release.touch()
        if proc.poll() is None: proc.terminate()


def test_close_rejects_stale_quote_and_uses_executable_side():
    class Store:
        def __init__(self): self.calls = []
        def account(self, agent):
            return {'state': 'OPEN_LONG', 'version': 3, 'position_json': '{"direction":"LONG","quantity":"0.1","trade_id":"t1"}'}
        def close_position(self, **kwargs): self.calls.append(kwargs); return {'accepted': True}
    store = Store()
    now = datetime.now(timezone.utc)
    quote = {'source': 'BINANCE_USDM_PERP', 'currency': 'USD_RESEARCH', 'timestamp_valid': True,
             'event_at': (now-timedelta(seconds=61)).isoformat(), 'received_at': (now-timedelta(seconds=61)).isoformat(),
             'bid': '100', 'ask': '101'}
    assert close_paper_account(store, 'A', quote, CostModel(), now)['reason'] == 'QUOTE_STALE'
    assert not store.calls
    quote['event_at'] = quote['received_at'] = now.isoformat()
    assert close_paper_account(store, 'A', quote, CostModel(), now)['accepted']
    assert store.calls[0]['price'] < 100
    assert store.calls[0]['idempotency_key'] == 'strategy-pause:A:t1'


def test_paper_close_persists_formal_and_shadow_independently(tmp_path):
    formal, shadow = (EPaperStore(tmp_path / name) for name in ('formal.sqlite', 'shadow.sqlite'))
    for store in (formal, shadow):
        result = store.open_position(agent='E', direction='LONG', quantity=Decimal('0.1'), price=Decimal('100'),
                                     stop=Decimal('90'), fee=Decimal('0.01'), slippage=Decimal('0'),
                                     idempotency_key='open', expected_version=0)
        assert result['accepted']
    now = datetime.now(timezone.utc)
    quote = {'source': 'BINANCE_USDM_PERP', 'currency': 'USD_RESEARCH', 'timestamp_valid': True,
             'event_at': now.isoformat(), 'received_at': now.isoformat(), 'bid': '101', 'ask': '102'}
    assert close_paper_account(formal, 'E', quote, CostModel(), now)['accepted']
    assert formal.account('E')['state'] == 'FLAT'
    assert shadow.account('E')['state'] == 'OPEN_LONG'
    assert close_paper_account(shadow, 'E', quote, CostModel(), now)['accepted']
    assert close_paper_account(EPaperStore(shadow.path), 'E', quote, CostModel(), now)['reason'] == 'ALREADY_FLAT'
    assert len(formal.fills()) == len(shadow.fills()) == 2


def test_pause_closes_formal_but_never_claims_missing_shadow_complete(tmp_path):
    control = StrategyControl(tmp_path / 'control.sqlite')
    formal = EPaperStore(tmp_path / 'formal.sqlite')
    assert formal.open_position(agent='E', direction='LONG', quantity=Decimal('0.1'), price=Decimal('100'),
                                stop=Decimal('90'), fee=Decimal('0.01'), slippage=Decimal('0'),
                                idempotency_key='open', expected_version=0)['accepted']
    now = datetime.now(timezone.utc)
    quote = {'source': 'BINANCE_USDM_PERP', 'currency': 'USD_RESEARCH', 'timestamp_valid': True,
             'event_at': now.isoformat(), 'received_at': now.isoformat(), 'bid': '101', 'ask': '102'}
    result = control.pause_accounts('E', {'formal': formal, 'shadow': None}, quote, CostModel(), now)
    assert formal.account('E')['state'] == 'FLAT'
    assert result['state'] == 'PAUSING'
    assert result['formal'] == 'FLAT' and result['shadow'] == 'UNAVAILABLE'
    assert not control.is_entry_allowed('E')
    assert not control.resume('E')


def test_retry_after_restart_finishes_and_can_resume(tmp_path):
    path = tmp_path / 'control.sqlite'
    control = StrategyControl(path)
    formal, shadow = (EPaperStore(tmp_path / name) for name in ('formal.sqlite', 'shadow.sqlite'))
    now = datetime.now(timezone.utc)
    assert control.pause_accounts('E', {'formal': formal, 'shadow': None}, {}, CostModel(), now)['state'] == 'PAUSING'
    restarted = StrategyControl(path)
    assert restarted.pause_accounts('E', {'formal': formal, 'shadow': shadow}, {}, CostModel(), now)['state'] == 'PAUSED'
    assert restarted.resume('E')
    assert restarted.is_entry_allowed('E')


def test_stale_quote_keeps_position_open_and_blocks_resume(tmp_path):
    control = StrategyControl(tmp_path / 'control.sqlite')
    formal, shadow = (EPaperStore(tmp_path / name) for name in ('formal.sqlite', 'shadow.sqlite'))
    assert formal.open_position(agent='E', direction='LONG', quantity=Decimal('0.1'), price=Decimal('100'),
                                stop=Decimal('90'), fee=Decimal('0.01'), slippage=Decimal('0'),
                                idempotency_key='open', expected_version=0)['accepted']
    now = datetime.now(timezone.utc)
    quote = {'source': 'BINANCE_USDM_PERP', 'currency': 'USD_RESEARCH', 'timestamp_valid': True,
             'event_at': (now-timedelta(seconds=61)).isoformat(), 'received_at': (now-timedelta(seconds=61)).isoformat(),
             'bid': '101', 'ask': '102'}
    result = control.pause_accounts('E', {'formal': formal, 'shadow': shadow}, quote, CostModel(), now)
    assert result['state'] == 'PAUSING' and result['formal'] == 'ERROR'
    assert formal.account('E')['state'] == 'OPEN_LONG'
    assert not control.resume('E')


def test_process_crash_releases_gate_and_preserves_pending_pause(tmp_path):
    path = tmp_path / 'control.sqlite'
    control = StrategyControl(path)
    control.request_pause('A')
    ready = tmp_path / 'ready'
    code = '''
import sys,time
from pathlib import Path
from backend.strategy_control import StrategyControl
c=StrategyControl(sys.argv[1])
with c.entry_guard('A') as allowed:
 assert not allowed
 Path(sys.argv[2]).touch()
 time.sleep(30)
'''
    proc = subprocess.Popen([sys.executable, '-c', code, str(path), str(ready)])
    try:
        for _ in range(100):
            if ready.exists(): break
            time.sleep(.05)
        assert ready.exists()
        proc.kill()
        proc.wait(timeout=5)
        restarted = StrategyControl(path)
        assert restarted.status('A')['state'] == 'PAUSING'
        assert not restarted.is_entry_allowed('A')
        assert restarted.is_entry_allowed('B')
    finally:
        if proc.poll() is None: proc.kill()


def test_ledger_entry_respects_strategy_gate(tmp_path):
    control = StrategyControl(tmp_path / 'control.sqlite')
    ledger = Ledger(tmp_path / 'paper.sqlite')
    control.request_pause('A')
    from backend.strategy_control import guarded_formal_open
    result = guarded_formal_open(control, ledger, 'A', agent='A', direction='LONG', quantity=Decimal('0.1'), price=Decimal('100'),
                                  stop=Decimal('90'), fee=Decimal('0.01'), slippage=Decimal('0'),
                                  idempotency_key='gated-open', expected_version=0)
    assert not result['accepted'] and result['reason'] == 'STRATEGY_PAUSED'
    assert ledger.account('A')['state'] == 'FLAT'


def test_shadow_close_reconciles_forward_metadata(tmp_path):
    import sqlite3
    ledger = Ledger(tmp_path / 'shadow.sqlite')
    assert ledger.open_position(agent='A', direction='LONG', quantity=Decimal('0.1'), price=Decimal('100'),
                                stop=Decimal('90'), fee=Decimal('0.01'), slippage=Decimal('0'),
                                idempotency_key='open', expected_version=0)['accepted']
    trade = json.loads(ledger.account('A')['position_json'])['trade_id']
    with sqlite3.connect(ledger.path) as db:
        db.execute('create table forward_positions (agent text primary key, trade_id text, signal_close text, initial_cash text)')
        db.execute('insert into forward_positions values (?,?,?,?)', ('A', trade, '2026-01-01T00:00:00+00:00', '10000'))
        db.execute('create table forward_trades (trade_id text primary key, agent text, signal_close text, closed_at text, net_pnl text, reason text)')
    now = datetime.now(timezone.utc)
    quote = {'source': 'BINANCE_USDM_PERP', 'currency': 'USD_RESEARCH', 'timestamp_valid': True,
             'event_at': now.isoformat(), 'received_at': now.isoformat(), 'bid': '101', 'ask': '102'}
    from backend.strategy_control import close_forward_account
    assert close_forward_account(ledger, 'A', quote, CostModel(), now)['accepted']
    with sqlite3.connect(ledger.path) as db:
        assert db.execute('select count(*) from forward_positions').fetchone()[0] == 0
        assert db.execute('select count(*) from forward_trades where trade_id=?', (trade,)).fetchone()[0] == 1


def test_formal_only_pause_leaves_shadow_running_and_unmodified(tmp_path):
    control = StrategyControl(tmp_path / 'control.sqlite')
    formal, shadow = (EPaperStore(tmp_path / name) for name in ('formal.sqlite', 'shadow.sqlite'))
    for store in (formal, shadow):
        assert store.open_position(agent='E', direction='LONG', quantity=Decimal('0.1'), price=Decimal('100'),
                                   stop=Decimal('90'), fee=Decimal('0.01'), slippage=Decimal('0'),
                                   idempotency_key='open', expected_version=0)['accepted']
    now = datetime.now(timezone.utc)
    quote = {'source': 'BINANCE_USDM_PERP', 'currency': 'USD_RESEARCH', 'timestamp_valid': True,
             'event_at': now.isoformat(), 'received_at': now.isoformat(), 'bid': '101', 'ask': '102'}
    status = control.pause_formal('E', formal, quote, CostModel(), now)
    assert status['state'] == 'PAUSED' and status['shadow'] == 'UNCHANGED'
    assert formal.account('E')['state'] == 'FLAT'
    assert shadow.account('E')['state'] == 'OPEN_LONG'
    assert not control.is_entry_allowed('E')
    assert control.resume('E')

