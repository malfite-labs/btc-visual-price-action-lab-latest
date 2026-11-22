"""One-shot read-only Alpaca BTC/USD validation. Never prints credentials or raw payloads."""

import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.market import AlpacaMarketData


async def main():
    client = AlpacaMarketData()
    out = {
        "source": client.source,
        "symbol": client.symbol,
        "market_type": "spot",
        "currency": "USD_RESEARCH",
        "tested_at_utc": datetime.now(UTC).isoformat(),
        "credentials_present": bool(client.credentials),
        "rest_bars": "NOT_TESTED",
        "rest_quote": "NOT_TESTED",
        "rest_orderbook": "NOT_TESTED",
        "websocket": "NOT_TESTED",
    }
    if not client.credentials:
        out["reason"] = "AUTHORIZED_KEY_VARIABLES_EMPTY_OR_PATH_UNAVAILABLE"
        print(json.dumps(out, indent=2))
        return
    started = time.perf_counter()
    try:
        end = datetime.now(UTC)
        bars = await client.bars(end - timedelta(days=3, minutes=20), end, "15Min")
        closed = [x for x in bars if x.is_closed]
        times = [datetime.fromisoformat(x.open_at) for x in closed]
        duplicates = len(times) - len(set(times))
        gaps = sum(1 for a, b in zip(times, times[1:]) if (b - a).total_seconds() != 900)
        out.update(
            {
                "rest_bars": "PASS",
                "bars_returned": len(bars),
                "closed_bars": len(closed),
                "zero_volume_bars": sum(1 for x in closed if float(x.volume) == 0),
                "duplicate_open_times": duplicates,
                "non_15m_intervals": gaps,
                "latest_closed_at": closed[-1].close_at if closed else None,
                "bars_request_latency_ms": round((time.perf_counter() - started) * 1000),
            }
        )
    except Exception as exc:
        out.update({"rest_bars": "FAIL", "error_class": type(exc).__name__})
    try:
        started = time.perf_counter()
        quote = await client.latest_quote()
        out.update(
            {
                "rest_quote": "PASS",
                "quote_age_ms": quote["age_ms"],
                "quote_request_latency_ms": round((time.perf_counter() - started) * 1000),
                "bid_present": bool(quote["bid"]),
                "ask_present": bool(quote["ask"]),
                "bid_size_present": bool(quote["bid_qty"]),
                "ask_size_present": bool(quote["ask_qty"]),
                "event_at": quote["event_at"],
                "received_at": quote["received_at"],
            }
        )
    except Exception as exc:
        out.update({"rest_quote": "FAIL", "error_class": type(exc).__name__})
    try:
        started = time.perf_counter()
        book = await client.orderbook()
        out.update(
            {
                "rest_orderbook": "PASS",
                "bid_levels": len(book["bids"]),
                "ask_levels": len(book["asks"]),
                "orderbook_request_latency_ms": round((time.perf_counter() - started) * 1000),
                "orderbook_event_at": book.get("event_at"),
            }
        )
    except Exception as exc:
        out.update({"rest_orderbook": "FAIL", "error_class": type(exc).__name__})
    stop = asyncio.Event()
    seen_events = []

    async def capture_event(event):
        if event.get("T") in {"q", "b"}:
            seen_events.append(event.get("T"))

    task = asyncio.create_task(client.websocket(capture_event, stop))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if client.status.get("websocket") == "CONNECTED" and seen_events:
            out["websocket"] = "PASS"
            out["websocket_first_event_type"] = "quote" if seen_events[0] == "q" else "bar"
            break
        if client.status.get("websocket") == "MODEL_NOT_CONFIGURED":
            out["websocket"] = "FAIL"
            break
        await asyncio.sleep(0.2)
    if out["websocket"] == "NOT_TESTED":
        out["websocket"] = "FAIL"
        out["websocket_error_class"] = client.status.get("last_error") or "TIMEOUT"
        out["websocket_auth_reply_types"] = client.status.get("websocket_auth_reply_types")
        out["websocket_auth_error_code"] = client.status.get("websocket_auth_error_code")
        out["websocket_sub_reply_types"] = client.status.get("websocket_sub_reply_types")
        out["websocket_sub_error_code"] = client.status.get("websocket_sub_error_code")
    stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # The private credential tuple is dereferenced before exit; values are never serialized.
    client.credentials = None
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
