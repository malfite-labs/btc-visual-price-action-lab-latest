"""Production V3.1 Mode B Extension Adapter for the 15 Aoying S / AY instances.

- Independent SQLite ledgers under data/extensions/<strategy_id>.sqlite3
- Formal paper sizing: $10,000 USD_RESEARCH, 2% risk budget, max 5x leverage, single position
- Driven by the single public 15m market feed + live quote stream
- Preserves native decision timeframes (15m / 1h / 4h / 1d UTC-aligned)
- Reuses evaluate_common_market_guard, classify_unified_status, CostModel, calculate_position_size,
  stop_triggered, and standard snapshot/fills schema for zero-hardcoding discovery across
  Main Console, Strategy Pool, Research Center V3, and Demo/Prod Copy-Trading interfaces.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from backend.common_guard import (
    CommonMarketGuard,
    UNIFIED_STATUS_LABELS,
    classify_unified_status,
)
from backend.domain import BookLevel, CostModel, calculate_position_size, stop_triggered
from backend.strategy_pool import FORMAT_V2, StrategyPool
from strategies.r_series.indicators import normalize_closed_15m_bar, parse_utc
from strategies.thc_ay.signals import (
    ThcAyCycleContext,
    compute_s_sl_tp,
    evaluate_ay_signal,
    evaluate_s_signal,
    validate_ay_entry,
)
from strategies.thc_ay.specs import (
    OBSERVATION_TIER,
    ORIGINAL_EVIDENCE_STATUS,
    STRATEGY_GROUP,
    THC_AY_IDS,
    THC_AY_SPECS,
    ThcAySpec,
)

UTC = timezone.utc
STEP_15M = timedelta(minutes=15)
INITIAL_EQUITY = D("10000")
RISK_FRACTION = D("0.02")
MAX_LEVERAGE = D("5")


def next_native_close_iso(after_dt: datetime, step_ms: int) -> str:
    ms = int(after_dt.astimezone(UTC).timestamp() * 1000)
    nxt = ((ms // step_ms) + 1) * step_ms
    return datetime.fromtimestamp(nxt / 1000.0, tz=UTC).isoformat()


def load_extended_15m_prefix(project_root: Path) -> List[Dict[str, Any]]:
    """Read-only load of the extended 549-day Binance USD-M 15m seed archive for warmup."""
    ext_db = project_root / "research" / "r_series" / "binance_btcusdt_15m_extended.sqlite3"
    if not ext_db.is_file():
        return []
    try:
        from research.r_series.backtest_runner import load_candles_ro
        bars, _ = load_candles_ro(ext_db)
        return bars
    except Exception:
        return []


def merge_15m_bars(prefix_bars: List[Dict[str, Any]], live_raw_bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge read-only historical prefix with live closed 15m bars, deduplicated by open timestamp."""
    by_t: Dict[int, Dict[str, Any]] = {}
    for b in prefix_bars:
        by_t[b["t"]] = b
    for raw in live_raw_bars:
        if raw.get("is_closed") is False:
            continue
        nb = normalize_closed_15m_bar(raw)
        by_t[nb["t"]] = nb
    return [by_t[t] for t in sorted(by_t.keys())]


class ThcAyPaperLedger:
    """Durable SQLite paper ledger for one frozen Aoying S / AY strategy instance."""

    def __init__(
        self,
        directory: str | Path,
        sid: str,
        *,
        cost: Optional[CostModel] = None,
        enabled: bool = False,
        activated_at: Optional[datetime] = None,
    ) -> None:
        if sid not in THC_AY_SPECS:
            raise ValueError(f"UNKNOWN_THC_AY_ID:{sid}")
        self.sid = sid
        self.spec: ThcAySpec = THC_AY_SPECS[sid]
        self.cost = cost or CostModel()
        self.doc: Dict[str, Any] = {
            "format": FORMAT_V2,
            "strategy_id": sid,
            "source_experiment": self.spec.source_experiment,
            "name": self.spec.name,
            "version": self.spec.version,
            "candidate_ref": f"{self.spec.source_experiment} (sha256:{self.spec.config_sha256[:12]})",
            "observation_tier": OBSERVATION_TIER,
            "original_evidence_status": ORIGINAL_EVIDENCE_STATUS,
            "group": self.spec.group,
            "family": self.spec.family,
            "timeframe": self.spec.timeframe,
            "min_daily_warmup": self.spec.min_daily_warmup,
            "description": self.spec.description,
            "parameters": dict(self.spec.parameters),
            "position": {
                "direction": "long" if self.spec.family == "AY03" else "both",
                "entry_timing": "next_open",
                "same_bar_priority": "stop",
                "max_hold_bars": self.spec.max_hold_bars,
                "target_r": self.spec.target_r,
                "cooldown_bars": self.spec.cooldown_bars,
            },
            "risk": {
                "initial_equity": str(INITIAL_EQUITY),
                "currency": "USD_RESEARCH",
                "max_leverage": int(MAX_LEVERAGE),
                "risk_fraction": str(RISK_FRACTION),
                "capital_model": "PAPER_10000_RISK_2PCT_5X",
            },
            "source_config_sha256": self.spec.config_sha256,
            "source_metrics_sha256": self.spec.metrics_sha256,
        }
        root = Path(directory).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"{sid}.sqlite3"

        if not self.path.exists():
            now_utc = datetime.now(UTC)
            act_iso = (activated_at or now_utc).astimezone(UTC).isoformat() if enabled else None
            with self._tx() as db:
                db.execute("CREATE TABLE snapshot (value TEXT NOT NULL)")
                db.execute("CREATE TABLE fills (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute(
                    "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "at TEXT NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL)"
                )
                init_state = {
                    "strategy_id": sid,
                    "source_experiment": self.spec.source_experiment,
                    "name": self.spec.name,
                    "group": self.spec.group,
                    "family": self.spec.family,
                    "version": self.spec.version,
                    "timeframe": self.spec.timeframe,
                    "config_sha256": self.spec.config_sha256,
                    "observation_tier": OBSERVATION_TIER,
                    "original_evidence_status": ORIGINAL_EVIDENCE_STATUS,
                    "enabled": bool(enabled),
                    "paused_by_user": False,
                    "registered_at": now_utc.isoformat(),
                    "activated_at": act_iso,
                    "cash": str(INITIAL_EQUITY),
                    "realized": "0",
                    "position": None,
                    "pending_entry": None,
                    "pending_exit": None,
                    "last_cycle": None,
                    "last_closed_signal_cycle": None,
                    "next_cycle": next_native_close_iso(now_utc, self.spec.step_ms),
                    "last_quote_at": None,
                    "last_status": "WAITING_SIGNAL" if enabled else "DISABLED",
                    "last_action": "WAIT" if enabled else "REGISTERED",
                    "last_rejection_reason": None,
                    "fault_reason": None,
                    "reconciling": False,
                    "last_protection_check_at": None,
                    "warmup_bars": 0,
                    "warmup_daily_bars": 0,
                    "warmup_through": None,
                    "cooldown_remaining": 0,
                }
                db.execute("INSERT INTO snapshot VALUES (?)", (json.dumps(init_state, sort_keys=True, ensure_ascii=False),))
                self._audit(db, "REGISTERED", {"version": self.spec.version, "config_sha256": self.spec.config_sha256, "enabled": bool(enabled)})
        else:
            with self._tx() as db:
                st = self._read(db)
                if st.get("strategy_id") != sid:
                    raise ValueError(f"LEDGER_IDENTITY_MISMATCH:{sid}")
                if st.get("group") != self.spec.group or st.get("family") != self.spec.family:
                    st["group"] = self.spec.group
                    st["family"] = self.spec.family
                    self._save(db, st)
        self._last_mark_write_mono: float = 0.0
        self._cached_snapshot: Dict[str, Any] = {}
        self._cached_fees: D = D(0)
        self._cached_fill_count: int = 0
        self._refresh_memory_summary()

    def _refresh_memory_summary(self) -> None:
        with self._tx() as db:
            st = self._read(db)
            fills = [json.loads(r[0]) for r in db.execute("SELECT value FROM fills ORDER BY rowid").fetchall()]
        self._cached_fees = sum((D(f["fee"]) for f in fills), D(0))
        self._cached_fill_count = len(fills)
        self._sync_fast_flags(st)

    def _sync_fast_flags(self, st: Dict[str, Any]) -> None:
        self._cached_snapshot = dict(st)
        self._has_pos = bool(st.get("position"))
        self._has_pending_entry = bool(st.get("pending_entry"))
        self._has_pending_exit = bool(st.get("pending_exit"))
        self._cached_pos = dict(st["position"]) if st.get("position") else None
        self._cached_fault = bool(st.get("fault_reason"))

    @contextmanager
    def _tx(self):
        conn = sqlite3.connect(str(self.path), timeout=5)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _read(db: sqlite3.Connection) -> Dict[str, Any]:
        return json.loads(db.execute("SELECT value FROM snapshot LIMIT 1").fetchone()[0])

    @staticmethod
    def _save(db: sqlite3.Connection, state: Dict[str, Any]) -> None:
        db.execute("UPDATE snapshot SET value=?", (json.dumps(state, sort_keys=True, ensure_ascii=False),))

    def _save_and_sync(self, db: sqlite3.Connection, state: Dict[str, Any]) -> None:
        self._save(db, state)
        self._sync_fast_flags(state)

    @staticmethod
    def _audit(db: sqlite3.Connection, event: str, details: Dict[str, Any], at: Optional[str] = None) -> None:
        db.execute(
            "INSERT INTO audit_log(at, event, details) VALUES(?,?,?)",
            (at or datetime.now(UTC).isoformat(), event, json.dumps(details, sort_keys=True, ensure_ascii=False)),
        )

    def snapshot(self) -> Dict[str, Any]:
        with self._tx() as db:
            return self._read(db)

    def fills(self) -> List[Dict[str, Any]]:
        with self._tx() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT value FROM fills ORDER BY rowid").fetchall()]

    def diagnostics(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._tx() as db:
            rows = db.execute(
                "SELECT id, at, event, details FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            out = []
            for rid, at, event, details in rows:
                try:
                    det = json.loads(details)
                except Exception:
                    det = {"raw": details}
                out.append({"id": rid, "at": at, "event": event, "details": det})
            return out

    def prewarm_from_context(self, ctx: ThcAyCycleContext) -> Dict[str, Any]:
        """Record warmup depth without generating any retrospective trades."""
        bars = ctx.bars_15m
        if not bars:
            return {"bars": 0, "daily_bars": 0, "through": None}
        latest = bars[-1]
        daily_cnt = ctx.closed_daily_count_at(latest["close_ms"])
        through = latest["close_at"]
        through_dt = parse_utc(through)
        with self._tx() as db:
            st = self._read(db)
            if st.get("last_cycle") is None:
                st["last_cycle"] = through
            st["warmup_bars"] = len(bars)
            st["warmup_daily_bars"] = daily_cnt
            st["warmup_through"] = through
            st["next_cycle"] = next_native_close_iso(through_dt, self.spec.step_ms)
            st["pending_entry"] = None
            self._audit(db, "PREWARM", {"bars_15m": len(bars), "daily_bars": daily_cnt, "through": through})
            self._save_and_sync(db, st)
        return {"bars": len(bars), "daily_bars": daily_cnt, "through": through}

    def set_enabled(self, enabled: bool, *, paused_by_user: bool = False) -> Dict[str, Any]:
        with self._tx() as db:
            st = self._read(db)
            st["enabled"] = bool(enabled)
            st["paused_by_user"] = bool(paused_by_user) if not enabled else False
            if enabled and not st.get("activated_at"):
                st["activated_at"] = datetime.now(UTC).isoformat()
            if not enabled:
                st["pending_entry"] = None  # Block new entries; existing position stays protected!
            st["last_status"] = classify_unified_status(
                enabled=st["enabled"],
                paused_by_user=st["paused_by_user"],
                market_ready=True,
                reconciling=bool(st.get("reconciling")),
                fault_reason=st.get("fault_reason"),
                holding=bool(st.get("position")),
                waiting_signal=not bool(st.get("position")),
            )
            self._audit(db, "SET_ENABLED", {"enabled": st["enabled"], "paused_by_user": st["paused_by_user"]})
            self._save_and_sync(db, st)
            return st

    def reconcile(self) -> Dict[str, Any]:
        with self._tx() as db:
            st = self._read(db)
            fills = [json.loads(r[0]) for r in db.execute("SELECT value FROM fills ORDER BY rowid").fetchall()]
            issues: List[str] = []
            seen = set()
            net_qty = D(0)
            calc_cash = INITIAL_EQUITY
            calc_realized = D(0)
            for f in fills:
                k = f.get("idempotency_key")
                if not k or k in seen:
                    issues.append("DUPLICATE_OR_MISSING_IDEMPOTENCY_KEY")
                seen.add(k)
                q = D(str(f["qty"]))
                fee = D(str(f["fee"]))
                if f["kind"] == "OPEN":
                    net_qty += q if f["direction"] == "LONG" else -q
                    calc_cash -= fee
                elif f["kind"] == "CLOSE":
                    net_qty -= q if f["direction"] == "LONG" else -q
                    calc_cash += D(str(f["gross"])) - fee
                    calc_realized += D(str(f["net"]))
            pos = st.get("position")
            expected_net = (D(str(pos["qty"])) * (D(1) if pos["direction"] == "LONG" else D(-1))) if pos else D(0)
            if pos and (D(str(pos.get("stop", "0"))) <= 0 or D(str(pos.get("target", "0"))) <= 0):
                issues.append("POSITION_PROTECTION_PARAMS_INVALID")
            if net_qty != expected_net:
                issues.append(f"POSITION_QTY_MISMATCH:{net_qty}!={expected_net}")
            if abs(D(str(st["cash"])) - calc_cash) > D("0.0001"):
                issues.append(f"CASH_MISMATCH:{st['cash']}!={calc_cash}")
            if abs(D(str(st["realized"])) - calc_realized) > D("0.0001"):
                issues.append(f"REALIZED_MISMATCH:{st['realized']}!={calc_realized}")
            if st.get("pending_exit") and not pos:
                issues.append("ORPHAN_PENDING_EXIT")
            st["fault_reason"] = ";".join(issues) if issues else None
            st["last_protection_check_at"] = datetime.now(UTC).isoformat()
            self._audit(db, "RECONCILE", {"ok": not issues, "issues": issues, "fill_count": len(fills)})
            self._save_and_sync(db, st)
            return {"ok": not issues, "issues": issues, "fill_count": len(fills)}

    def _insert_fill(self, db: sqlite3.Connection, idem_key: str, payload: Dict[str, Any]) -> bool:
        cur = db.execute(
            "INSERT OR IGNORE INTO fills(id, value) VALUES(?, ?)",
            (idem_key, json.dumps(payload, sort_keys=True, ensure_ascii=False)),
        )
        if cur.rowcount == 1:
            self._cached_fees += D(str(payload.get("fee", "0")))
            self._cached_fill_count += 1
            return True
        return False

    def _settle_close(self, db, st, *, event_at: str, outcome: Dict[str, Any], reason: str, idem_key: str) -> str:
        pos = st["position"]
        close_side = "SELL" if pos["direction"] == "LONG" else "BUY"
        fill_payload = {
            "idempotency_key": idem_key,
            "strategy_id": self.sid,
            "version": pos["version"],
            "trade_id": pos["trade_id"],
            "signal_id": pos.get("signal_id"),
            "signal_source": pos.get("signal_source"),
            "kind": "CLOSE",
            "side": close_side,
            "direction": pos["direction"],
            "at": event_at,
            "cycle": event_at,
            "qty": pos["qty"],
            "price": str(outcome["price"]),
            "fee": str(outcome["fee"]),
            "slippage": str(outcome["slippage"]),
            "gross": str(outcome["gross"]),
            "net": str(outcome["net"]),
            "reason": reason,
        }
        if not self._insert_fill(db, idem_key, fill_payload):
            return "DUPLICATE_EXIT_IGNORED"
        st["cash"] = str(D(st["cash"]) + outcome["gross"] - outcome["fee"])
        st["realized"] = str(D(st["realized"]) + outcome["net"])
        st["position"] = None
        st["pending_exit"] = None
        if self.spec.cooldown_bars > 0:
            st["cooldown_remaining"] = self.spec.cooldown_bars + 1
        st["last_action"] = f"CLOSE_{reason}"
        return f"CLOSE_{reason}"

    def _check_bar_close_exit(self, pos: Dict[str, Any], latest_15m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Check conservative STOP -> TARGET -> MAX_HOLD on closed 15m bar."""
        doc_for_close = dict(self.doc)
        if self.spec.max_hold_bars is None:
            # S-series has no max_hold_bars; use a very large cap so only STOP/TARGET trigger
            doc_for_close["position"] = {**self.doc["position"], "max_hold_bars": 10**9}
        return StrategyPool._close_position(
            pos, doc_for_close, [latest_15m], D(pos["fee_rate"]), D(pos["slippage_rate"])
        )

    def on_closed_cycle(
        self,
        ctx: ThcAyCycleContext,
        *,
        guard: CommonMarketGuard,
        close: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        bars = ctx.bars_15m
        if not bars:
            raise ValueError("EMPTY_BARS")
        latest = bars[-1]
        cycle_dt = parse_utc(latest["close_at"])
        cycle_iso = cycle_dt.isoformat()
        close_ms = latest["close_ms"]

        with self._tx() as db:
            st = self._read(db)
            if st.get("fault_reason"):
                return {"status": "RECONCILE_ERROR", "reason": st["fault_reason"]}
            last_dt = parse_utc(st["last_cycle"]) if st.get("last_cycle") else None
            if last_dt is not None and cycle_dt <= last_dt:
                return {"status": "DUPLICATE_OR_STALE", "cycle": cycle_iso}

            gap = last_dt is not None and (cycle_dt - last_dt) != STEP_15M
            pos = st.get("position")
            action = "WAIT"

            if pos and gap:
                st["fault_reason"] = "POSITION_GAP_RECONCILIATION_REQUIRED"
                st["last_action"] = "BLOCKED_GAP_RECONCILIATION_REQUIRED"
                self._audit(db, "GAP_BLOCK", {"from": st["last_cycle"], "to": cycle_iso}, at=cycle_iso)
                self._save_and_sync(db, st)
                return {"status": "RECONCILE_ERROR", "reason": "POSITION_GAP_RECONCILIATION_REQUIRED"}

            if st.get("pending_entry"):
                st["pending_entry"] = None
                action = "ENTRY_EXPIRED"

            if pos and not st.get("pending_exit"):
                outcome = self._check_bar_close_exit(pos, latest)
                if outcome is not None:
                    if not guard.quote_ready:
                        st["pending_exit"] = {
                            "trade_id": pos["trade_id"],
                            "reason": outcome["reason"],
                            "trigger_price": str(outcome["price"]),
                            "triggered_at": cycle_iso,
                            "event_key": f"{self.sid}:exit:{pos['trade_id']}:{outcome['reason']}",
                        }
                        action = f"EXIT_PENDING_{outcome['reason']}"
                        self._audit(db, "EXIT_PENDING", st["pending_exit"], at=cycle_iso)
                    else:
                        idem = f"{self.sid}:close:{pos['trade_id']}:{outcome['reason']}"
                        action = self._settle_close(
                            db, st, event_at=cycle_iso, outcome=outcome, reason=outcome["reason"], idem_key=idem
                        )
                else:
                    pos["bars_held"] = int(pos.get("bars_held", 0)) + 1
                    pos["mark"] = str(latest["close"])
                    action = "HOLD"

            # Decrement cooldown counter on closed 15m bar when flat
            cd = int(st.get("cooldown_remaining") or 0)
            if not st.get("position") and cd > 0:
                cd = max(0, cd - 1)
                st["cooldown_remaining"] = cd

            act_dt = parse_utc(st["activated_at"]) if st.get("activated_at") else None
            after_activation = (act_dt is None) or (cycle_dt >= act_dt)
            st["warmup_bars"] = len(bars)
            st["warmup_daily_bars"] = ctx.closed_daily_count_at(close_ms)
            st["next_cycle"] = next_native_close_iso(cycle_dt, self.spec.step_ms)

            if gap or not guard.entry_ready:
                if not st.get("position"):
                    action = "GAP_NO_ENTRY" if gap else f"MARKET_BLOCKED:{guard.reason}"
            elif not st.get("enabled") or not after_activation:
                if not st.get("position"):
                    action = "PAUSED" if st.get("paused_by_user") else "WAITING_ACTIVATION"
            elif not st.get("position") and not st.get("pending_exit"):
                if self.spec.is_s_family:
                    sig, sig_status, native_iso = evaluate_s_signal(self.spec, ctx, close_ms)
                else:
                    sig, sig_status, native_iso = evaluate_ay_signal(self.spec, ctx)
                if native_iso is not None:
                    st["last_closed_signal_cycle"] = native_iso
                if sig is not None:
                    if cd > 0:
                        st["last_rejection_reason"] = "COOLDOWN"
                        self._audit(db, "SIGNAL_REJECTED", {"reason": "COOLDOWN", "signal_id": sig["signal_id"]}, at=cycle_iso)
                        action = "REJECTED:COOLDOWN"
                    else:
                        st["pending_entry"] = sig
                        action = f"PENDING_NEXT_OPEN:{sig['direction']}"
                        self._audit(db, "SIGNAL_PENDING", {"signal_id": sig["signal_id"], "direction": sig["direction"], "cycle": sig["signal_cycle"]}, at=cycle_iso)
                else:
                    action = sig_status

            st["last_cycle"] = cycle_iso
            st["last_action"] = action
            st["last_protection_check_at"] = datetime.now(UTC).isoformat()
            st["last_status"] = classify_unified_status(
                enabled=bool(st.get("enabled")),
                paused_by_user=bool(st.get("paused_by_user")),
                market_ready=guard.entry_ready,
                reconciling=False,
                fault_reason=st.get("fault_reason"),
                holding=bool(st.get("position")),
                waiting_signal=not bool(st.get("position")),
            )
            self._save_and_sync(db, st)
            return {
                "status": st["last_status"],
                "action": action,
                "cycle": cycle_iso,
                "last_closed_signal_cycle": st.get("last_closed_signal_cycle"),
                "next_cycle": st.get("next_cycle"),
            }

    def on_quote(self, quote: Dict[str, Any], *, guard: CommonMarketGuard) -> Dict[str, Any]:
        # Fast-path: zero disk I/O when flat with no pending entry/exit
        if not (self._has_pos or self._has_pending_entry or self._has_pending_exit or self._cached_fault):
            return {"status": "NO_POSITION"}
        fee_rate = D(str(self.cost.fee_rate))
        slip = D(str(self.cost.adverse_slippage_bps)) / D("10000")
        try:
            bid = D(str(quote["bid"]))
            ask = D(str(quote["ask"]))
            age_ms = int(quote.get("age_ms", 10**9))
            valid_q = (
                quote.get("source") == "BINANCE_USDM_PERP"
                and quote.get("symbol") == "BTC/USD"
                and quote.get("currency") == "USD_RESEARCH"
                and quote.get("timestamp_valid") is True
                and 0 <= age_ms <= self.cost.max_quote_age_ms
                and bid.is_finite() and ask.is_finite() and 0 < bid <= ask
            )
            event_dt = parse_utc(quote["event_at"])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return {"status": "INVALID_QUOTE"}

        if not valid_q or not guard.quote_ready:
            return {"status": "QUOTE_STALE_OR_NOT_READY"}

        if self._has_pos and not self._has_pending_exit and not self._has_pending_entry and self._cached_pos:
            cp = self._cached_pos
            long_p = cp["direction"] == "LONG"
            hit_s, _, _ = stop_triggered(cp["direction"], D(cp["stop"]), bid, ask)
            hit_t = (bid >= D(cp["target"])) if long_p else (ask <= D(cp["target"]))
            now_m = time.monotonic()
            if not hit_s and not hit_t and (now_m - self._last_mark_write_mono) < 5.0:
                return {"status": "HOLD"}
            self._last_mark_write_mono = now_m

        event_iso = event_dt.isoformat()
        with self._tx() as db:
            st = self._read(db)
            if st.get("fault_reason"):
                return {"status": "RECONCILE_ERROR", "reason": st["fault_reason"]}
            if st.get("last_quote_at") and event_dt <= parse_utc(st["last_quote_at"]):
                return {"status": "DUPLICATE_OR_STALE_QUOTE"}
            st["last_quote_at"] = event_iso
            st["last_protection_check_at"] = datetime.now(UTC).isoformat()

            pos = st.get("position")
            if pos and st.get("pending_exit"):
                pe = st["pending_exit"]
                long_pos = pos["direction"] == "LONG"
                ref_px = bid if long_pos else ask
                side_sign = D(1) if long_pos else D(-1)
                px = ref_px * (D(1) - slip * side_sign)
                qty = D(pos["qty"])
                gross = (px - D(pos["entry"])) * qty * side_sign
                fee = abs(px * qty) * fee_rate
                net = gross - D(pos.get("entry_fee", "0")) - fee
                outcome = {"price": px, "gross": gross, "fee": fee, "net": net, "slippage": abs(ref_px * qty - px * qty), "reason": pe["reason"]}
                idem = f"{self.sid}:close:{pos['trade_id']}:{pe['reason']}"
                action = self._settle_close(db, st, event_at=event_iso, outcome=outcome, reason=f"{pe['reason']}_RECOVERED", idem_key=idem)
                self._save_and_sync(db, st)
                return {"status": action, "recovered": True}

            if pos:
                long_pos = pos["direction"] == "LONG"
                ref_px = bid if long_pos else ask
                pos["mark"] = str(ref_px)
                stop_px, target_px = D(pos["stop"]), D(pos["target"])
                hit_stop, _, _ = stop_triggered(pos["direction"], stop_px, bid, ask)
                hit_target = (bid >= target_px) if long_pos else (ask <= target_px)
                if hit_stop or hit_target:
                    reason = "STOP_QUOTE" if hit_stop else "TARGET_QUOTE"
                    side_sign = D(1) if long_pos else D(-1)
                    px = ref_px * (D(1) - slip * side_sign)
                    qty = D(pos["qty"])
                    gross = (px - D(pos["entry"])) * qty * side_sign
                    fee = abs(px * qty) * fee_rate
                    net = gross - D(pos.get("entry_fee", "0")) - fee
                    outcome = {"price": px, "gross": gross, "fee": fee, "net": net, "slippage": abs(ref_px * qty - px * qty), "reason": reason}
                    idem = f"{self.sid}:close:{pos['trade_id']}:{reason}"
                    action = self._settle_close(db, st, event_at=event_iso, outcome=outcome, reason=reason, idem_key=idem)
                    self._save_and_sync(db, st)
                    return {"status": action}
                self._save_and_sync(db, st)
                return {"status": "HOLD"}

            if not pos and st.get("pending_entry"):
                sig = st["pending_entry"]
                sig_dt = parse_utc(sig["signal_cycle"])
                if event_dt < sig_dt:
                    self._save_and_sync(db, st)
                    return {"status": "EARLY_QUOTE"}
                st["pending_entry"] = None
                if event_dt >= sig_dt + STEP_15M:
                    st["last_action"] = "ENTRY_EXPIRED"
                    self._audit(db, "ENTRY_EXPIRED", {"signal_id": sig["signal_id"], "at": event_iso}, at=event_iso)
                    self._save_and_sync(db, st)
                    return {"status": "ENTRY_EXPIRED"}
                if not st.get("enabled") or not guard.entry_ready:
                    st["last_action"] = "ENTRY_BLOCKED"
                    self._audit(db, "ENTRY_BLOCKED", {"signal_id": sig["signal_id"], "enabled": st.get("enabled"), "guard": guard.reason}, at=event_iso)
                    self._save_and_sync(db, st)
                    return {"status": "ENTRY_BLOCKED"}

                direction = sig["direction"]
                ref_px = ask if direction == "LONG" else bid
                side_sign = D(1) if direction == "LONG" else D(-1)
                entry_px = ref_px * (D(1) + slip * side_sign)

                if self.spec.is_s_family:
                    sl_f, tp_f = compute_s_sl_tp(sig["sub_strategy"], direction, float(entry_px), sig, self.spec.parameters)
                    stop_px, target_px = D(str(sl_f)), D(str(tp_f))
                    if stop_px <= 0 or target_px <= 0 or (entry_px - stop_px) * side_sign <= 0 or (target_px - entry_px) * side_sign <= 0:
                        reason = "INVALID_SL_TP_GEOMETRY"
                        st["last_action"] = f"ENTRY_REJECTED:{reason}"
                        st["last_rejection_reason"] = reason
                        self._audit(db, "ENTRY_REJECTED", {"signal_id": sig["signal_id"], "reason": reason}, at=event_iso)
                        self._save_and_sync(db, st)
                        return {"status": "ENTRY_REJECTED", "reason": reason}
                else:
                    stop_px = D(str(sig["stop"]))
                    exp_target = D(str(sig["target"])) if sig.get("target") is not None else None
                    target_px, rej_reason = validate_ay_entry(
                        direction, stop_px, exp_target, sig.get("target_r"), ref_px, entry_px, fee_rate, slip
                    )
                    if rej_reason or target_px is None:
                        st["last_action"] = f"ENTRY_REJECTED:{rej_reason}"
                        st["last_rejection_reason"] = rej_reason
                        self._audit(db, "ENTRY_REJECTED", {"signal_id": sig["signal_id"], "reason": rej_reason}, at=event_iso)
                        self._save_and_sync(db, st)
                        return {"status": "ENTRY_REJECTED", "reason": rej_reason}

                cash = D(st["cash"])
                book = [BookLevel(entry_px, D("1e30"))]
                sized = calculate_position_size(
                    equity=cash,
                    available_cash=cash,
                    entry_price=entry_px,
                    stop_price=stop_px,
                    direction=direction,
                    bids=book,
                    asks=book,
                    cost=self.cost,
                    risk_fraction=RISK_FRACTION,
                    leverage=MAX_LEVERAGE,
                )
                if not sized.accepted:
                    st["last_action"] = f"ENTRY_REJECTED:{sized.reason}"
                    st["last_rejection_reason"] = sized.reason
                    self._audit(db, "ENTRY_REJECTED", {"signal_id": sig["signal_id"], "reason": sized.reason}, at=event_iso)
                    self._save_and_sync(db, st)
                    return {"status": "ENTRY_REJECTED", "reason": sized.reason}

                entry_fee = sized.notional * fee_rate
                entry_slip = abs(entry_px - ref_px) * sized.quantity
                trade_id = f"{self.sid}:{sig['signal_id']}"
                idem = f"{self.sid}:open:{sig['signal_id']}"
                new_pos = {
                    "version": self.spec.version,
                    "trade_id": trade_id,
                    "signal_id": sig["signal_id"],
                    "signal_cycle": sig["signal_cycle"],
                    "first_eligible_open_at": sig["signal_cycle"],
                    "opened_cycle": event_iso,
                    "signal_source": sig.get("signal_source"),
                    "sub_strategy": sig.get("sub_strategy"),
                    "direction": direction,
                    "entry": str(entry_px),
                    "reference_price": str(ref_px),
                    "stop": str(stop_px),
                    "target": str(target_px),
                    "qty": str(sized.quantity),
                    "entry_fee": str(entry_fee),
                    "margin": str(sized.notional / MAX_LEVERAGE),
                    "fee_rate": str(fee_rate),
                    "slippage_rate": str(slip),
                    "mark": str(ref_px),
                    "bars_held": 0,
                    "max_hold_bars": self.spec.max_hold_bars,
                }
                fill_payload = {
                    "idempotency_key": idem,
                    "strategy_id": self.sid,
                    "version": self.spec.version,
                    "trade_id": trade_id,
                    "signal_id": sig["signal_id"],
                    "signal_cycle": sig["signal_cycle"],
                    "signal_source": sig.get("signal_source"),
                    "kind": "OPEN",
                    "side": "BUY" if direction == "LONG" else "SELL",
                    "direction": direction,
                    "at": event_iso,
                    "cycle": event_iso,
                    "qty": str(sized.quantity),
                    "price": str(entry_px),
                    "fee": str(entry_fee),
                    "slippage": str(entry_slip),
                    "gross": "0",
                    "net": "0",
                    "reason": "ENTRY_NEXT_OPEN_QUOTE",
                }
                if not self._insert_fill(db, idem, fill_payload):
                    self._save_and_sync(db, st)
                    return {"status": "DUPLICATE_ENTRY_IGNORED"}
                st["cash"] = str(cash - entry_fee)
                st["position"] = new_pos
                st["last_action"] = f"OPEN_{direction}"
                self._audit(db, "OPEN", {"trade_id": trade_id, "direction": direction, "price": str(entry_px), "stop": str(stop_px), "target": str(target_px)}, at=event_iso)
                self._save_and_sync(db, st)
                return {"status": f"OPEN_{direction}", "trade_id": trade_id}

            self._save_and_sync(db, st)
            return {"status": "NO_POSITION"}


class ThcAyExtension:
    """Multi-instance Mode B manager for the 15 Aoying S / AY instances under data/extensions/."""

    def __init__(
        self,
        directory: str | Path,
        *,
        project_root: Optional[Path] = None,
        register: bool = False,
        enabled: Iterable[str] = (),
        excluded_ids: Iterable[str] = (),
        cost: Optional[CostModel] = None,
    ) -> None:
        self.root = Path(directory)
        self.project_root = project_root or self.root.resolve().parents[1]
        self.cost = cost or CostModel()
        self.ledgers: Dict[str, ThcAyPaperLedger] = {}
        self._prefix_15m: Optional[List[Dict[str, Any]]] = None
        self._inc_ctx: Optional[ThcAyCycleContext] = None
        enabled_set = {str(x).strip().lower() for x in enabled if str(x).strip()}
        excluded_set = {str(x).strip().lower() for x in excluded_ids if str(x).strip()}
        if register:
            self.root.mkdir(parents=True, exist_ok=True)
        for sid in THC_AY_IDS:
            if sid.lower() in excluded_set:
                continue
            db_path = self.root / f"{sid}.sqlite3"
            if db_path.exists():
                lg = ThcAyPaperLedger(self.root, sid, cost=self.cost)
                lg.reconcile()
                self.ledgers[sid] = lg
            elif register:
                lg = ThcAyPaperLedger(self.root, sid, cost=self.cost, enabled=(sid in enabled_set))
                self.ledgers[sid] = lg

    def unload(self, sid: str) -> bool:
        """Dynamically unload a strategy from memory (called by StrategyLifecycleManager)."""
        lg = self.ledgers.pop(str(sid), None)
        if lg is None:
            return False
        lg._cached_snapshot = {}
        lg._cached_pos = None
        if not self.ledgers:
            self._inc_ctx = None
            self._prefix_15m = None
        return True

    def _get_prefix_15m(self) -> List[Dict[str, Any]]:
        if self._prefix_15m is None:
            self._prefix_15m = load_extended_15m_prefix(self.project_root)
        return self._prefix_15m

    def build_context(self, live_raw_bars: List[Dict[str, Any]], *, close: Optional[datetime] = None) -> ThcAyCycleContext:
        c_ms = int(close.astimezone(UTC).timestamp() * 1000) if close is not None else None
        if self._inc_ctx is not None and self._inc_ctx.bars_15m:
            last_ctx_ms = self._inc_ctx.bars_15m[-1]["close_ms"]
            # Fast incremental O(1) path: live bars advance forward without re-merging 52,783 bars!
            if c_ms is None or c_ms >= last_ctx_ms:
                new_tail: List[Dict[str, Any]] = []
                for raw in reversed(live_raw_bars):
                    if raw.get("is_closed") is False:
                        continue
                    nb = raw if ("t" in raw and "close_ms" in raw and "o" in raw) else normalize_closed_15m_bar(raw)
                    if c_ms is not None and nb["close_ms"] > c_ms:
                        continue
                    if nb["close_ms"] <= last_ctx_ms:
                        break
                    new_tail.append(nb)
                if new_tail:
                    new_tail.reverse()
                    self._inc_ctx.extend_15m(new_tail)
                return self._inc_ctx
            # Historical/replay slice earlier than cached head: return sliced view without mutating live cache
            sliced = [b for b in self._inc_ctx.bars_15m if b["close_ms"] <= c_ms]
            return ThcAyCycleContext(sliced)

        merged = merge_15m_bars(self._get_prefix_15m(), live_raw_bars)
        if c_ms is not None:
            merged = [b for b in merged if b["close_ms"] <= c_ms]
        ctx = ThcAyCycleContext(merged)
        if c_ms is None or (merged and merged[-1]["close_ms"] == c_ms):
            self._inc_ctx = ctx
            # Release duplicate 52,704-bar prefix list (~52.7 MB RAM saved permanently!)
            self._prefix_15m = None
        return ctx

    def prewarm_all(self, live_raw_bars: List[Dict[str, Any]]) -> Dict[str, Any]:
        ctx = self.build_context(live_raw_bars)
        out = {}
        for sid, lg in self.ledgers.items():
            st = lg.snapshot()
            if st.get("warmup_bars", 0) == 0 or st.get("warmup_daily_bars", 0) < lg.spec.min_daily_warmup:
                out[sid] = lg.prewarm_from_context(ctx)
        return out

    def cycle_all(self, live_raw_bars: List[Dict[str, Any]], close: datetime, guard: CommonMarketGuard) -> Dict[str, Any]:
        ctx = self.build_context(live_raw_bars, close=close)
        out = {}
        for sid, lg in self.ledgers.items():
            out[sid] = lg.on_closed_cycle(ctx, guard=guard, close=close)
        return out

    def ledger_quote(self, sid: str, quote: Dict[str, Any], guard: CommonMarketGuard) -> Dict[str, Any]:
        return self.ledgers[sid].on_quote(quote, guard=guard)

    def set_enabled(self, sid: str, enabled: bool) -> Dict[str, Any]:
        return self.ledgers[sid].set_enabled(enabled, paused_by_user=not enabled)

    def diagnostics(self, sid: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self.ledgers[sid].diagnostics(limit=limit)

    def versions(self, sid: str) -> List[Dict[str, Any]]:
        lg = self.ledgers[sid]
        doc_str = json.dumps(lg.doc, sort_keys=True, ensure_ascii=False)
        st = lg._cached_snapshot or lg.snapshot()
        return [{
            "strategy_id": sid,
            "version": lg.spec.version,
            "sha256": lg.spec.config_sha256,
            "document_sha256": hashlib.sha256(doc_str.encode("utf-8")).hexdigest(),
            "document": doc_str,
            "uploaded_at": st.get("registered_at") or "",
            "enabled_at": st.get("activated_at") if st.get("enabled") else None,
        }]

    def list(self, *, market_ready: Optional[bool] = None) -> List[Dict[str, Any]]:
        items = []
        for sid, lg in list(self.ledgers.items()):
            s = lg._cached_snapshot or lg.snapshot()
            p = s.get("position")
            cash = D(s["cash"])
            realized = D(s.get("realized", "0"))
            unreal = D(0)
            if p:
                mark = D(p.get("mark", p["entry"]))
                unreal = (mark - D(p["entry"])) * D(p["qty"]) * (D(1) if p["direction"] == "LONG" else D(-1))
            fees = lg._cached_fees
            fill_count = lg._cached_fill_count
            enabled = bool(s.get("enabled"))
            status = classify_unified_status(
                enabled=enabled,
                paused_by_user=bool(s.get("paused_by_user")),
                market_ready=True if market_ready is None else bool(market_ready),
                reconciling=bool(s.get("reconciling")),
                fault_reason=s.get("fault_reason"),
                holding=bool(p),
                waiting_signal=not bool(p),
            )
            items.append({
                "id": sid,
                "name": lg.spec.name,
                "group": lg.spec.group,
                "strategy_group": lg.spec.group,
                "family": lg.spec.family,
                "timeframe": lg.spec.timeframe,
                "source_experiment": lg.spec.source_experiment,
                "active_version": lg.spec.version,
                "latest_version": lg.spec.version,
                "config_sha256": lg.spec.config_sha256,
                "candidate_ref": lg.doc["candidate_ref"],
                "observation_tier": OBSERVATION_TIER,
                "original_evidence_status": ORIGINAL_EVIDENCE_STATUS,
                "description": lg.spec.description,
                "enabled": enabled,
                "status": status,
                "status_label": UNIFIED_STATUS_LABELS.get(status, status),
                "position_direction": p["direction"] if p else "FLAT",
                "position_amount": str(D(p["entry"]) * D(p["qty"])) if p else "0",
                "initial_equity": str(INITIAL_EQUITY),
                "cash": str(cash),
                "available_cash": str(cash),
                "equity": str(cash + unreal),
                "realized": str(realized),
                "unrealized": str(unreal),
                "fees": str(fees),
                "fill_count": fill_count,
                "position": p,
                "stop_price": p.get("stop") if p else None,
                "target_price": p.get("target") if p else None,
                "last_cycle": s.get("last_cycle"),
                "last_closed_signal_cycle": s.get("last_closed_signal_cycle"),
                "next_cycle": s.get("next_cycle"),
                "last_status": status,
                "last_action": s.get("last_action") or "WAIT",
                "last_rejection_reason": s.get("last_rejection_reason"),
                "warmup_bars": s.get("warmup_bars", 0),
                "warmup_daily_bars": s.get("warmup_daily_bars", 0),
                "activated_at": s.get("activated_at"),
                "is_extension": True,
            })
        return items
