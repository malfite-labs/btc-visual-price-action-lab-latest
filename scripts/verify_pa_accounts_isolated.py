"""Isolated one-cycle check for registered PA accounts; never writes production DB."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.strategy_pool import StrategyPool

SOURCE_DB = ROOT / "data" / "strategy-pool.sqlite3"
EVIDENCE_DIR = ROOT / ".acceptance-data"


def main() -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    test_db = EVIDENCE_DIR / f"pa-cycle-isolated-{stamp}.sqlite3"
    source = sqlite3.connect(SOURCE_DB)
    target = sqlite3.connect(test_db)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    pool = StrategyPool(test_db)
    now = datetime.now(timezone.utc)
    close = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    end = close
    bars = []
    for index in range(288):
        bar_close = end - timedelta(minutes=15 * (287 - index))
        bar_open = bar_close - timedelta(minutes=15)
        bars.append({
            "symbol": "BTC/USD", "interval": "15Min",
            "open_at": bar_open.isoformat(), "close_at": bar_close.isoformat(),
            "open": "100", "high": "100", "low": "100", "close": "100",
            "volume": "1", "trade_count": 1, "vwap": "100", "is_closed": True,
            "source": "BINANCE_USDM_PERP", "currency": "USD_RESEARCH",
        })
    quote = {
        "symbol": "BTC/USD", "bid": "100", "ask": "100.1",
        "event_at": now.isoformat(), "received_at": now.isoformat(), "age_ms": 0,
        "timestamp_valid": True, "source": "BINANCE_USDM_PERP",
        "currency": "USD_RESEARCH",
    }
    outcomes = pool.cycle(bars, close, quote=quote, quote_age_ms=0)
    accounts = pool.list()
    if len(outcomes) != 8 or len(accounts) != 8:
        raise RuntimeError(f"EXPECTED_8_PA_ACCOUNTS:{len(outcomes)}:{len(accounts)}")
    if any(item["last_cycle"] != close.isoformat() for item in accounts):
        raise RuntimeError("PA_DECISION_CYCLE_NOT_RECORDED_FOR_EACH_ACCOUNT")
    if any(item["cash"] != "10000.00" for item in accounts):
        raise RuntimeError("PA_ACCOUNT_INITIAL_EQUITY_MISMATCH")
    if any(pool.fills(item["id"]) for item in accounts):
        raise RuntimeError("ISOLATED_CHECK_CREATED_FILL")

    print(json.dumps({
        "mode": "ISOLATED_DETERMINISTIC_SAMPLE_NOT_NATURAL_MARKET_EVIDENCE",
        "cycle_close": close.isoformat(),
        "accounts": [{"strategy_id": item["id"], "initial_equity": "10000.00",
                      "cash": item["cash"], "status": item["last_status"],
                      "action": item["last_action"], "fills": len(pool.fills(item["id"]))}
                     for item in accounts],
        "test_database": str(test_db),
    }, indent=2))


if __name__ == "__main__":
    main()
