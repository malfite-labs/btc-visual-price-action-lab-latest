from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
import pytest
from backend.d_circuit import DCircuit

UTC = timezone.utc
DAY = datetime(2026, 11, 5, tzinfo=UTC)


def baseline(circuit):
    return circuit.record_day_start(midnight=DAY, equity='10000', provenance='verified-midnight-snapshot')


def test_missing_baseline_and_equity_never_allow_entries(tmp_path):
    guard = DCircuit(tmp_path / 'c.sqlite')
    result = guard.assess(now=DAY, equity='10000')
    assert result['reason'] == 'DAY_START_EQUITY_UNAVAILABLE'
    assert result['block_entries'] and result['allow_exits'] and not result['flatten_required']
    baseline(guard)
    assert guard.assess(now=DAY, equity=None)['reason'] == 'EQUITY_UNAVAILABLE'
    assert guard.assess(now=DAY, equity='NaN')['block_entries']


def test_midnight_snapshot_is_immutable_and_normalized_to_utc(tmp_path):
    guard = DCircuit(tmp_path / 'c.sqlite')
    assert baseline(guard) == {'accepted': True, 'duplicate': False}
    assert guard.record_day_start(midnight=DAY.astimezone(timezone(timedelta(hours=8))), equity=Decimal('10000.00'), provenance='verified-midnight-snapshot')['duplicate']
    assert guard.record_day_start(midnight=DAY, equity='9999', provenance='verified-midnight-snapshot')['reason'] == 'DAY_START_CONFLICT'
    with pytest.raises(ValueError, match='MIDNIGHT_REQUIRED'):
        guard.record_day_start(midnight=DAY + timedelta(minutes=1), equity=10000, provenance='x')
    with pytest.raises(ValueError, match='TIMEZONE_REQUIRED'):
        guard.assess(now=DAY.replace(tzinfo=None), equity=10000)


def test_day_loss_latches_at_exact_one_percent_and_recovers_next_utc_day(tmp_path):
    path = tmp_path / 'c.sqlite'; guard = DCircuit(path); baseline(guard)
    assert guard.assess(now=DAY + timedelta(hours=1), equity='9900.01')['reason'] == 'READY'
    result = guard.assess(now=DAY + timedelta(hours=2), equity='9900')
    assert result['block_entries'] and result['flatten_required'] and result['allow_exits']
    restored = DCircuit(path)
    assert restored.assess(now=DAY + timedelta(hours=3), equity='10100')['flatten_required']
    assert restored.assess(now=DAY + timedelta(hours=4), equity=None)['flatten_required']
    assert restored.assess(now=DAY + timedelta(days=1), equity='10100')['reason'] == 'DAY_START_EQUITY_UNAVAILABLE'
    restored.record_day_start(midnight=DAY + timedelta(days=1), equity='10100', provenance='next-midnight')
    assert restored.assess(now=DAY + timedelta(days=1), equity='10100')['reason'] == 'READY'
    assert restored.assess(now=DAY + timedelta(hours=5), equity='10000')['reason'] == 'OUT_OF_ORDER_EQUITY'


def test_nonpositive_equity_is_loss_not_missing(tmp_path):
    guard = DCircuit(tmp_path / 'c.sqlite'); baseline(guard)
    assert guard.assess(now=DAY + timedelta(seconds=1), equity='0')['flatten_required']
    assert guard.assess(now=DAY + timedelta(seconds=2), equity='-10')['flatten_required']


def test_three_distinct_losses_pause_24h_and_survive_restart(tmp_path):
    path = tmp_path / 'c.sqlite'; guard = DCircuit(path); baseline(guard)
    for i in range(3):
        result = guard.record_closed_trade(trade_id=str(i), net_pnl='-1', closed_at=DAY + timedelta(hours=i+1))
    assert result['loss_streak'] == 3
    third = DAY + timedelta(hours=3)
    same = guard.record_closed_trade(trade_id='2', net_pnl='-1.00', closed_at=third)
    assert same['accepted'] and same['duplicate']
    assert guard.record_closed_trade(trade_id='2', net_pnl='-2', closed_at=third)['reason'] == 'TRADE_CONFLICT'
    guard = DCircuit(path)
    assert guard.assess(now=third + timedelta(seconds=1), equity=10000)['reason'] == 'LOSS_STREAK_PAUSE'
    next_day = DAY + timedelta(days=1)
    guard.record_day_start(midnight=next_day, equity=10000, provenance='second-day')
    assert guard.assess(now=third + timedelta(hours=23), equity=10000)['block_entries']
    assert guard.assess(now=third + timedelta(hours=24), equity=10000)['reason'] == 'READY'


def test_profit_or_zero_breaks_loss_streak_but_does_not_clear_active_pause(tmp_path):
    guard = DCircuit(tmp_path / 'c.sqlite'); baseline(guard)
    for i in range(3): guard.record_closed_trade(trade_id=str(i), net_pnl=-2, closed_at=DAY + timedelta(hours=i+1))
    result = guard.record_closed_trade(trade_id='win', net_pnl=0, closed_at=DAY + timedelta(hours=4))
    assert result['loss_streak'] == 0
    assert guard.assess(now=DAY + timedelta(hours=4), equity=10000)['reason'] == 'LOSS_STREAK_PAUSE'


def test_ordering_and_concurrent_duplicates(tmp_path):
    guard = DCircuit(tmp_path / 'c.sqlite'); baseline(guard)
    def write(_): return guard.record_closed_trade(trade_id='same', net_pnl=-1, closed_at=DAY + timedelta(hours=2))
    with ThreadPoolExecutor(max_workers=6) as pool: results = list(pool.map(write, range(6)))
    assert sum(not x['duplicate'] for x in results) == 1
    assert guard.record_closed_trade(trade_id='past', net_pnl=-1, closed_at=DAY + timedelta(hours=1))['reason'] == 'OUT_OF_ORDER_TRADE'
    assert guard.record_closed_trade(trade_id='tie', net_pnl=-1, closed_at=DAY + timedelta(hours=2))['reason'] == 'OUT_OF_ORDER_TRADE'
    assert guard.assess(now=DAY + timedelta(hours=3), equity=10000)['loss_streak'] == 1
    assert guard.assess(now=DAY + timedelta(hours=2), equity=10000)['reason'] == 'OUT_OF_ORDER_EQUITY'


def test_refuses_unrelated_database(tmp_path):
    path = tmp_path / 'other.sqlite'
    with sqlite3.connect(path) as db: db.execute('create table unrelated(id integer)')
    with pytest.raises(RuntimeError, match='IDENTITY_MISMATCH'): DCircuit(path)
