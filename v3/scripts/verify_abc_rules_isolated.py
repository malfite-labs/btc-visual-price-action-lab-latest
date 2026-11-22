"""Run the real A/B/C rule runner once against an isolated deterministic sample."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.ledger import Ledger
from backend.rule_cycle import RuleCycleRunner

SOURCE_DB = ROOT / "data" / "paper.sqlite3"
EVIDENCE_DIR = ROOT / ".acceptance-data"


def main() -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    test_db = EVIDENCE_DIR / f"abc-cycle-isolated-{stamp}.sqlite3"
    source = sqlite3.connect(SOURCE_DB)
    target = sqlite3.connect(test_db)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    ledger = Ledger(test_db)
    runner = RuleCycleRunner(ledger, test_db, enabled=True)
    now = datetime.now(timezone.utc)
    close = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    bars = []
    for index in range(288):
        bar_close = close - timedelta(minutes=15 * (287 - index))
        bar_open = bar_close - timedelta(minutes=15)
        bars.append({
            "symbol": "BTC/USD", "interval": "15Min",
            "open_at": bar_open.isoformat(), "close_at": bar_close.isoformat(),
            "open": "100", "high": "101", "low": "99", "close": "100",
            "volume": "1", "trade_count": 1, "vwap": "100", "is_closed": True,
            "source": "BINANCE_USDM_PERP", "currency": "USD_RESEARCH",
        })
    quote = {
        "symbol": "BTC/USD", "bid": "99.9", "ask": "100.1", "bid_qty": "1", "ask_qty": "1",
        "event_at": now.isoformat(), "received_at": now.isoformat(), "age_ms": 0,
        "timestamp_valid": True, "source": "BINANCE_USDM_PERP", "currency": "USD_RESEARCH",
    }
    result = asyncio.run(runner.run_cycle(
        candles=bars, candle_close=close, quote=quote, orderbook=None, now=now,
    ))
    accounts = {agent: ledger.account(agent) for agent in "ABC"}
    if set(result) != set("ABC") or any(result[a]["status"] != "VALID" for a in "ABC"):
        raise RuntimeError(f"ABC_RULE_CYCLE_FAILED:{result}")
    if any(accounts[a]["cash"] != "10000.00" for a in "ABC"):
        raise RuntimeError("ABC_INITIAL_EQUITY_MISMATCH")
    if any(ledger.fills(a) for a in "ABC"):
        raise RuntimeError("ISOLATED_CHECK_CREATED_FILL")

    print(json.dumps({
        "mode": "ISOLATED_DETERMINISTIC_SAMPLE_NOT_NATURAL_MARKET_EVIDENCE",
        "cycle_close": close.isoformat(),
        "strategies": [{"id": agent, "version": runner.profile[agent]["version"],
                        "action": result[agent]["action"], "cash": accounts[agent]["cash"],
                        "fills": len(ledger.fills(agent))}
                       for agent in "ABC"],
        "test_database": str(test_db),
    }, indent=2))


if __name__ == "__main__":
    main()
