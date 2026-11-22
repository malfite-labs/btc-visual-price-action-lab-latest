import sqlite3
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import pytest
from backend.d_paper import initialize_store, observe_cycle, read_status

UTC = timezone.utc

class Archive:
    def __init__(self, bars):
        self.bars = bars
    def read(self, start, end):
        return self.bars


def test_store_rejects_unrelated_database(tmp_path):
    path = tmp_path / 'foreign.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('create table important(value text)')
        db.execute("insert into important values('keep')")
    with pytest.raises(ValueError, match='IDENTITY'):
        initialize_store(path)
    with sqlite3.connect(path) as db:
        assert db.execute('select value from important').fetchone() == ('keep',)


def test_observer_atomic_and_never_opens(tmp_path, monkeypatch):
    import backend.d_signal
    path = tmp_path / 'd.sqlite3'
    initialize_store(path)
    close = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    bar = {'close_at': close.isoformat()}
    future = {'close_at': (close + timedelta(minutes=15)).isoformat()}
    def evaluate(bars, state):
        assert bars == [bar]
        state = dict(state, phase='PENDING', last_close=close.isoformat())
        return {'status': 'PENDING', 'reason': 'D_CONFIRM', 'state': state, 'signal': {'direction': 'LONG'}}
    monkeypatch.setattr(backend.d_signal, 'evaluate', evaluate)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: observe_cycle(path, Archive([bar, future]), close, close + timedelta(seconds=1)), range(5)))
    assert sum(result['status'] == 'PENDING' for result in results) == 1
    assert sum(result['status'] == 'DUPLICATE_BAR' for result in results) == 4
    status = read_status(path)
    assert status['decision']['action'] == 'PENDING'
    assert status['execution_ready'] is False
    assert status['reason'] == 'EXECUTION_NOT_IMPLEMENTED'
    assert status['fills'] == []
    assert status['account']['state'] == 'FLAT'
    assert status['equity'] == '10000.00'
    with sqlite3.connect(path) as db:
        assert db.execute('select count(*) from d_cycles').fetchone() == (1,)


def test_expired_and_missing_bars_fail_closed(tmp_path):
    path = tmp_path / 'd.sqlite3'; initialize_store(path)
    close = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    assert observe_cycle(path, Archive([]), close, close + timedelta(minutes=15))['status'] == 'CYCLE_EXPIRED'
    assert read_status(path)['decision'] is None
    result = observe_cycle(path, Archive([]), close, close + timedelta(seconds=5))
    assert result['reason'] == 'LATEST_CANDLE_MISSING'
    assert read_status(path)['account']['state'] == 'FLAT'


def test_duplicate_does_not_read_archive_again_and_failed_read_is_retryable(tmp_path):
    path = tmp_path / 'd.sqlite3'; initialize_store(path)
    close = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    class Broken:
        def read(self, start, end):
            raise RuntimeError('archive unavailable')
    with pytest.raises(RuntimeError):
        observe_cycle(path, Broken(), close, close + timedelta(seconds=1))
    assert read_status(path)['decision'] is None
    assert observe_cycle(path, Archive([]), close, close + timedelta(seconds=2))['reason'] == 'LATEST_CANDLE_MISSING'
    assert observe_cycle(path, Broken(), close, close + timedelta(seconds=3))['status'] == 'DUPLICATE_BAR'
