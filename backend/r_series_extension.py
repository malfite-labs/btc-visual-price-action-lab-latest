"""Production V3.1 R-Series (R01-R06 V1.1.0) Extension Adapter.

Mirrors backend/pa_extension.py with strict isolation:
- Independent SQLite ledgers under data/r-series/R01..R06.sqlite3 ($10,000 USD_RESEARCH, 5x, 2% risk)
- Zero modification to existing 16 strategies or public protection engine
- Directly reuses evaluate_common_market_guard, classify_unified_status, CostModel,
  StrategyPool._new_position, StrategyPool._close_position, stop_triggered
- Pre-warms TD Sequential / ATR / EMA / Volume SMA / 1h & 4h state from full closed
  candle archive without generating any retrospective fills before activated_at
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.common_guard import (
    CommonMarketGuard,
    UNIFIED_STATUS_LABELS,
    classify_unified_status,
)
from backend.domain import CostModel, stop_triggered
from backend.strategy_pool import FORMAT_V2, StrategyPool, StrategyValidationError
from strategies.r_series.indicators import normalize_closed_15m_bar, parse_utc
from strategies.r_series.strategies import R_SERIES_IDS
from strategies.r_series.v11_engine import MultiTFContextV11, R_V11_SPECS, RSignalEngineV11

UTC = timezone.utc
STEP = timedelta(minutes=15)
INITIAL_EQUITY = D("10000")
R_SERIES_NAMES = {sid: R_V11_SPECS[sid].name for sid in R_SERIES_IDS}


class RSeriesPaperLedgerV11:
    """Durable SQLite paper ledger for one frozen R-Series V1.1.0 strategy."""

    def __init__(
        self,
        directory: str | Path,
        sid: str,
        *,
        cost: Optional[CostModel] = None,
        enabled: bool = False,
        activated_at: Optional[datetime] = None,
    ) -> None:
        if sid not in R_V11_SPECS:
            raise ValueError(f"UNKNOWN_R_SERIES_ID:{sid}")
        self.sid = sid
        self.spec = R_V11_SPECS[sid]
        self.cost = cost or CostModel()
        self.doc = {
            "format": FORMAT_V2,
            "strategy_id": sid,
            "name": self.spec.name,
            "version": self.spec.version,
            "candidate_ref": self.spec.candidate_ref,
            "observation_tier": self.spec.observation_tier,
            "group": "R系列",
            "description": self.spec.description,
            "parameters": dict(self.spec.parameters),
            "position": {
                "direction": "both",
                "entry_timing": "next_open",
                "same_bar_priority": "stop",
                "max_hold_bars": self.spec.max_hold_bars,
                "target_r": self.spec.target_r,
            },
            "risk": {
                "initial_equity": str(INITIAL_EQUITY),
                "currency": "USD_RESEARCH",
                "max_leverage": 5,
                "risk_fraction": "0.02",
                "capital_model": "PAPER_10000_RISK_2PCT_5X",
            },
        }
        root = Path(directory).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"{sid}.sqlite3"
        self.engine = RSignalEngineV11(sid)

        if not self.path.exists():
            act_iso = (activated_at or datetime.now(UTC)).astimezone(UTC).isoformat() if enabled else None
            with self._tx() as db:
                db.execute("CREATE TABLE snapshot (value TEXT NOT NULL)")
                db.execute("CREATE TABLE fills (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute(
                    "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "at TEXT NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL)"
                )
                init_state = {
                    "strategy_id": sid,
                    "name": self.spec.name,
                    "group": "R系列",
                    "version": self.spec.version,
                    "candidate_ref": self.spec.candidate_ref,
                    "observation_tier": self.spec.observation_tier,
                    "enabled": bool(enabled),
                    "paused_by_user": False,
                    "activated_at": act_iso,
                    "cash": str(INITIAL_EQUITY),
                    "realized": "0",
                    "position": None,
                    "pending_entry": None,
                    "pending_exit": None,
                    "last_cycle": None,
                    "last_quote_at": None,
                    "last_status": "WAITING_SIGNAL" if enabled else "DISABLED",
                    "last_action": "WAIT" if enabled else "REGISTERED",
                    "last_rejection_reason": None,
                    "fault_reason": None,
                    "reconciling": False,
                    "last_protection_check_at": None,
                    "warmup_bars": 0,
                    "warmup_through": None,
                    "signal_state": self.engine.export_state(),
                }
                db.execute("INSERT INTO snapshot VALUES (?)", (json.dumps(init_state, sort_keys=True, ensure_ascii=False),))
        else:
            st = self.snapshot()
            if st.get("strategy_id") != sid:
                raise ValueError("R_SERIES_LEDGER_IDENTITY_MISMATCH")
            self.engine.load_state(st.get("signal_state"))
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

    def prewarm_from_history(self, raw_bars: List[Dict[str, Any]], *, activated_at: Optional[datetime] = None) -> Dict[str, Any]:
        """Pre-warm signal state from fixed historical origin WITHOUT generating any retrospective trades."""
        bars = [normalize_closed_15m_bar(b) for b in raw_bars if b.get("is_closed") is not False]
        if not bars:
            return {"bars": 0, "through": None}
        ctx = MultiTFContextV11(bars)
        eng = RSignalEngineV11(self.sid)
        for i in range(len(bars)):
            eng.step(ctx, i, has_open_position=False)
        # Clear any historical unconsumed candidate so we ONLY accept new signals after activation!
        eng.reset_transient()
        through = bars[-1]["close_at"]
        act_iso = (activated_at or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self._tx() as db:
            st = self._read(db)
            if st.get("last_cycle") is None:
                st["last_cycle"] = through
            st["warmup_bars"] = len(bars)
            st["warmup_through"] = through
            st["signal_state"] = eng.export_state()
            st["pending_entry"] = None
            if st.get("enabled") and not st.get("activated_at"):
                st["activated_at"] = act_iso
            self._audit(db, "PREWARM", {"bars": len(bars), "through": through, "activated_at": st.get("activated_at")})
            self._save(db, st)
        self.engine.load_state(eng.export_state())
        return {"bars": len(bars), "through": through}

    def set_enabled(self, enabled: bool, *, paused_by_user: bool = False) -> Dict[str, Any]:
        with self._tx() as db:
            st = self._read(db)
            st["enabled"] = bool(enabled)
            st["paused_by_user"] = bool(paused_by_user) if not enabled else False
            if enabled and not st.get("activated_at"):
                st["activated_at"] = datetime.now(UTC).isoformat()
            if not enabled:
                st["pending_entry"] = None  # Block new entries; existing position & pending_exit stay protected!
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
        cur = db.execute("INSERT OR IGNORE INTO fills(id, value) VALUES(?, ?)", (idem_key, json.dumps(payload, sort_keys=True, ensure_ascii=False)))
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
        st["last_action"] = f"CLOSE_{reason}"
        return f"CLOSE_{reason}"

    def on_closed_cycle(
        self,
        raw_bars: List[Dict[str, Any]],
        *,
        guard: CommonMarketGuard,
        close: Optional[datetime] = None,
        shared_ctx: Optional[MultiTFContextV11] = None,
    ) -> Dict[str, Any]:
        if shared_ctx is not None:
            ctx = shared_ctx
            bars = ctx.bars_15m
            if not bars:
                raise ValueError("EMPTY_BARS")
        else:
            bars = [b if ("t" in b and "close_ms" in b and "o" in b) else normalize_closed_15m_bar(b) for b in raw_bars if b.get("is_closed") is not False]
            if not bars:
                raise ValueError("EMPTY_BARS")
            if close is not None:
                c_ms = int(close.astimezone(UTC).timestamp() * 1000)
                bars = [b for b in bars if b["close_ms"] <= c_ms]
            if not bars:
                raise ValueError("NO_BARS_AT_CLOSE")
            ctx = MultiTFContextV11(bars)
        latest = bars[-1]
        cycle_dt = parse_utc(latest["close_at"])
        cycle_iso = cycle_dt.isoformat()
        idx = len(bars) - 1

        with self._tx() as db:
            st = self._read(db)
            self.engine.load_state(st.get("signal_state"))
            if st.get("fault_reason"):
                return {"status": "RECONCILE_ERROR", "reason": st["fault_reason"]}
            last_dt = parse_utc(st["last_cycle"]) if st.get("last_cycle") else None
            if last_dt is not None and cycle_dt <= last_dt:
                return {"status": "DUPLICATE_OR_STALE", "cycle": cycle_iso}

            gap = last_dt is not None and (cycle_dt - last_dt) != STEP
            pos = st.get("position")
            action = "WAIT"

            if pos and gap:
                st["fault_reason"] = "POSITION_GAP_RECONCILIATION_REQUIRED"
                st["last_action"] = "BLOCKED_GAP_RECONCILIATION_REQUIRED"
                self._audit(db, "GAP_BLOCK", {"from": st["last_cycle"], "to": cycle_iso}, at=cycle_iso)
                self._save(db, st)
                return {"status": "RECONCILE_ERROR", "reason": "POSITION_GAP_RECONCILIATION_REQUIRED"}

            if st.get("pending_entry"):
                st["pending_entry"] = None
                action = "ENTRY_EXPIRED"

            if pos and not st.get("pending_exit"):
                outcome = StrategyPool._close_position(pos, self.doc, [latest], D(pos["fee_rate"]), D(pos["slippage_rate"]))
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
                        action = self._settle_close(db, st, event_at=cycle_iso, outcome=outcome, reason=outcome["reason"], idem_key=idem)
                else:
                    pos["bars_held"] = int(pos.get("bars_held", 0)) + 1
                    pos["mark"] = str(latest["close"])
                    action = "HOLD"

            act_dt = parse_utc(st["activated_at"]) if st.get("activated_at") else None
            after_activation = (act_dt is None) or (cycle_dt >= act_dt)

            if gap or not guard.entry_ready:
                self.engine.reset_transient()
                if not pos:
                    action = "GAP_NO_ENTRY" if gap else f"MARKET_BLOCKED:{guard.reason}"
            elif not st.get("enabled") or not after_activation:
                if not pos:
                    action = "PAUSED" if st.get("paused_by_user") else "WAITING_ACTIVATION"
            elif not pos and not st.get("pending_exit"):
                pending_sig, sig_status = self.engine.step(ctx, idx, has_open_position=False)
                if pending_sig is not None:
                    st["pending_entry"] = pending_sig
                    action = f"PENDING_NEXT_OPEN:{pending_sig['direction']}"
                else:
                    action = sig_status

            st["last_cycle"] = cycle_iso
            st["last_action"] = action
            st["last_rejection_reason"] = self.engine.last_rejection_reason
            st["signal_state"] = self.engine.export_state()
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
            return {"status": st["last_status"], "action": action, "cycle": cycle_iso}

    def on_quote(self, quote: Dict[str, Any], *, guard: CommonMarketGuard) -> Dict[str, Any]:
        # Fast-path: zero disk I/O when flat with no pending entry/exit (prevents WS bookTicker backpressure!)
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

        # Fast-path while holding: check stop & target on EVERY tick in memory;
        # only hit SQLite immediately on trigger OR every 5s for mark-price persistence.
        if self._has_pos and not self._has_pending_exit and not self._has_pending_entry and self._cached_pos:
            import time as _t
            cp = self._cached_pos
            long_p = cp["direction"] == "LONG"
            hit_s, _, _ = stop_triggered(cp["direction"], D(cp["stop"]), bid, ask)
            hit_t = (bid >= D(cp["target"])) if long_p else (ask <= D(cp["target"]))
            now_m = _t.monotonic()
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
                if event_dt >= sig_dt + STEP:
                    st["last_action"] = "ENTRY_EXPIRED"
                    self._save_and_sync(db, st)
                    return {"status": "ENTRY_EXPIRED"}
                if not st.get("enabled") or not guard.entry_ready:
                    st["last_action"] = "ENTRY_BLOCKED"
                    self._save_and_sync(db, st)
                    return {"status": "ENTRY_BLOCKED"}

                direction = sig["direction"]
                ref_px = ask if direction == "LONG" else bid
                try:
                    new_pos, entry_fee, entry_slip = StrategyPool._new_position(
                        self.sid, self.doc, [{"close": str(ref_px)}], event_iso,
                        fee_rate, slip, self.cost.quantity_step, self.cost.minimum_quantity,
                        self.cost.minimum_notional, D(st["cash"]),
                        entry_reference=str(ref_px), direction=direction, stop_price=D(sig["stop"]),
                    )
                except StrategyValidationError as exc:
                    st["last_action"] = f"ENTRY_REJECTED:{exc}"
                    st["last_rejection_reason"] = str(exc)
                    self._save_and_sync(db, st)
                    return {"status": "ENTRY_REJECTED", "reason": str(exc)}

                trade_id = f"{self.sid}:{sig['signal_id']}"
                new_pos["trade_id"] = trade_id
                new_pos["signal_id"] = sig["signal_id"]
                new_pos["signal_source"] = sig.get("signal_source")
                idem = f"{self.sid}:open:{sig['signal_id']}"
                fill_payload = {
                    "idempotency_key": idem, "strategy_id": self.sid, "version": self.spec.version,
                    "trade_id": trade_id, "signal_id": sig["signal_id"], "signal_source": sig.get("signal_source"),
                    "kind": "OPEN", "side": "BUY" if direction == "LONG" else "SELL", "direction": direction,
                    "at": event_iso, "cycle": event_iso, "qty": new_pos["qty"], "price": new_pos["entry"],
                    "fee": str(entry_fee), "slippage": str(entry_slip), "gross": "0", "net": "0",
                    "reason": "ENTRY_NEXT_OPEN",
                }
                if not self._insert_fill(db, idem, fill_payload):
                    self._save_and_sync(db, st)
                    return {"status": "DUPLICATE_ENTRY_IGNORED"}
                st["cash"] = str(D(st["cash"]) - entry_fee)
                st["position"] = new_pos
                st["last_action"] = f"OPEN_{direction}"
                self._save_and_sync(db, st)
                return {"status": f"OPEN_{direction}", "trade_id": trade_id}

            self._save_and_sync(db, st)
            return {"status": "NO_POSITION"}


class RSeriesExtension:
    """Multi-strategy manager for R01-R06 V1.1.0 inside backend/app.py."""

    def __init__(
        self,
        directory: str | Path,
        *,
        register: bool = False,
        enabled=(),
        excluded_ids=(),
        cost: Optional[CostModel] = None,
    ):
        self.root = Path(directory)
        self.cost = cost or CostModel()
        self.ledgers: Dict[str, RSeriesPaperLedgerV11] = {}
        enabled_set = {str(x).strip().upper() for x in enabled if str(x).strip()}
        excluded_set = {str(x).strip().upper() for x in excluded_ids if str(x).strip()}
        if register:
            self.root.mkdir(parents=True, exist_ok=True)
        for sid in R_SERIES_IDS:
            if sid in excluded_set:
                continue
            db_path = self.root / f"{sid}.sqlite3"
            if db_path.exists():
                lg = RSeriesPaperLedgerV11(self.root, sid, cost=self.cost)
                lg.reconcile()
                self.ledgers[sid] = lg
            elif register:
                lg = RSeriesPaperLedgerV11(self.root, sid, cost=self.cost, enabled=(sid in enabled_set))
                self.ledgers[sid] = lg

    def unload(self, sid: str) -> bool:
        """Dynamically unload a strategy from memory (called by StrategyLifecycleManager)."""
        lg = self.ledgers.pop(str(sid).upper(), None)
        if lg is None:
            return False
        lg.engine.reset_transient()
        lg._cached_snapshot = {}
        lg._cached_pos = None
        return True

    def build_context(self, raw_bars: List[Dict[str, Any]], *, close: Optional[datetime] = None) -> MultiTFContextV11:
        bars = [b if ("t" in b and "close_ms" in b and "o" in b) else normalize_closed_15m_bar(b) for b in raw_bars if b.get("is_closed") is not False]
        if close is not None:
            c_ms = int(close.astimezone(UTC).timestamp() * 1000)
            if bars and bars[-1]["close_ms"] > c_ms:
                bars = [b for b in bars if b["close_ms"] <= c_ms]
        return MultiTFContextV11(bars)

    def prewarm_all(self, raw_bars: List[Dict[str, Any]]) -> Dict[str, Any]:
        out = {}
        for sid, lg in self.ledgers.items():
            st = lg.snapshot()
            if st.get("warmup_bars", 0) == 0:
                out[sid] = lg.prewarm_from_history(raw_bars)
        return out

    def ledger_cycle(
        self,
        sid: str,
        raw_bars: List[Dict[str, Any]],
        close: datetime,
        guard: CommonMarketGuard,
        *,
        shared_ctx: Optional[MultiTFContextV11] = None,
    ) -> Dict[str, Any]:
        return self.ledgers[sid].on_closed_cycle(raw_bars, guard=guard, close=close, shared_ctx=shared_ctx)

    def ledger_quote(self, sid: str, quote: Dict[str, Any], guard: CommonMarketGuard) -> Dict[str, Any]:
        return self.ledgers[sid].on_quote(quote, guard=guard)

    def set_enabled(self, sid: str, enabled: bool) -> Dict[str, Any]:
        return self.ledgers[sid].set_enabled(enabled, paused_by_user=not enabled)

    def versions(self, sid: str) -> List[Dict[str, Any]]:
        lg = self.ledgers[sid]
        doc_str = json.dumps(lg.doc, sort_keys=True, ensure_ascii=False)
        sha = hashlib.sha256(doc_str.encode("utf-8")).hexdigest()
        st = lg._cached_snapshot or lg.snapshot()
        return [{
            "strategy_id": sid,
            "version": lg.spec.version,
            "sha256": sha,
            "document": doc_str,
            "uploaded_at": st.get("activated_at") or "",
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
                "group": "R系列",
                "strategy_group": "R系列",
                "active_version": lg.spec.version,
                "latest_version": lg.spec.version,
                "candidate_ref": lg.spec.candidate_ref,
                "observation_tier": lg.spec.observation_tier,
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
                "last_status": status,
                "last_action": s.get("last_action") or "WAIT",
                "last_rejection_reason": s.get("last_rejection_reason"),
                "warmup_bars": s.get("warmup_bars", 0),
                "activated_at": s.get("activated_at"),
                "is_r_series": True,
            })
        return items
