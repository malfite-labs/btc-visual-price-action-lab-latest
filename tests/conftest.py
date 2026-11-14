"""Hard isolation for tests that import the application module at collection time."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DATA_ROOT = Path(tempfile.gettempdir()) / f"btc-lab-pytest-{os.getpid()}"
TEST_DATA_ROOT.mkdir(parents=True, exist_ok=True)

# Force application imports and subprocesses spawned by tests onto disposable stores.
os.environ["BTC_LAB_TEST_MODE"] = "true"
os.environ["BTC_LAB_DATA_DIR"] = str(TEST_DATA_ROOT)
os.environ["BTC_LAB_DB"] = str(TEST_DATA_ROOT / "paper.sqlite3")
os.environ["BTC_LAB_CANDLE_DB"] = str(TEST_DATA_ROOT / "market-candles.sqlite3")
os.environ["BTC_LAB_STRATEGY_POOL_DB"] = str(TEST_DATA_ROOT / "strategy-pool.sqlite3")


def pytest_sessionstart(session):
    """Fail closed before test modules can import backend.app or create SQLite schemas."""
    protected = (PROJECT_ROOT / "data").resolve()
    configured = [
        os.environ.get("BTC_LAB_DATA_DIR", ""),
        os.environ.get("BTC_LAB_DB", ""),
        os.environ.get("BTC_LAB_CANDLE_DB", ""),
        os.environ.get("BTC_LAB_STRATEGY_POOL_DB", ""),
    ]
    for value in configured:
        path = Path(value).resolve()
        if path == protected or protected in path.parents:
            raise RuntimeError(f"TEST_DATABASE_MUST_NOT_USE_PRODUCTION_DATA: {path}")

