"""Single-source common market guard and unified strategy state machine for all BTC V3.1 strategies.

All 16 built-in strategies (A, B, C, D, E, G, PA-01..PA-10) and any future uploaded
StrategyPool strategies share this exact market-readiness evaluation and state mapping.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

UNIFIED_STATUS_LABELS = {
    'RUNNING': '运行中',
    'WAITING_SIGNAL': '等待信号',
    'MARKET_RECOVERING': '行情恢复中',
    'RECONCILING': '对账中',
    'RECONCILE_ERROR': '对账异常',
    'PAUSED': '已暂停',
    'DISABLED': '已停用',
}


@dataclass(frozen=True)
class CommonMarketGuard:
    """Snapshot of the single shared market gate (equivalent to ABC's live market health)."""
    feed_live: bool
    quote_fresh: bool
    ws_healthy: bool
    candles_complete: bool
    startup_monitor_checked: bool
    reason: str | None = None

    @property
    def quote_ready(self) -> bool:
        """Fresh quote + healthy WS: sufficient for protective exit management and quote validation."""
        return self.quote_fresh and self.ws_healthy

    @property
    def entry_ready(self) -> bool:
        """Full common entry gate: live feed + fresh quote + healthy WS + contiguous 288 candles."""
        return (
            self.feed_live
            and self.quote_fresh
            and self.ws_healthy
            and self.candles_complete
            and self.startup_monitor_checked
        )

    def to_dict(self) -> dict:
        return {
            'version': 'COMMON-MARKET-GUARD-V1',
            'entry_ready': self.entry_ready,
            'quote_ready': self.quote_ready,
            'feed_live': self.feed_live,
            'quote_fresh': self.quote_fresh,
            'ws_healthy': self.ws_healthy,
            'candles_complete': self.candles_complete,
            'startup_monitor_checked': self.startup_monitor_checked,
            'reason': None if self.entry_ready else (self.reason or 'MARKET_RECOVERING'),
        }


def evaluate_common_market_guard(
    *,
    feed_health: str | None,
    quote: dict | None,
    ws_recovery_state: str | None,
    gap_count_288: int | None,
    candle_count: int,
    max_quote_age_ms: int = 60_000,
    startup_monitor_checked: bool = True,
) -> CommonMarketGuard:
    """Single source of truth for market readiness across all strategies."""
    q = quote or {}
    age_raw = q.get('age_ms')
    try:
        age_ms = int(age_raw) if age_raw is not None else 10**9
    except (TypeError, ValueError):
        age_ms = 10**9

    feed_live = (feed_health == 'LIVE')
    ws_healthy = (ws_recovery_state == 'HEALTHY')
    quote_fresh = bool(
        q.get('source') == 'BINANCE_USDM_PERP'
        and q.get('timestamp_valid') is True
        and 0 <= age_ms <= max_quote_age_ms
    )
    candles_complete = (gap_count_288 == 0 and candle_count >= 288)
    checked = bool(startup_monitor_checked)

    reason = None
    if not ws_healthy:
        reason = f'WS_{ws_recovery_state or "UNKNOWN"}'
    elif not feed_live:
        reason = f'FEED_{feed_health or "UNKNOWN"}'
    elif not quote_fresh:
        reason = 'QUOTE_STALE_OR_INVALID'
    elif not candles_complete:
        reason = 'CANDLES_INCOMPLETE'
    elif not checked:
        reason = 'STARTUP_MONITOR_PENDING'

    return CommonMarketGuard(
        feed_live=feed_live,
        quote_fresh=quote_fresh,
        ws_healthy=ws_healthy,
        candles_complete=candles_complete,
        startup_monitor_checked=checked,
        reason=reason,
    )


def classify_unified_status(
    *,
    enabled: bool,
    paused_by_user: bool = False,
    market_ready: bool = True,
    reconciling: bool = False,
    fault_reason: str | None = None,
    holding: bool = False,
    waiting_signal: bool = False,
) -> str:
    """Map any strategy's live runtime facts into one of the 7 canonical states.

    Priority:
      1. Real ledger/reconciliation fault -> RECONCILE_ERROR (never masked by recovery)
      2. Active reconciliation -> RECONCILING
      3. Manual pause -> PAUSED (never auto-cleared by market recovery)
      4. Disabled -> DISABLED
      5. Common market guard not ready -> MARKET_RECOVERING (auto-recovers when feed returns)
      6. Healthy + holding -> RUNNING
      7. Healthy + flat -> WAITING_SIGNAL (if waiting_signal) else RUNNING
    """
    if fault_reason:
        return 'RECONCILE_ERROR'
    if reconciling:
        return 'RECONCILING'
    if not enabled:
        return 'PAUSED' if paused_by_user else 'DISABLED'
    if not market_ready:
        return 'MARKET_RECOVERING'
    if holding:
        return 'RUNNING'
    return 'WAITING_SIGNAL' if waiting_signal else 'RUNNING'
