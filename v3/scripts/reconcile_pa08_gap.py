"""One-time, auditable PA-08 candle-proxy gap reconciliation for internal paper mode."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.strategy_pool import StrategyPool

STRATEGY_ID = "pa-three-bar-hhhl"
ACCEPTED_STANDARD = "CANDLE_OHLC_PROXY_V1"


def get_json(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:8011{path}", timeout=5) as response:
        return json.load(response)


def run(audit_dir: Path) -> dict:
    audit_dir.mkdir(parents=True, exist_ok=True)
    system = get_json("/api/v1/system/status")
    payload = get_json("/api/v1/strategy-pool")
    account = next(item for item in payload["items"] if item["id"] == STRATEGY_ID)
    if not (system.get("scheduler") == "ENABLED_INTERNAL_PAPER"
            and system.get("paper_automation") == "ENABLED_INTERNAL_PAPER"
            and system.get("feed_health") == "LIVE" and system.get("websocket") == "CONNECTED"
            and system.get("websocket_quote_health") == "FRESH"
            and system.get("stop_monitor") == "QUOTE_EVENT_INTERNAL_PAPER_STOP_MONITOR"):
        raise RuntimeError("MARKET_OR_STOP_MONITOR_NOT_HEALTHY")
    if not account.get("enabled") or account.get("last_status") != "BLOCKED_GAP_RECONCILIATION_REQUIRED":
        raise RuntimeError("PA08_NOT_IN_EXPECTED_BLOCKED_STATE")
    position = json.loads(account["position"])
    start = datetime.fromisoformat(account["last_cycle"].replace("Z", "+00:00")).astimezone(timezone.utc)
    end = datetime.fromisoformat(system["last_candle_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    if end <= start or (end - start).total_seconds() % 900:
        raise RuntimeError("INVALID_GAP_RANGE")
    archive = ROOT / "data" / "market-candles.sqlite3"
    uri = archive.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("CANDLE_ARCHIVE_INTEGRITY_FAILED")
        bars = [json.loads(row["payload"]) for row in db.execute(
            "SELECT payload FROM candles WHERE symbol=? AND open_at>=? AND open_at<? ORDER BY open_at",
            ("BTC/USD", start.isoformat(), end.isoformat()),
        )]
    expected = int((end - start).total_seconds() // 900)
    if len(bars) != expected:
        raise RuntimeError(f"CANDLE_COUNT_MISMATCH:{len(bars)}!={expected}")
    prev = start
    highs: list[Decimal] = []
    lows: list[Decimal] = []
    zero_volume: list[str] = []
    for bar in bars:
        opened = datetime.fromisoformat(bar["open_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
        closed = datetime.fromisoformat(bar["close_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
        if (opened != prev or closed - opened != timedelta(minutes=15) or bar.get("is_closed") is not True
                or bar.get("symbol") != "BTC/USD" or bar.get("interval", "").lower() not in ("15m", "15min")
                or bar.get("source") != "ALPACA_US_BTCUSD_SPOT" or bar.get("currency") != "USD_RESEARCH"):
            raise RuntimeError("CANDLE_GAP_OR_SOURCE_INVALID")
        values = {key: Decimal(str(bar[key])) for key in ("open", "high", "low", "close", "volume")}
        if (min(values[key] for key in ("open", "high", "low", "close")) <= 0
                or values["high"] < max(values["open"], values["close"], values["low"])
                or values["low"] > min(values["open"], values["close"], values["high"])):
            raise RuntimeError("CANDLE_OHLC_INVALID")
        if values["volume"] == 0:
            zero_volume.append(bar["open_at"])
        highs.append(values["high"])
        lows.append(values["low"])
        prev = closed
    if prev != end:
        raise RuntimeError("LAST_CANDLE_DOES_NOT_MATCH_SYSTEM_WATERMARK")
    stop = Decimal(position["stop"])
    target = Decimal(position["target"])
    stop_hit = max(highs) >= stop if position["direction"] == "SHORT" else min(lows) <= stop
    target_hit = min(lows) <= target if position["direction"] == "SHORT" else max(highs) >= target
    bars_held_after = int(position["bars_held"]) + len(bars)
    max_hold = int(position["max_hold_bars"])
    if stop_hit or target_hit or bars_held_after >= max_hold:
        raise RuntimeError("CANDLE_PROXY_INDICATES_EXIT_OR_TIME_LIMIT")

    strategy_pool = StrategyPool(ROOT / "data" / "strategy-pool.sqlite3")
    before = strategy_pool.fills(STRATEGY_ID, limit=10000)
    before_position = json.loads(account["position"])
    current_open = [fill for fill in before if fill["cycle"] == position["opened_cycle"]
                    and fill["side"] == ("BUY" if position["direction"] == "LONG" else "SELL")
                    and Decimal(fill["qty"]) == Decimal(position["qty"])
                    and fill["reason"] in ("ENTRY", "ENTRY_NEXT_OPEN")]
    if len(current_open) != 1:
        raise RuntimeError("ACTIVE_POSITION_OPEN_FILL_NOT_UNIQUE")
    if any(datetime.fromisoformat(fill["cycle"].replace("Z", "+00:00")) > start
           and datetime.fromisoformat(fill["cycle"].replace("Z", "+00:00")) <= end for fill in before):
        raise RuntimeError("FILL_EVENT_EXISTS_DURING_GAP")
    if any(fill["cycle"] > position["opened_cycle"] for fill in before):
        raise RuntimeError("POST_OPEN_FILL_REQUIRES_MANUAL_RECONCILIATION")
    idempotency_keys = [fill["idempotency_key"] for fill in before]
    if len(idempotency_keys) != len(set(idempotency_keys)):
        raise RuntimeError("DUPLICATE_FILL_IDEMPOTENCY_KEY")

    evidence = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "standard": ACCEPTED_STANDARD,
        "user_acceptance": "15m candle high/low gap proxy accepted; absent bid/ask ticks may create paper simulation error.",
        "system_status": {key: system.get(key) for key in ("scheduler", "paper_automation", "feed_health", "websocket", "websocket_quote_health", "stop_monitor", "last_candle_at")},
        "strategy_id": STRATEGY_ID,
        "version": account["active_version"],
        "from_last_natural_cycle": start.isoformat(),
        "through_latest_archived_close": end.isoformat(),
        "bar_count": len(bars), "expected_count": expected,
        "bars_sha256": hashlib.sha256(json.dumps(bars, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
        "zero_volume_bars": zero_volume,
        "max_high": str(max(highs)), "min_low": str(min(lows)),
        "stop_hit": stop_hit, "target_hit": target_hit,
        "bars_held_before": int(position["bars_held"]), "bars_held_after": bars_held_after, "max_hold_bars": max_hold,
        "position_before": before_position,
        "current_open_fill": current_open[0],
        "fill_count_before": len(before), "no_gap_fills": True,
        "historical_ledger_note": "Legacy unpaired entry-only rows from earlier censored periods are preserved; active position matches one exact latest opening fill. No old rows were altered.",
    }
    (audit_dir / "pre-reconciliation-evidence.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    result = strategy_pool.reconcile_candle_gap(
        STRATEGY_ID, expected_last_cycle=start, through_cycle=end, bars=bars,
        expected_position=position, accepted_standard=ACCEPTED_STANDARD,
    )
    after = next(item for item in strategy_pool.list() if item["id"] == STRATEGY_ID)
    after_position = json.loads(after["position"])
    after_fills = strategy_pool.fills(STRATEGY_ID, limit=10000)
    if (after["last_cycle"] != account["last_cycle"] or after_position["entry"] != position["entry"]
            or after_position["stop"] != position["stop"] or after_position["target"] != position["target"]
            or after_position["qty"] != position["qty"] or after_position["direction"] != position["direction"]
            or after_position["bars_held"] != bars_held_after or before != after_fills):
        raise RuntimeError("POST_RECONCILIATION_POSITION_OR_LEDGER_CHANGED_UNEXPECTEDLY")
    (audit_dir / "reconciliation-result.json").write_text(json.dumps({
        "result": result, "position_after": after_position, "last_cycle_unchanged": after["last_cycle"],
        "fills_unchanged": len(before) == len(after_fills),
        "audit_record": strategy_pool.gap_reconciliations(STRATEGY_ID)[-1],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.audit_dir), indent=2, ensure_ascii=False))
