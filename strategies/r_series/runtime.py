"""R-Series (R01-R06) isolated SQLite ledger & V3.1 unified protection adapter.

Directly reuses V3.1 public interfaces without modifying any existing engine:
- backend.common_guard.evaluate_common_market_guard & classify_unified_status
- backend.domain.CostModel & stop_triggered
- backend.strategy_pool.StrategyPool._new_position, _close_position, _exit_reason_v2, _valid_quote
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
    evaluate_common_market_guard,
)
from backend.domain import CostModel, stop_triggered
from backend.strategy_pool import FORMAT_V2, StrategyPool, StrategyValidationError
from strategies.r_series.indicators import normalize_closed_15m_bar, parse_utc
from strategies.r_series.strategies import (
    PrecomputedMultiTFContext,
    R_SPECS,
    RSignalEngine,
)

UTC = timezone.utc
STEP = timedelta(minutes=15)
INITIAL_EQUITY = D("10000")


class RSeriesLedger:
    """Durable, isolated SQLite paper ledger for one R-Series strategy (R01-R06).

    Implements V3.1 Unified Market Protection & Exit Protection Spec V1.0:
    - Common market guard gating for entries (never blocks protective exits on valid quotes)
    - Pause stops new entries while keeping protective exits active
    - Two-phase pending exit persistence (EXIT_PENDING) & idempotent recovery across restarts
    - Gap detection & reconciliation blocking (never fabricates fills from stale quotes)
    - Full reconciliation check of account cash, position, pending exit, and fills
    """

    def __init__(
        self,
        directory: str | Path,
        sid: str,
        *,
        cost: Optional[CostModel] = None,
        max_hold_bars: Optional[int] = None,
        target_r: Optional[float] = None,
        enabled: bool = False,
    ) -> None:
        if sid not in R_SPECS:
            raise ValueError(f"UNKNOWN_R_STRATEGY:{sid}")
        self.sid = sid
        self.spec = R_SPECS[sid]
        self.cost = cost or CostModel()
        self.max_hold_bars = max_hold_bars if max_hold_bars is not None else self.spec.max_hold_bars
        self.target_r = target_r if target_r is not None else self.spec.target_r
        self.doc = {
            "format": FORMAT_V2,
            "strategy_id": sid,
            "name": self.spec.name,
            "version": self.spec.version,
            "position": {
                "direction": "both",
                "entry_timing": "next_open",
                "same_bar_priority": "stop",
                "max_hold_bars": self.max_hold_bars,
                "target_r": self.target_r,
            },
            "risk": {
                "initial_equity": str(INITIAL_EQUITY),
                "currency": "USD_RESEARCH",
                "max_leverage": 5,
                "risk_fraction": "0.02",
            },
        }
        root = Path(directory).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"{sid}.sqlite3"
        self.engine = RSignalEngine(sid, stop_atr_mult=self.spec.stop_atr_mult)

        if not self.path.exists():
            with self._tx() as db:
                db.execute("CREATE TABLE snapshot (value TEXT NOT NULL)")
                db.execute("CREATE TABLE fills (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL)")
                init_state = {
                    "strategy_id": sid,
                    "name": self.spec.name,
                    "group": "R系列",
                    "version": self.spec.version,
                    "enabled": bool(enabled),
                    "paused_by_user": False,
                    "cash": str(INITIAL_EQUITY),
                    "realized": "0",
                    "position": None,
                    "pending_entry": None,
                    "pending_exit": None,
                    "last_cycle": None,
                    "last_quote_at": None,
                    "last_status": "DISABLED" if not enabled else "WAITING_SIGNAL",
                    "last_action": "REGISTERED",
                    "fault_reason": None,
                    "reconciling": False,
                    "last_protection_check_at": None,
                    "signal_state": self.engine.export_state(),
                    "consumed_signal_ids": [],
                }
                db.execute("INSERT INTO snapshot VALUES (?)", (json.dumps(init_state, sort_keys=True),))
        else:
            st = self.snapshot()
            if st.get("strategy_id") != sid:
                raise ValueError("LEDGER_IDENTITY_MISMATCH")
            self.engine.load_state(st.get("signal_state"))

    @classmethod
    def open_existing(cls, directory: str | Path, sid: str, **kwargs) -> "RSeriesLedger":
        path = Path(directory).resolve() / f"{sid}.sqlite3"
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return cls(directory, sid, **kwargs)

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

    @staticmethod
    def _audit(db: sqlite3.Connection, event: str, details: Dict[str, Any], at: Optional[str] = None) -> None:
        ts = at or datetime.now(UTC).isoformat()
        db.execute("INSERT INTO audit_log(at, event, details) VALUES(?,?,?)", (ts, event, json.dumps(details, sort_keys=True, ensure_ascii=False)))

    def snapshot(self) -> Dict[str, Any]:
        with self._tx() as db:
            return self._read(db)

    def fills(self) -> List[Dict[str, Any]]:
        with self._tx() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT value FROM fills ORDER BY rowid").fetchall()]

    def set_enabled(self, enabled: bool, *, paused_by_user: bool = False) -> Dict[str, Any]:
        with self._tx() as db:
            st = self._read(db)
            st["enabled"] = bool(enabled)
            st["paused_by_user"] = bool(paused_by_user) if not enabled else False
            if not enabled:
                # Pausing blocks new entries; existing position & pending_exit remain protected!
                st["pending_entry"] = None
            st["last_status"] = classify_unified_status(
                enabled=st["enabled"],
                paused_by_user=st["paused_by_user"],
                market_ready=True,
                reconciling=bool(st.get("reconciling")),
                fault_reason=st.get("fault_reason"),
                holding=bool(st.get("position")),
                waiting_signal=True,
            )
            self._audit(db, "SET_ENABLED", {"enabled": st["enabled"], "paused_by_user": st["paused_by_user"]})
            self._save(db, st)
            return st

    def unified_status(self, guard: Optional[CommonMarketGuard] = None) -> Dict[str, Any]:
        st = self.snapshot()
        market_ready = True if guard is None else guard.entry_ready
        code = classify_unified_status(
            enabled=bool(st.get("enabled")),
            paused_by_user=bool(st.get("paused_by_user")),
            market_ready=market_ready,
            reconciling=bool(st.get("reconciling")),
            fault_reason=st.get("fault_reason"),
            holding=bool(st.get("position")),
            waiting_signal=not bool(st.get("position")),
        )
        return {
            "strategy_id": self.sid,
            "status_code": code,
            "status_label": UNIFIED_STATUS_LABELS[code],
            "reason": st.get("fault_reason") or (guard.reason if (guard and not guard.entry_ready) else st.get("last_action")),
            "last_cycle": st.get("last_cycle"),
            "last_protection_check_at": st.get("last_protection_check_at"),
            "holding": bool(st.get("position")),
            "pending_exit": st.get("pending_exit"),
        }

    def reconcile(self) -> Dict[str, Any]:
        """Reconcile account cash, fills, open position, and pending exit after restart."""
        with self._tx() as db:
            st = self._read(db)
            st["reconciling"] = True
            fills = [json.loads(r[0]) for r in db.execute("SELECT value FROM fills ORDER BY rowid").fetchall()]
            issues: List[str] = []

            # Verify fill idempotency uniqueness & net open quantity
            seen_keys = set()
            net_qty = D(0)
            calc_cash = INITIAL_EQUITY
            calc_realized = D(0)
            for f in fills:
                k = f.get("idempotency_key")
                if not k or k in seen_keys:
                    issues.append("DUPLICATE_OR_MISSING_IDEMPOTENCY_KEY")
                seen_keys.add(k)
                q = D(str(f["qty"]))
                fee = D(str(f["fee"]))
                if f["kind"] == "OPEN":
                    net_qty += q if f["direction"] == "LONG" else -q
                    calc_cash -= fee
                elif f["kind"] == "CLOSE":
                    net_qty -= q if f["direction"] == "LONG" else -q
                    gross = D(str(f["gross"]))
                    net = D(str(f["net"]))
                    calc_cash += gross - fee
                    calc_realized += net

            pos = st.get("position")
            expected_net = D(0)
            if pos:
                expected_net = D(str(pos["qty"])) * (D(1) if pos["direction"] == "LONG" else D(-1))
                if D(str(pos.get("stop", "0"))) <= 0 or D(str(pos.get("target", "0"))) <= 0:
                    issues.append("POSITION_PROTECTION_PARAMS_INVALID")
            if net_qty != expected_net:
                issues.append(f"POSITION_QTY_MISMATCH:{net_qty}!={expected_net}")
            if abs(D(str(st["cash"])) - calc_cash) > D("0.0001"):
                issues.append(f"CASH_MISMATCH:{st['cash']}!={calc_cash}")
            if abs(D(str(st["realized"])) - calc_realized) > D("0.0001"):
                issues.append(f"REALIZED_MISMATCH:{st['realized']}!={calc_realized}")
            if st.get("pending_exit") and not pos:
                issues.append("ORPHAN_PENDING_EXIT")

            st["reconciling"] = False
            st["fault_reason"] = ";".join(issues) if issues else None
            st["last_protection_check_at"] = datetime.now(UTC).isoformat()
            self._audit(db, "RECONCILE", {"ok": not issues, "issues": issues, "fill_count": len(fills)})
            self._save(db, st)
            return {"ok": not issues, "issues": issues, "fill_count": len(fills), "position": pos, "pending_exit": st.get("pending_exit")}

    def _insert_fill(self, db: sqlite3.Connection, idem_key: str, payload: Dict[str, Any]) -> bool:
        cur = db.execute("INSERT OR IGNORE INTO fills(id, value) VALUES(?, ?)", (idem_key, json.dumps(payload, sort_keys=True, ensure_ascii=False)))
        return cur.rowcount == 1

    def _settle_close(
        self,
        db: sqlite3.Connection,
        st: Dict[str, Any],
        *,
        event_at: str,
        outcome: Dict[str, Any],
        reason: str,
        idem_key: str,
    ) -> str:
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
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Process one closed 15m cycle using the full history window from fixed origin."""
        bars = [normalize_closed_15m_bar(b) for b in raw_bars]
        if not bars:
            raise ValueError("EMPTY_BARS")
        latest = bars[-1]
        cycle_dt = parse_utc(latest["close_at"])
        cycle_iso = cycle_dt.isoformat()

        ctx = PrecomputedMultiTFContext(bars)
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

            # If holding & gap detected -> block reconciliation (never fabricate fill from OHLC gap!)
            if pos and gap:
                st["fault_reason"] = "POSITION_GAP_RECONCILIATION_REQUIRED"
                st["last_action"] = "BLOCKED_GAP_RECONCILIATION_REQUIRED"
                self._audit(db, "GAP_BLOCK", {"from": st["last_cycle"], "to": cycle_iso}, at=cycle_iso)
                self._save(db, st)
                return {"status": "RECONCILE_ERROR", "reason": "POSITION_GAP_RECONCILIATION_REQUIRED"}

            # Expire any unconsumed pending_entry from an earlier cycle
            if st.get("pending_entry"):
                st["pending_entry"] = None
                action = "ENTRY_EXPIRED"

            # Holding position: evaluate V3.1 StrategyPool._close_position (STOP > TARGET > TIME)
            if pos and not st.get("pending_exit"):
                outcome = StrategyPool._close_position(
                    pos, self.doc, [latest], D(pos["fee_rate"]), D(pos["slippage_rate"])
                )
                if outcome is not None:
                    # If quote is not ready / feed not live, persist pending_exit instead of fabricating fill!
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

            # Entry signal evaluation (strictly gated by enabled + guard.entry_ready + no gap + flat)
            if gap or not guard.entry_ready:
                self.engine.reset_transient()
                if not pos:
                    action = "GAP_NO_ENTRY" if gap else f"MARKET_BLOCKED:{guard.reason}"
            elif not st.get("enabled"):
                if not pos:
                    action = "PAUSED" if st.get("paused_by_user") else "DISABLED"
            elif not pos and not st.get("pending_exit"):
                pending_sig, sig_status = self.engine.step(ctx, idx, has_open_position=False)
                if pending_sig is not None:
                    st["pending_entry"] = pending_sig
                    action = f"PENDING_NEXT_OPEN:{pending_sig['direction']}"
                else:
                    action = sig_status

            st["last_cycle"] = cycle_iso
            st["last_action"] = action
            st["signal_state"] = self.engine.export_state()
            st["last_protection_check_at"] = (now or datetime.now(UTC)).isoformat()
            st["last_status"] = classify_unified_status(
                enabled=bool(st.get("enabled")),
                paused_by_user=bool(st.get("paused_by_user")),
                market_ready=guard.entry_ready,
                reconciling=False,
                fault_reason=st.get("fault_reason"),
                holding=bool(st.get("position")),
                waiting_signal=not bool(st.get("position")),
            )
            self._save(db, st)
            return {"status": st["last_status"], "action": action, "cycle": cycle_iso}

    def on_quote(
        self,
        quote: Dict[str, Any],
        *,
        guard: CommonMarketGuard,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Real-time quote handler:
        1. Recovers any persisted pending_exit once a fresh valid quote arrives.
        2. Executes protective STOP/TARGET exits even when strategy is PAUSED or entry_ready is False
           (as long as quote_ready is True and quote is valid & fresh).
        3. Executes pending_entry at next 15m open ONLY when enabled + guard.entry_ready + valid quote.
        """
        fee_rate = D(str(self.cost.fee_rate))
        slip = D(str(self.cost.adverse_slippage_bps)) / D("10000")

        # Validate quote freshness & schema using V3.1 rules
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

        event_iso = event_dt.isoformat()
        with self._tx() as db:
            st = self._read(db)
            if st.get("fault_reason"):
                return {"status": "RECONCILE_ERROR", "reason": st["fault_reason"]}
            if st.get("last_quote_at") and event_dt <= parse_utc(st["last_quote_at"]):
                return {"status": "DUPLICATE_OR_STALE_QUOTE"}
            st["last_quote_at"] = event_iso
            st["last_protection_check_at"] = (now or datetime.now(UTC)).isoformat()

            pos = st.get("position")

            # 1. Recover persisted pending_exit (highest priority, works even when paused!)
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
                outcome = {
                    "price": px, "gross": gross, "fee": fee, "net": net,
                    "slippage": abs(ref_px * qty - px * qty), "reason": pe["reason"],
                }
                idem = f"{self.sid}:close:{pos['trade_id']}:{pe['reason']}"
                action = self._settle_close(
                    db, st, event_at=event_iso, outcome=outcome, reason=f"{pe['reason']}_RECOVERED", idem_key=idem
                )
                self._save(db, st)
                return {"status": action, "recovered": True}

            # 2. Active position protective exit check (works even when PAUSED!)
            if pos:
                long_pos = pos["direction"] == "LONG"
                ref_px = bid if long_pos else ask
                pos["mark"] = str(ref_px)
                stop_px = D(pos["stop"])
                target_px = D(pos["target"])
                hit_stop, _, _ = stop_triggered(pos["direction"], stop_px, bid, ask)
                hit_target = (bid >= target_px) if long_pos else (ask <= target_px)
                if hit_stop or hit_target:
                    # Conservative priority: STOP > TARGET
                    reason = "STOP_QUOTE" if hit_stop else "TARGET_QUOTE"
                    side_sign = D(1) if long_pos else D(-1)
                    px = ref_px * (D(1) - slip * side_sign)
                    qty = D(pos["qty"])
                    gross = (px - D(pos["entry"])) * qty * side_sign
                    fee = abs(px * qty) * fee_rate
                    net = gross - D(pos.get("entry_fee", "0")) - fee
                    outcome = {
                        "price": px, "gross": gross, "fee": fee, "net": net,
                        "slippage": abs(ref_px * qty - px * qty), "reason": reason,
                    }
                    idem = f"{self.sid}:close:{pos['trade_id']}:{reason}"
                    action = self._settle_close(
                        db, st, event_at=event_iso, outcome=outcome, reason=reason, idem_key=idem
                    )
                    self._save(db, st)
                    return {"status": action}
                self._save(db, st)
                return {"status": "HOLD"}

            # 3. Pending entry execution (strictly requires enabled + guard.entry_ready + valid timing window)
            if not pos and st.get("pending_entry"):
                sig = st["pending_entry"]
                sig_dt = parse_utc(sig["signal_cycle"])
                if event_dt < sig_dt:
                    self._save(db, st)
                    return {"status": "EARLY_QUOTE"}
                st["pending_entry"] = None
                if event_dt >= sig_dt + STEP:
                    st["last_action"] = "ENTRY_EXPIRED"
                    self._save(db, st)
                    return {"status": "ENTRY_EXPIRED"}
                if not st.get("enabled") or not guard.entry_ready:
                    st["last_action"] = "ENTRY_BLOCKED"
                    self._save(db, st)
                    return {"status": "ENTRY_BLOCKED"}

                direction = sig["direction"]
                ref_px = ask if direction == "LONG" else bid
                try:
                    new_pos, entry_fee, entry_slip = StrategyPool._new_position(
                        self.sid,
                        self.doc,
                        [{"close": str(ref_px)}],
                        event_iso,
                        fee_rate,
                        slip,
                        self.cost.quantity_step,
                        self.cost.minimum_quantity,
                        self.cost.minimum_notional,
                        D(st["cash"]),
                        entry_reference=str(ref_px),
                        direction=direction,
                        stop_price=D(sig["stop"]),
                    )
                except StrategyValidationError as exc:
                    st["last_action"] = f"ENTRY_REJECTED:{exc}"
                    self._save(db, st)
                    return {"status": "ENTRY_REJECTED", "reason": str(exc)}

                trade_id = f"{self.sid}:{sig['signal_id']}"
                new_pos["trade_id"] = trade_id
                new_pos["signal_id"] = sig["signal_id"]
                new_pos["signal_source"] = sig.get("signal_source")
                idem = f"{self.sid}:open:{sig['signal_id']}"
                fill_payload = {
                    "idempotency_key": idem,
                    "strategy_id": self.sid,
                    "version": self.spec.version,
                    "trade_id": trade_id,
                    "signal_id": sig["signal_id"],
                    "signal_source": sig.get("signal_source"),
                    "kind": "OPEN",
                    "side": "BUY" if direction == "LONG" else "SELL",
                    "direction": direction,
                    "at": event_iso,
                    "cycle": event_iso,
                    "qty": new_pos["qty"],
                    "price": new_pos["entry"],
                    "fee": str(entry_fee),
                    "slippage": str(entry_slip),
                    "gross": "0",
                    "net": "0",
                    "reason": "ENTRY_NEXT_OPEN",
                }
                if not self._insert_fill(db, idem, fill_payload):
                    self._save(db, st)
                    return {"status": "DUPLICATE_ENTRY_IGNORED"}
                st["cash"] = str(D(st["cash"]) - entry_fee)
                st["position"] = new_pos
                st["last_action"] = f"OPEN_{direction}"
                self._save(db, st)
                return {"status": f"OPEN_{direction}", "trade_id": trade_id}

            self._save(db, st)
            return {"status": "NO_POSITION"}

    def persist_pending_exit_for_test(self, reason: str, trigger_price: str, triggered_at: str) -> None:
        """Helper for restart-during-pending-exit verification tests."""
        with self._tx() as db:
            st = self._read(db)
            pos = st.get("position")
            if not pos:
                raise ValueError("NO_POSITION")
            st["pending_exit"] = {
                "trade_id": pos["trade_id"],
                "reason": reason,
                "trigger_price": trigger_price,
                "triggered_at": triggered_at,
                "event_key": f"{self.sid}:exit:{pos['trade_id']}:{reason}",
            }
            self._save(db, st)
