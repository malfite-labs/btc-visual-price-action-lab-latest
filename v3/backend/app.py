from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import monotonic

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.charts import render_ai_snapshot
from backend.candle_archive import CandleArchive, utc as archive_utc
from backend.evolution import ReviewCoordinator
from backend.ledger import Ledger
from backend.market import (
    BinanceMarketData,
    _StreamLease,
    candle_hash,
    claim_closed_bar_for_dispatch,
    market_open_gate_reason,
    quote_timing,
    websocket_quote_health,
)
from backend.orchestration import PaperCycleRunner
from backend.scheduler import PaperCycleScheduler
from backend.rule_cycle import RuleCycleRunner, load_profile
from backend.settings import (
    C_APPROVED_STRATEGY_SHA256,
    C_RESEARCH_MAINTENANCE_RATE,
    COST_MODEL,
    INITIAL_EQUITY,
    PAPER_CURRENCY,
)
from backend.strategy import StrategyRegistry, present_e_runtime
from backend.pa_extension import PAExtension, NAMES as PA_EXTENSION_NAMES
from backend.vision import (
    AgentOrchestrator,
    VisionProviderConfig,
    public_vision_status,
)
from backend.vision_provider import ConfigurableVisionAdapter

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.getenv("BTC_LAB_DATA_DIR", str(ROOT / "data"))).resolve()
if os.getenv("BTC_LAB_TEST_MODE", "false").lower() == "true" and not os.getenv("BTC_LAB_DATA_DIR"):
    raise RuntimeError("TEST_DATA_ROOT_REQUIRED")
DB_PATH = os.getenv("BTC_LAB_DB", str(DATA_ROOT / "paper.sqlite3"))

# Acquire ownership before constructing any database-backed component.
# Test subprocesses use their isolated data root; production copies must run
# from the one designated deployment directory and share its OS file lock.
if os.getenv('BTC_LAB_TEST_MODE', 'false').lower() == 'true':
    _runtime_lock_path = DATA_ROOT / 'v3-backend-instance.lock'
else:
    _official_root = (Path.home() / 'Documents' / '量化升级版' / 'btc-paper-lab-v3' / 'windows-binance-deploy').resolve()
    if ROOT.resolve() != _official_root:
        raise RuntimeError('V3_OFFICIAL_DEPLOYMENT_REQUIRED')
    _runtime_lock_path = _official_root / '.run' / 'v3-backend-instance.lock'
_runtime_lease = _StreamLease(_runtime_lock_path)
if not _runtime_lease.acquire():
    raise RuntimeError('V3_BACKEND_ALREADY_RUNNING: ' + _runtime_lease.failure_reason)


def _load_project_deepseek_key() -> None:
    """Load only this project's ignored DeepSeek key without emitting or persisting it elsewhere."""
    if os.getenv("DEEPSEEK_API_KEY"):
        return
    secret_file = ROOT / ".env"
    if not secret_file.is_file():
        return
    for line in secret_file.read_text(encoding="utf-8-sig").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == "DEEPSEEK_API_KEY":
            os.environ.setdefault("DEEPSEEK_API_KEY", value.strip().strip("\"'"))
            return


def _stored_image_capability(model: str, base_url: str, endpoint_path: str) -> bool:
    marker = DATA_ROOT / "vision-capability.json"
    if not marker.is_file():
        return False
    try:
        import json

        evidence = json.loads(marker.read_text(encoding="utf-8"))
        return bool(
            evidence.get("verified") is True
            and evidence.get("model") == model
            and evidence.get("endpoint") == base_url.rstrip("/") + endpoint_path
            and isinstance(evidence.get("verified_at_utc"), str)
            and len(evidence.get("image_sha256", "")) == 64
        )
    except (OSError, ValueError, TypeError):
        return False


_load_project_deepseek_key()
ledger = Ledger(DB_PATH, mirror_enabled=True)
from backend.g_mirror import GMirror
g_mirror = GMirror(DATA_ROOT / "g" / "mirror.sqlite3", cost=COST_MODEL)
ledger.set_mirror_dispatcher(g_mirror.handle_c_fill)
archive_path = os.getenv('BTC_LAB_CANDLE_DB', str(Path(DB_PATH).with_name('market-candles.sqlite3')))
candle_archive = CandleArchive(archive_path)
decision_store = AgentOrchestrator(DB_PATH)
strategies = StrategyRegistry(
    ROOT / "strategies", activate_c=True, c_expected_sha256=C_APPROVED_STRATEGY_SHA256
)
evolution = ReviewCoordinator(DB_PATH)
market = BinanceMarketData(
    stream_lock_path=ROOT / ".run" / "binance-stream.lock",
    recovery_history_path=ROOT / ".run" / "binance-stream-recovery.jsonl",
)
vision_config = VisionProviderConfig.from_env()
vision_adapter = ConfigurableVisionAdapter(vision_config)
vision_adapter.image_capability_verified = _stored_image_capability(
    vision_config.model, vision_config.base_url, vision_config.endpoint_path
)
# The user froze this research model; separate explicit arming is still required.
paper_cost_approved = True
paper_automation_requested = os.getenv("PAPER_AUTOMATION_ENABLED", "false").lower() == "true"


def _paper_automation_gate(config, adapter) -> tuple[bool, str]:
    if not adapter.configured:
        return False, "MODEL_NOT_CONFIGURED_OR_UNVERIFIED"
    budget = getattr(adapter, "_budget", None)
    if budget is not None and budget.fault():
        return False, "MODEL_PROVIDER_AUTH_BLOCKED"
    if (
        config.model != "deepseek-flash"
        or config.base_url.rstrip("/") != "https://api.deepseek.com"
        or config.endpoint_path != "/chat/completions"
        or config.thinking_mode != "disabled"
    ):
        return False, "MODEL_CONFIGURATION_MISMATCH"
    if (
        config.total_budget_usd is None
        or config.total_budget_usd > Decimal("5")
        or config.budget_db_path == ":memory:"
    ):
        return False, "CUMULATIVE_BUDGET_NOT_SAFE"
    try:
        Path(config.budget_db_path).resolve().relative_to((DATA_ROOT).resolve())
    except ValueError:
        return False, "BUDGET_PATH_OUTSIDE_PROJECT"
    return True, "READY"


def _formal_mode():
    path = DATA_ROOT / 'formal-mode.json'
    if not path.exists():
        return 'VISUAL'
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if data.get('mode') in ('VISUAL', 'RULE_DRIVEN'):
            return data['mode']
    except (OSError, ValueError, TypeError):
        pass
    return 'INVALID'

formal_mode = _formal_mode()
if not paper_automation_requested:
    paper_gate_ok, paper_gate_reason = False, 'NOT_ARMED'
elif formal_mode == 'RULE_DRIVEN':
    try:
        load_profile()
        paper_gate_ok, paper_gate_reason = True, 'RULE_PROFILE_READY'
    except Exception:
        paper_gate_ok, paper_gate_reason = False, 'RULE_PROFILE_INVALID'
elif formal_mode == 'VISUAL':
    paper_gate_ok, paper_gate_reason = _paper_automation_gate(vision_config, vision_adapter)
else:
    paper_gate_ok, paper_gate_reason = False, 'FORMAL_MODE_INVALID'
paper_scheduler_lease = (
    _StreamLease(ROOT / ".run" / "paper-scheduler.lock") if paper_gate_ok else None
)
if paper_scheduler_lease is not None and not paper_scheduler_lease.acquire():
    paper_gate_ok, paper_gate_reason = False, "PAPER_SCHEDULER_ALREADY_RUNNING"
paper_automation_enabled = paper_automation_requested and paper_gate_ok
c_strategy_loaded = strategies.current_strategy("C") is not None
paper_runner = (
    RuleCycleRunner(ledger, DB_PATH, COST_MODEL, enabled=paper_automation_enabled and paper_cost_approved)
    if formal_mode == 'RULE_DRIVEN' and paper_gate_ok else
    PaperCycleRunner(ledger, decision_store, COST_MODEL,
        enabled=paper_automation_enabled and paper_cost_approved,
        cost_model_approved=paper_cost_approved, enable_c_5x=c_strategy_loaded,
        c_maintenance_rate=C_RESEARCH_MAINTENANCE_RATE if c_strategy_loaded else None)
)
paper_scheduler = PaperCycleScheduler(
    DB_PATH,
    paper_runner,
    enabled=paper_automation_enabled and paper_cost_approved,
    # Protection must survive strategy configuration failure and process restart.
    c_maintenance_rate=C_RESEARCH_MAINTENANCE_RATE,
)
candles: list[dict] = []
market_status = {
    "rest": "NOT_TESTED",
    "websocket": "NOT_TESTED",
    "feed_health": "UNKNOWN",
    "source": "BINANCE_USDM_PERP",
    "currency": "USD_RESEARCH",
    "model": "MODEL_NOT_CONFIGURED",
    "mode": "INTERNAL_PAPER_LEDGER",
    "LIVE_TRADING_DISABLED": True,
}
clients: set[WebSocket] = set()
stop_event = asyncio.Event()
tasks: list[asyncio.Task] = []
lifespan_active = False
market_task_startup = "NOT_STARTED"


async def broadcast(data: dict):
    dead = []
    for ws in clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


_e_scheduler_started_at = datetime.now(UTC)
_e_market_tasks: set[asyncio.Task] = set()


async def market_loop():
    global candles
    if not market.credentials:
        market_status["feed_health"] = "CREDENTIALS_NOT_CONFIGURED"
        return
    while not stop_event.is_set():
        try:
            end = datetime.now(UTC)
            fetched = await market.bars(end - timedelta(days=4), end)
            unique = {c.open_at: c for c in fetched if c.is_closed}
            candles = [unique[k].__dict__ for k in sorted(unique)]
            try:
                candle_archive.store(candles)
                market_status['candle_archive'] = 'OK'
            except (OSError, sqlite3.Error, ValueError) as exc:
                market_status['candle_archive'] = 'ERROR:' + type(exc).__name__
            market_status["rest"] = "PASS"
            market_status["last_candle_at"] = candles[-1]["close_at"] if candles else None
            market_status["candles_count"] = len(candles)
            market_status["candle_sha256"] = (
                candle_hash(
                    [__import__("backend.market", fromlist=["Candle"]).Candle(**c) for c in candles]
                )
                if candles
                else None
            )
            tail = candles[-288:]
            gap_count = sum(
                1
                for left, right in zip(tail, tail[1:])
                if (
                    datetime.fromisoformat(right["open_at"])
                    - datetime.fromisoformat(left["open_at"])
                ).total_seconds()
                != 900
            )
            market_status["gap_count_288"] = gap_count
            market_status["feed_health"] = (
                "LIVE" if len(tail) == 288 and gap_count == 0 else "DATA_INCOMPLETE"
            )
            stream_quote = market.status.get("last_quote")
            stream_health = websocket_quote_health(
                market.status.get("websocket", "UNKNOWN"),
                market.status.get("websocket_last_quote_received_at"),
                datetime.now(UTC),
                timestamp_valid=bool(stream_quote and stream_quote.get("timestamp_valid")),
                last_quote_event_at=(stream_quote or {}).get("event_at"),
            )
            market.confirm_market_recovered(
                quote_fresh=stream_health == "FRESH",
                candles_complete=(len(tail) == 288 and gap_count == 0),
                candles_gap_count=gap_count,
            )
            await broadcast(
                {
                    "type": "candles",
                    "count": len(candles),
                    "last_close": market_status["last_candle_at"],
                    "health": market_status["feed_health"],
                }
            )
            if candles:
                last_close = datetime.fromisoformat(candles[-1]["close_at"])
                if last_close.tzinfo is None:
                    last_close = last_close.replace(tzinfo=UTC)
                # E claims the same closed snapshot independently of ABC's watermark.
                e_dispatch=globals().get('_schedule_e_market_from_loop')
                if e_dispatch is not None:e_dispatch(candles,last_close)
                d_dispatch=globals().get('_schedule_d_market_from_loop')
                if d_dispatch is not None:d_dispatch(last_close)
                dispatchable, dispatch_reason = claim_closed_bar_for_dispatch(
                    last_close, ROOT / ".run" / "market-decision-watermark.json"
                )
                market_status["decision_dispatch"] = dispatch_reason
                if dispatchable:
                    await process_closed_bar(last_close)
        except Exception as exc:
            market_status["rest"] = "FAIL"
            market_status["feed_health"] = "STALE"
            market_status["last_error"] = type(exc).__name__
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except TimeoutError:
            pass


async def quote_loop():
    while not stop_event.is_set():
        if market.credentials:
            try:
                quote = await market.latest_quote()
                market_status["quote"] = quote
                market_status["rest_quote"] = "PASS"
                await process_stop_quote(quote)
                await broadcast({"type": "quote", "quote": quote})
            except Exception as exc:
                market_status["rest_quote"] = "FAIL"
                market_status["quote_error"] = type(exc).__name__
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2)
        except TimeoutError:
            pass


def _fail_d_dispatch(bridge, reason):
    try:
        bridge.fail_dispatch(reason)
    except Exception:
        logging.getLogger(__name__).exception('D failure latch failed')


async def stream_events(event: dict):
    market_status["websocket"] = market.status.get("websocket", "CONNECTED")
    if event.get("T") == "q":
        d_dispatched = False
        try:
            event_at = datetime.fromisoformat(event["t"])
            received = datetime.now(UTC)
            timing = quote_timing(event_at, received)
            quote = {
                "symbol": "BTC/USD",
                "bid": str(event["bp"]),
                "ask": str(event["ap"]),
                "bid_qty": str(event["bs"]),
                "ask_qty": str(event["as"]),
                "event_at": (
                    event_at.astimezone(UTC).isoformat()
                    if event_at.tzinfo
                    else event_at.isoformat()
                ),
                "received_at": received.isoformat(),
                **timing,
                "source": "BINANCE_USDM_PERP",
                "currency": "USD_RESEARCH",
            }
            market_status["quote"] = quote
            market_status["websocket_quote"] = (
                "PASS" if timing["timestamp_valid"] else "TIMESTAMP_INVALID"
            )
            if d_bridge is not None:
                try:
                    bid, ask = Decimal(str(event['bp'])), Decimal(str(event['ap']))
                    raw_valid = (event.get('S') == 'BTC/USD'
                                 and event.get('source') == 'BINANCE_USDM_PERP'
                                 and event.get('currency') == 'USD_RESEARCH'
                                 and event.get('timestamp_valid') is True
                                 and timing['timestamp_valid'] is True
                                 and bid.is_finite() and ask.is_finite() and 0 < bid <= ask)
                    if not raw_valid:
                        _fail_d_dispatch(d_bridge, 'D_RAW_QUOTE_INVALID')
                    elif not d_bridge.submit_quote(quote):
                        _fail_d_dispatch(d_bridge, 'D_QUOTE_SUBMIT_FAILED')
                except (KeyError, TypeError, ValueError, ArithmeticError, InvalidOperation):
                    _fail_d_dispatch(d_bridge, 'D_RAW_QUOTE_INVALID')
                except Exception:
                    logging.getLogger(__name__).exception('D quote dispatch failed')
                    _fail_d_dispatch(d_bridge, 'D_QUOTE_SUBMIT_FAILED')
                d_dispatched = True
            await process_stop_quote(quote)
        except Exception as exc:
            if d_bridge is not None and not d_dispatched:
                _fail_d_dispatch(d_bridge, 'D_QUOTE_PARSE_FAILED')
            market_status["websocket_quote"] = "PARSE_ERROR"
            market_status["quote_error"] = type(exc).__name__
    elif event.get("T") == "error":
        market_status["websocket"] = "ERROR"
    await broadcast({"type": "market_event", "event": event})


async def process_stop_quote(quote: dict) -> None:
    """Run persistent stop monitoring and pending-stop recovery on quote events, without AI."""
    if quote.get("source") != market.source:
        return
    try:
        await asyncio.to_thread(g_mirror.monitor_quote, quote)
        ledger.schedule_mirror_outbox()
    except Exception:
        # G is isolated: its emergency exit must never disrupt C's existing protection loop.
        logging.getLogger(__name__).exception("G mirror risk monitor failed")
    try:
        await asyncio.to_thread(strategy_pool.monitor_quote, quote,
                                 max_age_ms=COST_MODEL.max_quote_age_ms,
                                 fee_rate=Decimal(str(COST_MODEL.fee_rate)),
                                 slippage=Decimal(str(COST_MODEL.adverse_slippage_bps))/Decimal(10000))
    except Exception:
        # Pool failures must not block the existing ABC/E protective quote path.
        logging.getLogger(__name__).exception('Isolated strategy-pool stop monitor failed')
    for sid in pa_extension.ledgers:
        try:
            await asyncio.to_thread(pa_extension.ledger_quote, sid, _quote_with_live_age(quote), _pa_quote_entries_allowed)
        except Exception:
            logging.getLogger(__name__).exception('%s isolated quote monitor failed', sid)
    event_material = "|".join(
        str(quote.get(key, "")) for key in ("event_at", "bid", "ask", "source")
    )
    event_id = "quote:" + hashlib.sha256(event_material.encode()).hexdigest()
    event_ids = {agent: event_id for agent in "ABC"}
    outcomes = paper_scheduler.monitor_stops(event_ids=event_ids, quote=quote, cost=COST_MODEL)
    recovery = paper_scheduler.recover_stops(quote=quote, cost=COST_MODEL)
    try:
        from backend.e_protection import protect_e_accounts
        e_protection = protect_e_accounts({"formal": DATA_ROOT / "e" / "formal.sqlite3",
                                           "shadow": DATA_ROOT / "e" / "shadow.sqlite3"}, quote, COST_MODEL)
        for kind, outcome in e_protection.items():
            if outcome.get("reason") == "PROTECTION_ERROR":
                logging.getLogger(__name__).error("E protection failed: %s %s", kind, outcome.get("error_class"))
    except Exception:
        # E protection errors must not disrupt the existing quote event loop.
        logging.getLogger(__name__).exception("E protection dispatch failed")
    if formal_mode == 'RULE_DRIVEN' and isinstance(paper_runner, RuleCycleRunner):
        paper_runner.monitor_targets(quote)
        from backend.recovery_guard import recovery_blocked
        if (quote.get('timestamp_valid') is True and 0 <= int(quote.get('age_ms', 10**9)) <= COST_MODEL.max_quote_age_ms
                and not recovery_blocked(ledger, paper_runner)):
            market_status['startup_position_monitor_checked'] = True
    for agent in set(outcomes) | set(recovery):
        row = outcomes.get(agent) or recovery.get(agent)
        if row and row.get("accepted"):
            await broadcast({"type": "ledger_changed", "agent": agent})


def _valid_cycle_candle(row: dict) -> bool:
    try:
        opened = datetime.fromisoformat(str(row["open_at"]).replace("Z", "+00:00"))
        ended = datetime.fromisoformat(str(row["close_at"]).replace("Z", "+00:00"))
        return bool(
            row.get("source") == "BINANCE_USDM_PERP"
            and row.get("currency") == "USD_RESEARCH"
            and row.get("symbol") == "BTC/USD"
            and str(row.get("interval", "")).lower() in {"15min", "15m"}
            and opened.tzinfo is not None
            and ended.tzinfo is not None
            and ended.astimezone(UTC) - opened.astimezone(UTC) == timedelta(minutes=15)
        )
    except (TypeError, ValueError, KeyError):
        return False


def _closed_cycle_candles(rows: list[dict], close: datetime) -> list[dict]:
    """Select a contiguous closed window ending at this cycle, excluding later data."""
    if close.tzinfo is None or close.utcoffset() is None:
        raise ValueError("CYCLE_TIME_INVALID")
    selected = []
    for row in rows:
        if row.get("is_closed") is not True:
            continue
        try:
            ended = datetime.fromisoformat(str(row["close_at"]).replace("Z", "+00:00"))
            if ended.tzinfo is None or ended.utcoffset() is None:
                raise ValueError("CANDLE_TIME_INVALID")
            ended = ended.astimezone(UTC)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("CANDLE_TIME_INVALID") from exc
        if ended <= close.astimezone(UTC):
            selected.append((ended, row))
    selected.sort(key=lambda pair: pair[0])
    window = selected[-288:]
    if len(window) != 288 or window[-1][0] != close.astimezone(UTC):
        raise ValueError("CLOSED_WINDOW_INCOMPLETE")
    if any(b[0] - a[0] != timedelta(minutes=15) for a, b in zip(window, window[1:])):
        raise ValueError("CLOSED_WINDOW_GAP_OR_DUPLICATE")
    return [row for _, row in window]


def _dispatch_e_market_snapshot(rows,close,*,now,schedule=True):
    """Claim E's event separately; an uncertain prior claim is never replayed."""
    from contextlib import closing
    from backend.e_visual import resolve_e_decision
    if close.tzinfo is None or now.tzinfo is None or not close.astimezone(UTC)<=now.astimezone(UTC)<close.astimezone(UTC)+timedelta(minutes=15):
        return 'CYCLE_EXPIRED'
    window=_closed_cycle_candles(rows,close)
    if any(not _valid_cycle_candle(row) for row in window):return 'CANDLE_SOURCE_OR_INTERVAL_MISMATCH'
    resolve_e_decision({'action':'SKIP'},window,mapping_verified=False)
    path=DATA_ROOT/'e'/'formal.sqlite3'
    if not path.exists():return 'E_LEDGER_MISSING'
    from backend.e_paper_store import EPaperStore
    store=EPaperStore(path)
    key=close.astimezone(UTC).isoformat()
    digest=hashlib.sha256(json.dumps(window,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str).encode('utf8')).hexdigest()
    with closing(sqlite3.connect(store.path,timeout=10)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS e_market_dispatch(close_at TEXT PRIMARY KEY,snapshot_sha256 TEXT NOT NULL,status TEXT NOT NULL)')
        if not db.execute('INSERT OR IGNORE INTO e_market_dispatch VALUES(?,?,?)',(key,digest,'CLAIMED')).rowcount:return 'DUPLICATE_BAR'
    if schedule:_schedule_e_observation([dict(row) for row in window],close)
    return 'DISPATCHED'


async def _dispatch_e_market_async(rows,close):
    try:
        result=await asyncio.to_thread(_dispatch_e_market_snapshot,rows,close,now=datetime.now(UTC),schedule=False)
        if result=='DISPATCHED':
            if not _schedule_e_observation(_closed_cycle_candles(rows,close),close):
                def mark_failed():
                    from contextlib import closing
                    with closing(sqlite3.connect(DATA_ROOT/'e'/'formal.sqlite3',timeout=10)) as db,db:
                        db.execute("UPDATE e_market_dispatch SET status='SCHEDULING_FAILED' WHERE close_at=?",(close.astimezone(UTC).isoformat(),))
                await asyncio.to_thread(mark_failed)
    except Exception:
        logging.getLogger(__name__).exception('E independent market task failed')


def _schedule_e_market_from_loop(rows,close):
    try:
        if close.astimezone(UTC)<=_e_scheduler_started_at:return
        task=asyncio.create_task(_dispatch_e_market_async([dict(row) for row in rows],close))
        _e_market_tasks.add(task)
        task.add_done_callback(_e_market_tasks.discard)
    except Exception:
        logging.getLogger(__name__).exception('E market dispatch failed')


_d_market_tasks: set[asyncio.Task] = set()
d_bridge = None


def _d_feed_healthy():
    quote = market.status.get('last_quote') or {}
    return (market.status.get('websocket_recovery_state') == 'HEALTHY'
            and websocket_quote_health(
                market.status.get('websocket', 'UNKNOWN'),
                market.status.get('websocket_last_quote_received_at'), datetime.now(UTC),
                timestamp_valid=bool(quote.get('timestamp_valid')),
                last_quote_event_at=quote.get('event_at')) == 'FRESH')


def _d_candles_complete():
    return (market_status.get('feed_health') == 'LIVE'
            and market_status.get('gap_count_288') == 0
            and len(candles) >= 288)


def _schedule_d_market_from_loop(close):
    """Dispatch only current closed bars to the independent D observer."""
    if close.astimezone(UTC) <= _e_scheduler_started_at:
        return
    if d_bridge is not None:
        try:
            if not d_bridge.submit_bar(close):
                _fail_d_dispatch(d_bridge, 'D_BAR_SUBMIT_FAILED')
        except Exception:
            logging.getLogger(__name__).exception('D bar dispatch failed')
            _fail_d_dispatch(d_bridge, 'D_BAR_SUBMIT_FAILED')
        return
    async def observe():
        try:
            from backend.d_paper import observe_cycle
            await asyncio.to_thread(observe_cycle, DATA_ROOT/'d'/'formal.sqlite3', candle_archive, close, datetime.now(UTC))
        except Exception:
            logging.getLogger(__name__).exception('D market observation failed')
    task = asyncio.create_task(observe())
    _d_market_tasks.add(task)
    task.add_done_callback(_d_market_tasks.discard)


def _cycle_decision_context(account, metrics, quote, close, image_hash, candle_digest, data_mode):
    """Represent one observed quote and one isolated account without inventing inputs."""
    position = json.loads(account["position_json"]) if account.get("position_json") else {}
    bid, ask = Decimal(str(quote["bid"])), Decimal(str(quote["ask"]))
    event = datetime.fromisoformat(str(quote["event_at"]).replace("Z", "+00:00"))
    if (
        not bid.is_finite()
        or not ask.is_finite()
        or bid <= 0
        or ask < bid
        or event.tzinfo is None
        or event.utcoffset() is None
        or event.astimezone(UTC) < close.astimezone(UTC)
    ):
        raise ValueError("DECISION_QUOTE_INVALID")
    try:
        equity = Decimal(str(metrics.get("equity")))
        available = Decimal(str(metrics.get("available_cash")))
        funds_ok = equity.is_finite() and available.is_finite() and equity > 0 and available > 0
    except (InvalidOperation, ValueError, TypeError):
        funds_ok = False
    quote_ok = (
        quote.get("source") == "BINANCE_USDM_PERP"
        and quote.get("currency") == "USD_RESEARCH"
        and quote.get("timestamp_valid", True) is True
        and 0 <= int(quote.get("age_ms", -1)) <= COST_MODEL.max_quote_age_ms
    )
    flat = account["state"] == "FLAT"
    allowed = flat and funds_ok and quote_ok
    reason = (
        "RISK_CHECK_PENDING"
        if allowed
        else "POSITION_NOT_FLAT"
        if not flat
        else "ACCOUNT_UNAVAILABLE"
        if not funds_ok
        else "QUOTE_UNAVAILABLE_OR_STALE"
    )
    return {
        "position": account["state"],
        "entry_price": position.get("entry_price"),
        "stop_loss": position.get("stop_price"),
        "stop_state": account["state"],
        "account_version": account["version"],
        "equity": metrics.get("equity"),
        "available_cash": metrics.get("available_cash"),
        "bid": str(bid),
        "ask": str(ask),
        "mid_price": str((bid + ask) / 2),
        "quote_time_utc": event.astimezone(UTC).isoformat(),
        "quote_age_ms": quote.get("age_ms"),
        "candle_close_utc": close.astimezone(UTC).isoformat(),
        "image_sha256": image_hash,
        "candle_sha256": candle_digest,
        "market_source": quote.get("source"),
        "currency": quote.get("currency"),
        "data_mode": data_mode,
        "open_allowed": allowed,
        "open_reason": reason,
    }


_e_observation_tasks: set[asyncio.Task] = set()
_e_observer = None
_e_observe_adapter = None
_e_observe_version = None


async def _e_quant_preflight(closed: list[dict], candidate: dict) -> dict:
    """Evaluate an E-only candidate using the existing quote and paper risk rules."""
    from backend.e_paper_store import EPaperStore
    from backend.e_quant_gate import evaluate_quant_candidate

    quote = await market.latest_quote()
    store = EPaperStore(DATA_ROOT / 'e' / 'formal.sqlite3')
    return evaluate_quant_candidate(
        store=store, bars=closed, candidate=candidate, quote=quote,
        now=datetime.now(UTC), cost=COST_MODEL,
    )


def _e_strategy_runtime():
    """Live E mode for the strategy list. Missing files leave the registry record unchanged."""
    data = DATA_ROOT / "e"
    if not (data / "auto-paper.json").is_file():
        return None
    from backend.e_quant_observer import selected_observe_strategy
    enabled, _short = _e_auto_settings()
    return {
        "present": True,
        "mode": "AUTO_PAPER" if enabled else "NOT_AUTO",
        "version": selected_observe_strategy(data),
    }


def _e_auto_settings():
    try:
        auto=json.loads((DATA_ROOT/'e'/'auto-paper.json').read_text(encoding='utf8'))
        if set(auto)!={'mode','long_enabled','short_enabled'} or auto['mode']!='AUTO_PAPER' or auto['long_enabled'] is not True or type(auto['short_enabled']) is not bool:return False,False
        if not auto['short_enabled']:return True,False
    except (OSError,ValueError,TypeError,KeyError):return False,False
    try:
        risk=json.loads((DATA_ROOT/'e'/'short-risk.json').read_text(encoding='utf8'))
        return True,risk=={'leverage':'1','maintenance_rate':'0.01','risk_fraction':'0.02','authorized':True}
    except (OSError,ValueError,TypeError):return True,False


async def _run_e_auto_observation(closed, close, observer, adapter,allow_short=False):
    """E uses the existing local market and a private ledger; ABC failures stay isolated."""
    from backend.e_observer import dispatch_e_observation
    from backend.e_auto_cycle import run_e_auto_cycle
    from backend.e_paper_store import EPaperStore
    async def shadow_baseline(quote):
        shadow_path=DATA_ROOT/'e'/'shadow.sqlite3'
        try:
            setting=json.loads((DATA_ROOT/'e'/'shadow-config.json').read_text(encoding='utf8'))
            if setting!={'mode':'BASELINE','enabled':True} or not shadow_path.exists():return
            await run_e_auto_cycle(store=EPaperStore(shadow_path),bars=closed,close=datetime.now(UTC),quote=dict(quote),cost=COST_MODEL,allow_short=allow_short)
        except Exception:
            logging.getLogger(__name__).exception('E shadow baseline failed')
    async def explain():
        if observer is not None:
            await dispatch_e_observation(observer,closed,close,adapter)
            return
        from backend.e_quant_observer import EQuantObserver
        from backend.e_observer import create_e_observe_adapter
        target=EQuantObserver(DATA_ROOT/'e-quant-observations.sqlite3',DATA_ROOT/'snapshots'/'E-V1.1',strategies)
        visual=create_e_observe_adapter(DATA_ROOT/'e',vision_adapter.image_capability_verified,strategy_version='E-V1.1-QUANT')
        await dispatch_e_observation(target,closed,close,visual or vision_adapter)
    path=DATA_ROOT / 'e' / 'formal.sqlite3'
    if not path.exists():
        logging.getLogger(__name__).error('E auto ledger is missing')
        return
    try:
        meta=json.loads((ROOT/'strategies'/'E'/'E-V1.1-QUANT.metadata.json').read_text(encoding='utf8'))
        addendum=meta.get('structure_addendum') or {}
        if (addendum.get('file')!='E-V1.1-STRUCTURE-ADDENDUM.md' or
            hashlib.sha256((ROOT/'strategies'/'E'/addendum['file']).read_bytes()).hexdigest()!=addendum.get('sha256') or
            not any(x.get('agent')=='E' and x.get('candidate_version')=='E-V1.1-QUANT' for x in strategies.list())):
            logging.getLogger(__name__).error('E strategy integrity check failed')
            return
        stream_quote=market.status.get('last_quote') or {}
        health=websocket_quote_health(market.status.get('websocket','UNKNOWN'),market.status.get('websocket_last_quote_received_at'),datetime.now(UTC),timestamp_valid=bool(stream_quote.get('timestamp_valid')),last_quote_event_at=stream_quote.get('event_at'))
        gate=market_open_gate_reason(feed_health=market_status.get('feed_health','UNKNOWN'),gap_count=market_status.get('gap_count_288'),candle_set_valid=bool(closed) and all(_valid_cycle_candle(row) for row in closed),websocket_state=market.status.get('websocket','UNKNOWN'),recovery_state=market.status.get('websocket_recovery_state','UNKNOWN'),websocket_quote_health_status=health)
        if gate:
            logging.getLogger(__name__).warning('E auto cycle blocked: %s',gate)
            return
        quote=await market.latest_quote()
        quote['event_id']='binance-quote:'+str(quote.get('event_at',''))
        shadow_task=asyncio.create_task(shadow_baseline(quote))
        _e_observation_tasks.add(shadow_task)
        shadow_task.add_done_callback(_e_observation_tasks.discard)
        await run_e_auto_cycle(store=EPaperStore(path),bars=closed,close=datetime.now(UTC),quote=quote,cost=COST_MODEL,explain=explain,allow_short=allow_short)
    except Exception:
        logging.getLogger(__name__).exception('E auto cycle failed')


def _schedule_e_observation(closed: list[dict], close: datetime) -> None:
    """Dispatch E independently after ABC completes; E is strictly observe-only."""
    global _e_observer, _e_observe_adapter, _e_observe_version
    from backend.e_observer import EObserver, dispatch_e_observation, create_e_observe_adapter
    from backend.e_quant_observer import EQuantObserver, selected_observe_strategy
    try:
        enabled,short_enabled=_e_auto_settings()
        if selected_observe_strategy(DATA_ROOT/'e')=='E-V1.1-QUANT' and enabled:
            task=asyncio.create_task(_run_e_auto_observation([dict(b) for b in closed],close,None,None,allow_short=short_enabled))
            _e_observation_tasks.add(task)
            task.add_done_callback(_e_observation_tasks.discard)
            return True
        if _e_observer is None:
            version = selected_observe_strategy(DATA_ROOT / "e")
            _e_observe_version = version
            if version == "E-V1.1-QUANT":
                _e_observer = EQuantObserver(DATA_ROOT / "e-quant-observations.sqlite3", DATA_ROOT / "snapshots" / "E-V1.1", strategies, recover_interrupted=True, preflight=_e_quant_preflight)
            elif version == "E-V1.0":
                _e_observer = EObserver(DATA_ROOT / "e-observations.sqlite3", DATA_ROOT / "snapshots" / "E", strategies, recover_interrupted=True)
            else:
                logging.getLogger(__name__).error("E observe strategy configuration is invalid")
                return False
        if _e_observe_adapter is None:
            _e_observe_adapter = create_e_observe_adapter(DATA_ROOT / "e", vision_adapter.image_capability_verified, strategy_version=_e_observe_version)
        task = asyncio.create_task(dispatch_e_observation(_e_observer, closed, close, _e_observe_adapter or vision_adapter))
        _e_observation_tasks.add(task)
        task.add_done_callback(_e_observation_tasks.discard)
        return True
    except Exception:
        # Observer initialization must never affect the existing ABC scheduler.
        logging.getLogger(__name__).exception('E observation scheduling failed')
        return False


async def process_closed_bar(
    candle_close: datetime,
    *,
    now: datetime | None = None,
    snapshot_path: str | Path | None = None,
) -> dict:
    """Create one shared chart and dispatch an eligible closed Binance candle to the paper gate."""
    if not paper_scheduler.enabled or not paper_runner.enabled:
        return {agent: {"status": "PAPER_AUTOMATION_DISABLED"} for agent in "ABC"}
    current = now or datetime.now(UTC)
    processing_started = monotonic()
    close = candle_close.astimezone(UTC)

    def blocked(status: str, error_class: str | None = None) -> dict:
        detail = {"cycle_close": close.isoformat(), "status": status}
        if error_class:
            detail["error_class"] = error_class
        ledger.record_system_event(status, detail)
        return {
            agent: {"status": status, **({"error_class": error_class} if error_class else {})}
            for agent in "ABC"
        }

    candle = next(
        (
            item
            for item in candles
            if item.get("close_at")
            and datetime.fromisoformat(item["close_at"]).astimezone(UTC) == close
            and item.get("is_closed") is True
        ),
        None,
    )
    if candle is None:
        return blocked("CLOSED_BINANCE_CANDLE_NOT_FOUND")
    if market_status.get("feed_health") != "LIVE" or market_status.get("gap_count_288") != 0:
        return blocked("MARKET_DATA_INCOMPLETE")
    try:
        closed = _closed_cycle_candles(candles, close)
    except ValueError as exc:
        return blocked(str(exc))
    if any(not _valid_cycle_candle(row) for row in closed):
        return blocked("CANDLE_SOURCE_OR_INTERVAL_MISMATCH")
    if close > current.astimezone(UTC):
        return blocked("CANDLE_NOT_CLOSED")
    if formal_mode == 'RULE_DRIVEN' and isinstance(paper_runner, RuleCycleRunner):
        paper_runner.mark_time_exits(close)
    stream_quote = market.status.get("last_quote")
    stream_health = websocket_quote_health(
        market.status.get("websocket", "UNKNOWN"),
        market.status.get("websocket_last_quote_received_at"),
        datetime.now(UTC),
        timestamp_valid=bool(stream_quote and stream_quote.get("timestamp_valid")),
        last_quote_event_at=(stream_quote or {}).get("event_at"),
    )
    gate_reason = market_open_gate_reason(
        feed_health=market_status.get("feed_health", "UNKNOWN"),
        gap_count=market_status.get("gap_count_288"),
        candle_set_valid=bool(closed) and all(_valid_cycle_candle(row) for row in closed),
        websocket_state=market.status.get("websocket", "UNKNOWN"),
        recovery_state=market.status.get("websocket_recovery_state", "UNKNOWN"),
        websocket_quote_health_status=stream_health,
    )
    if gate_reason:
        return blocked(gate_reason)
    if formal_mode == 'RULE_DRIVEN':
        from backend.recovery_guard import recovery_blocked
        if os.getenv('BTC_REQUIRE_STARTUP_MONITOR') == 'true' and not market_status.get('startup_position_monitor_checked'):
            return blocked('STARTUP_POSITION_MONITOR_PENDING')
        if recovery_blocked(ledger, paper_runner):
            return blocked('PROTECTIVE_EXIT_RECOVERY_PENDING')
        try:
            activation_data = json.loads((DATA_ROOT / 'formal-mode.json').read_text(encoding='utf-8-sig'))
            activated_at = datetime.fromisoformat(activation_data['activated_at_utc'])
            if activated_at.tzinfo is None or close <= activated_at.astimezone(UTC):
                return blocked('RULE_MODE_PRE_ACTIVATION')
            quote = await market.latest_quote()
            quote_observed = monotonic()
            quote_time = datetime.fromisoformat(str(quote['event_at']).replace('Z', '+00:00'))
            timing = quote_timing(quote_time, datetime.now(UTC))
            if not timing['timestamp_valid']:
                raise ValueError('QUOTE_TIMESTAMP_INVALID')
            quote['age_ms'] = max(int(quote.get('age_ms', 10**9)), timing['age_ms'])
            quote['event_id'] = 'binance-quote:' + quote['event_at']
            book = None  # Quote-side full-fill model does not require orderbook depth.
            quote['age_ms'] += max(0, int((monotonic() - quote_observed) * 1000))
        except Exception as exc:
            return blocked('BINANCE_EXECUTABLE_DATA_UNAVAILABLE', type(exc).__name__)
        result = await paper_scheduler.run_closed_cycle(
            candle_close=close, candle_is_closed=True, now=datetime.now(UTC),
            candles=closed, quote=quote, orderbook=book,
        )
        await _dispatch_strategy_pool_cycle(closed, close, quote)
        await broadcast({'type': 'scheduler_cycle', 'close_at': close.isoformat(), 'result': result})
        return result
    from backend.market import Candle

    path = (
        Path(snapshot_path)
        if snapshot_path
        else DATA_ROOT / "snapshots" / f"{close:%Y%m%dT%H%M%SZ}.png"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    evidence = render_ai_snapshot([Candle(**row) for row in closed], path)
    image = path.read_bytes()
    try:
        quote = await market.latest_quote()
        quote_observed = monotonic()
        quote_time = datetime.fromisoformat(str(quote["event_at"]).replace("Z", "+00:00"))
        quote_asof = current + timedelta(seconds=max(0, quote_observed - processing_started))
        timing = quote_timing(quote_time, quote_asof)
        if not timing["timestamp_valid"]:
            raise ValueError("QUOTE_TIMESTAMP_INVALID")
        quote["age_ms"] = max(int(quote.get("age_ms", 10**9)), timing["age_ms"])
        quote["event_id"] = "binance-quote:" + quote["event_at"]
        book = await market.orderbook()
        book_observed = monotonic()
        quote["age_ms"] += max(0, int((monotonic() - quote_observed) * 1000))
        book["age_ms"] = int(book.get("age_ms", 10**9)) + max(
            0, int((monotonic() - book_observed) * 1000)
        )
    except Exception as exc:
        return blocked("BINANCE_EXECUTABLE_DATA_UNAVAILABLE", type(exc).__name__)
    strategies_for_cycle = {agent: strategies.current_strategy(agent) for agent in "ABC"}
    account_states = {}
    try:
        for agent in "ABC":
            account = ledger.account(agent)
            metrics = ledger.account_metrics(agent, quote, COST_MODEL)
            account_states[agent] = _cycle_decision_context(
                account,
                metrics,
                quote,
                close,
                evidence["sha256"],
                evidence["candle_sha256"],
                "TEST_SNAPSHOT" if paper_runner.require_test_adapter else "LIVE_MARKET",
            )
    except (ValueError, KeyError, TypeError, InvalidOperation):
        return blocked("DECISION_CONTEXT_INVALID")
    cycle_deadline = close + timedelta(minutes=15)
    current += timedelta(seconds=max(0, monotonic() - processing_started))
    timing = quote_timing(quote_time, current)
    quote["age_ms"] = max(quote["age_ms"], timing["age_ms"])
    book["age_ms"] += max(0, int((monotonic() - book_observed) * 1000))
    for context in account_states.values():
        context["quote_age_ms"] = quote["age_ms"]
        if not paper_runner._fresh(quote, book, COST_MODEL):
            context["open_allowed"] = False
            context["open_reason"] = "QUOTE_OR_BOOK_STALE"

    refresh_started = monotonic()

    async def refresh_market():
        latest_stream_quote = market.status.get("last_quote")
        latest_stream_health = websocket_quote_health(
            market.status.get("websocket", "UNKNOWN"),
            market.status.get("websocket_last_quote_received_at"),
            datetime.now(UTC),
            timestamp_valid=bool(latest_stream_quote and latest_stream_quote.get("timestamp_valid")),
            last_quote_event_at=(latest_stream_quote or {}).get("event_at"),
        )
        refresh_gate = market_open_gate_reason(
            feed_health=market_status.get("feed_health", "UNKNOWN"),
            gap_count=market_status.get("gap_count_288"),
            candle_set_valid=True,
            websocket_state=market.status.get("websocket", "UNKNOWN"),
            recovery_state=market.status.get("websocket_recovery_state", "UNKNOWN"),
            websocket_quote_health_status=latest_stream_health,
        )
        if refresh_gate:
            raise RuntimeError(refresh_gate)
        updated_quote = await market.latest_quote()
        updated_book = {}
        latest_stream_quote = market.status.get("last_quote")
        latest_stream_health = websocket_quote_health(
            market.status.get("websocket", "UNKNOWN"),
            market.status.get("websocket_last_quote_received_at"),
            datetime.now(UTC),
            timestamp_valid=bool(latest_stream_quote and latest_stream_quote.get("timestamp_valid")),
            last_quote_event_at=(latest_stream_quote or {}).get("event_at"),
        )
        refresh_gate = market_open_gate_reason(
            feed_health=market_status.get("feed_health", "UNKNOWN"),
            gap_count=market_status.get("gap_count_288"),
            candle_set_valid=True,
            websocket_state=market.status.get("websocket", "UNKNOWN"),
            recovery_state=market.status.get("websocket_recovery_state", "UNKNOWN"),
            websocket_quote_health_status=latest_stream_health,
        )
        if refresh_gate:
            raise RuntimeError(refresh_gate)
        updated_at = datetime.fromisoformat(str(updated_quote["event_at"]).replace("Z", "+00:00"))
        asof = current + timedelta(seconds=max(0, monotonic() - refresh_started))
        updated_timing = quote_timing(updated_at, asof)
        if not updated_timing["timestamp_valid"] or updated_at.astimezone(
            UTC
        ) < quote_time.astimezone(UTC):
            raise ValueError("REFRESH_QUOTE_TIMESTAMP_INVALID_OR_REGRESSED")
        updated_quote["age_ms"] = max(
            int(updated_quote.get("age_ms", 10**9)), updated_timing["age_ms"]
        )
        updated_quote["event_id"] = "binance-quote:" + updated_quote["event_at"]
        return updated_quote, updated_book

    result = await paper_scheduler.run_closed_cycle(
        candle_close=close,
        candle_is_closed=True,
        cycle_deadline=cycle_deadline,
        now=current,
        adapter=vision_adapter,
        image=image,
        strategies=strategies_for_cycle,
        account_states=account_states,
        quote=quote,
        orderbook=book,
        feed_healthy=True,
        refresh_market=refresh_market,
    )
    # New strategies consume the same validated natural bar and shared candles.
    # Their isolated failures cannot affect ABCDE results or ledgers.
    await _dispatch_strategy_pool_cycle(closed, close, market_status.get('quote'))
    for agent, outcome in result.items():
        execution = outcome.get("execution") or {}
        if execution.get("accepted") is False:
            ledger.record_system_event(
                "SIMULATION_EXECUTION_BLOCKED",
                {
                    "cycle_close": close.isoformat(),
                    "agent": agent,
                    "reason": execution.get("reason", "UNKNOWN"),
                },
            )
    await broadcast({"type": "scheduler_cycle", "close_at": close.isoformat(), "result": result})
    return result


async def _dispatch_strategy_pool_cycle(closed, close, quote):
    """Run the isolated PA cycle once with the already validated close and shared quote."""
    try:
        pool_bars = [dict(x) for x in closed[-1000:]]
        pool_quote = _quote_with_live_age(quote) or {}
        recovery = await asyncio.to_thread(
            strategy_pool.recover_gaps, pool_bars, close, healthy=_pa_quote_entries_allowed())
        if recovery:
            logging.getLogger(__name__).info('Strategy pool gap recovery: %s', recovery)
        outcomes = await asyncio.to_thread(
            strategy_pool.cycle, pool_bars, close,
            max_age_ms=COST_MODEL.max_quote_age_ms,
            quote_age_ms=int(pool_quote.get('age_ms', 10**9)),
            quote=pool_quote,
            fee_rate=Decimal(str(COST_MODEL.fee_rate)),
            slippage=Decimal(str(COST_MODEL.adverse_slippage_bps)) / Decimal(10000),
            quantity_step=Decimal(str(COST_MODEL.quantity_step)),
            min_quantity=Decimal(str(COST_MODEL.minimum_quantity)),
            min_notional=Decimal(str(COST_MODEL.minimum_notional)),
        )
        logging.getLogger(__name__).info(
            'Isolated strategy-pool cycle close=%s outcomes=%s',
            close.isoformat(), json.dumps(outcomes, ensure_ascii=False, sort_keys=True),
        )
    except Exception:
        logging.getLogger(__name__).exception(
            'Isolated strategy-pool cycle failed close=%s', close.isoformat(),
        )

    for sid in pa_extension.ledgers:
        try:
            action = await asyncio.to_thread(
                pa_extension.ledger_cycle, sid, closed, close, _pa_quote_entries_allowed)
            logging.getLogger(__name__).info('%s cycle close=%s action=%s', sid, close.isoformat(), action)
        except Exception:
            logging.getLogger(__name__).exception('%s isolated cycle failed', sid)


def _pa_quote_entries_allowed():
    quote = _quote_with_live_age(market_status.get('quote')) or {}
    return bool(
        market_status.get('feed_health') == 'LIVE'
        and market_status.get('gap_count_288') == 0
        and market_status.get('startup_position_monitor_checked') is True
        and market.status.get('websocket_recovery_state') == 'HEALTHY'
        and quote.get('source') == 'BINANCE_USDM_PERP'
        and quote.get('timestamp_valid') is True
        and 0 <= int(quote.get('age_ms', 10**9)) <= COST_MODEL.max_quote_age_ms
    )


def _d_auto_paper_requested():
    """Use explicit opt-in and the existing paper automation gate."""
    if not paper_automation_enabled or formal_mode != 'RULE_DRIVEN':
        return False
    value = os.getenv('D_AUTO_PAPER_ENABLED')
    if value is not None:
        return value.lower() == 'true'
    try:
        path = DATA_ROOT / 'd' / 'auto-paper.json'
        config = json.loads(path.read_text(encoding='utf8'))
        return (isinstance(config, dict) and set(config) == {'mode', 'enabled'}
                and config['mode'] == 'AUTO_PAPER' and config['enabled'] is True)
    except (OSError, ValueError, TypeError):
        value = os.getenv('D_AUTO_PAPER_ENABLED')
        return value is not None and value.lower() == 'true'


@asynccontextmanager
async def lifespan(app: FastAPI):
    global lifespan_active, market_task_startup, d_bridge
    if lifespan_active:
        yield
        return
    lifespan_active = True
    import sys
    logging.getLogger('uvicorn.error').info(
        'V3 startup app_file=%s cwd=%s python=%s data_root=%s paper_db=%s strategy_pool_db=%s version=%s',
        Path(__file__).resolve(), Path.cwd(), sys.executable, DATA_ROOT, DB_PATH,
        strategy_pool.path, app.version,
    )
    global pa_extension
    pa_extension = PAExtension(
        DATA_ROOT / 'pa-v1-extensions',
        register=os.getenv('BTC_PA_EXTENSION_REGISTER', 'true').lower() == 'true',
        enabled={item.strip().upper() for item in os.getenv('BTC_PA_EXTENSION_ENABLED', 'PA-09,PA-10').split(',') if item.strip()},
        cost=COST_MODEL,
    )
    stop_event.clear()
    try:
        try:
            await asyncio.to_thread(ledger.dispatch_mirror_outbox)
        except Exception:
            logging.getLogger(__name__).exception("G mirror outbox replay failed")
        async def start_d_components():
            global d_bridge
            try:
                from backend.d_paper import initialize_store
                await asyncio.to_thread(initialize_store, DATA_ROOT/'d'/'formal.sqlite3')
            except (OSError, sqlite3.Error, ValueError):
                logging.getLogger(__name__).exception('D observation store unavailable')
                return
            if stop_event.is_set() or not _d_auto_paper_requested():
                return
            try:
                from backend.d_bridge import DBridge
                bridge = DBridge(DATA_ROOT/'d'/'formal.sqlite3', DATA_ROOT/'d'/'trading.sqlite3',
                                 candle_archive, feed_healthy=_d_feed_healthy,
                                 candles_complete=_d_candles_complete)
                await bridge.start()
                if stop_event.is_set():
                    await bridge.close()
                else:
                    d_bridge = bridge
            except Exception:
                logging.getLogger(__name__).exception('D bridge startup failed')
        d_start_task = asyncio.create_task(start_d_components())
        if market.credentials:
            tasks.extend(
                [
                    asyncio.create_task(market_loop()),
                    asyncio.create_task(quote_loop()),
                    asyncio.create_task(market.websocket(stream_events, stop_event)),
                ]
            )
            market_task_startup = "STARTED"
        else:
            market_task_startup = "BLOCKED_CREDENTIALS"
        yield
    finally:
        stop_event.set()
        if d_start_task is not None:
            await asyncio.gather(d_start_task, return_exceptions=True)
        if d_bridge is not None:
            bridge, d_bridge = d_bridge, None
            try:
                await bridge.close()
            except Exception:
                logging.getLogger(__name__).exception('D bridge shutdown failed')
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        tasks.clear()
        for task in _d_market_tasks:
            task.cancel()
        if _d_market_tasks:
            await asyncio.gather(*_d_market_tasks, return_exceptions=True)
        market_task_startup = "STOPPED"
        lifespan_active = False
        if paper_scheduler_lease is not None:
            paper_scheduler_lease.release()


app = FastAPI(title="BTC Visual Price Action Lab", version="1.0.4", lifespan=lifespan)

# User supplied strategies are stored in their own database. They are never
# initialized from ABCDE's paper database and remain disabled after upload.
from backend.strategy_pool import StrategyPool, StrategyValidationError
strategy_pool = StrategyPool(os.getenv('BTC_LAB_STRATEGY_POOL_DB', str(DATA_ROOT / 'strategy-pool.sqlite3')))
pa_extension = PAExtension(
    DATA_ROOT / 'pa-v1-extensions',
    register=False,
    enabled=(),
    cost=COST_MODEL,
)

@app.get('/api/v1/g-mirror/status')
async def g_mirror_status():
    quote = _quote_with_live_age(market_status.get('quote')) or {}
    status = g_mirror.status(quote)
    c_account = ledger.account('C')
    c_position = json.loads(c_account['position_json']) if c_account.get('position_json') else None
    status['c_position'] = c_position
    pairs = g_mirror.pairs()
    status['latest_pair'] = pairs[-1] if pairs else None
    if status['latest_pair']:
        pair = status['latest_pair']
        gfill = next((fill for fill in reversed(g_mirror.fills()) if fill['c_trade_id'] == pair['c_trade_id']), None)
        cfill = next((fill for fill in reversed(ledger.fills('C')) if fill['fill_id'] == (gfill or {}).get('source_fill_id')), None)
        status['latest_g_fill'] = gfill
        status['latest_c_fill'] = cfill
        if cfill and gfill:
            try:
                c_at = datetime.fromisoformat(cfill['occurred_at'].replace('Z', '+00:00'))
                g_at = datetime.fromisoformat(gfill['occurred_at'].replace('Z', '+00:00'))
                status['last_fill_delay_ms'] = int(abs((g_at-c_at).total_seconds())*1000)
            except (TypeError, ValueError):
                status['last_fill_delay_ms'] = None
    return status

@app.get('/api/v1/g-mirror/fills')
async def g_mirror_fills(limit: int = 500):
    if not 1 <= limit <= 2000:
        raise HTTPException(400, 'LIMIT_INVALID')
    rows = g_mirror.fills()
    return {'items': rows[-limit:]}

@app.get('/api/v1/strategy-pool')
async def strategy_pool_list():
    return {'items': strategy_pool.list() + pa_extension.list()}

@app.get('/api/v1/strategy-pool/{strategy_id}/versions')
async def strategy_pool_versions(strategy_id: str):
    return {'items': strategy_pool.versions(strategy_id)}

@app.post('/api/v1/strategy-pool/upload')
async def strategy_pool_upload(request: Request):
    try:
        raw = await request.body()
        if len(raw) > 64000: raise StrategyValidationError('文件超过 64KB')
        result = strategy_pool.upload(raw)
        history = strategy_pool.versions(result['strategy_id'])
        result['previous_version'] = history[-2]['version'] if len(history) > 1 else None
        result['change_summary'] = '首个版本' if len(history) == 1 else '新版本已保存；旧版本及其持仓归属不变'
        result['version_diff'] = strategy_pool.diff(result['strategy_id'],history[-2]['version'],result['version']) if len(history)>1 else {'from':None,'to':result['version'],'changes':[]}
        return {'validation': {'valid': True, 'checks': ['结构与字段', '受限规则', '市场与周期', '固定风险配置'], 'errors': []}, **result}
    except StrategyValidationError as exc:
        raise HTTPException(status_code=422, detail={'valid':False,'errors':[str(exc)]}) from exc

@app.post('/api/v1/strategy-pool/{strategy_id}/enable')
async def strategy_pool_enable(strategy_id: str, request: Request):
    if strategy_id.upper() in PA_EXTENSION_NAMES:
        import hmac
        token = os.getenv('BTC_LAB_RECOVERY_TOKEN', '').encode()
        supplied = request.headers.get('x-btc-recovery-token', '').encode()
        if not token or len(token) < 32 or not hmac.compare_digest(token, supplied):
            raise HTTPException(403, 'PA_EXTENSION_NOT_AUTHORIZED')
        if strategy_id not in pa_extension.ledgers:
            raise HTTPException(404, 'PA_EXTENSION_NOT_REGISTERED')
        body = await request.json()
        if not isinstance(body, dict) or body.get('version') != '1.0.0' or type(body.get('enabled')) is not bool:
            raise HTTPException(422, 'PA_EXTENSION_ENABLE_INPUT_INVALID')
        def ready():
            return _pa_quote_entries_allowed() and pa_extension.exit_recent(strategy_id)
        try:
            action = await asyncio.to_thread(
                pa_extension.set_enabled, strategy_id, body['enabled'], datetime.now(UTC), ready)
        except (ValueError, sqlite3.Error, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {'ok': True, 'id': strategy_id, 'enabled': body['enabled'], 'action': action}
    body=await request.json()
    try: result=strategy_pool.enable(strategy_id,body.get('version'),body.get('enabled') is True)
    except StrategyValidationError as exc: raise HTTPException(status_code=409,detail=str(exc)) from exc
    return {'ok':True,**result,'items':strategy_pool.list()}

@app.post('/api/v1/strategy-pool/{strategy_id}/archive')
async def strategy_pool_archive(strategy_id: str):
    try: result=strategy_pool.archive(strategy_id)
    except StrategyValidationError as exc: raise HTTPException(status_code=409,detail=str(exc)) from exc
    return {'ok':True,**result}

@app.post('/api/v1/strategy-pool/{strategy_id}/restore')
async def strategy_pool_restore(strategy_id: str, request: Request):
    body=await request.json()
    try: result=strategy_pool.restore(strategy_id,body.get('version'))
    except StrategyValidationError as exc: raise HTTPException(status_code=409,detail=str(exc)) from exc
    return {'ok':True,**result,'items':strategy_pool.list()}

@app.get('/api/v1/strategy-pool/{strategy_id}/lifecycle-events')
async def strategy_pool_lifecycle_events(strategy_id: str):
    return {'items':strategy_pool.lifecycle_events(strategy_id)}

@app.get('/api/v1/strategy-pool/{strategy_id}/diff')
async def strategy_pool_diff(strategy_id: str, old: str, new: str):
    try: return strategy_pool.diff(strategy_id,old,new)
    except StrategyValidationError as exc: raise HTTPException(status_code=404,detail=str(exc)) from exc

@app.get('/api/v1/strategy-pool/{strategy_id}/fills')
async def strategy_pool_fills(strategy_id: str, limit: int = 500):
    if strategy_id in pa_extension.ledgers:
        if not 1 <= limit <= 2000: raise HTTPException(400, 'LIMIT_INVALID')
        return {'items': pa_extension.ledgers[strategy_id].fills()[-limit:][::-1]}
    if not 1 <= limit <= 2000: raise HTTPException(400,'LIMIT_INVALID')
    return {'items':strategy_pool.fills(strategy_id,limit)}

@app.get('/api/v1/strategy-pool/{strategy_id}/decisions')
async def strategy_pool_decisions(strategy_id: str, limit: int = 100):
    if not 1 <= limit <= 1000: raise HTTPException(400,'LIMIT_INVALID')
    return {'items':strategy_pool.diagnostics(strategy_id,limit)}

@app.post('/api/v1/strategy-pool/{strategy_id}/backtest')
async def strategy_pool_backtest(strategy_id: str, request: Request):
    body=await request.json();version=body.get('version');start=body.get('start');end=body.get('end')
    try:
        begin=archive_utc(start);finish=archive_utc(end);now=datetime.now(UTC)
        if not version or begin>=finish or finish-begin>timedelta(days=30) or begin.minute%15 or begin.second or begin.microsecond or finish.minute%15 or finish.second or finish.microsecond or finish>now:
            raise ValueError('INVALID_RANGE')
        rows=candle_archive.read(begin,finish)
        expected=int((finish-begin).total_seconds()//900)
        if len(rows)!=expected: raise StrategyValidationError(f'历史数据不完整：需要 {expected} 根，当前 {len(rows)} 根；未请求补取数据')
        return strategy_pool.backtest(strategy_id,version,rows,start=begin.isoformat(),end=finish.isoformat(),fee_rate=Decimal(str(COST_MODEL.fee_rate)),slippage=Decimal(str(COST_MODEL.adverse_slippage_bps))/Decimal(10000),quantity_step=Decimal(str(COST_MODEL.quantity_step)),min_quantity=Decimal(str(COST_MODEL.minimum_quantity)),min_notional=Decimal(str(COST_MODEL.minimum_notional)))
    except StrategyValidationError as exc: raise HTTPException(422,detail=str(exc)) from exc
    except (ValueError,TypeError) as exc: raise HTTPException(400,'INVALID_BACKTEST_RANGE_OR_DATA') from exc
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5174",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class CImport(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "version": app.version, "live_trading_disabled": True}


def _strategy_control():
    from backend.strategy_control import StrategyControl
    return StrategyControl(DATA_ROOT / 'strategy-control.sqlite3')


def _strategy_stores(agent):
    if agent == 'E':
        from backend.e_paper_store import EPaperStore
        paths = {'formal': DATA_ROOT / 'e/formal.sqlite3', 'shadow': DATA_ROOT / 'e/shadow.sqlite3'}
        result = {}
        for name, path in paths.items():
            try: result[name] = EPaperStore(path) if path.is_file() else None
            except Exception: result[name] = None
        return result
    path = ROOT / 'docs/reports/abc-forward-v26/shadow-ledger.sqlite3'
    shadow = None
    if path.is_file():
        try:
            with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
                if db.execute('pragma quick_check').fetchone()[0] != 'ok': raise ValueError('BAD_SHADOW')
                required = {'accounts', 'fills', 'forward_cycles', 'forward_positions'}
                existing = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
                if not required <= existing: raise ValueError('INVALID_SHADOW_IDENTITY')
            shadow = Ledger(path)
        except Exception:
            shadow = None
    return {'formal': ledger, 'shadow': shadow}


def _strategy_formal_store(agent):
    if agent != 'E': return ledger
    from backend.e_paper_store import EPaperStore
    path = DATA_ROOT / 'e/formal.sqlite3'
    return EPaperStore(path) if path.is_file() else None


@app.get('/api/v1/strategy-control')
async def strategy_control_status():
    control = _strategy_control()
    return {agent: control.status(agent) for agent in 'ABCE'}


@app.post('/api/v1/strategy-control/{agent}/{action}')
async def strategy_control_action(agent: str, action: str, request: Request):
    if request.client is None or request.client.host not in ('127.0.0.1', '::1'):
        raise HTTPException(status_code=403, detail='LOCAL_ONLY')
    origin = request.headers.get('origin')
    if origin and origin not in ('http://127.0.0.1:5174', 'http://localhost:5174'):
        raise HTTPException(status_code=403, detail='ORIGIN_DENIED')
    if formal_mode != 'RULE_DRIVEN':
        raise HTTPException(status_code=409, detail='RULE_DRIVEN_REQUIRED')
    agent = agent.upper()
    if agent not in 'ABCE' or len(agent) != 1 or action not in ('pause', 'resume'):
        raise HTTPException(status_code=400, detail='INVALID_ACTION')
    control = _strategy_control()
    if action == 'resume':
        if not await asyncio.to_thread(control.resume, agent):
            raise HTTPException(status_code=409, detail='PAUSE_NOT_COMPLETE')
        return control.status(agent)
    await asyncio.to_thread(control.request_pause, agent)
    try:
        store = await asyncio.to_thread(_strategy_formal_store, agent)
    except Exception:
        store = None
    quote = _quote_with_live_age(market_status.get('quote')) or {}
    return await asyncio.to_thread(control.pause_formal, agent, store, quote, COST_MODEL, datetime.now(UTC))


def _shadow_evolution_status():
    db = ROOT / 'docs' / 'reports' / 'abc-forward-v26' / 'shadow-ledger.sqlite3'
    if not db.is_file():
        return {'state': 'UNAVAILABLE', 'agents': {}}
    try:
        with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=1) as c:
            rows = {}
            for a in 'ABC':
                count = c.execute('select count(*) from forward_trades where agent=?', (a,)).fetchone()[0]
                review = c.execute('select batch,status from forward_reviews where agent=? order by batch desc limit 1', (a,)).fetchone()
                rows[a] = {'closed_trades': count, 'next_review_at': (count // 40 + 1) * 40, 'latest_review': list(review) if review else None}
        state_path = db.parent / 'status.json'
        from backend.stats_panel import shadow_state
        state = shadow_state(db.parent,json.loads(state_path.read_text(encoding='utf8'))) if state_path.exists() else 'UNKNOWN'
        return {'state': state, 'agents': rows}
    except (sqlite3.Error, OSError, ValueError):
        return {'state': 'UNAVAILABLE', 'agents': {}}

@app.get("/api/v1/system/status")
async def system_status():
    vision_config = VisionProviderConfig.from_env()
    vision_status = public_vision_status(
        vision_config,
        credential_present=bool(os.getenv(vision_config.api_key_env)),
        image_capability_verified=vision_adapter.image_capability_verified,
    )
    live_quote = _quote_with_live_age(market_status.get("quote"))
    age = (live_quote or {}).get("age_ms")
    timestamp_valid = bool(live_quote and live_quote.get("timestamp_valid", True))
    stale = age is None or age > COST_MODEL.max_quote_age_ms or not timestamp_valid
    quote_health = (
        "UNAVAILABLE"
        if live_quote is None
        else "TIMESTAMP_INVALID"
        if not timestamp_valid
        else "STALE"
        if age is None or age > COST_MODEL.max_quote_age_ms
        else "FRESH"
    )
    base_health = market_status.get("feed_health", "UNKNOWN")
    effective_health = "STALE" if stale else base_health
    stream_state = market.status.get("websocket", market_status.get("websocket", "UNKNOWN"))
    stream_quote = market.status.get("last_quote")
    stream_quote_health = websocket_quote_health(
        stream_state,
        market.status.get("websocket_last_quote_received_at"),
        datetime.now(UTC),
        timestamp_valid=bool(stream_quote and stream_quote.get("timestamp_valid", True)),
        last_quote_event_at=(stream_quote or {}).get("event_at"),
    )
    return {
        **market_status,
        "quote": live_quote,
        "websocket": stream_state,
        "market_task_startup": market_task_startup,
        "websocket_quote_health": stream_quote_health,
        "websocket_recovery_state": market.status.get("websocket_recovery_state", "UNKNOWN"),
        "websocket_recovery_round": market.status.get("websocket_recovery_round", 0),
        "websocket_recovery_attempts_in_round": market.status.get(
            "websocket_recovery_attempts_in_round", 0
        ),
        "websocket_recovery_failed_rounds": market.status.get(
            "websocket_recovery_failed_rounds", 0
        ),
        "websocket_recovery_first_failure_at": market.status.get(
            "websocket_recovery_first_failure_at"
        ),
        "websocket_recovery_success_at": market.status.get("websocket_recovery_success_at"),
        "websocket_protection_reason": market.status.get("websocket_protection_reason"),
        "websocket_recovery_cooldown_until": market.status.get(
            "websocket_recovery_cooldown_until"
        ),
        "websocket_error_code": market.status.get("websocket_error_code"),
        "websocket_failure_reason": market.status.get("websocket_failure_reason"),
        "websocket_last_error_class": market.status.get("websocket_last_error_class"),
        "websocket_reconnect_count": market.status.get("websocket_reconnect_count", 0),
        "websocket_last_message_received_at": market.status.get(
            "websocket_last_message_received_at"
        ),
        "websocket_last_quote_received_at": market.status.get("websocket_last_quote_received_at"),
        "feed_health": effective_health,
        "quote_health": quote_health,
        "quote_fresh": not stale,
        "stop_monitor": "QUOTE_EVENT_INTERNAL_PAPER_STOP_MONITOR",
        "scheduler": (
            "ENABLED_INTERNAL_PAPER" if paper_scheduler.enabled else "DISABLED_NOT_ARMED"
        ),
        "paper_gate_reason": paper_gate_reason,
        "vision": vision_status,
        "vision_budget": vision_adapter._budget.summary(),
        "paper_automation": (
            "ENABLED_INTERNAL_PAPER" if paper_scheduler.enabled else "DISABLED_NOT_ARMED"
        ),
        "database": str(ledger.path.name),
        "formal_trading_mode": formal_mode,
        "rule_rejection_audit_version": (paper_runner.audit_version if isinstance(paper_runner, RuleCycleRunner) else None),
        "shadow_evolution": await asyncio.to_thread(_shadow_evolution_status),
        "active_rule_versions": ({a: paper_runner.profile[a] for a in "ABC"} if isinstance(paper_runner, RuleCycleRunner) else {}),
        "cost_model": {
            "status": "USER_APPROVED_RESEARCH_MODEL",
            "model": COST_MODEL.provenance(),
            "maker_reference_rate": str(COST_MODEL.maker_reference_rate),
            "taker_fee_rate": str(COST_MODEL.fee_rate),
            "slippage_per_fill": str(COST_MODEL.adverse_slippage_bps / 10000),
            "fee_bps_each_side": str(COST_MODEL.fee_rate * 10000),
            "adverse_slippage_bps": str(COST_MODEL.adverse_slippage_bps),
            "quote_book_max_age_ms": COST_MODEL.max_quote_age_ms,
            "quantity_step_btc": str(COST_MODEL.quantity_step),
            "minimum_notional_usd": str(COST_MODEL.minimum_notional),
            "price_guard_bps": str(COST_MODEL.max_price_deviation_bps),
            "funding": "NOT_INCLUDED",
        },
    }


# Observation APIs are display-only: no strategy dispatch or ledger writes.
from backend.kline_observation import CandleReader, EventReader
observation_candles = CandleReader()
observation_events = EventReader(ROOT, data_root=DATA_ROOT)

@app.get("/api/v1/observation/candles")
async def observation_candle_route(interval: str = "15m", limit: int = 500, end_time: int | None = None):
    try:
        return await observation_candles.get(interval, limit, end_time)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "PUBLIC_CANDLES_UNAVAILABLE") from exc

@app.get("/api/v1/observation/events")
async def observation_events_route():
    items = await asyncio.to_thread(observation_events.read)
    return {"source":"FORMAL_PAPER_LEDGERS", "items":items, "errors":list(observation_events.errors)}

@app.get("/api/v1/market/candles")
async def get_candles(limit: int = 672):
    if not 1 <= limit <= 1000:
        raise HTTPException(400, "LIMIT_INVALID")
    data = candles[-limit:]
    return {
        "symbol": "BTC/USD",
        "interval": "15m",
        "source": "BINANCE_USDM_PERP",
        "currency": "USD_RESEARCH",
        "items": data,
        "count": len(data),
        "expected_288": len(data) >= 288 and market_status.get("gap_count_288") == 0,
        "expected_96": len(data) >= 96 and market_status.get("gap_count_288") == 0,
        "hash": market_status.get("candle_sha256"),
    }


@app.get('/api/v1/market/candles/history')
async def candle_history(start: str, end: str, backfill: bool = False):
    try:
        begin=archive_utc(start);finish=archive_utc(end);now=datetime.now(UTC)
        if begin>=finish or finish-begin>timedelta(days=30) or begin.minute%15 or begin.second or begin.microsecond or finish.minute%15 or finish.second or finish.microsecond or finish>now:raise ValueError('INVALID_RANGE')
    except (ValueError,TypeError):raise HTTPException(400,'INVALID_HISTORY_RANGE')
    rows=candle_archive.read(begin,finish)
    expected=int((finish-begin).total_seconds()//900)
    error=None
    if backfill and len(rows)<expected:
        try:
            fetched=await market.bars(begin,finish)
            candle_archive.store([x.__dict__ for x in fetched if x.is_closed and begin<=archive_utc(x.open_at)<finish])
            rows=candle_archive.read(begin,finish)
        except Exception as exc:error=type(exc).__name__
    return {'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','items':rows,'expected':expected,'missing':max(0,expected-len(rows)),'backfill_error':error}


@app.get("/api/v1/market/sources")
async def market_sources():
    return {
        "active": "BINANCE_USDM_PERP",
        "currency": "USD_RESEARCH",
        "providers": [
            {"id": "BINANCE_USDM_PERP", "enabled": True, "market_type": "perpetual",
             "symbol": "BTCUSDT", "quote_currency": "USDT"},
        ],
    }


@app.get("/api/v1/market/quote")
async def get_quote():
    return _quote_with_live_age(market_status.get("quote")) or {
        "status": "UNAVAILABLE",
        "source": "BINANCE_USDM_PERP",
    }


def _quote_with_live_age(quote: dict | None) -> dict | None:
    if not quote:
        return None
    try:
        received = datetime.fromisoformat(quote["received_at"])
        now = datetime.now(UTC)
        if received.tzinfo is None or received.utcoffset() is None or received > now:
            return {
                **quote,
                "timestamp_valid": False,
                "timestamp_status": "INVALID_RECEIVE_TIMESTAMP",
                "age_ms": 10**9,
            }
        if quote.get("timestamp_valid") is False:
            return {**quote, "age_ms": 10**9}
        age = int((now - received.astimezone(UTC)).total_seconds() * 1000)
        source_age = int(quote.get("age_ms", 10**9))
        return {**quote, "age_ms": source_age + age}
    except (KeyError, ValueError, TypeError):
        return {
            **quote,
            "timestamp_valid": False,
            "timestamp_status": "INVALID_TIMESTAMP",
            "age_ms": 10**9,
        }


@app.get("/api/v1/market/orderbook")
async def get_orderbook():
    if not market.credentials:
        return {"status": "CREDENTIALS_NOT_CONFIGURED"}
    try:
        return await market.orderbook()
    except Exception as exc:
        return {"status": "UNAVAILABLE", "error": type(exc).__name__}


@app.get("/api/v1/e/accounts")
async def get_e_accounts():
    from backend.e_paper_store import read_e_store
    from backend.e_quant_observer import selected_observe_strategy
    quote = _quote_with_live_age(market_status.get("quote"))
    auto_enabled,short_enabled=_e_auto_settings()
    enabled=selected_observe_strategy(DATA_ROOT/'e')=='E-V1.1-QUANT' and auto_enabled
    try:
        shadow=json.loads((DATA_ROOT/'e'/'shadow-config.json').read_text(encoding='utf8'))
    except (OSError,ValueError,TypeError):shadow=None
    return {"formal": read_e_store(DATA_ROOT / "e" / "formal.sqlite3", quote=quote),
            "shadow": read_e_store(DATA_ROOT / "e" / "shadow.sqlite3", quote=quote),
            "runtime":{"mode":"AUTO_PAPER" if enabled else "OBSERVE_ONLY","setups":{"E1":enabled,"E2":enabled,"E3":enabled,"E4":enabled and short_enabled},"shadow_execution":"BASELINE" if enabled and shadow=={'mode':'BASELINE','enabled':True} else "NOT_CONNECTED"}}


@app.get('/api/v1/d/status')
async def get_d_status():
    if d_bridge is not None:
        try:
            return await d_bridge.status(market_status.get('quote'))
        except Exception:
            logging.getLogger(__name__).exception('D bridge status failed')
            return {'status':'UNAVAILABLE', 'reason':'D_BRIDGE_STATUS_ERROR', 'execution_ready':False}
    from backend.d_paper import read_status
    result = read_status(DATA_ROOT/'d'/'formal.sqlite3')
    result.update(enabled=False, control_state='PAUSED')
    return result


@app.post('/api/v1/d/control')
async def set_d_control(request: Request):
    global d_bridge
    if request.client is None or request.client.host not in ('127.0.0.1', '::1'):
        raise HTTPException(status_code=403, detail='LOCAL_ONLY')
    origin = request.headers.get('origin')
    if origin and origin not in ('http://127.0.0.1:5175', 'http://localhost:5175'):
        raise HTTPException(status_code=403, detail='ORIGIN_DENIED')
    body = await request.json()
    enabled = body.get('enabled') if isinstance(body, dict) else None
    if type(enabled) is not bool:
        raise HTTPException(status_code=422, detail='ENABLED_MUST_BE_BOOLEAN')
    if formal_mode != 'RULE_DRIVEN' or not paper_automation_enabled:
        raise HTTPException(status_code=409, detail='PAPER_AUTOMATION_NOT_READY')

    config_path = DATA_ROOT / 'd' / 'auto-paper.json'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = config_path.with_suffix('.json.tmp')
    payload = json.dumps({'mode': 'AUTO_PAPER', 'enabled': enabled}, indent=2) + '\n'

    if enabled and d_bridge is None:
        from backend.d_paper import initialize_store
        from backend.d_bridge import DBridge
        await asyncio.to_thread(initialize_store, DATA_ROOT / 'd' / 'formal.sqlite3')
        bridge = DBridge(DATA_ROOT / 'd' / 'formal.sqlite3', DATA_ROOT / 'd' / 'trading.sqlite3',
                         candle_archive, feed_healthy=_d_feed_healthy,
                         candles_complete=_d_candles_complete)
        await bridge.start()
        try:
            temp_path.write_text(payload, encoding='utf-8')
            temp_path.replace(config_path)
        except OSError:
            await bridge.close()
            raise
        os.environ['D_AUTO_PAPER_ENABLED'] = 'true'
        d_bridge = bridge
    else:
        try:
            temp_path.write_text(payload, encoding='utf-8')
            temp_path.replace(config_path)
        except OSError as exc:
            raise HTTPException(status_code=500, detail='D_CONTROL_SAVE_FAILED') from exc
        os.environ['D_AUTO_PAPER_ENABLED'] = str(enabled).lower()
        if d_bridge is not None:
            try:
                await asyncio.to_thread(d_bridge.set_enabled, enabled)
            except Exception as exc:
                raise HTTPException(status_code=409, detail='D_CONTROL_UPDATE_FAILED') from exc

    if d_bridge is None:
        result = await get_d_status()
        result.update(enabled=False, control_state='PAUSED')
        return result
    return await d_bridge.status(market_status.get('quote'))


@app.get("/api/v1/e/quant-observations")
async def get_e_quant_observations():
    from backend.e_quant_observer import read_quant_observations
    return read_quant_observations(DATA_ROOT / "e-quant-observations.sqlite3")


@app.get("/api/v1/e/observations")
async def get_e_observations():
    from backend.e_observer import read_observations
    return read_observations(DATA_ROOT / "e-observations.sqlite3")


@app.get("/api/v1/agents")
async def get_agents():
    state = {r["agent"]: r for r in ledger.accounts()}
    versions = {r["agent"]: r for r in strategies.list()}
    vision_config = VisionProviderConfig.from_env()
    vision = public_vision_status(
        vision_config,
        credential_present=bool(os.getenv(vision_config.api_key_env)),
        image_capability_verified=vision_adapter.image_capability_verified,
    )
    return [
        {
            "agent": x,
            "color": {"A": "#e2555b", "B": "#26b37a", "C": "#ed992f"}[x],
            'status': ('RULE_DRIVEN' if isinstance(paper_runner, RuleCycleRunner) else vision['state'] if x in {'A', 'B'} or c_strategy_loaded else 'C_NOT_CONFIGURED'),
            "account": {
                **state[x],
                "leverage": "5"
                if x == "C" and c_strategy_loaded
                else "1"
                if x in {"A", "B"}
                else None,
                "maintenance_rate": str(C_RESEARCH_MAINTENANCE_RATE)
                if x == "C" and c_strategy_loaded
                else None,
                "metrics": ledger.account_metrics(
                    x, _quote_with_live_age(market_status.get("quote")), COST_MODEL
                ),
            },
            "strategy": versions[x],
            "active_rule": (paper_runner.profile[x] if isinstance(paper_runner, RuleCycleRunner) else None),
        }
        for x in "ABC"
    ]


@app.get("/api/v1/agents/{agent}/account")
async def get_account(agent: str):
    try:
        row = ledger.account(agent.upper())
    except KeyError:
        raise HTTPException(404, "AGENT_NOT_FOUND")
    return {
        **row,
        **ledger.account_metrics(
            agent.upper(), _quote_with_live_age(market_status.get("quote")), COST_MODEL
        ),
        "currency": PAPER_CURRENCY,
        "initial_equity": INITIAL_EQUITY,
    }


@app.get('/api/v1/statistics/abc')
async def abc_statistics():
    from backend.stats_panel import build_stats
    return await asyncio.to_thread(build_stats, DB_PATH, ROOT, ledger, _quote_with_live_age(market_status.get('quote')), COST_MODEL, paper_scheduler.enabled)


@app.get("/api/v1/fills")
async def get_fills(agent: str | None = None):
    return {"items": ledger.fills(agent.upper() if agent else None)}


@app.get("/api/v1/trades")
async def get_trades(agent: str | None = None):
    fills = ledger.fills(agent.upper() if agent else None)
    grouped: dict[str, list] = {}
    for f in fills:
        grouped.setdefault(f["trade_id"], []).append(f)
    return {
        "items": [{"trade_id": k, "fills": v, "complete": len(v) >= 2} for k, v in grouped.items()]
    }


@app.get("/api/v1/trades/{trade_id}")
async def get_trade(trade_id: str):
    fills = [f for f in ledger.fills() if f["trade_id"] == trade_id]
    if not fills:
        raise HTTPException(404, "TRADE_NOT_FOUND")
    return {"trade_id": trade_id, "fills": fills, "complete": len(fills) >= 2}


@app.get("/api/v1/research/book-pa/latest")
async def get_book_pa_research():
    """Read the separate research store; never create it or invoke a model."""
    from backend.book_pa_research import KNOWLEDGE, latest_research

    return latest_research(KNOWLEDGE / "research_results.sqlite3")


@app.get("/api/v1/journal")
async def get_journal(agent: str | None = None):
    agents = [agent.upper()] if agent else list("ABC")
    if any(item not in {"A", "B", "C"} for item in agents):
        raise HTTPException(404, "AGENT_NOT_FOUND")
    items = [row for item in agents for row in decision_store.decisions_for(item)]
    items.sort(key=lambda item: item["candle_close"], reverse=True)
    fills = {
        row["decision_id"]: row
        for row in ledger.fills(agent.upper() if agent else None)
        if row.get("decision_id")
    }
    for item in items:
        item["fills"] = [fills[item["decision_id"]]] if item["decision_id"] in fills else []
    return {
        "items": items,
        "status": "READY" if items else "NO_DECISIONS",
        "agent": agent,
    }


@app.get("/api/v1/agents/{agent}/decisions")
async def get_decisions(agent: str):
    if agent.upper() not in {"A", "B", "C"}:
        raise HTTPException(404, "AGENT_NOT_FOUND")
    items = decision_store.decisions_for(agent.upper())
    return {"items": items, "status": "READY" if items else "NO_DECISIONS_MODEL_NOT_CONFIGURED"}


@app.get("/api/v1/strategies")
async def get_strategies():
    runtime = _e_strategy_runtime()
    return {"items": [present_e_runtime(item, runtime) for item in strategies.list()]}


@app.get("/api/v1/strategies/{agent}/versions")
async def get_strategy_versions(agent: str):
    item = next((x for x in strategies.list() if x["agent"] == agent.upper()), None)
    if item is None:
        raise HTTPException(404, "AGENT_NOT_FOUND")
    return {"agent": agent.upper(), **present_e_runtime(item, _e_strategy_runtime())}


@app.get("/api/v1/strategies/{agent}/diff")
async def diff_strategy(agent: str, left: str, right: str):
    import difflib

    if agent.upper() not in "ABC":
        raise HTTPException(404, "AGENT_NOT_FOUND")
    files = {p.stem: p for p in (ROOT / "strategies" / agent.upper()).glob("v*.md")}
    if left not in files or right not in files:
        raise HTTPException(404, "VERSION_NOT_FOUND")
    diff = list(
        difflib.unified_diff(
            files[left].read_text(encoding="utf-8").splitlines(),
            files[right].read_text(encoding="utf-8").splitlines(),
            fromfile=left,
            tofile=right,
            lineterm="",
        )
    )
    return {"agent": agent.upper(), "left": left, "right": right, "diff": diff}


@app.post("/api/v1/strategies/C/import")
async def import_c(payload: CImport):
    result = strategies.import_c(payload.content)
    if not result["accepted"]:
        raise HTTPException(422, detail=result)
    return result


@app.get("/api/v1/evolutions")
async def get_evolutions(agent: str | None = None):
    return {
        "items": [
            {
                **evolution.status(x),
                "queued_reviews": sum(
                    1 for r in evolution.status(x)["reviews"] if r[1] == "QUEUED"
                ),
                "status": "WAITING_FOR_REAL_CLOSED_TRADES",
            }
            for x in "ABC"
            if agent is None or x == agent.upper()
        ]
    }


@app.get("/api/v1/evolutions/{experiment_id}/comparison")
async def get_evolution_comparison(experiment_id: str):
    return {
        "experiment_id": experiment_id,
        "status": "INSUFFICIENT_SAMPLE",
        "checkpoints_days": [7, 14, 21, 30],
        "minimum_closed_trades_per_side": 20,
        "promotion": "NOT_EVALUATED_NO_REAL_TRADES",
    }


@app.get("/api/v1/audit/events")
async def get_audit():
    return {"items": [], "status": "NO_EXECUTIONS"}


@app.get("/api/v1/market/snapshot")
async def create_snapshot():
    if len(candles) < 288:
        raise HTTPException(
            409, detail={"error": "INSUFFICIENT_GLOBAL_CANDLES", "available": len(candles)}
        )
    path = DATA_ROOT / "latest_ai_snapshot.png"
    evidence = render_ai_snapshot(
        [__import__("backend.market", fromlist=["Candle"]).Candle(**x) for x in candles], path
    )
    return {k: v for k, v in evidence.items() if k != "path"}


@app.get("/api/v1/market/snapshot/image")
async def snapshot_image():
    path = DATA_ROOT / "latest_ai_snapshot.png"
    if not path.exists():
        raise HTTPException(404, "SNAPSHOT_NOT_GENERATED")
    return FileResponse(path, media_type="image/png")


@app.websocket("/api/v1/events/stream")
async def events_stream(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        await ws.send_json({"type": "status", "data": market_status})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)

