"""BTC Paper Lab V3.1 — Unified Strategy Research V3 Data & Metrics Engine.

Strict read-only research engine:
- Layer 1: Dynamic strategy discovery (zero hardcoded strategy scope).
- Layer 2: Concurrent, fault-isolated read-only data adapter (API + SQLite mode=ro).
- Layer 3: Unified, deterministic, testable performance & attribution calculator.
- Layer 4: TTL cache, range filtering, version breakdown, multi-strategy comparison,
           saved backtest integration (strictly isolated from live paper), and CSV/MD export.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import threading
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

BEIJING = timezone(timedelta(hours=8), name='Asia/Shanghai')
UTC = timezone.utc
CALCULATION_VERSION = 'RESEARCH-METRICS-V3.0.0'
ROOT = Path(__file__).resolve().parents[1]

# Built-in fallback names only when a strategy document lacks a name field (never restricts discovery!)
_LEGACY_FALLBACK_NAMES = {
    'A': 'A · 突破回踩',
    'B': 'B · 反转（趋势过滤）',
    'C': 'C · Tony裸K研究近似',
    'D': 'D · 急跌反转',
    'E': 'E · V1.1量化',
    'G': 'G · C反向镜像',
    'pa-trend-40-breakout': 'PA-01 顺势突破',
    'pa-sma20-pullback-confirm': 'PA-02 SMA20顺势回踩',
    'pa-full-engulfing-reversal': 'PA-03 双重吞噬反转',
    'pa-gapless-three-bar-reversal': 'PA-04 无缺口三K反转',
    'pa-trend-false-break-reclaim': 'PA-05 顺势假突破收复',
    'pa-inside-bar-break': 'PA-06 内包K线趋势突破',
    'pa-big-candle-continuation': 'PA-07 大实体K线趋势推进',
    'pa-three-bar-hhhl': 'PA-08 三根高低点持续推进',
    'PA-09': 'PA-09 区间边界 Pinbar 反转',
    'PA-10': 'PA-10 完整压力支撑互换',
}


def dec(v: Any) -> Decimal | None:
    if v is None or v == '':
        return None
    try:
        d = Decimal(str(v).replace(',', '').strip())
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(UTC)


def fmt_bj(value: Any, length: int = 19) -> str:
    dt = parse_utc(value)
    if dt is None:
        return '—'
    return dt.astimezone(BEIJING).strftime('%Y-%m-%d %H:%M:%S')[:length]


def normalize_exit_reason_category(reason: str | None) -> str:
    code = str(reason or '').upper().strip()
    if not code:
        return '未知原因'
    if 'TARGET' in code or 'TAKE_PROFIT' in code or 'TP' in code:
        return '止盈'
    if 'STOP' in code or 'SL' in code or 'RISK_EXIT' in code:
        return '止损'
    if 'TIME' in code or 'MAX_HOLD' in code:
        return '时间退出'
    if 'RULE' in code or 'SIGNAL' in code or 'CLOSE' in code or 'MIRROR' in code:
        return '策略信号退出'
    return '其他退出'


def _connect_ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


class ReadOnlyHttpClient:
    """Thread-safe GET-only loopback client with strict short timeout and zero proxy overhead."""

    def __init__(self, base_url: str = 'http://127.0.0.1:8012', timeout: float = 4.0):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path: str) -> Any:
        if not path.startswith('/api/v1/'):
            raise ValueError('API_PATH_INVALID')
        req = urllib.request.Request(self.base_url + path, method='GET')
        with self.opener.open(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))


def pair_pool_fills_v3(fills: list[dict], strategy_id: str, account_id: str) -> tuple[list[dict], list[str], int]:
    """Pair complete single-position lifecycles for any Strategy Pool strategy.

    Returns (closed_trades, issues, active_open_count).
    Active open positions are separated from data-integrity issues so an open trade
    never invalidates historical closed trades.
    """
    rows = []
    issues = []
    keys = set()
    for f in fills or []:
        key = f.get('idempotency_key')
        at = parse_utc(f.get('created_at') or f.get('cycle'))
        cycle = parse_utc(f.get('cycle') or f.get('created_at'))
        if not isinstance(key, str) or key in keys or at is None or cycle is None:
            issues.append('UNVERIFIED: 成交缺少唯一事件键或时间')
            continue
        keys.add(key)
        rows.append((at, cycle, f))
    rows.sort(key=lambda x: (x[0], x[1], x[2].get('idempotency_key', '')))

    closed: list[dict] = []
    active = None
    for at, cycle, f in rows:
        reason = str(f.get('reason') or '')
        side = str(f.get('side') or '').upper()
        qty = dec(f.get('qty'))
        price = dec(f.get('price'))
        fee = dec(f.get('fee'))
        slip = dec(f.get('slippage')) or Decimal(0)
        version = str(f.get('version') or '—')
        if side not in ('BUY', 'SELL') or qty is None or price is None or fee is None or qty <= 0 or price <= 0 or fee < 0:
            issues.append(f"UNVERIFIED: 成交字段无效 ({f.get('idempotency_key')})")
            continue
        if reason in ('ENTRY', 'ENTRY_NEXT_OPEN') or f.get('idempotency_key', '').startswith(f'{strategy_id}:open:'):
            if active is not None:
                issues.append(f"UNVERIFIED: 存在未平仓重叠开仓 ({active[2].get('idempotency_key')})")
            active = (at, cycle, f)
            continue
        if reason in ('STOP', 'TARGET', 'TIME', 'STOP_QUOTE', 'TARGET_QUOTE', 'RULE'):
            if active is None:
                issues.append(f"UNVERIFIED: 退出成交缺少对应开仓 ({f.get('idempotency_key')})")
                continue
            otime, ocycle, entry = active
            active = None
            eqty = dec(entry.get('qty'))
            eprice = dec(entry.get('price'))
            efee = dec(entry.get('fee'))
            eslip = dec(entry.get('slippage')) or Decimal(0)
            if eqty != qty or side == str(entry.get('side')).upper():
                issues.append(f"UNVERIFIED: 开平仓数量或方向不匹配 ({f.get('idempotency_key')})")
                continue
            direction = 'LONG' if str(entry.get('side')).upper() == 'BUY' else 'SHORT'
            sign = Decimal(1) if direction == 'LONG' else Decimal(-1)
            gross = (price - eprice) * qty * sign
            total_fees = efee + fee
            net = gross - total_fees
            dur_sec = max(0.0, (at - otime).total_seconds())
            closed.append({
                'trade_id': f"pool:{entry.get('idempotency_key')}",
                'strategy_id': strategy_id,
                'account_id': account_id,
                'opening_strategy_version': str(entry.get('version') or '—'),
                'execution_strategy_version': version,
                'entry_time': otime.isoformat(),
                'exit_time': at.isoformat(),
                'side': direction,
                'entry_price': str(eprice),
                'exit_price': str(price),
                'quantity': str(qty),
                'gross_pnl': str(gross),
                'open_fee': str(efee),
                'close_fee': str(fee),
                'fees': str(total_fees),
                'slippage': str(eslip + slip),
                'net_pnl': str(net),
                'exit_reason': reason,
                'exit_reason_category': normalize_exit_reason_category(reason),
                'holding_duration_sec': dur_sec,
                'close_evidence_status': 'VERIFIED_LIFECYCLE',
            })
        else:
            issues.append(f"UNVERIFIED: 未知成交原因 ({reason})")
    active_count = 1 if active is not None else 0
    return closed, issues, active_count


def pair_ledger_fills_v3(fills: list[dict], strategy_id: str, account_id: str, *, audit_map: dict[str, dict] | None = None) -> tuple[list[dict], list[str], int]:
    """Pair complete closed trades for core strategies (A/B/C/D/E/G) while cleanly separating active open positions."""
    groups: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    issues: list[str] = []
    for f in fills or []:
        key = f.get('g_trade_id') if strategy_id == 'G' else f.get('trade_id')
        if not key:
            issues.append('UNVERIFIED: 成交缺少交易编号')
            continue
        effect = str(f.get('position_effect') or f.get('effect') or '').upper()
        if effect in ('OPEN', 'ADD'):
            kind = 'OPEN'
        elif effect in ('CLOSE', 'PARTIAL_CLOSE', 'RISK_EXIT', 'STOP_EXIT'):
            kind = 'CLOSE'
        else:
            issues.append(f'UNVERIFIED: 未知成交类型 {effect}')
            continue
        groups[str(key)].append((kind, f))

    closed: list[dict] = []
    open_count = 0
    for tid, rows in groups.items():
        opens = [f for k, f in rows if k == 'OPEN']
        closes = [f for k, f in rows if k == 'CLOSE']
        if opens and not closes:
            # Currently open position — NOT a data integrity error!
            open_count += 1
            continue
        if not opens or not closes:
            issues.append(f'{tid}: 缺少对应开仓或平仓记录')
            continue
        valid_rows = True
        for _, f in rows:
            q, p, fe, at = dec(f.get('quantity')), dec(f.get('price')), dec(f.get('fee')), parse_utc(f.get('occurred_at'))
            if q is None or p is None or fe is None or at is None or q <= 0 or p <= 0 or fe < 0:
                valid_rows = False
                break
        if not valid_rows:
            issues.append(f'{tid}: 价格/数量/费用或时间缺失')
            continue
        oq = sum((dec(f['quantity']) for f in opens), Decimal(0))
        cq = sum((dec(f['quantity']) for f in closes), Decimal(0))
        if cq < oq:
            # Partially closed, still open
            open_count += 1
            continue
        if cq != oq:
            issues.append(f'{tid}: 开平仓数量不一致 ({oq} vs {cq})')
            continue
        dirs = {str(f.get('position_direction') or f.get('direction') or '').upper() for _, f in rows}
        if len(dirs) != 1 or not dirs <= {'LONG', 'SHORT'}:
            issues.append(f'{tid}: 开平仓方向不一致')
            continue
        direction = next(iter(dirs))
        sign = Decimal(1) if direction == 'LONG' else Decimal(-1)
        open_val = sum((dec(f['price']) * dec(f['quantity']) for f in opens), Decimal(0))
        close_val = sum((dec(f['price']) * dec(f['quantity']) for f in closes), Decimal(0))
        open_fee = sum((dec(f['fee']) for f in opens), Decimal(0))
        close_fee = sum((dec(f['fee']) for f in closes), Decimal(0))
        total_fees = open_fee + close_fee
        gross = (close_val - open_val) * sign
        slippage = sum((dec(f.get('slippage')) or Decimal(0) for _, f in rows), Decimal(0))

        # Use audited net_pnl if provided by ledger/G/D to prevent fee double-deduction
        evidence = 'VERIFIED_LEDGER'
        if strategy_id in ('D', 'G'):
            pnls = [dec(f.get('pnl')) for f in closes]
            if all(p is not None for p in pnls):
                net = sum(pnls, Decimal(0))
                evidence = 'VERIFIED_EXPLICIT_NET'
            else:
                net = gross - total_fees
        elif audit_map and tid in audit_map and dec(audit_map[tid].get('net_pnl')) is not None:
            net = dec(audit_map[tid]['net_pnl'])
            evidence = 'AUDITED_POSITION_CLOSED'
        else:
            net = gross - total_fees

        otime = min(parse_utc(f['occurred_at']) for f in opens)
        ctime = max(parse_utc(f['occurred_at']) for f in closes)
        dur_sec = max(0.0, (ctime - otime).total_seconds())
        open_ver = str(opens[0].get('strategy_version') or opens[0].get('version') or '—')
        exec_ver = str(closes[-1].get('strategy_version') or closes[-1].get('version') or open_ver)
        exit_reason = '; '.join(str(f.get('reason') or f.get('rationale') or '') for f in closes if (f.get('reason') or f.get('rationale')))
        closed.append({
            'trade_id': tid,
            'strategy_id': strategy_id,
            'account_id': account_id,
            'opening_strategy_version': open_ver,
            'execution_strategy_version': exec_ver,
            'entry_time': otime.isoformat(),
            'exit_time': ctime.isoformat(),
            'side': direction,
            'entry_price': str(open_val / oq),
            'exit_price': str(close_val / cq),
            'quantity': str(oq),
            'gross_pnl': str(gross),
            'open_fee': str(open_fee),
            'close_fee': str(close_fee),
            'fees': str(total_fees),
            'slippage': str(slippage),
            'net_pnl': str(net),
            'exit_reason': exit_reason or '—',
            'exit_reason_category': normalize_exit_reason_category(exit_reason),
            'holding_duration_sec': dur_sec,
            'close_evidence_status': evidence,
        })
    closed.sort(key=lambda x: x['exit_time'])
    return closed, issues, open_count


def build_equity_series(
    closed_trades: list[dict],
    *,
    initial_capital: Decimal,
    current_equity: Decimal | None,
    current_realized: Decimal | None,
    current_unrealized: Decimal | None,
    running_since: str | None,
    snapshot_time: str,
) -> tuple[list[dict], dict]:
    """Reconstruct truthful realized equity series from verified closed trades + current snapshot.

    Explicitly marks basis (`REALIZED_RECONSTRUCTED` vs `CURRENT_SNAPSHOT`) so realized curves
    are never misrepresented as continuous intraday mark-to-market curves.
    """
    points: list[dict] = []
    cum_realized = Decimal(0)
    peak = initial_capital

    if running_since and parse_utc(running_since):
        points.append({
            'timestamp': parse_utc(running_since).isoformat(),
            'equity': str(initial_capital),
            'realized_pnl': '0',
            'unrealized_pnl': '0',
            'equity_peak': str(initial_capital),
            'drawdown_amount': '0',
            'drawdown_pct': '0',
            'basis': 'INITIAL_CAPITAL',
        })

    for t in sorted(closed_trades, key=lambda x: x['exit_time']):
        net = dec(t.get('net_pnl'))
        if net is None:
            continue
        cum_realized += net
        eq = initial_capital + cum_realized
        if eq > peak:
            peak = eq
        dd_amt = peak - eq
        dd_pct = (dd_amt / peak * Decimal(100)) if peak > 0 else Decimal(0)
        points.append({
            'timestamp': t['exit_time'],
            'equity': str(eq),
            'realized_pnl': str(cum_realized),
            'unrealized_pnl': '0',
            'equity_peak': str(peak),
            'drawdown_amount': str(dd_amt),
            'drawdown_pct': str(dd_pct),
            'basis': 'REALIZED_RECONSTRUCTED',
            'trade_id': t['trade_id'],
        })

    if current_equity is not None:
        if current_equity > peak:
            peak = current_equity
        dd_amt = peak - current_equity
        dd_pct = (dd_amt / peak * Decimal(100)) if peak > 0 else Decimal(0)
        points.append({
            'timestamp': snapshot_time,
            'equity': str(current_equity),
            'realized_pnl': str(current_realized if current_realized is not None else cum_realized),
            'unrealized_pnl': str(current_unrealized if current_unrealized is not None else Decimal(0)),
            'equity_peak': str(peak),
            'drawdown_amount': str(dd_amt),
            'drawdown_pct': str(dd_pct),
            'basis': 'CURRENT_SNAPSHOT',
        })

    summary = compute_drawdown_summary(points)
    return points, summary


def compute_drawdown_summary(points: list[dict]) -> dict:
    """Compute exact peak, max drawdown amount/pct, start, trough, recovery, and current drawdown state."""
    if len(points) < 2:
        return {
            'points': len(points),
            'peak_equity': points[0]['equity'] if points else None,
            'max_drawdown_amount': None,
            'max_drawdown_pct': None,
            'dd_start': None,
            'dd_trough': None,
            'dd_recovery': None,
            'in_drawdown': False,
            'basis': '样本不足（少于2个有效权益观测点）',
        }
    peak = dec(points[0]['equity']) or Decimal(0)
    peak_time = points[0]['timestamp']
    max_dd_amt = Decimal(0)
    max_dd_pct = Decimal(0)
    dd_start = None
    dd_trough = None
    dd_recovery = None
    in_max_dd = False

    for p in points:
        eq = dec(p['equity'])
        if eq is None:
            continue
        if eq >= peak:
            if in_max_dd and dd_recovery is None:
                dd_recovery = p['timestamp']
                in_max_dd = False
            peak = eq
            peak_time = p['timestamp']
        else:
            amt = peak - eq
            pct = (amt / peak * Decimal(100)) if peak > 0 else Decimal(0)
            if pct > max_dd_pct:
                max_dd_pct = pct
                max_dd_amt = amt
                dd_start = peak_time
                dd_trough = p['timestamp']
                dd_recovery = None
                in_max_dd = True

    last_eq = dec(points[-1]['equity']) or Decimal(0)
    currently_in_dd = last_eq < peak
    return {
        'points': len(points),
        'peak_equity': str(peak),
        'max_drawdown_amount': str(max_dd_amt),
        'max_drawdown_pct': float(max_dd_pct),
        'dd_start': dd_start,
        'dd_trough': dd_trough,
        'dd_recovery': dd_recovery if not in_max_dd else None,
        'in_drawdown': currently_in_dd,
        'basis': '已实现闭合权益重建序列 + 当前账户快照（不含缺失盘中逐分钟估值）',
    }


def compute_metrics(
    closed_trades: list[dict],
    *,
    initial_capital: Decimal,
    current_equity: Decimal | None,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    all_closed_trades: list[dict] | None = None,
    equity_points: list[dict] | None = None,
) -> dict:
    """Unified, deterministic performance calculator (Section 17)."""
    # Compute interval start equity (NOT always initial_capital when start_dt is set!)
    prior_realized = Decimal(0)
    if start_dt is not None and all_closed_trades is not None:
        for t in all_closed_trades:
            et = parse_utc(t.get('exit_time'))
            if et is not None and et < start_dt:
                prior_realized += dec(t.get('net_pnl')) or Decimal(0)
    interval_start_equity = initial_capital + prior_realized

    # Filter trades inside [start_dt, end_dt]
    selected = []
    for t in closed_trades:
        et = parse_utc(t.get('exit_time'))
        if et is None:
            continue
        if start_dt is not None and et < start_dt:
            continue
        if end_dt is not None and et > end_dt:
            continue
        selected.append(t)

    nets = [dec(t.get('net_pnl')) for t in selected]
    nets = [n for n in nets if n is not None]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    flats = [n for n in nets if n == 0]
    fees_total = sum((dec(t.get('fees')) or Decimal(0) for t in selected), Decimal(0))
    gross_total = sum((dec(t.get('gross_pnl')) or Decimal(0) for t in selected), Decimal(0))
    slip_total = sum((dec(t.get('slippage')) or Decimal(0) for t in selected), Decimal(0))
    net_total = sum(nets, Decimal(0))

    # Consecutive wins / losses
    max_cw = max_cl = cur_w = cur_l = 0
    for n in nets:
        if n > 0:
            cur_w += 1; cur_l = 0
            max_cw = max(max_cw, cur_w)
        elif n < 0:
            cur_l += 1; cur_w = 0
            max_cl = max(max_cl, cur_l)
        else:
            cur_w = cur_l = 0

    # Holding duration
    durs = [float(t.get('holding_duration_sec')) for t in selected if t.get('holding_duration_sec') is not None]
    avg_hold_sec = (sum(durs) / len(durs)) if durs else None

    # Interval end equity & return %
    if end_dt is None and current_equity is not None:
        interval_end_equity = current_equity
    else:
        interval_end_equity = interval_start_equity + net_total
    interval_net_pnl = interval_end_equity - interval_start_equity
    net_return_pct = float((interval_net_pnl / interval_start_equity) * Decimal(100)) if interval_start_equity > 0 else None

    # Profit factor (never fake 999!)
    sum_wins = sum(wins, Decimal(0))
    sum_losses_abs = abs(sum(losses, Decimal(0)))
    if sum_losses_abs > 0:
        pf_val = float(sum_wins / sum_losses_abs)
        pf_label = f'{pf_val:.2f}'
    elif sum_wins > 0:
        pf_val = None
        pf_label = '∞（无亏损交易）'
    else:
        pf_val = None
        pf_label = '—'

    # Interval equity curve & drawdown
    if equity_points is not None:
        filtered_pts = [
            p for p in equity_points
            if (start_dt is None or (parse_utc(p['timestamp']) or datetime.min.replace(tzinfo=UTC)) >= start_dt)
            and (end_dt is None or (parse_utc(p['timestamp']) or datetime.max.replace(tzinfo=UTC)) <= end_dt)
        ]
        if start_dt is not None and (not filtered_pts or (parse_utc(filtered_pts[0]['timestamp']) or start_dt) > start_dt):
            filtered_pts.insert(0, {
                'timestamp': start_dt.isoformat(),
                'equity': str(interval_start_equity),
                'realized_pnl': str(prior_realized),
                'unrealized_pnl': '0',
                'basis': 'INTERVAL_START',
            })
        dd_summary = compute_drawdown_summary(filtered_pts)
    else:
        dd_summary = compute_drawdown_summary([])

    return {
        'closed_count': len(nets),
        'win_count': len(wins),
        'loss_count': len(losses),
        'flat_count': len(flats),
        'win_rate': (len(wins) / len(nets) * 100.0) if nets else None,
        'win_rate_basis': '净盈利闭合交易数 ÷ 有效完整闭合交易总数（含持平）',
        'profit_factor': pf_val,
        'profit_factor_label': pf_label,
        'interval_start_equity': str(interval_start_equity),
        'interval_end_equity': str(interval_end_equity),
        'interval_net_pnl': str(interval_net_pnl),
        'net_return_pct': net_return_pct,
        'gross_pnl': str(gross_total),
        'net_pnl_closed': str(net_total),
        'fees': str(fees_total),
        'slippage': str(slip_total),
        'avg_win': str(sum_wins / len(wins)) if wins else None,
        'avg_loss': str(sum(losses, Decimal(0)) / len(losses)) if losses else None,
        'avg_net_pnl': str(net_total / len(nets)) if nets else None,
        'max_single_win': str(max(wins)) if wins else None,
        'max_single_loss': str(min(losses)) if losses else None,
        'max_consec_wins': max_cw,
        'max_consec_losses': max_cl,
        'avg_holding_sec': avg_hold_sec,
        'max_drawdown_amount': dd_summary.get('max_drawdown_amount'),
        'max_drawdown_pct': dd_summary.get('max_drawdown_pct'),
        'drawdown_summary': dd_summary,
        'sample_status': '暂无成交' if len(nets) == 0 else ('样本不足(<30)' if len(nets) < 30 else '有效样本'),
    }


def compute_attribution(closed_trades: list[dict]) -> dict:
    """Section 16: Long/Short, Exit Reason, Entry Hour/Weekday (UTC+8), Holding Duration, Cost Breakdown."""
    def _bucket_stats(trades: list[dict]) -> dict:
        nets = [dec(t.get('net_pnl')) for t in trades]
        nets = [n for n in nets if n is not None]
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n < 0]
        sw = sum(wins, Decimal(0))
        sl = abs(sum(losses, Decimal(0)))
        pf = f'{float(sw / sl):.2f}' if sl > 0 else ('∞（无亏损）' if sw > 0 else '—')
        return {
            'count': len(nets),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': (len(wins) / len(nets) * 100.0) if nets else None,
            'net_pnl': str(sum(nets, Decimal(0))),
            'avg_pnl': str(sum(nets, Decimal(0)) / len(nets)) if nets else None,
            'profit_factor': pf,
        }

    # 1. Long vs Short
    by_dir = {'LONG': [], 'SHORT': []}
    for t in closed_trades:
        d = str(t.get('side') or '').upper()
        if d in by_dir:
            by_dir[d].append(t)
    direction_stats = {k: _bucket_stats(v) for k, v in by_dir.items()}

    # 2. Exit Reason
    by_reason: dict[str, list[dict]] = defaultdict(list)
    for t in closed_trades:
        cat = t.get('exit_reason_category') or normalize_exit_reason_category(t.get('exit_reason'))
        by_reason[cat].append(t)
    reason_order = ('止盈', '止损', '策略信号退出', '时间退出', '其他退出', '未知原因')
    exit_reason_stats = {r: _bucket_stats(by_reason[r]) for r in reason_order if by_reason.get(r)}

    # 3. Entry Hour & Weekday (UTC+8 Beijing)
    by_hour: dict[str, list[dict]] = defaultdict(list)
    by_weekday: dict[str, list[dict]] = defaultdict(list)
    wd_names = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')
    for t in closed_trades:
        dt = parse_utc(t.get('entry_time'))
        if dt is None:
            continue
        bj = dt.astimezone(BEIJING)
        h_bucket = f'{(bj.hour // 4) * 4:02d}:00–{(bj.hour // 4) * 4 + 3:02d}:59'
        by_hour[h_bucket].append(t)
        by_weekday[wd_names[bj.weekday()]].append(t)
    hour_stats = {k: _bucket_stats(by_hour[k]) for k in sorted(by_hour.keys())}
    weekday_stats = {w: _bucket_stats(by_weekday[w]) for w in wd_names if by_weekday.get(w)}

    # 4. Holding Duration buckets
    dur_buckets = [
        ('1小时以内', 0, 3600),
        ('1至4小时', 3600, 14400),
        ('4至12小时', 14400, 43200),
        ('12至24小时', 43200, 86400),
        ('24小时以上', 86400, float('inf')),
    ]
    by_dur: dict[str, list[dict]] = {lbl: [] for lbl, _, _ in dur_buckets}
    for t in closed_trades:
        sec = t.get('holding_duration_sec')
        if sec is None:
            continue
        for lbl, lo, hi in dur_buckets:
            if lo <= float(sec) < hi:
                by_dur[lbl].append(t)
                break
    duration_stats = {lbl: _bucket_stats(by_dur[lbl]) for lbl, _, _ in dur_buckets if by_dur[lbl]}

    # 5. Cost Analysis
    gross_sum = sum((dec(t.get('gross_pnl')) or Decimal(0) for t in closed_trades), Decimal(0))
    fee_sum = sum((dec(t.get('fees')) or Decimal(0) for t in closed_trades), Decimal(0))
    slip_sum = sum((dec(t.get('slippage')) or Decimal(0) for t in closed_trades), Decimal(0))
    net_sum = sum((dec(t.get('net_pnl')) or Decimal(0) for t in closed_trades), Decimal(0))
    cost_stats = {
        'gross_pnl': str(gross_sum),
        'total_fees': str(fee_sum),
        'recorded_slippage': str(slip_sum),
        'net_pnl': str(net_sum),
        'slippage_note': '成交价格已包含不利滑点，净盈亏 = 成交价差毛盈亏 − 累计手续费（不重复扣减滑点）；未计入资金费率。',
    }

    return {
        'direction': direction_stats,
        'exit_reason': exit_reason_stats,
        'entry_hour_bj': hour_stats,
        'entry_weekday_bj': weekday_stats,
        'holding_duration': duration_stats,
        'cost': cost_stats,
        'disclaimer': '归因分析仅描述历史统计关联，不代表因果关系或未来盈利保证。',
    }


def compute_version_breakdown(closed_trades: list[dict], version_history: list[dict] | None = None) -> list[dict]:
    """Section 18: Group closed trades by opening_strategy_version (never reassigning old trades to latest version)."""
    by_ver: dict[str, list[dict]] = defaultdict(list)
    for t in closed_trades:
        ver = str(t.get('opening_strategy_version') or '—')
        by_ver[ver].append(t)

    known_meta = {str(v.get('version')): v for v in (version_history or []) if v.get('version')}
    all_vers = list(dict.fromkeys(list(known_meta.keys()) + list(by_ver.keys())))
    out = []
    for ver in all_vers:
        trades = by_ver.get(ver, [])
        nets = [dec(t.get('net_pnl')) for t in trades if dec(t.get('net_pnl')) is not None]
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n < 0]
        sw = sum(wins, Decimal(0))
        sl = abs(sum(losses, Decimal(0)))
        pf = f'{float(sw / sl):.2f}' if sl > 0 else ('∞（无亏损）' if sw > 0 else '—')
        times = [t['exit_time'] for t in trades if t.get('exit_time')]
        meta = known_meta.get(ver) or {}
        out.append({
            'version': ver,
            'enabled_at': meta.get('enabled_at') or meta.get('uploaded_at') or '—',
            'trade_start': min(times) if times else '—',
            'trade_end': max(times) if times else '—',
            'closed_count': len(nets),
            'net_pnl': str(sum(nets, Decimal(0))),
            'win_rate': (len(wins) / len(nets) * 100.0) if nets else None,
            'profit_factor': pf,
        })
    return out


def compute_multi_compare(records: list[dict], *, common_window: bool = True, start_dt: datetime | None = None, end_dt: datetime | None = None) -> dict:
    """Section 12: Multi-strategy comparison with common overlap window & capital-model comparability check."""
    if not records:
        return {'strategies': [], 'common_start': None, 'common_end': None, 'comparable': True, 'warning': None}

    starts = [parse_utc(r['base'].get('running_since')) for r in records if parse_utc(r['base'].get('running_since'))]
    eff_start = start_dt
    if common_window and starts:
        latest_start = max(starts)
        eff_start = max(eff_start, latest_start) if eff_start else latest_start

    capital_models = {r['account'].get('capital_model') for r in records}
    comparable = len(capital_models) <= 1
    warning = None if comparable else f"警告：所选策略包含不同资金模型（{', '.join(sorted(str(x) for x in capital_models))}），仅支持描述性对比，不可直接排名。"

    compared = []
    for r in records:
        init_cap = dec(r['account'].get('initial_capital')) or Decimal('10000')
        cur_eq = dec(r['account'].get('current_equity'))
        m = compute_metrics(
            r['closed_trades'],
            initial_capital=init_cap,
            current_equity=cur_eq,
            start_dt=eff_start,
            end_dt=end_dt,
            all_closed_trades=r['closed_trades'],
            equity_points=r['equity_series'],
        )
        # Normalized curve (base = 100.0 at interval_start_equity)
        base_eq = dec(m['interval_start_equity']) or init_cap
        norm_pts = []
        for p in r['equity_series']:
            pt_dt = parse_utc(p['timestamp'])
            if pt_dt is None:
                continue
            if eff_start and pt_dt < eff_start:
                continue
            if end_dt and pt_dt > end_dt:
                continue
            eq = dec(p['equity'])
            if eq is None or base_eq <= 0:
                continue
            norm_val = float(eq / base_eq * Decimal(100))
            ret_val = norm_val - 100.0
            norm_pts.append({
                'timestamp': p['timestamp'],
                'normalized_equity': norm_val,
                'return_pct': ret_val,
                'drawdown_pct': float(dec(p.get('drawdown_pct')) or Decimal(0)),
            })
        attr = compute_attribution([
            t for t in r['closed_trades']
            if (eff_start is None or (parse_utc(t['exit_time']) or datetime.min.replace(tzinfo=UTC)) >= eff_start)
            and (end_dt is None or (parse_utc(t['exit_time']) or datetime.max.replace(tzinfo=UTC)) <= end_dt)
        ])
        compared.append({
            'strategy_id': r['base']['strategy_id'],
            'strategy_name': r['base']['strategy_name'],
            'strategy_version': r['base']['strategy_version'],
            'capital_model': r['account']['capital_model'],
            'metrics': m,
            'direction': attr['direction'],
            'normalized_series': norm_pts,
        })

    return {
        'strategies': compared,
        'common_start': eff_start.isoformat() if eff_start else None,
        'common_end': end_dt.isoformat() if end_dt else None,
        'comparable': comparable,
        'warning': warning,
    }


class ResearchV3Engine:
    """Unified read-only Research V3 Engine with concurrent loading, TTL cache, and fault isolation."""

    def __init__(self, root: Path | str = ROOT, *, client: ReadOnlyHttpClient | None = None, cache_ttl_sec: float = 15.0):
        self.root = Path(root).resolve()
        self.data_dir = self.root / 'data'
        self.client = client or ReadOnlyHttpClient()
        self.cache_ttl_sec = cache_ttl_sec
        self._lock = threading.RLock()
        self._cache: dict | None = None
        self._cache_ts: float = 0.0

    def load_snapshot(self, *, force: bool = False) -> dict:
        with self._lock:
            now_mono = time.perf_counter()
            if not force and self._cache is not None and (now_mono - self._cache_ts) < self.cache_ttl_sec:
                return dict(self._cache, from_cache=True)

        t0 = time.perf_counter()
        snapshot_time = datetime.now(UTC).isoformat()
        errors: list[str] = []

        # Concurrent HTTP GETs (fault-isolated, never stalls sequentially!)
        endpoints = {
            'agents': '/api/v1/agents',
            'stats_abc': '/api/v1/statistics/abc',
            'acc_a': '/api/v1/agents/A/account',
            'acc_b': '/api/v1/agents/B/account',
            'acc_c': '/api/v1/agents/C/account',
            'e_accounts': '/api/v1/e/accounts',
            'g_status': '/api/v1/g-mirror/status',
            'g_fills': '/api/v1/g-mirror/fills?limit=2000',
            'strategies': '/api/v1/strategies',
            'fills': '/api/v1/fills',
            'pool_api': '/api/v1/strategy-pool',
        }
        api_data: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(self.client.get, ep): k for k, ep in endpoints.items()}
            for fut in as_completed(futs):
                k = futs[fut]
                try:
                    api_data[k] = fut.result()
                except Exception as exc:
                    api_data[k] = None
                    errors.append(f'{endpoints[k]}: {type(exc).__name__}')

        # Read local SQLite databases in strict mode=ro (fast, zero write locks)
        pool_db = self._read_strategy_pool_db(errors)
        ext_db = self._read_pa_extensions_db(errors)
        d_db = self._read_d_db(errors)
        paper_audits = self._read_paper_audits(errors)

        agents_map = {a['agent']: a for a in (api_data.get('agents') or []) if isinstance(a, dict) and a.get('agent')}
        stats_abc = (api_data.get('stats_abc') or {}).get('agents') or {}
        all_abc_fills = (api_data.get('fills') or {}).get('items') or []
        reg_map = {r.get('agent'): r for r in ((api_data.get('strategies') or {}).get('items') or []) if isinstance(r, dict)}
        pool_api_map = {p.get('id'): p for p in ((api_data.get('pool_api') or {}).get('items') or []) if isinstance(p, dict)}

        records: list[dict] = []

        # 1. Core ABC (discovered dynamically from agents_map / paper.sqlite3)
        for sid in sorted(set(agents_map.keys()) | {'A', 'B', 'C'}):
            if sid not in ('A', 'B', 'C'):
                continue
            a = agents_map.get(sid) or {}
            st = stats_abc.get(sid) or {}
            acc_ep = api_data.get(f'acc_{sid.lower()}') or {}
            fills = [f for f in all_abc_fills if f.get('agent') == sid]
            closed, issues, open_cnt = pair_ledger_fills_v3(fills, sid, f'paper:{sid}', audit_map=paper_audits.get(sid))
            init_cap = dec(acc_ep.get('initial_equity')) or dec(st.get('initial_equity')) or Decimal('10000')
            cur_eq = dec(acc_ep.get('equity')) if dec(acc_ep.get('equity')) is not None else dec(st.get('equity'))
            cur_real = dec(acc_ep.get('realized_pnl')) if dec(acc_ep.get('realized_pnl')) is not None else dec(st.get('realized_net_pnl') if st.get('realized_net_pnl') is not None else st.get('realized_pnl'))
            cur_unreal = dec(acc_ep.get('unrealized_pnl')) if dec(acc_ep.get('unrealized_pnl')) is not None else dec(st.get('unrealized_pnl'))
            cur_fees = dec(st.get('fees')) or sum((dec(f.get('fee')) or Decimal(0) for f in fills), Decimal(0))
            ver = (a.get('active_rule') or {}).get('version') or 'v2.6'
            label = (a.get('active_rule') or {}).get('label') or _LEGACY_FALLBACK_NAMES.get(sid, sid)
            first_ts = min((f.get('occurred_at') for f in fills if f.get('occurred_at')), default=None)
            eq_series, dd_sum = build_equity_series(
                closed, initial_capital=init_cap, current_equity=cur_eq,
                current_realized=cur_real, current_unrealized=cur_unreal,
                running_since=first_ts, snapshot_time=snapshot_time,
            )
            records.append(self._pack_record(
                sid=sid, name=label, group='原有策略', version=str(ver), stype='CORE_RULE',
                state=str(st.get('state') or a.get('state') or 'RUNNING'),
                capital_model='PAPER_10000_RISK_2PCT_5X', init_cap=init_cap,
                cur_eq=cur_eq, avail=dec(st.get('available_cash')), cur_real=cur_real, cur_unreal=cur_unreal,
                fees=cur_fees, position=st.get('position'), fills=fills, closed=closed, issues=issues,
                open_cnt=open_cnt, eq_series=eq_series, dd_sum=dd_sum,
                source='GET /api/v1/statistics/abc, /fills + paper.sqlite3(ro)',
                running_since=first_ts, snapshot_time=snapshot_time, versions_history=[],
            ))

        # 2. Strategy D
        if d_db is not None:
            fills = d_db.get('fills') or []
            closed, issues, open_cnt = pair_ledger_fills_v3(fills, 'D', 'paper:D')
            init_cap = Decimal('10000')
            cur_eq = dec(d_db.get('equity'))
            cur_real = dec(d_db.get('realized_pnl'))
            cur_unreal = dec(d_db.get('unrealized_pnl'))
            cur_fees = sum((dec(f.get('fee')) or Decimal(0) for f in fills), Decimal(0))
            first_ts = min((f.get('occurred_at') for f in fills if f.get('occurred_at')), default=d_db.get('first_cycle'))
            eq_series, dd_sum = build_equity_series(
                closed, initial_capital=init_cap, current_equity=cur_eq,
                current_realized=cur_real, current_unrealized=cur_unreal,
                running_since=first_ts, snapshot_time=snapshot_time,
            )
            records.append(self._pack_record(
                sid='D', name=_LEGACY_FALLBACK_NAMES['D'], group='原有策略',
                version=str(d_db.get('rule_version') or 'D-COMMON-GUARD-V1'), stype='CORE_D',
                state=str(d_db.get('status') or 'RUNNING'),
                capital_model='PAPER_10000_RISK_2PCT_5X', init_cap=init_cap,
                cur_eq=cur_eq, avail=cur_eq, cur_real=cur_real, cur_unreal=cur_unreal,
                fees=cur_fees, position=(d_db.get('account') or {}).get('position'),
                fills=fills, closed=closed, issues=issues, open_cnt=open_cnt,
                eq_series=eq_series, dd_sum=dd_sum,
                source='SQLite mode=ro: d/trading.sqlite3, d/formal.sqlite3',
                running_since=first_ts, snapshot_time=snapshot_time, versions_history=[],
            ))

        # 3. Strategy E
        e_data = api_data.get('e_accounts') or {}
        if e_data.get('formal'):
            formal = e_data['formal']
            fills = formal.get('fills') or []
            closed, issues, open_cnt = pair_ledger_fills_v3(fills, 'E', 'paper:E', audit_map=paper_audits.get('E'))
            init_cap = Decimal('10000')
            cur_eq = dec(formal.get('equity'))
            cur_real = dec(formal.get('realized_pnl'))
            cur_unreal = dec(formal.get('unrealized_pnl'))
            cur_fees = sum((dec(f.get('fee')) or Decimal(0) for f in fills), Decimal(0))
            first_ts = min((f.get('occurred_at') for f in fills if f.get('occurred_at')), default=None)
            eq_series, dd_sum = build_equity_series(
                closed, initial_capital=init_cap, current_equity=cur_eq,
                current_realized=cur_real, current_unrealized=cur_unreal,
                running_since=first_ts, snapshot_time=snapshot_time,
            )
            records.append(self._pack_record(
                sid='E', name=_LEGACY_FALLBACK_NAMES['E'], group='原有策略',
                version=str((reg_map.get('E') or {}).get('driving_version') or 'E-V1.1'), stype='CORE_E',
                state=str((e_data.get('runtime') or {}).get('mode') or 'RUNNING'),
                capital_model='PAPER_10000_RISK_2PCT_5X', init_cap=init_cap,
                cur_eq=cur_eq, avail=cur_eq, cur_real=cur_real, cur_unreal=cur_unreal,
                fees=cur_fees, position=(formal.get('account') or {}).get('position_json'),
                fills=fills, closed=closed, issues=issues, open_cnt=open_cnt,
                eq_series=eq_series, dd_sum=dd_sum,
                source='GET /api/v1/e/accounts + e/formal.sqlite3(ro)',
                running_since=first_ts, snapshot_time=snapshot_time, versions_history=[],
            ))

        # 4. Strategy G (Mirror)
        g_data = api_data.get('g_status') or {}
        if g_data:
            g_fills = (api_data.get('g_fills') or {}).get('items') or []
            closed, issues, open_cnt = pair_ledger_fills_v3(g_fills, 'G', 'paper:G')
            if len(g_fills) >= 2000:
                issues.append('G 成交接口达到 2000 条上限，更早历史可能截断')
            init_cap = Decimal('10000')
            cur_eq = dec(g_data.get('equity'))
            cur_real = dec(g_data.get('realized'))
            cur_unreal = dec(g_data.get('unrealized'))
            cur_fees = dec(g_data.get('fees')) or sum((dec(f.get('fee')) or Decimal(0) for f in g_fills), Decimal(0))
            first_ts = min((f.get('occurred_at') for f in g_fills if f.get('occurred_at')), default=None)
            eq_series, dd_sum = build_equity_series(
                closed, initial_capital=init_cap, current_equity=cur_eq,
                current_realized=cur_real, current_unrealized=cur_unreal,
                running_since=first_ts, snapshot_time=snapshot_time,
            )
            records.append(self._pack_record(
                sid='G', name=_LEGACY_FALLBACK_NAMES['G'], group='镜像策略',
                version=str(g_data.get('version') or 'MIRROR-V1'), stype='MIRROR_EVENT',
                state=str(g_data.get('follow_state') or 'RUNNING'),
                capital_model='MIRROR_C_EVENT_10000', init_cap=init_cap,
                cur_eq=cur_eq, avail=cur_eq, cur_real=cur_real, cur_unreal=cur_unreal,
                fees=cur_fees, position=g_data.get('position'),
                fills=g_fills, closed=closed, issues=issues, open_cnt=open_cnt,
                eq_series=eq_series, dd_sum=dd_sum,
                source='GET /api/v1/g-mirror/status, /fills',
                running_since=first_ts, snapshot_time=snapshot_time, versions_history=[],
            ))

        # 5. ALL Strategy Pool strategies (dynamically discovered: PA-01..08, Chan, and ANY future upload!)
        for sid, pdata in sorted(pool_db.items()):
            if pdata.get('archived_at'):
                continue
            api_item = pool_api_map.get(sid) or {}
            doc = pdata.get('document') or {}
            fills = pdata.get('fills') or []
            closed, issues, open_cnt = pair_pool_fills_v3(fills, sid, f'pool:{sid}')
            init_cap = dec((doc.get('risk') or {}).get('initial_equity')) or Decimal('10000')
            cur_eq = dec(api_item.get('equity')) if api_item.get('equity') is not None else dec(pdata.get('equity'))
            cur_real = dec(api_item.get('realized')) if api_item.get('realized') is not None else dec(pdata.get('realized'))
            cur_unreal = dec(api_item.get('unrealized')) if api_item.get('unrealized') is not None else dec(pdata.get('unrealized'))
            cur_fees = dec(pdata.get('fees')) or Decimal(0)
            name = str(doc.get('name') or api_item.get('name') or _LEGACY_FALLBACK_NAMES.get(sid) or f'名称待完善（{sid}）').strip()
            first_ts = pdata.get('registered_at') or min((f.get('created_at') or f.get('cycle') for f in fills), default=None)
            eq_series, dd_sum = build_equity_series(
                closed, initial_capital=init_cap, current_equity=cur_eq,
                current_realized=cur_real, current_unrealized=cur_unreal,
                running_since=first_ts, snapshot_time=snapshot_time,
            )
            records.append(self._pack_record(
                sid=sid, name=name, group='策略池',
                version=str(pdata.get('active_version') or pdata.get('latest_version') or '1.0.0'),
                stype=str(doc.get('format') or 'POOL_V2'),
                state=str(api_item.get('status') or pdata.get('status') or ('RUNNING' if pdata.get('enabled') else 'PAUSED')),
                capital_model='PAPER_10000_RISK_2PCT_5X', init_cap=init_cap,
                cur_eq=cur_eq, avail=dec(pdata.get('cash')), cur_real=cur_real, cur_unreal=cur_unreal,
                fees=cur_fees, position=pdata.get('position'),
                fills=fills, closed=closed, issues=issues, open_cnt=open_cnt,
                eq_series=eq_series, dd_sum=dd_sum,
                source='SQLite mode=ro: strategy-pool.sqlite3 + /api/v1/strategy-pool',
                running_since=first_ts, snapshot_time=snapshot_time,
                versions_history=pdata.get('versions_history') or [],
                description=str(doc.get('description') or ''),
            ))

        # 6. Dynamic PA Extension ledgers (PA-09, PA-10, and any future pa-v1-extensions/*.sqlite3)
        for sid, edata in sorted(ext_db.items()):
            api_item = pool_api_map.get(sid) or {}
            fills = edata.get('fills') or []
            closed, issues, open_cnt = pair_ledger_fills_v3(fills, sid, f'ext:{sid}')
            init_cap = Decimal('10000')
            cur_eq = dec(api_item.get('equity')) if api_item.get('equity') is not None else dec(edata.get('equity'))
            cur_real = dec(api_item.get('realized')) if api_item.get('realized') is not None else Decimal(0)
            cur_unreal = dec(api_item.get('unrealized')) if api_item.get('unrealized') is not None else Decimal(0)
            cur_fees = sum((dec(f.get('fee')) or Decimal(0) for f in fills), Decimal(0))
            name = str(api_item.get('name') or _LEGACY_FALLBACK_NAMES.get(sid) or sid)
            first_ts = min((f.get('at') or f.get('occurred_at') for f in fills if f.get('at') or f.get('occurred_at')), default=None)
            eq_series, dd_sum = build_equity_series(
                closed, initial_capital=init_cap, current_equity=cur_eq,
                current_realized=cur_real, current_unrealized=cur_unreal,
                running_since=first_ts, snapshot_time=snapshot_time,
            )
            records.append(self._pack_record(
                sid=sid, name=name, group='策略池',
                version=str(edata.get('active_version') or '1.0.0'), stype='PA_EXTENSION',
                state=str(api_item.get('status') or edata.get('status') or 'RUNNING'),
                capital_model='PAPER_10000_RISK_2PCT_5X', init_cap=init_cap,
                cur_eq=cur_eq, avail=dec(edata.get('cash')), cur_real=cur_real, cur_unreal=cur_unreal,
                fees=cur_fees, position=edata.get('position'),
                fills=fills, closed=closed, issues=issues, open_cnt=open_cnt,
                eq_series=eq_series, dd_sum=dd_sum,
                source=f'SQLite mode=ro: pa-v1-extensions/{sid}.sqlite3',
                running_since=first_ts, snapshot_time=snapshot_time, versions_history=[],
            ))

        # 7. Saved Backtest Reports (strictly isolated from live paper!)
        backtests = self.load_backtest_reports()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        snapshot = {
            'snapshot_time': snapshot_time,
            'load_ms': elapsed_ms,
            'from_cache': False,
            'stale': False,
            'data_source_mode': 'PAPER_LIVE',
            'atomicity_note': '分接口与只读账本时点快照（非严格原子快照），不同资金模型及时间窗口不可直接排名。',
            'calculation_version': CALCULATION_VERSION,
            'strategies': records,
            'backtest_reports': backtests,
            'errors': errors,
        }
        with self._lock:
            self._cache = snapshot
            self._cache_ts = time.perf_counter()
        return snapshot

    @staticmethod
    def _pack_record(
        *, sid, name, group, version, stype, state, capital_model, init_cap,
        cur_eq, avail, cur_real, cur_unreal, fees, position, fills, closed, issues,
        open_cnt, eq_series, dd_sum, source, running_since, snapshot_time,
        versions_history, description='',
    ) -> dict:
        metrics = compute_metrics(
            closed, initial_capital=init_cap, current_equity=cur_eq,
            all_closed_trades=closed, equity_points=eq_series,
        )
        cov_start = running_since or (closed[0]['entry_time'] if closed else None)
        cov_end = snapshot_time
        if issues:
            completeness = f'部分缺失/待核查（{len(issues)}项）'
        elif len(closed) == 0:
            completeness = '暂无成交'
        elif len(closed) < 30:
            completeness = f'样本不足（{len(closed)}笔）'
        else:
            completeness = '完整'

        run_days = None
        if cov_start and parse_utc(cov_start):
            run_days = max(0, (parse_utc(snapshot_time) - parse_utc(cov_start)).days)

        return {
            'base': {
                'strategy_id': sid,
                'strategy_name': name,
                'strategy_group': group,
                'strategy_version': version,
                'strategy_type': stype,
                'symbol': 'BTC/USD',
                'timeframe': '15m',
                'registered_at': running_since,
                'running_since': running_since,
                'running_days': run_days,
                'state': state,
                'description': description,
                'versions_history': versions_history,
            },
            'account': {
                'account_id': f'{group}:{sid}',
                'account_mode': 'PAPER_SIMULATION',
                'currency': 'USD_RESEARCH',
                'capital_model': capital_model,
                'initial_capital': str(init_cap),
                'current_equity': str(cur_eq) if cur_eq is not None else None,
                'available_balance': str(avail) if avail is not None else None,
                'realized_pnl': str(cur_real) if cur_real is not None else None,
                'unrealized_pnl': str(cur_unreal) if cur_unreal is not None else None,
                'total_fees': str(fees) if fees is not None else '0',
                'current_position': position,
                'active_open_count': open_cnt,
            },
            'fills': fills,
            'closed_trades': closed,
            'metrics': metrics,
            'equity_series': eq_series,
            'drawdown_summary': dd_sum,
            'data_quality': {
                'data_source': source,
                'coverage_start': cov_start,
                'coverage_end': cov_end,
                'snapshot_time': snapshot_time,
                'completeness': completeness,
                'missing_data': issues,
                'metric_availability': {
                    'win_rate': len(closed) > 0,
                    'profit_factor': metrics['profit_factor'] is not None,
                    'max_drawdown': dd_sum.get('max_drawdown_pct') is not None,
                },
                'calculation_version': CALCULATION_VERSION,
            },
        }

    def _read_strategy_pool_db(self, errors: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        conn = None
        try:
            conn = _connect_ro(self.data_dir / 'strategy-pool.sqlite3')
            if conn is None:
                return out
            rows = conn.execute(
                'SELECT s.id,s.active_version,s.enabled,s.paused_at,s.archived_at,'
                'a.cash,a.position,a.realized,a.last_cycle,a.last_status,a.last_action '
                'FROM strategies s LEFT JOIN accounts a ON a.strategy_id=s.id ORDER BY s.id'
            ).fetchall()
            # Batch-load versions & fills in 2 single SQL queries (O(1) queries even for 100+ strategies!)
            ver_by_sid: dict[str, list[dict]] = defaultdict(list)
            for vr in conn.execute('SELECT strategy_id,version,document,uploaded_at,enabled_at FROM versions ORDER BY uploaded_at'):
                ver_by_sid[vr['strategy_id']].append(dict(vr))
            fills_by_sid: dict[str, list[dict]] = defaultdict(list)
            for fr in conn.execute('SELECT strategy_id,version,cycle,side,qty,price,fee,slippage,reason,idempotency_key,created_at FROM fills ORDER BY id'):
                fills_by_sid[fr['strategy_id']].append(dict(fr))

            for r in rows:
                sid = r['id']
                vlist = ver_by_sid.get(sid, [])
                active_ver = r['active_version'] or (vlist[-1]['version'] if vlist else None)
                doc_row = next((v for v in vlist if v['version'] == active_ver), vlist[-1] if vlist else None)
                doc = json.loads(doc_row['document']) if (doc_row and doc_row.get('document')) else {}
                pos = json.loads(r['position']) if r['position'] else None
                fills = fills_by_sid.get(sid, [])
                fees_sum = sum((dec(f.get('fee')) or Decimal(0) for f in fills), Decimal(0))
                cash = dec(r['cash'])
                unreal = Decimal(0)
                if pos and not pos.get('pending_entry'):
                    mark = dec(pos.get('mark')) or dec(pos.get('entry')) or Decimal(0)
                    entry = dec(pos.get('entry')) or Decimal(0)
                    qty = dec(pos.get('qty')) or Decimal(0)
                    sign = Decimal(1) if str(pos.get('direction', 'LONG')).upper() == 'LONG' else Decimal(-1)
                    unreal = (mark - entry) * qty * sign
                eq = (cash + unreal) if cash is not None else None
                out[sid] = {
                    'id': sid, 'active_version': active_ver,
                    'latest_version': vlist[-1]['version'] if vlist else None,
                    'enabled': bool(r['enabled']), 'archived_at': r['archived_at'],
                    'status': 'RUNNING' if r['enabled'] else 'PAUSED',
                    'cash': r['cash'], 'equity': str(eq) if eq is not None else None,
                    'realized': r['realized'], 'unrealized': str(unreal),
                    'fees': str(fees_sum), 'position': pos, 'fills': fills, 'document': doc,
                    'registered_at': vlist[0]['uploaded_at'] if vlist else None,
                    'versions_history': [{'version': v['version'], 'uploaded_at': v['uploaded_at'], 'enabled_at': v['enabled_at']} for v in vlist],
                }
        except Exception as exc:
            errors.append(f'strategy-pool.sqlite3: {type(exc).__name__}')
        finally:
            if conn is not None:
                conn.close()
        return out

    def _read_pa_extensions_db(self, errors: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        ext_dir = self.data_dir / 'pa-v1-extensions'
        if not ext_dir.is_dir():
            return out
        for path in sorted(ext_dir.glob('*.sqlite3')):
            sid = path.stem
            conn = None
            try:
                conn = _connect_ro(path)
                if conn is None:
                    continue
                row = conn.execute('SELECT value FROM snapshot LIMIT 1').fetchone()
                if not row:
                    continue
                s = json.loads(row['value'])
                fills = [json.loads(r['value']) for r in conn.execute('SELECT value FROM fills ORDER BY rowid')]
                pos = s.get('position')
                cash = dec(s.get('cash'))
                out[sid] = {
                    'id': sid, 'active_version': s.get('version') or '1.0.0',
                    'enabled': bool(s.get('enabled')), 'status': s.get('last_status') or 'RUNNING',
                    'cash': str(cash) if cash is not None else '10000',
                    'equity': str(cash) if (cash is not None and not pos) else None,
                    'position': pos, 'fills': fills,
                }
            except Exception as exc:
                errors.append(f'{path.name}: {type(exc).__name__}')
            finally:
                if conn is not None:
                    conn.close()
        return out

    def _read_d_db(self, errors: list[str]) -> dict | None:
        t_path = self.data_dir / 'd' / 'trading.sqlite3'
        if not t_path.is_file():
            return None
        out: dict[str, Any] = {}
        conn = None
        try:
            conn = _connect_ro(t_path)
            r = conn.execute('SELECT cash,position_json,realized_pnl FROM d_account WHERE id=1').fetchone()
            if r:
                pos = json.loads(r['position_json']) if r['position_json'] else None
                out.update(account={'position': pos}, equity=r['cash'] if pos is None else None,
                           realized_pnl=r['realized_pnl'], unrealized_pnl='0' if pos is None else None)
            fills = []
            for row in conn.execute('SELECT f.*,o.operation,o.response_json FROM d_fills f LEFT JOIN d_operations o ON o.idempotency_key=f.idempotency_key ORDER BY f.occurred_at,f.fill_id'):
                item = dict(row)
                try:
                    resp = json.loads(item.pop('response_json') or '{}')
                except ValueError:
                    resp = {}
                if resp.get('accepted') is True:
                    item['position_direction'] = 'LONG'
                    if item.get('effect') == 'CLOSE':
                        item['pnl'] = resp.get('net_pnl')
                fills.append(item)
            out['fills'] = fills
        except Exception as exc:
            errors.append(f'd/trading.sqlite3: {type(exc).__name__}')
        finally:
            if conn is not None:
                conn.close()

        conn = None
        try:
            conn = _connect_ro(self.data_dir / 'd' / 'formal.sqlite3')
            if conn is not None:
                r = conn.execute('SELECT protection,reason FROM d_runtime_status WHERE id=1').fetchone()
                if r:
                    out['status'] = r['protection']
                c_first = conn.execute('SELECT MIN(close_at) FROM d_cycles').fetchone()
                if c_first and c_first[0]:
                    out['first_cycle'] = c_first[0]
            cfg = self.root / 'config' / 'strategy_d' / 'D_source_recovered_2026-11-21.json'
            if cfg.is_file():
                out['rule_version'] = json.loads(cfg.read_text(encoding='utf-8-sig')).get('version')
        except Exception as exc:
            errors.append(f'd/formal.sqlite3: {type(exc).__name__}')
        finally:
            if conn is not None:
                conn.close()
        return out

    def _read_paper_audits(self, errors: list[str]) -> dict[str, dict[str, dict]]:
        out: dict[str, dict[str, dict]] = defaultdict(dict)
        for rel, agent_map in (('paper.sqlite3', {'A': 'A', 'B': 'B', 'C': 'C'}), ('e/formal.sqlite3', {'A': 'E'})):
            conn = None
            try:
                conn = _connect_ro(self.data_dir / rel)
                if conn is None:
                    continue
                for r in conn.execute("SELECT agent,payload FROM audit_events WHERE kind='POSITION_CLOSED'"):
                    mapped = agent_map.get(r['agent'])
                    if not mapped:
                        continue
                    try:
                        p = json.loads(r['payload'] or '{}')
                    except ValueError:
                        continue
                    tid = str(p.get('trade_id') or '')
                    if tid:
                        out[mapped][tid] = p
            except Exception as exc:
                errors.append(f'{rel}: {type(exc).__name__}')
            finally:
                if conn is not None:
                    conn.close()
        return out

    def load_backtest_reports(self, limit: int = 50) -> list[dict]:
        """Read saved backtest reports read-only from strategy-pool-backtests.sqlite3 (strictly isolated)."""
        out: list[dict] = []
        conn = None
        try:
            conn = _connect_ro(self.data_dir / 'strategy-pool-backtests.sqlite3')
            if conn is None:
                return out
            for r in conn.execute('SELECT id,strategy_id,version,result,created_at FROM reports ORDER BY created_at DESC LIMIT ?', (limit,)):
                try:
                    rep = json.loads(r['result'])
                except ValueError:
                    continue
                out.append({
                    'report_id': r['id'],
                    'strategy_id': r['strategy_id'],
                    'strategy_name': rep.get('strategy_name') or _LEGACY_FALLBACK_NAMES.get(r['strategy_id'], r['strategy_id']),
                    'version': r['version'],
                    'created_at': r['created_at'],
                    'start': rep.get('actual_start') or rep.get('start'),
                    'end': rep.get('actual_end') or rep.get('end'),
                    'bar_count': rep.get('bar_count') or len(rep.get('equity_curve') or []),
                    'initial_equity': rep.get('initial_equity', '10000'),
                    'final_equity': rep.get('final_equity'),
                    'net_pnl': rep.get('net_pnl'),
                    'net_return_pct': rep.get('net_return_pct'),
                    'closed_trade_count': rep.get('closed_trade_count', 0),
                    'win_rate': float(rep.get('win_rate') or 0) * 100.0,
                    'profit_factor_label': rep.get('profit_factor_label') or '—',
                    'max_drawdown_pct': rep.get('max_drawdown_pct'),
                    'fees': rep.get('fees'),
                    'data_gaps': len(rep.get('data_gaps') or []),
                    'source': 'BACKTEST_SAVED (strategy-pool-backtests.sqlite3)',
                })
        except Exception:
            pass
        finally:
            if conn is not None:
                conn.close()
        return out


def export_research_report(
    *,
    output_dir: Path | str,
    records: list[dict],
    mode: str = 'overview',
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    snapshot_time: str = '',
    compare_result: dict | None = None,
) -> list[Path]:
    """Export structured CSV + AI-friendly Markdown research report (Section 20)."""
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    ts_slug = datetime.now(BEIJING).strftime('%Y%m%d_%H%M%S')
    csv_path = out / f'research_v3_{mode}_{ts_slug}.csv'
    md_path = out / f'research_v3_{mode}_{ts_slug}.md'

    rows_flat = []
    for r in records:
        init_cap = dec(r['account'].get('initial_capital')) or Decimal('10000')
        cur_eq = dec(r['account'].get('current_equity'))
        m = compute_metrics(
            r['closed_trades'], initial_capital=init_cap, current_equity=cur_eq,
            start_dt=start_dt, end_dt=end_dt, all_closed_trades=r['closed_trades'],
            equity_points=r['equity_series'],
        )
        rows_flat.append({
            'strategy_id': r['base']['strategy_id'],
            'strategy_name': r['base']['strategy_name'],
            'strategy_group': r['base']['strategy_group'],
            'strategy_version': r['base']['strategy_version'],
            'state': r['base']['state'],
            'capital_model': r['account']['capital_model'],
            'initial_capital': r['account']['initial_capital'],
            'current_equity': r['account']['current_equity'] or '—',
            'interval_net_pnl': m['interval_net_pnl'],
            'net_return_pct': f"{m['net_return_pct']:.2f}%" if m['net_return_pct'] is not None else '—',
            'closed_count': m['closed_count'],
            'win_rate': f"{m['win_rate']:.1f}%" if m['win_rate'] is not None else '—',
            'profit_factor': m['profit_factor_label'],
            'max_drawdown_pct': f"{m['max_drawdown_pct']:.2f}%" if m['max_drawdown_pct'] is not None else '—',
            'fees': m['fees'],
            'avg_net_pnl': m['avg_net_pnl'] or '—',
            'completeness': r['data_quality']['completeness'],
            'data_source': r['data_quality']['data_source'],
        })

    if rows_flat:
        with csv_path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows_flat[0].keys()))
            writer.writeheader()
            writer.writerows(rows_flat)

    # Structured Markdown report (traceable, AI-friendly, no fabricated optimization claims)
    lines = [
        f'# BTC Paper Lab V3.1 — 量化策略研究报告（{mode}）',
        '',
        f'- **生成时间（北京时间）**：{datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")}',
        f'- **快照时间（UTC）**：{snapshot_time or datetime.now(UTC).isoformat()}',
        f'- **研究区间**：{fmt_bj(start_dt) if start_dt else "全部有效历史"} ~ {fmt_bj(end_dt) if end_dt else "最新快照"}',
        f'- **指标计算版本**：`{CALCULATION_VERSION}`',
        '- **严谨性声明**：不同资金模型与时间窗口不可直接排名；缺少完整闭合证据的成交不计入胜率与盈亏比；本报告仅陈述客观历史统计，不包含未经证实的自动优化结论。',
        '',
        '## 一、核心绩效汇总',
        '',
        '| 策略 ID | 中文名称 | 版本 | 资金模型 | 当前权益 | 区间净盈亏 | 收益率 | 闭合笔数 | 胜率 | 盈利因子 | 最大回撤 | 手续费 | 数据质量 |',
        '|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|',
    ]
    for r in rows_flat:
        lines.append(
            f"| {r['strategy_id']} | {r['strategy_name']} | {r['strategy_version']} | {r['capital_model']} | "
            f"{r['current_equity']} | {r['interval_net_pnl']} | {r['net_return_pct']} | {r['closed_count']} | "
            f"{r['win_rate']} | {r['profit_factor']} | {r['max_drawdown_pct']} | {r['fees']} | {r['completeness']} |"
        )

    if len(records) == 1:
        r0 = records[0]
        attr = compute_attribution(r0['closed_trades'])
        lines.extend([
            '',
            '## 二、多空方向与退出原因归因',
            '',
            f"- **做多**：{attr['direction']['LONG']['count']} 笔，净盈亏 `{attr['direction']['LONG']['net_pnl']}`，胜率 `{attr['direction']['LONG']['win_rate']:.1f}%`（若有效）" if attr['direction']['LONG']['win_rate'] is not None else f"- **做多**：{attr['direction']['LONG']['count']} 笔（暂无闭合）",
            f"- **做空**：{attr['direction']['SHORT']['count']} 笔，净盈亏 `{attr['direction']['SHORT']['net_pnl']}`，胜率 `{attr['direction']['SHORT']['win_rate']:.1f}%`" if attr['direction']['SHORT']['win_rate'] is not None else f"- **做空**：{attr['direction']['SHORT']['count']} 笔（暂无闭合）",
            f"- **成本拆解**：毛盈亏 `{attr['cost']['gross_pnl']}`，手续费 `{attr['cost']['total_fees']}`，净盈亏 `{attr['cost']['net_pnl']}`（{attr['cost']['slippage_note']}）",
        ])
    elif compare_result and compare_result.get('warning'):
        lines.extend(['', f"> **可比性提示**：{compare_result['warning']}"])

    md_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return [csv_path, md_path]
