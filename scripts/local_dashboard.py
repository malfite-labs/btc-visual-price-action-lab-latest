"""Local paper-service launcher and read-only account dashboard."""
import json
import hashlib
import os
import sqlite3
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import calendar as month_calendar
import tkinter as tk
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
NO_WINDOW = subprocess.CREATE_NO_WINDOW
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PA_STRATEGIES = (
    ('pa-trend-40-breakout', 'PA-01 顺势突破'),
    ('pa-sma20-pullback-confirm', 'PA-02 SMA20顺势回踩'),
    ('pa-full-engulfing-reversal', 'PA-03 双重吞噬反转'),
    ('pa-gapless-three-bar-reversal', 'PA-04 无缺口三K反转'),
    ('pa-trend-false-break-reclaim', 'PA-05 顺势假突破收复'),
    ('pa-inside-bar-break', 'PA-06 内包K线趋势突破'),
    ('pa-big-candle-continuation', 'PA-07 大实体K线趋势推进'),
    ('pa-three-bar-hhhl', 'PA-08 三根高低点持续推进'),
)
PA_STRATEGY_IDS = tuple(strategy_id for strategy_id, _ in PA_STRATEGIES)
CORE_STRATEGY_LABELS = (
    ('A', 'A'),
    ('B', 'B'),
    ('C', 'C'),
    ('D', 'D'),
    ('E', 'E'),
    ('G', 'G · C反向镜像'),
)
RULE_DETAIL_IDS = ('A', 'B', 'C', 'D', 'E', 'G', *PA_STRATEGY_IDS)
BEIJING = timezone(timedelta(hours=8), name='Asia/Shanghai')


def _pa_extension_names():
    try:
        from backend.pa_extension import NAMES
        return dict(NAMES)
    except Exception:
        return {}


def strategy_display_catalog(extra_names=None):
    """Single source of truth for ordered strategy_id -> display label mapping."""
    catalog = dict(CORE_STRATEGY_LABELS)
    for strategy_id, label in PA_STRATEGIES:
        code, _, title = label.partition(' ')
        catalog[strategy_id] = f'{code} · {title}' if title else code
    for strategy_id, title in _pa_extension_names().items():
        catalog[strategy_id] = f'{strategy_id} · {title}' if title and title != strategy_id else strategy_id
    if extra_names:
        for strategy_id, title in extra_names.items():
            if strategy_id not in catalog and title:
                text = str(title).strip()
                catalog[strategy_id] = text if text == strategy_id or ' · ' in text else f'{strategy_id} · {text}'
    return catalog


def strategy_display_name(strategy_id, extra_names=None):
    if not strategy_id:
        return '未知'
    sid = str(strategy_id)
    return strategy_display_catalog(extra_names).get(sid, sid)


def strategy_sort_key(strategy_id):
    sid = str(strategy_id or '')
    ordered = tuple(strategy_display_catalog().keys())
    if sid in ordered:
        return (0, ordered.index(sid), sid)
    if sid.upper().startswith('PA-') and sid[3:].isdigit():
        return (1, int(sid[3:]), sid)
    return (2, 0, sid.casefold())


def build_live_paper_rows(cached_ids=(), data_dir=None):
    """Return ordered {strategy_id: display_label} for live-copy strategy selection."""
    from contextlib import closing
    data = Path(data_dir) if data_dir is not None else Path(__file__).resolve().parents[1] / 'data'
    ids = set(cached_ids or ())
    extra_names = {}
    try:
        with closing(sqlite3.connect((data / 'paper.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
            ids.update(row[0] for row in db.execute('SELECT agent FROM accounts'))
    except sqlite3.Error:
        pass
    try:
        with closing(sqlite3.connect((data / 'strategy-pool.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
            query = (
                'SELECT s.id, v.document FROM strategies s '
                'LEFT JOIN versions v ON v.strategy_id = s.id AND v.version = s.active_version '
                'WHERE s.archived_at IS NULL'
            )
            for sid, raw_doc in db.execute(query):
                ids.add(sid)
                if raw_doc:
                    try:
                        doc = json.loads(raw_doc)
                        name = doc.get('name') or doc.get('display_name') or doc.get('title')
                        if name:
                            extra_names[sid] = str(name)
                    except (ValueError, TypeError):
                        pass
    except sqlite3.Error:
        pass
    ids.update(path.stem for path in (data / 'pa-v1-extensions').glob('PA-*.sqlite3'))
    for strategy, ledger in (('D', 'd/trading.sqlite3'), ('E', 'e/formal.sqlite3'), ('G', 'g/mirror.sqlite3')):
        if (data / ledger).is_file():
            ids.add(strategy)
    catalog = strategy_display_catalog(extra_names)
    return {sid: catalog.get(sid, sid) for sid in sorted(ids, key=strategy_sort_key)}


def _apply_persistent_user_proxy(env):
    """Use the Windows user's durable proxy when recovering the local API.

    Codex may inject a per-command proxy that is gone after the command exits.
    Reading HKCU Environment keeps GUI-triggered recovery independent of that
    temporary process environment.
    """
    if os.name != 'nt':
        return env
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            values = {}
            for name in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY'):
                try:
                    values[name] = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    pass
        proxy = values.get('HTTPS_PROXY') or values.get('HTTP_PROXY') or values.get('ALL_PROXY')
        if proxy:
            for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'WS_PROXY', 'WSS_PROXY',
                         'http_proxy', 'https_proxy', 'all_proxy', 'ws_proxy', 'wss_proxy'):
                env[name] = proxy
            env['NO_PROXY'] = 'localhost,127.0.0.1,::1'
            env['no_proxy'] = env['NO_PROXY']
    except (OSError, ImportError):
        pass
    return env


def rule_detail_target(row_id):
    """Keep detail actions bound to stable strategy IDs, never visual row indices."""
    if row_id not in RULE_DETAIL_IDS:
        raise ValueError('UNKNOWN_STRATEGY')
    return row_id


def detail_click_target(row_id, column):
    """Return the row's stable ID only for the detail action column."""
    return rule_detail_target(row_id) if column == '#14' else None


def _bar_offset(offset):
    try: offset = int(offset)
    except (ValueError, TypeError): return f'偏移{offset}根'
    return '最新已收盘K线' if offset == 0 else f'向前{offset}根K线'


def explain_v2_expr(expr, params=None):
    """Render every v2 AST node; unknown operators retain their full source JSON."""
    params = params or {}
    if not isinstance(expr, dict) or 'op' not in expr:
        return f'未识别表达式（原始值：{json.dumps(expr, ensure_ascii=False, sort_keys=True)}）'
    op, args = expr.get('op'), expr.get('args', [])
    if not isinstance(args, list):
        return f'未识别表达式（原始值：{json.dumps(expr, ensure_ascii=False, sort_keys=True)}）'
    if op in ('and', 'or'):
        joiner = ' 且 ' if op == 'and' else ' 或 '
        return '(' + joiner.join(explain_v2_expr(x, params) for x in args) + ')'
    if op == 'not' and len(args) == 1:
        return '非（' + explain_v2_expr(args[0], params) + '）'
    compare = {'gt': '>', 'gte': '≥', 'lt': '<', 'lte': '≤', 'eq': '='}
    if op in compare and len(args) == 2:
        return f'{explain_v2_expr(args[0], params)} {compare[op]} {explain_v2_expr(args[1], params)}'
    if op in ('crosses_above', 'crosses_below') and len(args) == 2:
        return f'{explain_v2_expr(args[0], params)} 向{("上" if op == "crosses_above" else "下")}穿越 {explain_v2_expr(args[1], params)}'
    if op == 'param' and len(args) == 1:
        name = str(args[0]); value = params.get(name, '参数值未提供')
        return f'参数 {name} = {value}'
    if op in ('open', 'high', 'low', 'close', 'volume') and len(args) == 1:
        label = {'open': '开盘价', 'high': '最高价', 'low': '最低价', 'close': '收盘价', 'volume': '成交量'}[op]
        return f'{label}（{_bar_offset(args[0])}）'
    if op in ('sma', 'ema', 'rsi', 'atr') and len(args) == (2 if op in ('sma', 'atr') else 1):
        period = args[0]
        offset = args[1] if len(args) > 1 else 0
        return f'{op.upper()}({period}, {_bar_offset(offset)})'
    if op in ('highest_high', 'lowest_low') and len(args) == 2:
        name = '区间最高价' if op == 'highest_high' else '区间最低价'
        return f'{name}（窗口{args[0]}根，结束偏移{args[1]}根）'
    if op == 'body_fraction' and len(args) == 1:
        return f'实体占K线全幅比例（{_bar_offset(args[0])}）'
    return f'未识别表达式（原始值：{json.dumps(expr, ensure_ascii=False, sort_keys=True)}）'


def rule_source_authority(strategy_id, runtime_sources, local_source):
    """A local file is authoritative only when backend runtime reports matching identity/hash."""
    runtime = (runtime_sources or {}).get(strategy_id) or {}
    if not runtime or not runtime.get('sha256') or not local_source:
        return {'confirmed': False, 'label': '当前规则详情来源未确认', 'runtime': runtime}
    same_version = not runtime.get('version') or runtime.get('version') == local_source.get('version')
    same_hash = runtime.get('sha256') == local_source.get('sha256')
    return {'confirmed': bool(same_version and same_hash),
            'label': '后端活动版本与本地规则文件哈希一致' if same_version and same_hash else '当前规则详情来源未确认（运行版本或哈希不匹配）',
            'runtime': runtime}


def cycle_label(value):
    if not value:
        return '—'
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return str(value)
        return parsed.astimezone().strftime('%Y-%m-%d %H:%M')
    except (ValueError, TypeError):
        return str(value)


def money_label(value):
    if value is None:
        return '—'
    try:
        amount = Decimal(str(value))
        return f'{amount:,.2f}' if amount.is_finite() else '—'
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


def pa_status_label(item):
    status = item.get('status')
    if status == 'ENABLED_NOT_RUNNING' and 'GAP' in str(item.get('last_status')):
        return '周期滞后'
    if status == 'RUNNING':
        return '运行中'
    if status == 'ENABLED_NOT_RUNNING':
        return '已启用待周期'
    if status in ('PAUSED', 'REGISTERED', 'REGISTERED_PAUSED'):
        return '已暂停' if item.get('paused_at') or status != 'REGISTERED' else '未启用'
    if status == 'FAULTED':
        return '故障'
    return str(status or '状态未知')


def cycle_health(processed, confirmed, *, now, blocked=False):
    if blocked:
        return '周期滞后'
    try:
        latest = datetime.fromisoformat(str(confirmed).replace('Z', '+00:00'))
        last = datetime.fromisoformat(str(processed).replace('Z', '+00:00')) if processed else None
        if latest.utcoffset() is None or (last and last.utcoffset() is None):
            return '等待数据'
        if last == latest:
            return '正常'
        if last and last > latest:
            return '等待数据'
        return '处理中' if now - latest <= timedelta(minutes=2) else '周期滞后'
    except (ValueError, TypeError):
        return '等待数据'


def console_rows(decisions, pa_items=(), pa_error=None, g_status=None, g_error=None, *, confirmed_cycle=None, now=None):
    rows = {}
    now = now or datetime.now(timezone.utc)
    control_names = {
        'RUNNING': '运行中', 'PAUSING': '未完成', 'PAUSED': '已暂停',
        'OBSERVE_ONLY': '观察中', 'PROTECTION_UNCERTAIN': '保护待确认',
        'D_OBSERVE_ONLY': '执行未接通', 'E_OBSERVE_ONLY': '观察中',
        'E_NOT_INITIALIZED': '账户未初始化',
    }
    for agent in ('A', 'B', 'C', 'D', 'E'):
        row = dict(decisions.get(agent) or {})
        row.update({
            'id': agent,
            'name': agent,
            'status_label': control_names.get(row.get('control'), '未知'),
            'control_label': control_names.get(row.get('control'), '未知'),
            'strategy_id': None,
        })
        cycle = row.get('cycle')
        if confirmed_cycle and row.get('control') == 'RUNNING':
            row['cycle_health'] = cycle_health(cycle, confirmed_cycle, now=now)
        elif row.get('control') in ('PAUSED','PAUSING','PROTECTION_UNCERTAIN'):
            row['cycle_health'] = '已暂停'
        row['cycle'] = cycle_label(cycle)
        rows[agent] = row

    items_by_id = {item.get('id'): item for item in pa_items if item.get('id')}
    for strategy_id, display_name in PA_STRATEGIES:
        item = items_by_id.get(strategy_id)
        if pa_error:
            row = {
                'id': strategy_id, 'strategy_id': strategy_id,
                'name': display_name, 'status_label': '读取失败',
                'control_label': '错误', 'control': 'UNKNOWN', 'state': '—',
                'amount': '—', 'leverage': '\u672a\u77e5', 'equity': None, 'realized': None,
                'unrealized': None, 'fees': None, 'action': f'策略池读取失败：{pa_error}',
                'cycle': '—', 'enabled': False, 'raw': None,
            }
        elif item is None:
            row = {
                'id': strategy_id, 'strategy_id': strategy_id,
                'name': display_name, 'status_label': '未登记',
                'control_label': '不可用', 'control': 'UNKNOWN', 'state': '—',
                'amount': '—', 'leverage': '\u672a\u77e5', 'equity': None, 'realized': None,
                'unrealized': None, 'fees': None, 'action': '策略池未返回该ID',
                'cycle': '—', 'enabled': False, 'raw': None,
            }
        else:
            label = pa_status_label(item)
            direction = item.get('position_direction') or 'UNKNOWN'
            position_labels = {
                'FLAT': '空仓', 'LONG': '做多', 'SHORT': '做空',
                'PENDING_LONG': '待开多', 'PENDING_SHORT': '待开空',
                'UNKNOWN': '状态未知',
            }
            enabled = bool(item.get('enabled'))
            row = {
                'id': strategy_id, 'strategy_id': strategy_id,
                'name': display_name, 'status_label': label,
                'control_label': '暂停' if enabled else '启动',
                'control': item.get('status'),
                'state': position_labels.get(direction, direction),
                'amount': money_label(item.get('position_amount') if direction not in ('FLAT', 'PENDING_LONG', 'PENDING_SHORT') else '0'),
                'leverage': leverage_label(item.get('leverage'), direction),
                'equity': money_label(item.get('equity')), 'realized': money_label(item.get('realized')),
                'unrealized': money_label(item.get('unrealized')), 'fees': money_label(item.get('fees')),
                'action': item.get('last_action') or item.get('action') or 'WAIT',
                'cycle': cycle_label(item.get('last_cycle')),
                'enabled': enabled, 'raw': item,
            }
            if enabled and confirmed_cycle:
                reason = str(item.get('last_status') or '')
                row['cycle_health'] = cycle_health(item.get('last_cycle'),confirmed_cycle,now=now,
                    blocked='GAP' in reason)
                if row['cycle_health'] == '正常' and reason.startswith('BLOCKED'):
                    row['cycle_health'] = '等待数据'
            elif not enabled and item.get('paused_at'):
                row['cycle_health'] = '已暂停'
        rows[strategy_id] = row
    if g_error:
        rows['G'] = {'id': 'G', 'strategy_id': 'G', 'name': 'C反向镜像', 'status_label': '异常',
                     'control_label': '只读', 'control': 'G_MIRROR', 'state': '—', 'amount': '—',
                     'leverage': '\u672a\u77e5', 'equity': None, 'realized': None, 'unrealized': None,
                     'fees': None, 'action': f'G状态读取失败：{g_error}', 'cycle': '—', 'enabled': True,
                     'raw': None, 'category': '镜像策略'}
    else:
        g_status = g_status or {}
        direction = g_status.get('position_direction')
        follow = g_status.get('follow_state') or g_status.get('status')
        g_err = g_status.get('last_error')
        benign_risk_exit = (
            g_err == 'MIRROR_RISK_EXIT'
            and follow == 'WAIT_FOR_C_NEXT_OPEN'
            and not g_status.get('position')
            and bool(g_status.get('enabled'))
        )
        label = (
            '异常' if (g_err and not benign_risk_exit)
            else '等待C开仓' if follow == 'WAIT_FOR_C_NEXT_OPEN'
            else '运行中' if follow in ('MIRRORING', 'PAIR_CLOSED')
            else '异常'
        )
        rows['G'] = {'id': 'G', 'strategy_id': 'G', 'name': 'C反向镜像', 'status_label': label,
                     'control_label': '只读', 'control': 'G_MIRROR',
                     'state': {'LONG': '做多', 'SHORT': '做空'}.get(direction, '空仓'),
                     'amount': money_label(g_status.get('position_amount') or '0'),
                     'leverage': leverage_label(g_status.get('leverage'), direction or 'FLAT'),
                     'equity': money_label(g_status.get('equity')), 'realized': money_label(g_status.get('realized')),
                     'unrealized': money_label(g_status.get('unrealized')), 'fees': money_label(g_status.get('fees')),
                     'action': g_status.get('last_action') or follow or 'WAIT', 'cycle': '—',
                     'cycle_health': '事件驱动', 'sync_at': cycle_label(g_status.get('last_cycle')),
                     'enabled': bool(g_status.get('enabled')), 'raw': g_status, 'category': '镜像策略'}
    ordered = ('A', 'B', 'C', 'D', 'E', 'G', *PA_STRATEGY_IDS)
    return {key: rows[key] for key in ordered if key in rows}


def strategy_view(row_id, row):
    """Separate the stable short ID, human name, strategy state, position and action."""
    if row_id in PA_STRATEGY_IDS:
        display_id, name = next((label.split(' ', 1) for sid, label in PA_STRATEGIES if sid == row_id))
    else:
        display_id, name = row_id, row.get('name') or row_id
    state = row.get('state')
    position = {
        'FLAT': '空仓', 'OPEN_LONG': '做多', 'OPEN_SHORT': '做空',
        'LONG': '做多', 'SHORT': '做空', 'PENDING_LONG': '待开多', 'PENDING_SHORT': '待开空',
    }.get(state, state or '—')
    return {
        'strategy_id': display_id, 'name': name,
        'status': row.get('status_label') or '状态未知',
        'position': position, 'action': action_label(row.get('action') or 'WAIT'),
    }


def console_control_state(row_id, row):
    if row_id == 'G':
        return 'disabled'
    if row_id in PA_STRATEGY_IDS:
        if row.get('status_label') in ('读取失败', '数据过期', '未登记'):
            return 'disabled'
        return 'on' if row.get('enabled') else 'off'
    if row_id == 'D':
        if 'enabled' not in row or row.get('control') == 'UNKNOWN':
            return 'disabled'
        return 'on' if row.get('enabled') else 'off'
    if row.get('control') in ('RUNNING', 'PAUSING'):
        return 'on'
    if row.get('control') == 'PAUSED':
        return 'off'
    return 'disabled'


def cycle_display(row):
    if row.get('cycle_health') == '事件驱动':
        return f"事件同步 {row['sync_at']}"
    when = row.get('cycle') or '—'
    return f"{row['cycle_health']} · {when}" if row.get('cycle_health') else when


STATUS_TONES = {
    'primary': ('#dbeafe', '#1d4ed8'),
    'success': ('#dcfce7', '#166534'),
    'warning': ('#ffedd5', '#9a3412'),
    'danger': ('#fee2e2', '#b91c1c'),
    'neutral': ('#f1f5f9', '#64748b'),
}


def system_status_badges(system, frontend_http, node):
    system = system or {}
    node_known = str(node or '').isdigit()
    http_ok = isinstance(frontend_http, int) and 200 <= frontend_http < 400
    scheduler = str(system.get('scheduler') or 'UNKNOWN')
    scheduler_label = {
        'ENABLED_INTERNAL_PAPER': '模拟交易正常', 'RUNNING': '调度运行中',
        'STOPPED': '调度已停止', 'FAULTED': '调度异常',
    }.get(scheduler, '调度状态未知' if scheduler in ('UNKNOWN', 'NONE') else '调度检查中')
    scheduler_tone = 'success' if scheduler in ('ENABLED_INTERNAL_PAPER', 'RUNNING') else 'danger' if scheduler in ('STOPPED', 'FAULTED') else 'neutral'

    def health_tone(value, good, warn=()):
        value = str(value or 'UNKNOWN').upper()
        if value in good: return 'success'
        if value in warn: return 'warning'
        if value in ('UNKNOWN', 'NONE', 'N/A', 'NOT_AVAILABLE'): return 'neutral'
        return 'danger'

    return [
        {'icon': 'node', 'label': f'节点 {node}' if node_known else '节点 未知', 'tone': 'primary' if node_known else 'neutral', 'raw': str(node or 'UNKNOWN')},
        {'icon': 'http', 'label': f'HTTP {frontend_http}', 'tone': 'success' if http_ok else 'danger' if frontend_http == '不可用' else 'neutral', 'raw': str(frontend_http)},
        {'icon': 'scheduler', 'label': scheduler_label, 'tone': scheduler_tone, 'raw': scheduler},
        {'icon': 'market', 'label': f"行情 {system.get('feed_health') or '未知'}", 'tone': health_tone(system.get('feed_health'), ('LIVE',)),'raw': str(system.get('feed_health') or 'UNKNOWN')},
        {'icon': 'quote', 'label': f"报价 {system.get('quote_health') or '未知'}", 'tone': health_tone(system.get('quote_health'), ('FRESH',), ('STALE',)),'raw': str(system.get('quote_health') or 'UNKNOWN')},
        {'icon': 'ws', 'label': f"WS {system.get('websocket_recovery_state') or '未知'}", 'tone': health_tone(system.get('websocket_recovery_state'), ('HEALTHY',), ('RECOVERING', 'CONNECTING')),'raw': str(system.get('websocket_recovery_state') or 'UNKNOWN')},
    ]


def operations_status_summary(system, health, pool_items, inventory, secondary_configured=False):
    """Read-only operational summary assembled from live API and backup inventory."""
    items = inventory.get('items', []) if isinstance(inventory, dict) else []
    verified = [x for x in items if x.get('backup_status') == 'VERIFIED']
    failed = [x for x in items if x.get('backup_status') == 'FAILED']
    pa = [x for x in pool_items if str(x.get('id', '')).startswith('pa-')]
    gap_blocked = [x.get('id') for x in pa if x.get('last_status') == 'BLOCKED_GAP_RECONCILIATION_REQUIRED']
    base_ready = ((system or {}).get('scheduler') == 'ENABLED_INTERNAL_PAPER'
                  and (system or {}).get('quote_health') == 'FRESH'
                  and (system or {}).get('websocket_recovery_state') == 'HEALTHY'
                  and (system or {}).get('startup_position_monitor_checked') is True
                  and bool(items) and len(verified) == len(items) and not failed)
    recovery_readiness = ('BLOCKED：缺口持仓待核对 ' + ', '.join(gap_blocked) if gap_blocked
                          else '具备基础恢复条件' if base_ready
                          else 'BLOCKED：调度、行情、持仓监控或备份门槛未全部满足')
    return {
        '8012版本': (health or {}).get('version', '未知'),
        '纸盘调度': (system or {}).get('scheduler', '未知'),
        '行情 / 报价 / WebSocket': ' / '.join(str((system or {}).get(k, '未知')) for k in ('feed_health', 'quote_health', 'websocket_recovery_state')),
        'ABC影子账本': ((system or {}).get('shadow_evolution') or {}).get('state', '未知'),
        '影子进化阈值': ' / '.join(f"{key}:{val.get('next_review_at', '未知')}" for key, val in (((system or {}).get('shadow_evolution') or {}).get('agents') or {}).items()) or '未知',
        'PA周期': '；'.join(f"{x.get('id')}={x.get('last_cycle') or '无'} ({x.get('status') or '未知'})" for x in pa) or '未读取',
        '最近备份': inventory.get('generated_at', '尚无备份记录') if isinstance(inventory, dict) else '尚无备份记录',
        '备份完整性': f"{len(verified)}项通过 / {len(failed)}项失败" if items else '尚无验证记录',
        '异地副本': '已配置' if secondary_configured else '未配置（仅本机备份）',
        '恢复条件': recovery_readiness,
        '止损监控': (system or {}).get('stop_monitor', '未知'),
        '安全提示': '有备份失败时禁止依赖该备份进行部署或重载。' if failed else '此摘要为只读状态，不替代持仓逐项核验。',
    }


def strategy_status_appearance(status):
    label = status or '状态未知'
    if label == '运行中': return {'icon': '▶', 'tone': 'success'}
    if label == '等待C开仓': return {'icon': '◷', 'tone': 'warning'}
    if label in ('已暂停', '未启用'): return {'icon': 'Ⅱ', 'tone': 'neutral'}
    if label in ('异常', '故障', '读取失败'): return {'icon': '!', 'tone': 'danger'}
    if label in ('已启用待周期', '等待数据', '保护待确认', '周期滞后'): return {'icon': '◷', 'tone': 'warning'}
    if label == '数据过期': return {'icon': '!', 'tone': 'neutral'}
    return {'icon': '·', 'tone': 'neutral'}


def draw_rounded_rect(canvas, x1, y1, x2, y2, radius, fill, outline=None, tag=None):
    radius = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    points = (x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
              x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
              x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1)
    return canvas.create_polygon(*points, smooth=True, splinesteps=20, fill=fill,
                                 outline=outline or fill, width=1, tags=(tag,) if tag else ())


def style_native_caption(window):
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = ctypes.windll.user32.GetAncestor(window.winfo_id(), 2) or window.winfo_id()
        caption = ctypes.c_uint32(0x005F3A1E)
        text_color = ctypes.c_uint32(0x00FFFFFF)
        border = ctypes.c_uint32(0x001E3A5F)
        dark_mode = ctypes.c_int(1)
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        for attribute in (19, 20):
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(dark_mode), ctypes.sizeof(dark_mode))
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text_color), ctypes.sizeof(text_color))
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
    except Exception:
        pass


def configure_detail_popup(window, owner):
    """Keep a detail window owned, visible, and alive for its full UI lifetime."""
    window.transient(owner)
    window.lift()
    window.focus_set()
    refs = getattr(owner, '_detail_popup_refs', None)
    if refs is None:
        refs = {}
        owner._detail_popup_refs = refs
    key = id(window)
    refs[key] = window

    def release(event):
        if getattr(event, 'widget', None) is window:
            refs.pop(key, None)

    window.bind('<Destroy>', release, add='+')


def filter_console_rows(rows, category='全部', status='全部状态', query=''):
    query = (query or '').strip().casefold()
    result = []
    for row_id, row in rows.items():
        is_pa = row_id in PA_STRATEGY_IDS
        is_original = row_id in ('A', 'B', 'C', 'D', 'E')
        if category == 'PA' and not is_pa or category == '原有' and not is_original or category == '镜像' and row_id != 'G':
            continue
        view = strategy_view(row_id, row)
        if status != '全部状态' and view['status'] != status:
            continue
        if query and query not in (row_id + ' ' + view['strategy_id'] + ' ' + view['name']).casefold():
            continue
        result.append(row_id)
    return result


def console_summary(rows):
    equity = Decimal('0')
    for row in rows.values():
        value = row.get('equity')
        try:
            amount = Decimal(str(value).replace(',', ''))
            if amount.is_finite():
                equity += amount
        except (InvalidOperation, TypeError, ValueError):
            pass
    running = sum(1 for row in rows.values() if row.get('status_label') == '运行中')
    open_positions = sum(1 for row in rows.values() if row.get('state') in ('做多', '做空', 'OPEN_LONG', 'OPEN_SHORT', 'LONG', 'SHORT'))
    return {'total': len(rows), 'running': running, 'positions': open_positions, 'equity': f'{equity:,.2f}'}


def keep_cached_rows(previous, current):
    """Retain the last valid PA account values when only the pool read fails."""
    merged = dict(current)
    for strategy_id in PA_STRATEGY_IDS:
        row = current.get(strategy_id) or {}
        if row.get('status_label') != '读取失败' or strategy_id not in previous:
            continue
        cached = dict(previous[strategy_id])
        cached.update({'status_label': '数据过期', 'control_label': '数据过期', 'enabled': False,
                       'action': row.get('action'), 'raw': None})
        merged[strategy_id] = cached
    return merged


def pa_trade_fills(strategy_id):
    if strategy_id not in PA_STRATEGY_IDS:
        raise ValueError('UNKNOWN_PA_STRATEGY')
    fills = api_json(f'/api/v1/strategy-pool/{strategy_id}/fills?limit=2000').get('items')
    if not isinstance(fills, list):
        raise RuntimeError('策略成交记录格式无效')
    return sorted(fills, key=lambda row: str(row.get('cycle') or row.get('created_at') or ''), reverse=True)


def set_pa_enabled(strategy_id, version, enabled):
    if strategy_id not in PA_STRATEGY_IDS or not version:
        raise ValueError('INVALID_PA_CONTROL')
    return api_json(f'/api/v1/strategy-pool/{strategy_id}/enable', method='POST',
                    payload={'version': version, 'enabled': bool(enabled)})


def set_d_enabled(enabled):
    return api_json('/api/v1/d/control', method='POST', payload={'enabled': bool(enabled)})


def position_label(state):
    return {'FLAT': '空仓', 'OPEN_LONG': '做多', 'OPEN_SHORT': '做空'}.get(state, state if state is not None else '—')


def action_label(action):
    if action is None or action == '':
        return '等待'
    raw = str(action).strip()
    code = raw.upper()
    labels = {
        'WAIT': '等待', 'IDLE': '等待', 'NO_SETUP': '等待', 'NO_PENDING_SIGNAL': '等待',
        'OTHER': '等待', 'OTHER_STATE': '等待',
        'CYCLE_ALREADY_SEEN': '等待', 'WAITING': '等待', 'READY': '等待',
        'HOLD': '持有', 'OPEN_LONG': '开多信号', 'OPEN_SHORT': '开空信号',
        'CLOSE_LONG': '平多决策', 'CLOSE_SHORT': '平空决策',
        'SETUP_DETECTED': '检测到形态', 'CONFLICT_SKIP': '信号冲突，跳过',
        'REJECTED': '未通过检查', 'PAPER_FILLED': '模拟成交', 'POSITION_MANAGED': '持仓管理',
        'PENDING': '处理中', 'EXECUTING': '执行中', 'CYCLE_ERROR': '周期异常',
        'OBSERVE_ONLY': '观察中', 'DATA_NOT_READY': '数据未就绪',
        'INSUFFICIENT_15M_HISTORY': '等待15分钟K线预热',
        'INSUFFICIENT_1H_HISTORY': '等待1小时K线预热',
        'EXECUTION_NOT_IMPLEMENTED': '模拟执行未接通',
        'E_NOT_INITIALIZED': '账户未初始化',
        'INSUFFICIENT_15M_HISTORY': '\u7b49\u5f8515\u5206\u949fK\u7ebf\u9884\u70ed',
        'INSUFFICIENT_1H_HISTORY': '\u7b49\u5f851\u5c0f\u65f6K\u7ebf\u9884\u70ed',
        'EXECUTION_NOT_IMPLEMENTED': '\u6a21\u62df\u6267\u884c\u672a\u63a5\u901a',
        'E_NOT_INITIALIZED': '\u8d26\u6237\u672a\u521d\u59cb\u5316',
        'SIGNAL_LONG': '待开多', 'SIGNAL_SHORT': '待开空',
        'CLOSE_STOP': '止损平仓', 'CLOSE_TARGET': '止盈平仓',
        'CLOSE_TIME': '到时平仓', 'CENSOR_POSITION_AT_GAP': '数据缺口，仓位已截断',
        'WAIT_FOR_C_NEXT_OPEN': '等待C下一笔开仓', 'PAIR_CLOSED': '同步平仓', 'MIRROR_OPEN': '反向镜像开仓',
        'REVERSE_MIRROR_OPEN': '反向镜像开仓', 'MIRROR_CLOSE': '同步平仓',
        'MIRROR_PARTIAL_CLOSE': '同步部分平仓', 'MIRROR_RISK_EXIT': '镜像风控紧急退出',
        'BLOCKED_GAP_RECONCILIATION_REQUIRED': '持仓周期缺口待对账',
        'BLOCKED_GAP_RECONCILED_AWAITING_NATURAL_CYCLE': '缺口已核对，等待周期',
        'BLOCKED:QUOTE_UNAVAILABLE_OR_STALE': '报价不可用或已过期',
        'D_RAW_QUOTE_INVALID': '保护：原始报价无效',
        'C_EVENT_REJECTED': '跟单被拒绝', 'WAIT_STOP_DIST': '等待止损距离条件',
        'WAIT_STOP_DISTANCE': '等待止损距离条件', 'WAIT_TRIGGER': '等待触发条件',
        'WAIT_ENTRY': '等待开仓条件', 'WAIT_NEXT_BAR': '等待下一根K线',
    }
    if code in labels:
        return labels[code]
    if code.startswith('WAIT_STOP_DIST'):
        return '等待止损距离条件'
    if code.startswith('WAIT_'):
        return '等待条件满足'
    if raw.isascii() and any(char.isalpha() for char in raw):
        if any(token in code for token in ('ERROR', 'FAILED', 'FAULT', 'INVALID')):
            return '需检查'
        return '需检查'
    return raw


def leverage_label(value, position_state=None):
    state = str(position_state or '').strip().upper()
    if state in ('FLAT', 'NONE', 'EMPTY'):
        return '\u7a7a\u4ed3'
    if state in ('PENDING', 'PENDING_LONG', 'PENDING_SHORT'):
        return '\u5f85\u5f00\u4ed3'
    if value is None or str(value).strip() in ('', '\u2014', '-', '\u672a\u77e5'):
        return '\u672a\u77e5'
    raw = str(value).strip().lower().removesuffix('x').strip()
    try:
        leverage = Decimal(raw)
        if not leverage.is_finite() or leverage <= 0:
            return '\u672a\u77e5'
        return f'{leverage:.2f}x'
    except (InvalidOperation, TypeError, ValueError):
        return '\u672a\u77e5'


def position_details(account):
    if account.get('state') == 'FLAT':
        return '0.00', leverage_label(None, 'FLAT')
    try:
        position = account.get('position_json') or account.get('position')
        if isinstance(position, str): position = json.loads(position)
        if not isinstance(position, dict): return '—', '—'
        quantity, price = Decimal(str(position['quantity'])), Decimal(str(position['entry_price']))
        if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price <= 0: return '—', '—'
        leverage = position.get('leverage') or account.get('leverage')
        return f'{quantity * price:.2f}', leverage_label(leverage, 'OPEN')
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return '—', '—'


def fetch(path, port=8012):
    with OPENER.open(f'http://127.0.0.1:{port}{path}', timeout=5) as response:
        return json.load(response) if port == 8012 else response.status


def trade_fills(agent, account='formal'):
    """Read individual fills; E's private A slot is exposed only through its E API."""
    if agent not in ('A', 'B', 'C', 'D', 'E') or account not in ('formal', 'shadow'):
        raise ValueError('INVALID_ACCOUNT')
    if agent == 'D':
        if account != 'formal':
            raise ValueError('D_SHADOW_NOT_AVAILABLE')
        data = fetch('/api/v1/d/status')
        if not isinstance(data.get('fills'), list):
            raise RuntimeError('D 成交明细不可用')
        fills = data['fills']
    elif agent == 'E':
        data = fetch('/api/v1/e/accounts').get(account, {})
        if data.get('status') != 'READY':
            raise RuntimeError('E ' + ('正式' if account == 'formal' else '影子') + '账户不可用')
        fills = data.get('fills', [])
    else:
        if account != 'formal':
            raise ValueError('ABC_SHADOW_NOT_AVAILABLE')
        fills = fetch('/api/v1/fills?agent=' + agent).get('items', [])
    return sorted(fills, key=lambda row: str(row.get('occurred_at') or ''), reverse=True)


def fill_values(fill):
    occurred = fill.get('occurred_at')
    try:
        parsed = datetime.fromisoformat(occurred.replace('Z', '+00:00'))
        occurred = parsed.astimezone().strftime('%Y-%m-%d %H:%M:%S') if parsed.tzinfo else occurred
    except (AttributeError, ValueError, TypeError):
        occurred = occurred or '—'
    reason = fill.get('rationale') or fill.get('reason') or ''
    effect = fill.get('position_effect') or fill.get('effect')
    if not effect:
        upper_reason = str(reason).upper()
        if 'ENTRY' in upper_reason or 'OPEN' in upper_reason:
            effect = 'OPEN'
        elif any(token in upper_reason for token in ('CLOSE', 'STOP', 'TARGET', 'TIME_EXIT', 'REVERSAL', 'RISK_EXIT')):
            effect = 'CLOSE'
    side = fill.get('side') or fill.get('direction')
    position = fill_position_direction(fill, effect, side, reason)
    direction = {'LONG': '多', 'SHORT': '空'}.get(position, position or '—')
    effect = {'OPEN': '开仓', 'CLOSE': '平仓'}.get(effect, effect or '—')
    return (occurred, effect, direction, fill.get('quantity') or '—', fill.get('price') or '—',
            fill.get('fee') or '—', fill.get('rationale') or '—', fill.get('trade_id') or '—')


DETAIL_FILL_COLUMNS = (
    'time', 'effect', 'side', 'position', 'quantity', 'price', 'fee', 'slippage',
    'pnl', 'reason', 'trade_id', 'linked_id', 'fill_id',
)


def fill_position_direction(fill, effect, side, reason):
    """Return the held position side, deriving it when a ledger has only buy/sell."""
    for value in (fill.get('position_direction'), fill.get('direction')):
        normalized = str(value or '').strip().upper()
        if normalized in ('LONG', '做多', '多') or normalized.endswith('_LONG'):
            return 'LONG'
        if normalized in ('SHORT', '做空', '空') or normalized.endswith('_SHORT'):
            return 'SHORT'

    effect_text = str(effect or '').strip().upper()
    if 'LONG' in effect_text:
        return 'LONG'
    if 'SHORT' in effect_text:
        return 'SHORT'

    side_text = str(side or '').strip().upper()
    if side_text in ('LONG', '做多', '多'):
        return 'LONG'
    if side_text in ('SHORT', '做空', '空'):
        return 'SHORT'

    reason_text = str(reason or '').strip().upper()
    action_text = effect_text or reason_text
    if 'OPEN' in action_text or 'ENTRY' in action_text:
        action = 'OPEN'
    elif any(token in action_text for token in ('CLOSE', 'STOP', 'TARGET', 'TIME_EXIT', 'REVERSAL', 'RISK_EXIT')):
        action = 'CLOSE'
    else:
        return None

    if side_text == 'BUY':
        return 'LONG' if action == 'OPEN' else 'SHORT'
    if side_text == 'SELL':
        return 'SHORT' if action == 'OPEN' else 'LONG'
    return None


def detail_fill_values(fill):
    """Normalize ABCDE, PA and G fills to one detail-table contract."""
    reason = fill.get('rationale') or fill.get('reason') or '—'
    effect = fill.get('position_effect') or fill.get('effect')
    if not effect:
        reason_upper = str(reason).upper()
        if 'ENTRY' in reason_upper:
            effect = 'OPEN'
        elif any(token in reason_upper for token in ('CLOSE', 'STOP', 'TARGET', 'TIME_EXIT', 'REVERSAL')):
            effect = 'CLOSE'
    effect_label = {'OPEN': '开仓', 'CLOSE': '平仓', 'PARTIAL_CLOSE': '部分平仓',
                    'MIRROR_RISK_EXIT': '风控退出'}.get(effect, effect or '—')
    side = fill.get('side') or fill.get('direction')
    side_label = {'BUY': '买入', 'SELL': '卖出', 'LONG': '多', 'SHORT': '空'}.get(side, side or '—')
    direction = fill_position_direction(fill, effect, side, reason)
    direction_label = {'LONG': '做多', 'SHORT': '做空'}.get(direction, direction or '—')
    event_at = fill.get('occurred_at') or fill.get('cycle') or fill.get('created_at')
    trade_id = fill.get('g_trade_id') or fill.get('trade_id') or '—'
    linked_id = fill.get('c_trade_id') or fill.get('source_fill_id') or '—'
    fill_id = fill.get('fill_id') or fill.get('idempotency_key') or fill.get('decision_id') or '—'
    return (
        cycle_label(event_at), effect_label, side_label, direction_label,
        fill.get('quantity') or fill.get('qty') or '—', fill.get('price') or '—',
        fill.get('fee') or '—', fill.get('slippage') or '—', fill.get('pnl') or '—',
        reason, trade_id, linked_id, fill_id,
    )


def _calendar_timestamp(fill):
    # PA `cycle` is the signal candle; `created_at` is the actual paper fill time.
    value = fill.get('occurred_at') or fill.get('created_at') or fill.get('cycle')
    if not value:
        raise ValueError('成交时间缺失')
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('成交时间缺少时区，无法可靠归属北京时间日期')
    return parsed.astimezone(BEIJING)


def _calendar_effect(fill):
    value = str(fill.get('position_effect') or fill.get('effect') or '').upper()
    if value in ('OPEN', 'ENTRY', 'OPEN_LONG', 'OPEN_SHORT'):
        return 'OPEN'
    if value in ('CLOSE', 'PARTIAL_CLOSE', 'EXIT', 'MIRROR_RISK_EXIT'):
        return 'CLOSE'
    reason = str(fill.get('reason') or fill.get('rationale') or '').upper()
    if any(token in reason for token in ('ENTRY', 'OPEN')):
        return 'OPEN'
    if any(token in reason for token in ('CLOSE', 'STOP', 'TARGET', 'TIME_EXIT', 'REVERSAL', 'RISK_EXIT', 'EXIT')):
        return 'CLOSE'
    return None


def _calendar_direction(fill, effect):
    for field in ('position_direction', 'direction'):
        value = str(fill.get(field) or '').upper()
        if value in ('LONG', 'SHORT'):
            return value
    side = str(fill.get('side') or '').upper()
    if side in ('BUY', 'SELL') and effect:
        if effect == 'OPEN': return 'LONG' if side == 'BUY' else 'SHORT'
        return 'SHORT' if side == 'BUY' else 'LONG'
    # D's confirmed paper ledger exposes only OPEN_LONG account states and no side field.
    if fill.get('effect') in ('OPEN', 'CLOSE') and fill.get('trade_id'):
        return 'LONG'
    return None


def calculate_pnl_calendar(fills, strategy_id, year, month):
    """Calculate realized PnL on Beijing close dates, without re-deducting slippage."""
    events, incomplete, trading_dates, transaction_fills = [], [], set(), {}
    for index, fill in enumerate(fills):
        effect = _calendar_effect(fill)
        if not effect:
            incomplete.append(f"第{index + 1}笔成交无法判定开平仓")
            continue
        try:
            at = _calendar_timestamp(fill)
            qty = Decimal(str(fill.get('quantity') or fill.get('qty')))
            fee = Decimal(str(fill.get('fee')))
            price = Decimal(str(fill.get('price')))
            if not all(x.is_finite() for x in (qty, fee, price)) or qty <= 0 or fee < 0 or price <= 0:
                raise ValueError('数量、价格或手续费无效')
            direction = _calendar_direction(fill, effect)
            if not direction: raise ValueError('持仓方向无法确认')
        except (ValueError, TypeError, InvalidOperation) as exc:
            incomplete.append(f"第{index + 1}笔成交：{exc}")
            continue
        trading_dates.add(at.date())
        transaction_fills.setdefault(at.date().isoformat(), []).append(fill)
        events.append({'source': fill, 'at': at, 'date': at.date(), 'effect': effect,
                       'qty': qty, 'fee': fee, 'price': price, 'direction': direction})
    events.sort(key=lambda event: event['at'])

    groups, active_pa = {}, None
    for idx, event in enumerate(events):
        fill = event['source']
        trade_id = fill.get('g_trade_id') or fill.get('trade_id')
        if strategy_id in PA_STRATEGY_IDS:
            if event['effect'] == 'OPEN':
                if active_pa is not None and groups[active_pa]['remaining'] > 0:
                    incomplete.append(f"{event['at'].isoformat()}: 前一笔PA持仓没有平仓成交，按缺失/删失处理后从新开仓重新配对")
                active_pa = f'pa-trade-{idx}'
                trade_id = active_pa
            elif not trade_id:
                if active_pa and groups.get(active_pa, {}).get('remaining', Decimal(0)) > 0:
                    trade_id = active_pa
                else:
                    incomplete.append(f"{event['at'].isoformat()}: PA退出成交没有可配对开仓")
                    continue
        elif not trade_id:
            incomplete.append(f"{event['at'].isoformat()}: 成交缺少交易编号")
            continue
        event['trade_key'] = str(trade_id)
        group = groups.setdefault(str(trade_id), {'entries': [], 'remaining': Decimal(0),
                                                   'closed_qty': Decimal(0), 'results': [], 'fills': []})
        group['fills'].append(event)
        if event['effect'] == 'OPEN':
            group['entries'].append({'event': event, 'qty_left': event['qty'], 'fee_left': event['fee']})
            group['remaining'] += event['qty']
        else:
            remaining_close = event['qty']; gross = Decimal(0); entry_fee = Decimal(0); allocations = []
            for entry in group['entries']:
                if remaining_close <= 0: break
                if entry['qty_left'] <= 0: continue
                take = min(remaining_close, entry['qty_left'])
                entry_event = entry['event']
                if entry_event['direction'] != event['direction']:
                    incomplete.append(f"{event['at'].isoformat()}: 开平仓方向不一致")
                    remaining_close = event['qty']
                    allocations = []
                    break
                sign = Decimal(1) if event['direction'] == 'LONG' else Decimal(-1)
                gross += (event['price'] - entry_event['price']) * take * sign
                fee_part = entry['fee_left'] * take / entry['qty_left']
                entry_fee += fee_part
                allocations.append((entry_event, take, fee_part))
                entry['fee_left'] -= fee_part; entry['qty_left'] -= take
                remaining_close -= take
            if remaining_close > Decimal('0.0000000001'):
                incomplete.append(f"{event['at'].isoformat()}: 平仓数量超过可配对开仓数量")
                continue
            explicit_pnl = fill.get('net_pnl')
            if explicit_pnl is None and strategy_id == 'G': explicit_pnl = fill.get('pnl')
            try:
                if explicit_pnl is not None:
                    net = Decimal(str(explicit_pnl))
                else:
                    realized_gross = gross
                    if strategy_id not in PA_STRATEGY_IDS:
                        realized_gross = gross.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    net = realized_gross - entry_fee - event['fee']
                if not net.is_finite(): raise InvalidOperation()
            except (InvalidOperation, ValueError):
                incomplete.append(f"{event['at'].isoformat()}: 平仓净盈亏无效")
                continue
            group['remaining'] -= event['qty']
            group['closed_qty'] += event['qty']
            group['results'].append(net)
            group['last_close_date'] = event['date']
            event['net'] = net
            event['fees_assigned'] = event['fee'] + entry_fee
            event['entry_events'] = [allocation[0] for allocation in allocations]
    daily = {}
    for event in events:
        if event['effect'] != 'CLOSE' or 'net' not in event or event['date'].year != year or event['date'].month != month:
            continue
        key = event['date'].isoformat()
        item = daily.setdefault(key, {'net': Decimal(0), 'fees': Decimal(0), 'close_events': [], 'fill_events': []})
        item['net'] += event['net']; item['fees'] += event.get('fees_assigned', event['fee'])
        item['close_events'].append(event)
        item['fill_events'].append(event['source'])
        for entry_event in event.get('entry_events', []):
            if entry_event['source'] not in item['fill_events']:
                item['fill_events'].append(entry_event['source'])
    complete_groups = [group for group in groups.values() if group['remaining'] == 0 and group['closed_qty'] > 0]
    all_trade_results = [sum(group['results'], Decimal(0)) for group in complete_groups]
    month_results = [sum(group['results'], Decimal(0)) for group in complete_groups
                     if group.get('last_close_date') and group['last_close_date'].year == year
                     and group['last_close_date'].month == month]
    for group in complete_groups:
        result = sum(group['results'], Decimal(0))
        final_date = group.get('last_close_date')
        if final_date and final_date.year == year and final_date.month == month:
            daily.setdefault(final_date.isoformat(), {'net': Decimal(0), 'fees': Decimal(0), 'close_events': [], 'fill_events': []})
            daily[final_date.isoformat()].update({'closed_count': daily[final_date.isoformat()].get('closed_count', 0) + 1,
                                                  'wins': daily[final_date.isoformat()].get('wins', 0) + int(result > 0),
                                                  'losses': daily[final_date.isoformat()].get('losses', 0) + int(result < 0)})
    month_losses = [item['net'] for item in daily.values() if item['net'] < 0]
    open_group_count = sum(1 for group in groups.values() if group['remaining'] > Decimal('0.0000000001'))
    selected_daily = {key: value for key, value in daily.items()}
    month_net = sum((item['net'] for item in selected_daily.values()), Decimal(0))
    return {'daily': selected_daily, 'monthly_net': month_net, 'closed_trade_results': month_results,
            'all_closed_trade_results': all_trade_results,
            'win_rate': (sum(result > 0 for result in month_results) / len(month_results)) if month_results else None,
            'trading_days': sum(1 for day in trading_dates if day.year == year and day.month == month),
            'max_daily_loss': min(month_losses) if month_losses else Decimal(0), 'incomplete': incomplete,
            'open_group_count': open_group_count, 'trade_events': events, 'transaction_fills': transaction_fills}


def _detail_position(account, row, strategy_id):
    raw = row.get('raw') or {}
    position = (account.get('position') or raw.get('position'))
    encoded = account.get('position_json')
    if isinstance(encoded, str):
        try: position = json.loads(encoded)
        except (ValueError, TypeError): position = None
    if isinstance(position, str):
        try: position = json.loads(position)
        except (ValueError, TypeError): position = None
    return position if isinstance(position, dict) else None


def _detail_number(value, digits=2, grouping=True):
    try:
        number = Decimal(str(value).replace(',', ''))
        if not number.is_finite(): return '—'
        return f'{number:,.{digits}f}' if grouping else f'{number:.{digits}f}'
    except (InvalidOperation, TypeError, ValueError):
        return '—'


def account_detail_metrics(strategy_id, row, account, fills):
    position = _detail_position(account, row, strategy_id)
    qty_value = (position or {}).get('quantity') or (position or {}).get('qty')
    entry_value = (position or {}).get('entry_price') or (position or {}).get('entry')
    try: notional = Decimal(str(qty_value)) * Decimal(str(entry_value)) if qty_value is not None and entry_value is not None else Decimal(0)
    except (InvalidOperation, TypeError, ValueError): notional = None
    realized = row.get('realized') if row.get('realized') is not None else account.get('realized_pnl')
    unrealized = row.get('unrealized') if row.get('unrealized') is not None else account.get('unrealized_pnl')
    equity = row.get('equity') if row.get('equity') is not None else account.get('equity')
    fee_sum = Decimal(0)
    for fill in fills:
        try: fee_sum += Decimal(str(fill.get('fee') or 0))
        except (InvalidOperation, TypeError, ValueError): pass
    fee_value = row.get('fees') if row.get('fees') not in (None, '—') else fee_sum
    result = calculate_pnl_calendar(fills, strategy_id, datetime.now(BEIJING).year, datetime.now(BEIJING).month)
    if realized is None:
        realized = sum(result['all_closed_trade_results'], Decimal(0))
    data_warnings = list(dict.fromkeys(result['incomplete']))
    if strategy_id in PA_STRATEGY_IDS and position is None and result['open_group_count']:
        data_warnings.append(f"当前账户为空仓，但有{result['open_group_count']}笔未配对开仓记录；可能存在缺口删失/账本缺行，不计入日历收益。")
    direction = (position or {}).get('direction')
    if not direction:
        state = str(row.get('state') or account.get('state') or '')
        direction = 'LONG' if state in ('OPEN_LONG', '做多', 'LONG') else 'SHORT' if state in ('OPEN_SHORT', '做空', 'SHORT') else None
    bars_held = (position or {}).get('bars_held')
    max_bars = (position or {}).get('max_hold_bars')
    return {
        'position': position, 'equity': _detail_number(equity), 'realized': _detail_number(realized),
        'unrealized': _detail_number(unrealized), 'fees': _detail_number(fee_value),
        'notional': _detail_number(notional) if notional is not None else '—',
        'closed_count': len(result['all_closed_trade_results']), 'direction': direction,
        'quantity': _detail_number(qty_value, 8, False), 'entry': _detail_number(entry_value, 2),
        'stop': _detail_number((position or {}).get('stop_price') or (position or {}).get('stop'), 2),
        'target': _detail_number((position or {}).get('target_price') or (position or {}).get('target'), 2),
        'bars': f'{bars_held}/{max_bars} 根' if bars_held is not None else '—',
        'leverage': leverage_label(row.get('leverage') or (position or {}).get('leverage'), direction),
        'cycle': row.get('cycle') or row.get('last_cycle') or '—',
        'action': row.get('action') or row.get('last_action') or '—',
        'error': row.get('last_error') or row.get('error') or account.get('last_error') or '无',
        'data_warnings': data_warnings,
    }


def _detail_position_text(position):
    if not isinstance(position, dict):
        return '空仓'
    if position.get('pending_entry'):
        return f"待入场 · {'做多' if position.get('direction') == 'LONG' else '做空'} · 等待下一根开盘报价"
    direction = {'LONG': '做多', 'SHORT': '做空'}.get(position.get('direction'), '方向未知')
    parts = [direction]
    qty = position.get('quantity') or position.get('qty')
    if qty is not None: parts.append(f'{qty} BTC')
    entry = position.get('entry_price') or position.get('entry')
    if entry is not None: parts.append(f'入场 {entry}')
    stop = position.get('stop_price') or position.get('stop')
    if stop is not None: parts.append(f'止损 {stop}')
    target = position.get('target_price') or position.get('target')
    if target is not None: parts.append(f'止盈 {target}')
    if position.get('leverage') is not None: parts.append(f"杠杆 {position['leverage']}x")
    if position.get('bars_held') is not None:
        parts.append(f"持仓 {position['bars_held']}/{position.get('max_hold_bars', '—')} 根")
    return ' · '.join(parts)


def detail_summary_text(strategy_id, row, account=None, account_kind='formal'):
    """Build a common, truthful account summary with strategy-specific context."""
    raw = row.get('raw') or {}
    account = account or {}
    if isinstance(account.get('position_json'), str):
        try: account_position = json.loads(account['position_json'])
        except (ValueError, TypeError): account_position = None
    else:
        account_position = account.get('position')
    if strategy_id == 'G':
        g_position = raw.get('position') or {}
        c_position = raw.get('c_position') or {}
        position_text = f"C：{_detail_position_text(c_position)}\nG：{_detail_position_text(g_position)}"
        pair = raw.get('latest_pair') or {}
        c_fill, g_fill = raw.get('latest_c_fill') or {}, raw.get('latest_g_fill') or {}
        extra = (f"镜像来源：C · 跟随状态：{raw.get('follow_state', '—')} · 配对状态：{pair.get('status', '—')}\n"
                 f"配对交易：C {pair.get('c_trade_id', '—')} / G {pair.get('g_trade_id', '—')} · 数量 {pair.get('quantity_opened', '—')} BTC\n"
                 f"最近C/G成交：{cycle_label(c_fill.get('occurred_at'))} / {cycle_label(g_fill.get('occurred_at'))} · 延迟 {raw.get('last_fill_delay_ms', '—')} ms · 跟单错误：{raw.get('last_error') or '无'}")
    elif strategy_id in PA_STRATEGY_IDS:
        position = raw.get('position')
        if isinstance(position, str):
            try: position = json.loads(position)
            except (ValueError, TypeError): position = None
        position_text = _detail_position_text(position)
        extra = (f"策略版本：{raw.get('active_version') or raw.get('position_version') or '—'} · 启用：{'是' if row.get('enabled') else '否'} · "
                 f"最近周期：{row.get('cycle', '—')} · 最近决策/风控原因：{row.get('action', '—')}\n"
                 f"错误/拒绝原因：{raw.get('last_error') or raw.get('error') or raw.get('last_rejection') or '无'}")
    else:
        position_text = _detail_position_text(account_position)
        if account.get('state') == 'FLAT': position_text = '空仓'
        extra = (f"账户状态：{account.get('state') or row.get('state') or '—'} · 账户现金：{money_label(account.get('cash'))} · "
                 f"可用资金：{money_label(account.get('available_cash'))} · 占用保证金：{money_label(account.get('used_margin'))}\n"
                 f"最近周期：{row.get('cycle', '—')} · 最近决策/风控原因：{row.get('action', '—')}")
        if strategy_id == 'E': extra = f'账户类别：{account_kind} · ' + extra
    version = (raw.get('active_version') or raw.get('position_version') or raw.get('version') or row.get('version') or '—')
    enabled_value = row.get('enabled', raw.get('enabled'))
    enabled_label = '未知' if enabled_value is None else '已启用' if enabled_value else '已暂停'
    identity = (f"策略：{strategy_id} · {row.get('name') or strategy_id} · 版本：{version} · "
                f"状态：{row.get('status_label') or '—'} · 开关：{enabled_label}")
    metrics = (f"权益：{row.get('equity', '—')} · 已实现：{row.get('realized', '—')} · 未实现：{row.get('unrealized', '—')} · "
               f"累计手续费：{row.get('fees', '—')} · 持仓金额：{row.get('amount', '—')} · 杠杆：{row.get('leverage', '—')}")
    return f'{identity}\n{metrics}\n当前持仓：{position_text}\n{extra}'


def cumulative_fees(fills):
    """Sum recorded formal-account fill fees without floating point rounding."""
    if not isinstance(fills, list):
        return None
    try:
        amounts = [Decimal(str(fill['fee'])) for fill in fills]
        if any(not value.is_finite() or value < 0 for value in amounts):
            return None
        return f"{sum(amounts, Decimal('0')):.2f}"
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


def d_control_state(status):
    if status.get('enabled') is False or status.get('status') == 'PAUSED':
        return 'PAUSED'
    if status.get('status') == 'AUTO_PAPER':
        return 'RUNNING' if status.get('execution_ready') is True else 'PROTECTION_UNCERTAIN'
    if status.get('status') == 'PROTECTION_UNCERTAIN':
        return 'PROTECTION_UNCERTAIN'
    if status.get('status') == 'OBSERVE_ONLY':
        return 'OBSERVE_ONLY'
    return 'UNKNOWN'


def e_control_state(formal, runtime, configured_control):
    if not isinstance(formal, dict) or formal.get('status') != 'READY':
        return 'E_NOT_INITIALIZED'
    if not isinstance(runtime, dict) or runtime.get('mode') != 'AUTO_PAPER':
        return 'E_OBSERVE_ONLY'
    return configured_control if configured_control in ('RUNNING', 'PAUSED', 'PAUSING') else 'UNKNOWN'


def snapshot():
    system = fetch('/api/v1/system/status')
    controls = fetch('/api/v1/strategy-control')
    stats = fetch('/api/v1/statistics/abc')['agents']
    accounts = {item['agent']: item['account'] for item in fetch('/api/v1/agents')}
    e = fetch('/api/v1/e/accounts')
    all_fills = fetch('/api/v1/fills').get('items')
    decisions = {}
    for agent in 'ABC':
        d = stats[agent]
        decisions[agent] = {'state': d.get('state'), 'equity': d.get('equity'), 'realized': d.get('realized_net_pnl'),
                            'unrealized': d.get('unrealized_pnl'), 'action': (d.get('last_decision') or {}).get('action'),
                            'cycle': (d.get('last_decision') or {}).get('candle_close')}
        decisions[agent]['amount'], decisions[agent]['leverage'] = position_details(accounts[agent])
        agent_fills = [fill for fill in all_fills if fill.get('agent') == agent] if isinstance(all_fills, list) else None
        decisions[agent]['fees'] = cumulative_fees(agent_fills)
    formal = e['formal']
    cycles = []
    dbpath = ROOT / 'data/e/formal.sqlite3'
    if dbpath.exists():
        db = sqlite3.connect(dbpath.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            if db.execute("select count(*) from sqlite_master where type='table' and name='e_quant_cycles'").fetchone()[0]:
                cycles = db.execute('select close_at,status from e_quant_cycles order by close_at desc limit 1').fetchall()
        finally:
            db.close()
    e_status = formal.get('status') if isinstance(formal, dict) else None
    e_runtime = e.get('runtime') if isinstance(e, dict) else None
    e_observation_cycle = None
    observation_db = ROOT / 'data/e-observations.sqlite3'
    if observation_db.exists():
        try:
            with sqlite3.connect(observation_db.resolve().as_uri() + '?mode=ro', uri=True, timeout=3) as db:
                if db.execute("select count(*) from sqlite_master where type='table' and name='e_observations'").fetchone()[0]:
                    e_observation_cycle = db.execute('select close_at,status from e_observations order by close_at desc limit 1').fetchone()
        except sqlite3.Error:
            e_observation_cycle = None
    if e_status != 'READY':
        e_action = 'E_NOT_INITIALIZED'
    elif cycles:
        e_action = cycles[0][1]
    elif e_observation_cycle:
        e_action = e_observation_cycle[1]
    else:
        e_action = 'WAIT_NEXT_BAR'
    decisions['E'] = {'state': (formal.get('account') or {}).get('state') if isinstance(formal, dict) else None,
                      'equity': formal.get('equity') if isinstance(formal, dict) else None,
                      'realized': formal.get('realized_pnl') if isinstance(formal, dict) else None,
                      'unrealized': formal.get('unrealized_pnl') if isinstance(formal, dict) else None,
                      'action': e_action,
                      'cycle': cycles[0][0] if cycles else e_observation_cycle[0] if e_observation_cycle else None}
    decisions['E']['amount'], decisions['E']['leverage'] = position_details(formal.get('account') or {})
    decisions['E']['fees'] = cumulative_fees(formal.get('fills')) if formal.get('status') == 'READY' else None
    for agent, row in decisions.items():
        configured_control = controls.get(agent, {}).get('state', 'UNKNOWN')
        row['control'] = e_control_state(formal, e_runtime, configured_control) if agent == 'E' else configured_control
    try:
        d_status = fetch('/api/v1/d/status')
        d_account = d_status.get('account') or {}
        d_decision = d_status.get('decision') or {}
        d_reason = d_decision.get('reason') or d_status.get('reason')
        d_action = d_reason if d_decision.get('action') == 'DATA_NOT_READY' else (d_decision.get('action') or d_reason or 'WAIT')
        d_control = d_control_state(d_status)
        if d_control == 'PROTECTION_UNCERTAIN' and d_status.get('reason'):
            d_action = d_status['reason']
        if d_status.get('status') == 'OBSERVE_ONLY' and d_status.get('execution_ready') is not True:
            d_control = 'D_OBSERVE_ONLY'
        decisions['D'] = {
            'state': d_account.get('state'), 'equity': d_status.get('equity'),
            'realized': d_status.get('realized_pnl'), 'unrealized': d_status.get('unrealized_pnl'),
            'action': d_action,
            'cycle': d_decision.get('candle_close'), 'control': d_control,
            'enabled': d_status.get('enabled') is True,
            'fees': cumulative_fees(d_status.get('fills')) if d_status.get('account') is not None else None,
        }
        decisions['D']['amount'], decisions['D']['leverage'] = position_details(d_account)
    except Exception:
        decisions['D'] = {'state': None, 'equity': None, 'realized': None, 'unrealized': None,
                          'action': '接口不可用', 'cycle': None, 'control': 'UNKNOWN',
                          'amount': None, 'leverage': None, 'fees': None}
    pa_items = []
    pa_error = None
    try:
        response = fetch('/api/v1/strategy-pool')
        pa_items = response.get('items')
        if not isinstance(pa_items, list):
            raise RuntimeError('策略池响应缺少items列表')
    except Exception as exc:
        pa_error = str(exc)
    try:
        g_status = fetch('/api/v1/g-mirror/status')
        g_error = None
    except Exception as exc:
        g_status, g_error = None, str(exc)
    return system, console_rows(decisions, pa_items, pa_error, g_status, g_error,
                                confirmed_cycle=system.get('last_candle_at'))


def _tracked_pid(name):
    path = ROOT / '.run' / f'{name}.pid'
    try:
        return int(path.read_text(encoding='ascii').strip())
    except (OSError, ValueError):
        return None


def _tracked_process_info(name):
    pid = _tracked_pid(name)
    pid_path = ROOT / '.run' / f'{name}.pid'
    if pid is None or not pid_path.exists():
        return None
    command = (
        f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
        "if($p){[pscustomobject]@{pid=$p.Id;name=$p.ProcessName;started=$p.StartTime.ToUniversalTime().ToString('o')}|ConvertTo-Json -Compress}"
    )
    try:
        raw = subprocess.check_output(
            ['powershell.exe', '-NoProfile', '-Command', command],
            creationflags=NO_WINDOW, timeout=10, stderr=subprocess.DEVNULL,
        ).decode('utf-8-sig').strip()
        if not raw:
            return None
        info = json.loads(raw)
        started = datetime.fromisoformat(info['started'].replace('Z', '+00:00')).timestamp()
        if int(info['pid']) != pid or abs(pid_path.stat().st_mtime - started) > 180:
            return None
        info['pid'] = pid
        return info
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def listeners():
    active = []
    probes = (
        ('backend', 8012, lambda: fetch('/api/v1/health').get('status') == 'ok'),
        ('frontend', 5175, lambda: fetch('/', 5175) == 200),
    )
    for name, port, probe in probes:
        try:
            if not probe():
                continue
        except Exception:
            continue
        pid = _tracked_pid(name)
        active.append({'port': port, 'pid': pid,
                       'exe': 'python' if name == 'backend' else 'node', 'cmd': name})
    return active


def _clear_benign_g_risk_exit_error(g_db_path=None):
    """Clear only benign MIRROR_RISK_EXIT residue when G is FLAT, enabled, and WAIT_FOR_C_NEXT_OPEN."""
    db_path = Path(g_db_path) if g_db_path is not None else ROOT / 'data' / 'g' / 'mirror.sqlite3'
    if not db_path.is_file():
        return False
    try:
        with sqlite3.connect(str(db_path), timeout=5) as db:
            row = db.execute(
                "SELECT enabled, position_json, follow_state, error FROM g_account WHERE singleton=1"
            ).fetchone()
            if not row:
                return False
            enabled, pos_json, follow_state, err = row
            if enabled == 1 and not pos_json and follow_state == 'WAIT_FOR_C_NEXT_OPEN' and err == 'MIRROR_RISK_EXIT':
                db.execute(
                    "UPDATE g_account SET error=NULL WHERE singleton=1 AND enabled=1 AND position_json IS NULL "
                    "AND follow_state='WAIT_FOR_C_NEXT_OPEN' AND error='MIRROR_RISK_EXIT'"
                )
                db.commit()
                return True
    except sqlite3.Error:
        return False
    return False


def summarize_and_recover_strategies(system, rows, pool_items=(), *, g_db_path=None):
    """Inspect all 16 strategies and recover only safe, empty-position benign states."""
    healthy_count = 0
    recovered = []
    protected = []
    paused = []

    # 1. Recover benign G MIRROR_RISK_EXIT residue if present
    g_row = rows.get('G') or {}
    g_raw = g_row.get('raw') or {}
    if (
        g_raw.get('last_error') == 'MIRROR_RISK_EXIT'
        and g_raw.get('follow_state') == 'WAIT_FOR_C_NEXT_OPEN'
        and not g_raw.get('position')
        and bool(g_raw.get('enabled'))
    ):
        if _clear_benign_g_risk_exit_error(g_db_path):
            recovered.append('G · C反向镜像（已清除历史镜像风控退出错误标记，恢复等待C开仓）')

    # 2. Classify A..E, G, PA-01..PA-08 from console_rows
    seen_ids = set()
    for sid, row in rows.items():
        seen_ids.add(sid)
        if sid == 'G' and any(item.startswith('G ·') for item in recovered):
            continue
        st = row.get('status_label') or '未知'
        if st in ('运行中', '等待C开仓', '已启用待周期', '观察中'):
            healthy_count += 1
        elif st in ('已暂停', '未启用'):
            paused.append(strategy_display_name(sid))
        else:
            reason = row.get('action') or st
            protected.append(f'{strategy_display_name(sid)}（{st}：{reason}）')

    # 3. Also include PA-09 / PA-10 from strategy-pool if present
    for item in pool_items or ():
        sid = item.get('id')
        if not sid or sid in seen_ids:
            continue
        seen_ids.add(sid)
        st = pa_status_label(item)
        if st in ('运行中', '已启用待周期'):
            healthy_count += 1
        elif st in ('已暂停', '未启用'):
            paused.append(strategy_display_name(sid))
        else:
            protected.append(f"{strategy_display_name(sid)}（{st}：{item.get('last_status') or '待核查'}）")

    total = healthy_count + len(recovered) + len(protected) + len(paused)
    summary = (
        f'检查 {total} 套策略，正常 {healthy_count} 套，成功恢复 {len(recovered)} 套，'
        f'仍受保护 {len(protected)} 套，主动停用 {len(paused)} 套。'
    )
    return {
        'total': total,
        'healthy': healthy_count,
        'recovered': recovered,
        'protected': protected,
        'paused': paused,
        'summary': summary,
    }


def recover():
    """Check 8012/5175 (start only if missing), verify market/scheduler, and recover eligible strategies."""
    active_ports = {item['port'] for item in listeners()}
    started_note = '后端 8012 与前端 5175 均已运行（未重复启动）'
    if not ({8012, 5175} <= active_ports):
        launcher = ROOT / 'scripts' / 'start_lab.ps1'
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(launcher)],
            cwd=ROOT, capture_output=True, text=True, creationflags=NO_WINDOW, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError('V3 启动失败：' + (result.stderr or result.stdout or '请检查日志').strip())
        started_note = '缺失服务已启动，后端 8012 与前端 5175 均已就绪'

    system, rows = snapshot()
    pool_items = []
    try:
        pool_items = fetch('/api/v1/strategy-pool').get('items') or []
    except Exception:
        pool_items = []

    report = summarize_and_recover_strategies(system, rows, pool_items)
    market_line = (
        f"行情 {system.get('feed_health', '未知')} / 报价 {system.get('quote_health', '未知')} / "
        f"WS {system.get('websocket_recovery_state', '未知')} · 调度 {system.get('scheduler', '未知')}"
    )
    lines = [started_note, market_line, report['summary']]
    if report['recovered']:
        lines.append('成功恢复：' + '；'.join(report['recovered']))
    if report['protected']:
        lines.append('仍受保护：' + '；'.join(report['protected']))
    if report['paused']:
        lines.append('主动停用（保留原状态）：' + '、'.join(report['paused']))
    return '\n'.join(lines)

def node_label():
    # This deployment uses Binance public market data. Do not infer Binance
    # node state from archived Alpaca credential files.
    return 1


def switch_node(number):
    if number == 1:
        return 'V3 uses Binance public USDⓈ-M market data on node 1.'
    if number == 2:
        raise RuntimeError(
            'Binance public-market backup node URL is not configured. '
            'The supplied file contains API credentials, not a REST/WebSocket endpoint. '
            'Legacy Alpaca node profiles are not used by this deployment.'
        )
    raise ValueError('Unsupported Binance market node.')


def api_json(path, method='GET', payload=None, raw=None):
    data=raw if raw is not None else (json.dumps(payload,ensure_ascii=False).encode('utf-8') if payload is not None else None)
    headers={'Content-Type':'application/json'} if data is not None else {}
    request=urllib.request.Request(f'http://127.0.0.1:8012{path}',data=data,headers=headers,method=method)
    try:
        with OPENER.open(request,timeout=15) as response:return json.load(response)
    except urllib.error.HTTPError as exc:
        detail=exc.read().decode('utf-8','replace')
        raise RuntimeError(f'HTTP {exc.code}: {detail}') from exc


def open_strategy_pool(root):
    window=tk.Toplevel(root);window.title('可扩展策略池');window.geometry('1180x610')
    ttk.Label(window,text='仅模拟盘 · BTC/USD · 15分钟 · 初始资金 $10,000 · 杠杆上限 5x · 单笔风险上限 2%').pack(anchor='w',padx=10,pady=8)
    cols=('id','version','status','position','amount','leverage','equity','realized','unrealized','fees','action','cycle')
    grid=ttk.Treeview(window,columns=cols,show='headings',height=12)
    titles=('策略 ID','版本','状态','持仓','持仓金额','杠杆','权益','已实现盈亏','未实现盈亏','手续费','最近行动','最近周期 UTC')
    widths=(145,80,130,70,105,65,100,100,100,85,125,175)
    for key,title,width in zip(cols,titles,widths):grid.heading(key,text=title);grid.column(key,width=width,anchor='center')
    grid.pack(fill='both',expand=True,padx=10,pady=6)
    info=tk.StringVar(value='新策略上传后默认暂停；启用后仍需自然15分钟周期处理才会显示运行中。')
    ttk.Label(window,textvariable=info,wraplength=1120).pack(anchor='w',padx=10,pady=6)
    def selected():
        sid=grid.focus()
        if not sid:raise RuntimeError('请先选择一条策略')
        row=next((x for x in api_json('/api/v1/strategy-pool').get('items',[]) if x['id']==sid),None)
        if not row:raise RuntimeError('策略已变化，请刷新列表')
        return row
    def refresh():
        try:
            items=api_json('/api/v1/strategy-pool').get('items',[])
            for iid in grid.get_children():grid.delete(iid)
            status_text={'REGISTERED':'已登记','REGISTERED_PAUSED':'已登记','PAUSED':'已暂停','ENABLED_NOT_RUNNING':'已启用，等待自然周期','RUNNING':'运行中','FAULTED':'故障'}
            for item in items:
                pos=json.loads(item['position']) if item.get('position') else None
                position=('做多 '+str(pos.get('qty')) if pos else '空仓')
                version=item.get('active_version') or (pos or {}).get('version') or '—'
                grid.insert('', 'end',iid=item['id'],values=(item['id'],version,status_text.get(item.get('status'),item.get('status')),
                    position,item.get('position_amount','0'),leverage_label(item.get('leverage'), item.get('position_direction')),
                    item.get('equity','—'),item.get('realized','0'),item.get('unrealized','0'),item.get('fees','0'),item.get('action','—'),item.get('last_cycle') or '—'))
            info.set(f'共 {len(items)} 条策略。暂停只阻止新开仓；已有仓位仍由开仓版本管理，并继续由共享新鲜报价止损。')
        except Exception as exc:info.set('读取策略池失败：'+str(exc))
    def upload_file():
        path=filedialog.askopenfilename(parent=window,title='选择版本化 JSON 策略',filetypes=[('JSON 策略','*.json')])
        if not path:return
        try:
            raw=Path(path).read_bytes()
            result=api_json('/api/v1/strategy-pool/upload',method='POST',raw=raw)
            diff=result.get('version_diff') or {};changes=diff.get('changes') or []
            document=result['document']
            lines=[f"校验：{'通过' if result.get('validation',{}).get('valid') else '失败'}",f"名称：{document['name']}",f"ID/版本：{result['strategy_id']} / {result['version']}",f"市场/周期：{document['market']} / {document['timeframe']}",f"风险设置：{json.dumps(document['risk'],ensure_ascii=False)}",f"摘要：{result['sha256']}","入场表达式："+json.dumps(document['entry'],ensure_ascii=False),"退出表达式："+json.dumps(document['exit'],ensure_ascii=False),f"状态：已登记，默认暂停",'版本差异：'+('；'.join(x['field'] for x in changes) if changes else '首个版本/无差异')]
            messagebox.showinfo('策略文件校验', '\n'.join(lines),parent=window);refresh()
        except Exception as exc:messagebox.showerror('上传或校验失败',str(exc),parent=window)
    def backtest():
        try:
            row=selected();versions=api_json(f"/api/v1/strategy-pool/{row['id']}/versions").get('items',[])
            version=row.get('active_version') or (versions[-1]['version'] if versions else None)
            if not version:raise RuntimeError('没有可回测版本')
            start=simpledialog.askstring('历史回测','输入起始 UTC 时间（15分钟边界），例如 2026-11-01T00:00:00Z',parent=window)
            if not start:return
            end=simpledialog.askstring('历史回测','输入结束 UTC 时间（15分钟边界），最多30天',parent=window)
            if not end:return
            report=api_json(f"/api/v1/strategy-pool/{row['id']}/backtest",method='POST',payload={'version':version,'start':start,'end':end})
            summary={'策略版本':report['strategy_version'],'净盈亏':report['net_pnl'],'已实现盈亏':report['realized_net_pnl'],'未实现盈亏':report['unrealized_net_pnl'],'手续费':report['fees'],'胜率':report['win_rate'],'最大回撤':report['max_drawdown'],'数据缺口':report['data_gaps'],'权益曲线点数':len(report['equity_curve']),'成交记录数':len(report['trades']),'期末未平仓':report['open_position']}
            result_window=tk.Toplevel(window);result_window.title('策略历史回测报告');result_window.geometry('950x620')
            view= tk.Text(result_window,wrap='none');ys=ttk.Scrollbar(result_window,orient='vertical',command=view.yview);xs=ttk.Scrollbar(result_window,orient='horizontal',command=view.xview)
            view.configure(yscrollcommand=ys.set,xscrollcommand=xs.set);view.grid(row=0,column=0,sticky='nsew');ys.grid(row=0,column=1,sticky='ns');xs.grid(row=1,column=0,sticky='ew')
            result_window.rowconfigure(0,weight=1);result_window.columnconfigure(0,weight=1)
            view.insert('1.0',json.dumps({'summary':summary,'full_report':report},ensure_ascii=False,indent=2));view.configure(state='disabled')
        except Exception as exc:messagebox.showerror('回测失败',str(exc),parent=window)
    def toggle():
        try:
            row=selected();is_enabled=bool(row.get('enabled'))
            versions=api_json(f"/api/v1/strategy-pool/{row['id']}/versions").get('items',[])
            version=row.get('active_version') or (versions[-1]['version'] if versions else None)
            if not version:raise RuntimeError('没有可启用版本')
            if is_enabled and not messagebox.askyesno('暂停策略','将停止该策略的新开仓；现有仓位继续按开仓版本的止损/退出规则管理。确定暂停？',parent=window):return
            result=api_json(f"/api/v1/strategy-pool/{row['id']}/enable",method='POST',payload={'version':version,'enabled':not is_enabled})
            info.set('状态：'+str(result.get('status'))+'；启用不等同运行中，等待自然周期执行后才显示运行中。');refresh()
        except Exception as exc:messagebox.showerror('操作失败',str(exc),parent=window)
    def activate_version():
        try:
            row=selected();versions=api_json(f"/api/v1/strategy-pool/{row['id']}/versions").get('items',[])
            if not versions:raise RuntimeError('没有可启用版本')
            choices=[x['version'] for x in versions]
            version=simpledialog.askstring('选择策略版本','可用版本：'+', '.join(choices)+'\n输入要启用的版本',initialvalue=choices[-1],parent=window)
            if not version:return
            if version not in choices:raise RuntimeError('版本不在历史列表中')
            if not messagebox.askyesno('启用策略版本',f'启用 {version} 只影响后续新开仓；现有仓位仍按各自开仓版本管理。继续？',parent=window):return
            result=api_json(f"/api/v1/strategy-pool/{row['id']}/enable",method='POST',payload={'version':version,'enabled':True})
            info.set('版本已启用：'+version+'；当前状态 '+str(result.get('status'))+'。');refresh()
        except Exception as exc:messagebox.showerror('版本操作失败',str(exc),parent=window)
    def show_diff():
        try:
            row=selected();versions=api_json(f"/api/v1/strategy-pool/{row['id']}/versions").get('items',[])
            if len(versions)<2:raise RuntimeError('该策略只有一个版本')
            old=versions[-2]['version'];new=versions[-1]['version']
            diff=api_json(f"/api/v1/strategy-pool/{row['id']}/diff?old={old}&new={new}")
            messagebox.showinfo('版本差异',json.dumps(diff,ensure_ascii=False,indent=2),parent=window)
        except Exception as exc:messagebox.showerror('无法查看差异',str(exc),parent=window)
    def details():
        try:
            row=selected();items=api_json(f"/api/v1/strategy-pool/{row['id']}/fills").get('items',[])
            messagebox.showinfo('策略成交明细',json.dumps(items[:100],ensure_ascii=False,indent=2),parent=window)
        except Exception as exc:messagebox.showerror('读取明细失败',str(exc),parent=window)
    buttons=ttk.Frame(window);buttons.pack(fill='x',padx=8,pady=7)
    ttk.Button(buttons,text='添加策略文件',command=upload_file).pack(side='left',padx=4)
    ttk.Button(buttons,text='更新策略（上传新版本）',command=upload_file).pack(side='left',padx=4)
    ttk.Button(buttons,text='查看版本差异',command=show_diff).pack(side='left',padx=4)
    ttk.Button(buttons,text='历史回测',command=backtest).pack(side='left',padx=4)
    ttk.Button(buttons,text='启用 / 暂停',command=toggle).pack(side='left',padx=4)
    ttk.Button(buttons,text='启用 / 回滚到指定版本',command=activate_version).pack(side='left',padx=4)
    ttk.Button(buttons,text='成交明细',command=details).pack(side='left',padx=4)
    ttk.Button(buttons,text='刷新',command=refresh).pack(side='left',padx=4)
    refresh()


def _detail_fetch(strategy_id, account_kind='formal'):
    if strategy_id in PA_STRATEGY_IDS:
        data = fetch('/api/v1/strategy-pool')
        item = next((entry for entry in data.get('items', []) if entry.get('id') == strategy_id), None)
        if item is None: raise RuntimeError('策略池未返回该策略')
        fresh_row = console_rows({}, [item])[strategy_id]
        fills = pa_trade_fills(strategy_id)
        return fresh_row, item, fills
    if strategy_id == 'G':
        status = fetch('/api/v1/g-mirror/status')
        fresh_row = console_rows({}, [], g_status=status)['G']
        fills = fetch('/api/v1/g-mirror/fills?limit=2000').get('items', [])
        return fresh_row, {}, fills
    if strategy_id == 'D':
        data = fetch('/api/v1/d/status')
        fresh_row = dict(data)
        account = data.get('account') or {}
        fresh_row.update({'state': account.get('state'), 'equity': data.get('equity'),
                          'realized': data.get('realized_pnl'), 'unrealized': data.get('unrealized_pnl'),
                          'fees': money_label(cumulative_fees(data.get('fills', []))),
                          'amount': position_details(account)[0], 'leverage': position_details(account)[1],
                          'cycle': cycle_label((data.get('decision') or {}).get('candle_close')),
                          'action': (data.get('decision') or {}).get('action') or data.get('reason') or data.get('status'),
                          'status_label': d_control_state(data), 'name': 'D'})
        return fresh_row, account, trade_fills('D')
    if strategy_id == 'E':
        data = fetch('/api/v1/e/accounts').get(account_kind, {})
        if data.get('status') != 'READY': raise RuntimeError(f'E {account_kind}账户不可用')
        account = data.get('account') or {}
        fresh_row = {}
        fresh_row.update({'name': 'E', 'state': account.get('state'), 'equity': data.get('equity'),
                          'realized': data.get('realized_pnl'), 'unrealized': data.get('unrealized_pnl'),
                          'fees': money_label(cumulative_fees(data.get('fills', []))),
                          'amount': position_details(account)[0], 'leverage': position_details(account)[1],
                          'cycle': '—', 'action': data.get('status'), 'status_label': '运行中'})
        return fresh_row, account, trade_fills('E', account_kind)
    if strategy_id in ('A', 'B', 'C'):
        account = fetch(f'/api/v1/agents/{strategy_id}/account')
        return {}, account, trade_fills(strategy_id)
    raise ValueError('UNKNOWN_STRATEGY')


def _file_sha256(path):
    try: return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError: return None


def _legacy_rules(strategy_id, system):
    """Describe the observed implementation and explicitly compare it to loaded runtime metadata."""
    if strategy_id in ('A', 'B', 'C'):
        profile_path = ROOT / 'config/rule_v26/profile.json'
        profile = json.loads(profile_path.read_text(encoding='utf-8'))
        configured = profile.get(strategy_id) or {}
        source_path = ROOT / 'config/rule_v26' / configured.get('file', '')
        source_hash = _file_sha256(source_path)
        runtime_sources = system.get('active_rule_versions') or {}
        authority = rule_source_authority(strategy_id, runtime_sources,
                                          {'version': configured.get('version'), 'sha256': source_hash})
        cfg_risk = {k: v for k, v in configured.items() if k in ('risk_fraction', 'max_leverage', 'require_target_cover_costs', 'target_r', 'max_hold')}
        if strategy_id == 'A':
            raw = json.loads(source_path.read_text(encoding='utf-8'))
            def custom_condition(x):
                if isinstance(x, dict) and {'left', 'op', 'right'} <= set(x):
                    def term(node):
                        if not isinstance(node, dict): return str(node)
                        kind = node.get('kind')
                        names = {'close': '收盘价', 'open': '开盘价', 'high': '最高价', 'low': '最低价',
                                 'rolling_high': f"此前{node.get('period', '—')}根最高价",
                                 'rolling_low': f"此前{node.get('period', '—')}根最低价",
                                 'body_ratio': 'K线实体/全幅比例'}
                        return f"{names.get(kind, '未识别字段 ' + str(kind))}（{_bar_offset(node.get('offset', 0))}）"
                    op = {'>': '>', '>=': '≥', '<': '<', '<=': '≤', '==': '='}.get(x['op'], x['op'])
                    return f"{term(x['left'])} {op} {term(x['right'])}"
                return json.dumps(x, ensure_ascii=False, sort_keys=True)
            long = '\n'.join('  • ' + custom_condition(x) for x in raw.get('long', []))
            short = '\n'.join('  • ' + custom_condition(x) for x in raw.get('short', []))
            logic = (f"做多（全部满足）：\n{long}\n做空（全部满足）：\n{short}\n"
                     f"信号后执行：规则周期在已收盘K线评估，下一次执行周期入场。止损：最近 {raw.get('stop_bars')} 根结构低点/高点外加 $0.01 缓冲；"
                     f"止盈：{raw.get('target_r')}R；最大持仓 {raw.get('max_hold')} 根规则周期。")
        elif strategy_id == 'B':
            logic = ("信号函数：signal_b_filtered（需至少52根历史）。先检查最近10根假突破/收复、长影线反转（影线至少实体/最小跳动的2倍，边缘容差0.1%）或吞没形态；"
                     "再要求方向过滤：SMA12>SMA48 且 SMA12 高于较早窗口SMA12（做多），做空反向。\n"
                     "做多形态：跌破前10根低点后收回；或靠近低点的长下影阳线；或阳线吞没前阴线。做空镜像。"
                     "止损：信号K及此前3根的极值外 $0.01；目标1.5R；最大持仓12根15分钟周期。")
        else:
            logic = ("信号函数：signal_c（至少13根历史）。趋势方向以最近已收盘价与12根前收盘比较。\n"
                     "T1：趋势中两根方向K后出现反转K，收盘越过前一根高/低点。T2：趋势方向收盘突破此前8根最高/最低。"
                     "T3：连续3根同向K，每根实体至少占全幅50%，收盘逐级推进。T4：至少96根历史，96根广义趋势相反且两根逆势K后出现结构反转突破。\n"
                     "止损：信号K及此前3根的低/高点外 $0.01；目标1.3R；最大持仓12根规则周期。"
                     "近20根附近反向障碍会随信号记录，但当前 profile 标注不以结构障碍否决。")
        return (f"规则来源：config/rule_v26/profile.json → {configured.get('file')}\n"
                f"版本：{configured.get('version', '—')} · 本地文件 SHA-256：{source_hash or '读取失败'}\n"
                f"运行时核对：{authority['label']}\n"
                f"策略逻辑：{logic}\n风险配置（profile）：{json.dumps(cfg_risk, ensure_ascii=False, sort_keys=True)}\n"
                "退出执行：共享 RuleCycleRunner 管理结构止损、目标和持仓时限；报价监控可在周期之间触发保护退出。\n"
                "说明：仅在运行时版本及哈希完全匹配时，上述本地源码描述才视为当前生效规则。")
    if strategy_id == 'D':
        path = 'backend/d_signal.py + backend/d_risk.py + backend/d_position_policy.py'
        return (f"当前规则详情来源未确认：D 状态接口没有返回已加载规则版本/文件哈希。以下是本机可见实现（{path}），不能据此断言与运行进程完全一致。\n\n"
                "方向：仅做多。数据：15分钟连续K线，至少960根，并要求对应至少240根完整1小时K线；重复周期及缺口阻断。\n"
                "候选：前一收盘低于EMA20且EMA20下行；回撤至少2.5×ATR14、刺破此前8根低点；当前阳线或下影占比≥0.35、CLV≥0.55、RVOL≥1.3。候选保存ATR、低点、3根颈线和 stop=低点−0.25ATR。\n"
                "确认窗口：1至6根内收盘突破颈线+0.05ATR、站上EMA9、阳线、CLV≥0.65、RVOL≥1.2、RSI14≥35且上升、距离低点≤3.5ATR；1小时不得满足空头过滤；止损距离0.6至3ATR，小时ATR/收盘≤4%。\n"
                "本地风控代码：风险预算2%、杠杆上限5、名义金额上限权益50%、单根成交量上限0.5%、最大价差0.05%；退出管理包含止损、TP1/TP2与时间退出。真实活动执行配置需后端提供版本/哈希后才能确认。")
    if strategy_id == 'E':
        return ("当前规则详情来源未确认：E 运行端可返回策略版本 E-V1.1-QUANT 与成交记录，但没有返回已加载源码哈希。以下是本机可见 backend/e_quant_v11.py 实现摘要。\n\n"
                "候选 E1：已确认区间下沿收复；E2：窗口下跌≥max(0.8%, 2.5×尺度/价格)后低点抬高并向上收复短期高点；E3：区间向上突破后回踩确认；E4：对应向下突破/回踩（是否允许空头还受运行配置控制）。\n"
                "范围窗口候选长度64/48/96/144根，需区间宽度约0.5%至6%、两侧多次触及且至少70%收盘留在区间。E2默认回看8根，E3/E4默认突破窗口8根。\n"
                "多候选优先级 E3 > E1 > E2 > E4；多空方向冲突跳过。入场还须通过量化执行/视觉门控；本机 detector 返回 order_eligible=false，不能单独证明当前部署配置。")
    raise ValueError('UNKNOWN_STRATEGY')


def _v2_rule_text(doc, item):
    position = doc.get('position') or {}; stop = position.get('stop') or {}
    params = doc.get('parameters') or {}
    long_expr = explain_v2_expr((doc.get('entry') or {}).get('long'), params)
    short_expr = explain_v2_expr((doc.get('entry') or {}).get('short'), params)
    stop_text = (f"{stop.get('kind', '未知类型')}；回看{stop.get('lookback', '—')}根；ATR周期{stop.get('atr_period', '—')}；"
                 f"ATR缓冲{stop.get('buffer_atr', '—')}；距离限制{stop.get('min_distance_atr', '—')}至{stop.get('max_distance_atr', '—')} ATR")
    return (f"规则来源：策略池数据库活动版本 {doc.get('strategy_id')} / {doc.get('version')}\n"
            f"SHA-256：{item.get('sha256') or '接口未返回'}\n市场/周期：{doc.get('market')} / {doc.get('timeframe')}\n"
            f"策略描述（原文）：{doc.get('description') or '—'}\n\n适用环境：按以下完整表达式触发；环境条件仅以表达式本身为准。\n"
            f"做多开仓条件：{long_expr}\n\n做空开仓条件：{short_expr}\n\n"
            f"信号确认：在策略池周期检查已收盘K线；方向配置={position.get('direction', '—')}。\n"
            f"开仓执行：{position.get('entry_timing', '未配置')}。\n止损：{stop_text}。\n"
            f"止盈：{position.get('target_r', '—')}R。\n最大持仓：{position.get('max_hold_bars', '—')} 根 {doc.get('timeframe')} K线。\n"
            f"同根止损/止盈冲突：{position.get('same_bar_priority', '未配置')} 优先。\n"
            f"特殊过滤：表达式中列出的全部AND/OR条件；额外成本过滤等设置以策略定义及执行端实现为准。\n"
            f"参数：{json.dumps(params, ensure_ascii=False, sort_keys=True)}\n风险：{json.dumps(doc.get('risk') or {}, ensure_ascii=False, sort_keys=True)}\n"
            f"当前活动版本：{item.get('active_version')}；当前启用：{'是' if item.get('enabled') else '否'}；持仓归属版本：{item.get('position_version') or '—'}")


def _mirror_rule_text(status):
    pair = status.get('latest_pair') or {}
    return ("策略类型：事件驱动的 C 反向镜像；来源 backend/g_mirror.py，API版本 " + str(status.get('version') or '—') +
            "。后端不提供模块哈希，代码文件与进程已加载字节无法独立校验，因此加载源码一致性未确认。\n\n"
            "C 实际开多成交 → G 尝试开空；C 实际开空成交 → G 尝试开多。仅消费已提交成交事件，不读取C信号、不重跑C模型。C实际平仓/部分平仓时，按配对交易同步反向平仓/减仓。\n"
            "数量规则：复制C实际成交BTC数量，必须满足数量精度、保证金和G独立风险检查，否则拒绝，不改变跟单比例。G按自己的有效买卖报价及现有手续费/不利滑点撮合。\n"
            "账户：独立10,000 USD_RESEARCH；2%风险预算、5x最大杠杆。风险保护优先；若紧急退出则记录 MIRROR_RISK_EXIT，C不受影响，该配对终止后只核销，不重复开仓。\n"
            "首次启动：不补造启动前的C仓位；等上线后C的下一笔全新开仓成交。\n"
            f"当前状态：{status.get('follow_state') or status.get('status') or '—'}；当前配对C交易ID：{pair.get('c_trade_id') or '—'}；"
            f"G交易ID：{pair.get('g_trade_id') or '—'}；配对状态：{pair.get('status') or '—'}；数量：{pair.get('quantity_opened') or '—'} BTC。")


def read_ledger_fills_page(agent, limit=100, offset=0):
    """Bounded, read-only pagination for the append-only ABC ledger."""
    if agent not in ('A', 'B', 'C') or not 1 <= int(limit) <= 500 or int(offset) < 0:
        raise ValueError('INVALID_LEDGER_PAGE')
    path = Path(os.getenv('BTC_LAB_DB', str(ROOT / 'data' / 'paper.sqlite3'))).resolve()
    uri = path.as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=3) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT * FROM fills WHERE agent=? ORDER BY occurred_at DESC LIMIT ? OFFSET ?',
                          (agent, int(limit), int(offset))).fetchall()
    return [dict(row) for row in rows]


def _collect_rule_detail(strategy_id, row, fill_offset=0):
    strategy_id = rule_detail_target(strategy_id)
    system = api_json('/api/v1/system/status')
    cost = system.get('cost_model') or {}
    doc = None; raw = None; detail_row = dict(row); account = {}; fills = []
    if strategy_id in PA_STRATEGY_IDS:
        pool = api_json('/api/v1/strategy-pool').get('items', [])
        item = next((x for x in pool if x.get('id') == strategy_id), None)
        if item is None: raise RuntimeError('策略池未返回该策略')
        versions = api_json(f"/api/v1/strategy-pool/{strategy_id}/versions").get('items', [])
        version = item.get('active_version')
        selected = next((x for x in versions if x.get('version') == version), None)
        if selected is None or not selected.get('document'):
            raise RuntimeError('活动版本规则文档不可用；未回退到其他版本')
        doc = json.loads(selected['document']); raw = selected['document']
        detail_row = console_rows({}, [item])[strategy_id]
        fills = pa_trade_fills(strategy_id)
        rule_text = _v2_rule_text(doc, selected)
        risk = {'策略定义': doc.get('risk'), '止损/止盈/时限': doc.get('position'), '成本模型（运行状态接口）': cost,
                '账户当前权益': detail_row.get('equity'), '账户当前实际杠杆': detail_row.get('leverage'),
                '最近拒绝原因': item.get('last_rejection') or item.get('last_error') or '—'}
    elif strategy_id == 'G':
        detail_row, account, fills = _detail_fetch('G')
        status = api_json('/api/v1/g-mirror/status')
        rule_text = _mirror_rule_text(status)
        risk = {'初始资金': '10000 USD_RESEARCH（g_mirror.V1.0.0 默认）', '单笔风险': '2%', '最大杠杆': '5x',
                '成本模型（运行状态接口）': cost, '当前实际仓位': _detail_position_text(status.get('position')),
                '账户权益': status.get('equity'), '异常/拒单原因': status.get('last_error') or status.get('error') or '—'}
    else:
        if strategy_id in ('A', 'B', 'C'):
            account = api_json(f'/api/v1/agents/{strategy_id}/account')
            fills = []
            detail_row = dict(row)
            active = (system.get('active_rule_versions') or {}).get(strategy_id) or {}
            detail_row.update({'version': active.get('version'), 'enabled': row.get('control') == 'RUNNING'})
        else:
            detail_row, account, fills = _detail_fetch(strategy_id)
        rule_text = _legacy_rules(strategy_id, system)
        if strategy_id in ('A', 'B', 'C'):
            profile = json.loads((ROOT / 'config/rule_v26/profile.json').read_text(encoding='utf-8'))
            configured = profile.get(strategy_id) or {}
            current_position = account.get('position') or account.get('position_json')
            if isinstance(current_position, str):
                try: current_position = json.loads(current_position)
                except (ValueError, TypeError): current_position = None
            risk = {'初始资金': '10000 USD_RESEARCH', '活动profile风险': {k: configured.get(k) for k in ('risk_fraction', 'max_leverage', 'target_r', 'max_hold', 'require_target_cover_costs')},
                    '成本模型（运行状态接口）': cost, '当前实际仓位': _detail_position_text(current_position),
                    '账户权益': account.get('equity'), '风险拒绝原因': (row.get('action') if 'REJECT' in str(row.get('action')) else '—')}
        elif strategy_id == 'D':
            risk = {'本机可见执行限制（运行版本未核验）': {'risk_fraction': '2%', 'max_leverage': '5x', 'max_notional_fraction': '50%', 'bar_volume_fraction': '0.5%', 'max_spread': '0.05%'},
                    '成本模型（运行状态接口）': cost, '当前账户': account, '当前拒绝原因': detail_row.get('reason') or detail_row.get('last_error') or '—'}
        else:
            risk = {'当前账户': account, '成本模型（运行状态接口）': cost,
                    '当前运行策略版本（若有成交）': next((f.get('strategy_version') for f in fills if f.get('strategy_version')), '—'),
                    '活动策略参数来源': '后端未提供活动源码哈希；本机实现摘要不能确认为当前加载版本。'}
    total_known = len(fills)
    if strategy_id in ('A', 'B', 'C'):
        try: fills = read_ledger_fills_page(strategy_id, 200, fill_offset)
        except (OSError, sqlite3.Error, ValueError): fills = fills[:200]
    else:
        fills = sorted(fills, key=lambda f: str(f.get('occurred_at') or f.get('cycle') or f.get('created_at') or ''), reverse=True)
        fills = fills[fill_offset:fill_offset + 200]
    runtime = detail_summary_text(strategy_id, detail_row, account)
    return {'row': detail_row, 'account': account, 'fills': fills, 'rules': rule_text,
            'risk': risk, 'runtime': runtime, 'raw_json': raw, 'cost': cost,
            'fill_offset': fill_offset, 'fill_count': len(fills), 'total_known': total_known}


def open_strategy_rule_details(root, strategy_id, row):
    """Read-only four-tab strategy rule, risk, runtime and fills window."""
    strategy_id = rule_detail_target(strategy_id)
    view = strategy_view(strategy_id, row)
    window = tk.Toplevel(root)
    configure_detail_popup(window, root)
    window.title(f"【{view['strategy_id']}】{view['name']}｜策略详情")
    window.geometry('900x650'); window.minsize(760, 520); window.configure(bg='#f5f7fb')
    window.iconbitmap(default=str(ROOT / 'frontend/public/btc-lab-logo.ico'))
    window.after(350, lambda: style_native_caption(window))

    header = tk.Frame(window, bg='white', highlightbackground='#e2e8f0', highlightthickness=1)
    header.pack(fill='x', padx=12, pady=10)
    enabled = row.get('enabled')
    version = row.get('active_version') or row.get('version') or row.get('position_version') or '运行时查询中'
    headline = tk.StringVar(value=f"{view['name']}  ·  ID {strategy_id}  ·  版本 {version}  ·  BTC/USD  ·  15分钟  ·  {'已启用' if enabled else '已暂停' if enabled is False else '启用状态查询中'}")
    tk.Label(header, textvariable=headline, bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 10, 'bold'),
             anchor='w', padx=12, pady=11, wraplength=850, justify='left').pack(fill='x')
    notebook = ttk.Notebook(window); notebook.pack(fill='both', expand=True, padx=12, pady=(0, 8))
    pages = {}
    for key, title in (('rules', '交易规则'), ('risk', '风控参数'), ('runtime', '运行状态'), ('fills', '成交记录')):
        page = tk.Frame(notebook, bg='white'); notebook.add(page, text=title); pages[key] = page
    status = tk.StringVar(value='正在读取活动规则和只读账户数据…')
    tk.Label(window, textvariable=status, bg='#f5f7fb', fg='#64748b', anchor='w').pack(fill='x', padx=14, pady=(0, 8))

    rules_text = tk.Text(pages['rules'], wrap='word', bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 9), relief='flat', padx=12, pady=10)
    rules_scroll = ttk.Scrollbar(pages['rules'], orient='vertical', command=rules_text.yview)
    rules_text.configure(yscrollcommand=rules_scroll.set); rules_text.grid(row=0, column=0, sticky='nsew'); rules_scroll.grid(row=0, column=1, sticky='ns')
    pages['rules'].rowconfigure(0, weight=1); pages['rules'].columnconfigure(0, weight=1)
    raw_button = tk.Button(pages['rules'], text='查看原始策略 JSON', bg='#2563eb', fg='white', relief='flat', activebackground='#1d4ed8', activeforeground='white', cursor='hand2')
    raw_button.grid(row=1, column=0, sticky='e', padx=12, pady=8)
    risk_text = tk.Text(pages['risk'], wrap='word', bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 9), relief='flat', padx=12, pady=10)
    risk_scroll = ttk.Scrollbar(pages['risk'], orient='vertical', command=risk_text.yview)
    risk_text.configure(yscrollcommand=risk_scroll.set); risk_text.grid(row=0, column=0, sticky='nsew'); risk_scroll.grid(row=0, column=1, sticky='ns')
    pages['risk'].rowconfigure(0, weight=1); pages['risk'].columnconfigure(0, weight=1)
    runtime_text = tk.Text(pages['runtime'], wrap='word', bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 9), relief='flat', padx=12, pady=10)
    runtime_scroll = ttk.Scrollbar(pages['runtime'], orient='vertical', command=runtime_text.yview)
    runtime_text.configure(yscrollcommand=runtime_scroll.set); runtime_text.grid(row=0, column=0, sticky='nsew'); runtime_scroll.grid(row=0, column=1, sticky='ns')
    pages['runtime'].rowconfigure(0, weight=1); pages['runtime'].columnconfigure(0, weight=1)

    fills_page = pages['fills']
    trade_cols = ('time', 'side', 'effect', 'qty', 'price', 'fee', 'slippage', 'reason', 'pnl', 'trade_id', 'linked_id')
    trade_grid = ttk.Treeview(fills_page, columns=trade_cols, show='headings', height=12, style='Strategy.Treeview')
    trade_titles = ('时间（本地）', '方向', '开/平', '数量 BTC', '成交价', '手续费', '滑点', '原因', '净盈亏', '交易编号', '关联C交易ID')
    trade_widths = (140, 70, 75, 110, 100, 85, 85, 210, 100, 220, 220)
    for col, title, width in zip(trade_cols, trade_titles, trade_widths):
        trade_grid.heading(col, text=title); trade_grid.column(col, width=width, minwidth=60,
            anchor='e' if col in ('qty', 'price', 'fee', 'slippage', 'pnl') else 'w', stretch=True)
    trade_y = ttk.Scrollbar(fills_page, orient='vertical', command=trade_grid.yview)
    trade_x = ttk.Scrollbar(fills_page, orient='horizontal', command=trade_grid.xview)
    trade_grid.configure(yscrollcommand=trade_y.set, xscrollcommand=trade_x.set)
    trade_grid.grid(row=0, column=0, sticky='nsew'); trade_y.grid(row=0, column=1, sticky='ns'); trade_x.grid(row=1, column=0, sticky='ew')
    fills_page.rowconfigure(0, weight=1); fills_page.columnconfigure(0, weight=1)
    page_bar = tk.Frame(fills_page, bg='white'); page_bar.grid(row=2, column=0, sticky='ew', padx=8, pady=6)
    page_offset = {'value': 0}; loaded = {'value': None}; page_label = tk.StringVar(value='')
    tk.Label(page_bar, textvariable=page_label, bg='white', fg='#64748b').pack(side='left')
    older_button = ttk.Button(page_bar, text='更早记录'); older_button.pack(side='right', padx=4)
    newer_button = ttk.Button(page_bar, text='更新记录'); newer_button.pack(side='right', padx=4)

    def set_readonly(textbox, content):
        textbox.configure(state='normal'); textbox.delete('1.0', 'end'); textbox.insert('1.0', content); textbox.configure(state='disabled')

    def show_raw():
        data = loaded.get('value')
        if not data or not data.get('raw_json'):
            messagebox.showinfo('原始策略 JSON', '该策略不是声明式 JSON，或当前活动版本尚未读取。', parent=window); return
        raw_window = tk.Toplevel(window); configure_detail_popup(raw_window, window); raw_window.title(f'{strategy_id} · 原始策略 JSON'); raw_window.geometry('820x560')
        raw_view = tk.Text(raw_window, wrap='none', font=('Consolas', 9), bg='#0f172a', fg='#e2e8f0')
        sy = ttk.Scrollbar(raw_window, orient='vertical', command=raw_view.yview); sx = ttk.Scrollbar(raw_window, orient='horizontal', command=raw_view.xview)
        raw_view.configure(yscrollcommand=sy.set, xscrollcommand=sx.set); raw_view.grid(row=0, column=0, sticky='nsew'); sy.grid(row=0, column=1, sticky='ns'); sx.grid(row=1, column=0, sticky='ew')
        raw_window.rowconfigure(0, weight=1); raw_window.columnconfigure(0, weight=1)
        raw_view.insert('1.0', json.dumps(json.loads(data['raw_json']), ensure_ascii=False, indent=2)); raw_view.configure(state='disabled')
    raw_button.configure(command=show_raw)

    def load(offset=0):
        page_offset['value'] = offset; status.set('正在读取真实规则、账户和成交记录…')
        def worker():
            try: result = _collect_rule_detail(strategy_id, row, offset)
            except Exception as exc:
                error = str(exc); root.after(0, lambda: status.set('详情读取失败：' + error)); return
            def show():
                if not window.winfo_exists(): return
                loaded['value'] = result
                current = result['row']; version_now = current.get('active_version') or current.get('version') or current.get('position_version') or '—'
                enabled_now = current.get('enabled', row.get('enabled'))
                headline.set(f"{view['name']}  ·  ID {strategy_id}  ·  版本 {version_now}  ·  BTC/USD  ·  15分钟  ·  {'已启用' if enabled_now else '已暂停' if enabled_now is False else '状态未知'}")
                set_readonly(rules_text, result['rules'])
                set_readonly(risk_text, json.dumps(result['risk'], ensure_ascii=False, indent=2, default=str))
                runtime = result['runtime']
                if strategy_id == 'G': runtime += '\n\n镜像配对状态、C/G成交时间与延迟来自G状态接口。'
                set_readonly(runtime_text, runtime)
                for iid in trade_grid.get_children(): trade_grid.delete(iid)
                for fill in result['fills']:
                    values = detail_fill_values(fill)
                    trade_grid.insert('', 'end', values=(values[0], values[2], values[1], values[4], values[5], values[6], values[7], values[9], values[8], values[10], values[11] if strategy_id == 'G' else '—'))
                page_label.set(f"按时间倒序 · 显示 {offset + 1}–{offset + len(result['fills'])} 条 · 单页最多200条" +
                               (' · ABC分页使用本地只读账本' if strategy_id in ('A', 'B', 'C') else ' · 当前端点最多返回2000条' if strategy_id in PA_STRATEGY_IDS or strategy_id == 'G' else ' · 后端端点暂不支持分页'))
                has_more = len(result['fills']) == 200 and (strategy_id in ('A', 'B', 'C') or offset + 200 < result.get('total_known', 0))
                older_button.configure(state='normal' if has_more else 'disabled', command=lambda: load(offset + 200))
                newer_button.configure(state='normal' if offset else 'disabled', command=lambda: load(max(0, offset - 200)))
                status.set('只读详情已更新 · 未发送交易或启停请求')
            root.after(0, show)
        threading.Thread(target=worker, daemon=True).start()

    ttk.Button(window, text='刷新详情', command=lambda: load(page_offset['value'])).pack(anchor='e', padx=14, pady=(0, 9))
    load()


def open_strategy_details(root, strategy_id, row):
    """One shared account and fill view for ABCDE, G and all PA strategies."""
    view = strategy_view(strategy_id, row)
    window = tk.Toplevel(root)
    configure_detail_popup(window, root)
    window.title(f"{view['strategy_id']} {view['name']} · 账户与成交明细")
    window.geometry('1360x760'); window.minsize(1000, 560); window.configure(bg='#f5f7fb')
    window.iconbitmap(default=str(ROOT / 'frontend/public/btc-lab-logo.ico'))
    window.after(350, lambda: style_native_caption(window))
    # Reserve the bottom command strip before packing the expandable table body.
    controls = tk.Frame(window, bg='#f5f7fb')
    controls.pack(side='bottom', fill='x', padx=14, pady=(4, 10))

    account_kind = tk.StringVar(value='formal')
    if strategy_id == 'E':
        selector = tk.Frame(window, bg='#f5f7fb')
        selector.pack(fill='x', padx=14, pady=(10, 0))
        tk.Label(selector, text='账户', bg='#f5f7fb', fg='#64748b', font=('Microsoft YaHei UI', 9)).pack(side='left')
        choice = ttk.Combobox(selector, textvariable=account_kind, state='readonly', width=12,
                              values=('formal', 'shadow'), style='Light.TCombobox')
        choice.pack(side='left', padx=8)

    head = tk.Frame(window, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    head.pack(fill='x', padx=14, pady=(10, 6))
    head_title = tk.StringVar(value=f"{view['strategy_id']}｜{view['name']}")
    head_subtitle = tk.StringVar(value=f"策略ID：{strategy_id}  ·  版本 {row.get('active_version') or row.get('version') or '—'}  ·  BTC/USD")
    head_state = tk.StringVar(value='状态读取中')
    tk.Label(head, text='◆', bg='white', fg='#2563eb', font=('Segoe UI Symbol', 22), padx=12).grid(row=0, column=0, rowspan=2)
    tk.Label(head, textvariable=head_title, bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 15, 'bold'), anchor='w').grid(row=0, column=1, sticky='w', pady=(9, 1))
    tk.Label(head, textvariable=head_subtitle, bg='white', fg='#64748b', font=('Microsoft YaHei UI', 9), anchor='w').grid(row=1, column=1, sticky='w', pady=(0, 9))
    head_badge = tk.Label(head, textvariable=head_state, bg='#e2e8f0', fg='#475569', font=('Microsoft YaHei UI', 9, 'bold'), padx=12, pady=6)
    head_badge.grid(row=0, column=2, rowspan=2, padx=15)
    head.columnconfigure(1, weight=1)

    stat_panel = tk.Frame(window, bg='#f5f7fb'); stat_panel.pack(fill='x', padx=14, pady=(0, 6))
    stat_defs = [('账户权益', 'equity', '#172b4d'), ('已实现盈亏', 'realized', '#16a34a'),
                 ('未实现盈亏', 'unrealized', '#16a34a'), ('累计手续费', 'fees', '#172b4d'),
                 ('持仓金额', 'notional', '#172b4d'), ('完整交易笔数', 'closed_count', '#172b4d')]
    stat_vars, stat_values = {}, {}
    for index, (title, key, color) in enumerate(stat_defs):
        panel = tk.Frame(stat_panel, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
        panel.grid(row=0, column=index, sticky='nsew', padx=(0 if index == 0 else 5, 0))
        tk.Label(panel, text=title, bg='white', fg='#64748b', font=('Microsoft YaHei UI', 8), anchor='w').pack(fill='x', padx=10, pady=(7, 1))
        stat_vars[key] = tk.StringVar(value='—')
        stat_values[key] = tk.Label(panel, textvariable=stat_vars[key], bg='white', fg=color,
                                    font=('Microsoft YaHei UI', 13, 'bold'), anchor='w')
        stat_values[key].pack(fill='x', padx=10, pady=(0, 7))
        stat_panel.columnconfigure(index, weight=1, uniform='stat')

    info_panel = tk.Frame(window, bg='#f5f7fb'); info_panel.pack(fill='x', padx=14, pady=(0, 7))
    hold_card = tk.Frame(info_panel, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    hold_card.pack(side='left', fill='both', expand=True, padx=(0, 5))
    run_card = tk.Frame(info_panel, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    run_card.pack(side='left', fill='both', expand=True, padx=(5, 0))
    tk.Label(hold_card, text='持仓信息', bg='white', fg='#1e3a5f', font=('Microsoft YaHei UI', 10, 'bold'), anchor='w').pack(fill='x', padx=11, pady=(6, 2))
    holding_var = tk.StringVar(value='空仓')
    tk.Label(hold_card, textvariable=holding_var, bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 9), anchor='w', justify='left', wraplength=660).pack(fill='x', padx=11, pady=(0, 8))
    tk.Label(run_card, text='策略参数与运行信息', bg='white', fg='#1e3a5f', font=('Microsoft YaHei UI', 10, 'bold'), anchor='w').pack(fill='x', padx=11, pady=(6, 2))
    runtime_var = tk.StringVar(value='正在读取运行状态…')
    tk.Label(run_card, textvariable=runtime_var, bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 9), anchor='w', justify='left', wraplength=660).pack(fill='x', padx=11, pady=(0, 8))

    area = tk.Frame(window, bg='#f5f7fb'); area.pack(fill='both', expand=True, padx=14, pady=6)
    widths = (155, 76, 74, 82, 115, 110, 92, 100, 100, 250, 240, 240, 280)
    titles = ('成交时间（本地）', '开/平', '买卖方向', '持仓方向', '数量 BTC', '成交价', '手续费',
              '滑点', '净盈亏', '成交/退出原因', '交易 ID', '关联交易/事件 ID', '成交/幂等 ID')
    grid = ttk.Treeview(area, columns=DETAIL_FILL_COLUMNS, show='headings', style='Strategy.Treeview')
    grid.tag_configure('stripe-even', background='#ffffff', foreground='#172b4d')
    grid.tag_configure('stripe-odd', background='#f8fafc', foreground='#172b4d')
    for key, title, width in zip(DETAIL_FILL_COLUMNS, titles, widths):
        grid.heading(key, text=title)
        grid.column(key, width=width, minwidth=70,
                    anchor='e' if key in ('quantity', 'price', 'fee', 'slippage', 'pnl') else 'w', stretch=True)
    ybar = ttk.Scrollbar(area, orient='vertical', command=grid.yview)
    xbar = ttk.Scrollbar(area, orient='horizontal', command=grid.xview)
    grid.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
    grid.grid(row=0, column=0, sticky='nsew'); ybar.grid(row=0, column=1, sticky='ns'); xbar.grid(row=1, column=0, sticky='ew')
    area.rowconfigure(0, weight=1); area.columnconfigure(0, weight=1)

    selected_detail = tk.StringVar(value='选择成交记录查看报价、决策、版本及成本模型等完整事件信息。')
    tk.Label(window, textvariable=selected_detail, bg='#ffffff', fg='#475569', anchor='w', justify='left',
             wraplength=1300, padx=12, pady=8, relief='solid', bd=1).pack(fill='x', padx=14, pady=(0, 5))
    message = tk.StringVar(value='正在读取账户与成交…')
    tk.Label(window, textvariable=message, bg='#f5f7fb', fg='#64748b', anchor='w').pack(fill='x', padx=14)

    def selected_fill(event=None):
        selection = grid.selection()
        if not selection: return
        fill = fill_by_iid.get(selection[0], {})
        metadata = [f"成交ID：{fill.get('fill_id') or fill.get('idempotency_key') or '—'}",
                    f"决策ID：{fill.get('decision_id') or '—'}",
                    f"报价ID：{fill.get('quote_id') or '—'}",
                    f"来源成交ID：{fill.get('source_fill_id') or '—'}",
                    f"策略版本：{fill.get('strategy_version') or fill.get('version') or '—'}"]
        cost = fill.get('cost_model_json')
        if cost:
            try:
                parsed = json.loads(cost) if isinstance(cost, str) else cost
                metadata.append('成本模型：' + json.dumps(parsed, ensure_ascii=False, sort_keys=True))
            except (TypeError, ValueError): metadata.append('成本模型：' + str(cost))
        selected_detail.set('  ·  '.join(metadata))
    grid.bind('<<TreeviewSelect>>', selected_fill)

    fill_by_iid = {}
    detail_snapshot = {'row': None, 'account': None, 'fills': None, 'metrics': None, 'error': None}

    def show_account(fresh_row, account, fills):
        metrics = account_detail_metrics(strategy_id, fresh_row, account, fills)
        detail_snapshot.update({'row': fresh_row, 'account': account, 'fills': fills, 'metrics': metrics, 'error': None})
        head_title.set(f"{view['strategy_id']}｜{fresh_row.get('name') or view['name']}")
        version = fresh_row.get('active_version') or fresh_row.get('version') or fresh_row.get('position_version') or '—'
        head_subtitle.set(f'策略ID：{strategy_id}  ·  当前版本 {version}  ·  BTC/USD  ·  15分钟')
        enabled = fresh_row.get('enabled')
        enabled_text = '已启用' if enabled is True else '已暂停' if enabled is False else '开关状态未知'
        status_text = fresh_row.get('status_label') or fresh_row.get('control') or '运行状态未知'
        head_state.set(f'{status_text}  ·  {enabled_text}')
        state_upper = str(status_text).upper()
        if 'FAULT' in state_upper or '故障' in status_text or '异常' in status_text:
            head_badge.configure(bg='#fee2e2', fg='#b91c1c')
        elif 'PAUSE' in state_upper or '暂停' in status_text:
            head_badge.configure(bg='#e2e8f0', fg='#475569')
        elif 'WAIT' in state_upper or '等待' in status_text or '启用' in status_text and '运行' not in status_text:
            head_badge.configure(bg='#ffedd5', fg='#c2410c')
        elif 'RUNNING' in state_upper or '运行中' in status_text or status_text == 'AUTO_PAPER':
            head_badge.configure(bg='#dcfce7', fg='#15803d')
        else:
            head_badge.configure(bg='#e2e8f0', fg='#475569')
        stat_vars['equity'].set(metrics['equity'])
        stat_vars['realized'].set(metrics['realized'])
        stat_vars['unrealized'].set(metrics['unrealized'])
        stat_vars['fees'].set(metrics['fees'])
        stat_vars['notional'].set(metrics['notional'])
        stat_vars['closed_count'].set(str(metrics['closed_count']))
        for key in ('realized', 'unrealized'):
            try: value = Decimal(str(metrics[key]).replace(',', ''))
            except (InvalidOperation, TypeError): value = Decimal(0)
            stat_values[key].configure(fg='#16a34a' if value >= 0 else '#dc2626')
        position = metrics['position']
        if not position:
            holding_var.set('持仓方向：空仓    BTC数量：0.00    入场价格：—    止损价格：—    止盈价格：—    已持仓K线根数：—')
        else:
            direction = {'LONG': '做多', 'SHORT': '做空'}.get(metrics['direction'], '方向未知')
            holding_var.set(f"持仓方向：{direction}    BTC数量：{metrics['quantity']}    入场价格：{metrics['entry']}    止损价格：{metrics['stop']}    止盈价格：{metrics['target']}    已持仓K线根数：{metrics['bars']}")
        runtime_var.set(f"杠杆：{metrics['leverage']}    最近15分钟周期：{cycle_label(metrics['cycle'])}    最近动作：{metrics['action']}\n风控拒绝/异常原因：{metrics['error']}")
    def load():
        selected_kind = account_kind.get()
        def worker():
            try:
                fresh_row, account, fills = _detail_fetch(strategy_id, selected_kind)
                if not fresh_row: fresh_row = dict(row)
                if strategy_id == 'G': fresh_row['name'] = row.get('name', 'C反向镜像')
                fills = sorted(fills, key=lambda item: str(item.get('occurred_at') or item.get('cycle') or item.get('created_at') or ''), reverse=True)
                def show():
                    if not window.winfo_exists(): return
                    show_account(fresh_row, account, fills)
                    for iid in grid.get_children(): grid.delete(iid)
                    fill_by_iid.clear()
                    for index, fill in enumerate(fills):
                        iid = str(index)
                        fill_by_iid[iid] = fill
                        grid.insert('', 'end', iid=iid, values=detail_fill_values(fill),
                                    tags=('stripe-even' if index % 2 == 0 else 'stripe-odd',))
                    label = '影子账户' if strategy_id == 'E' and selected_kind == 'shadow' else '正式纸盘'
                    warning = (detail_snapshot.get('metrics') or {}).get('data_warnings') or []
                    message.set(f'{label} · {len(fills)} 条成交记录（开仓、平仓分行）' + (f' · 数据核对提示：{warning[0]}' if warning else ''))
                    selected_detail.set('选择成交记录查看报价、决策、版本及成本模型等完整事件信息。')
                root.after(0, show)
            except Exception as exc:
                error = str(exc)
                detail_snapshot.update({'error': error})
                root.after(0, lambda: message.set('账户或成交读取失败：' + error))
        threading.Thread(target=worker, daemon=True).start()

    def open_pnl_calendar():
        cal = tk.Toplevel(window); configure_detail_popup(cal, window)
        cal.title(f'{view["strategy_id"]} {view["name"]} · 盈亏日历'); cal.geometry('720x790'); cal.minsize(660, 700)
        cal.configure(bg='#111827'); cal.after(350, lambda: style_native_caption(cal))
        month = {'year': datetime.now(BEIJING).year, 'month': datetime.now(BEIJING).month}
        selected_date = {'value': None}; day_details = {}; calendar_data = {'transaction_fills': {}}
        tk.Label(cal, text=f"{view['strategy_id']} · 按平仓日期统计的已实现净盈亏（北京时间）", bg='#111827', fg='#cbd5e1',
                 font=('Microsoft YaHei UI', 9), anchor='w').pack(fill='x', padx=16, pady=(12, 5))
        nav = tk.Frame(cal, bg='#1f2937'); nav.pack(fill='x', padx=12)
        month_label = tk.StringVar(value='')
        def change_month(delta):
            total = month['year'] * 12 + month['month'] - 1 + delta
            month.update(year=total // 12, month=total % 12 + 1); draw_month()
        tk.Button(nav, text='‹', command=lambda: change_month(-1), bg='#263449', fg='white', relief='flat', width=4).pack(side='left', padx=7, pady=6)
        tk.Label(nav, textvariable=month_label, bg='#1f2937', fg='white', font=('Microsoft YaHei UI', 14, 'bold')).pack(side='left', expand=True)
        tk.Button(nav, text='›', command=lambda: change_month(1), bg='#263449', fg='white', relief='flat', width=4).pack(side='right', padx=7, pady=6)
        week = tk.Frame(cal, bg='#111827'); week.pack(fill='x', padx=12, pady=(8, 1))
        for index, name in enumerate(('一', '二', '三', '四', '五', '六', '日')):
            tk.Label(week, text=name, bg='#111827', fg='#94a3b8', font=('Microsoft YaHei UI', 9, 'bold')).grid(row=0, column=index, sticky='nsew', pady=4)
            week.columnconfigure(index, weight=1, uniform='weekday')
        grid_frame = tk.Frame(cal, bg='#111827'); grid_frame.pack(fill='x', padx=12)
        metrics_bar = tk.Frame(cal, bg='#1f2937'); metrics_bar.pack(fill='x', padx=12, pady=(9, 6))
        month_metrics = {}
        for idx, title in enumerate(('当月净盈亏', '胜率', '交易天数', '最大单日亏损')):
            cell = tk.Frame(metrics_bar, bg='#1f2937'); cell.grid(row=0, column=idx, sticky='nsew', padx=4, pady=8)
            tk.Label(cell, text=title, bg='#1f2937', fg='#94a3b8', font=('Microsoft YaHei UI', 8)).pack()
            value = tk.StringVar(value='—'); month_metrics[title] = value
            tk.Label(cell, textvariable=value, bg='#1f2937', fg='white', font=('Microsoft YaHei UI', 11, 'bold')).pack(pady=(2, 0))
            metrics_bar.columnconfigure(idx, weight=1, uniform='m')
        selected_summary = tk.StringVar(value='点击日期查看当日真实成交与已实现盈亏。')
        tk.Label(cal, textvariable=selected_summary, bg='#111827', fg='#e2e8f0', justify='left', anchor='w', wraplength=680,
                 font=('Microsoft YaHei UI', 9)).pack(fill='x', padx=16, pady=(2, 4))
        event_detail_var = tk.StringVar(value='选择下方成交可查看原始账本字段。')
        detail_cols = ('time', 'effect', 'side', 'qty', 'price', 'fee', 'reason', 'trade')
        day_grid = ttk.Treeview(cal, columns=detail_cols, show='headings', height=5, style='Strategy.Treeview')
        for key, title, width in zip(detail_cols, ('时间', '开/平', '方向', '数量', '成交价', '费用', '原因', '交易ID'), (135, 55, 55, 80, 90, 75, 150, 150)):
            day_grid.heading(key, text=title); day_grid.column(key, width=width, minwidth=55, anchor='e' if key in ('qty', 'price', 'fee') else 'w')
        day_grid.pack(fill='both', expand=True, padx=12, pady=(0, 8))
        day_fill_map = {}
        day_grid.bind('<<TreeviewSelect>>', lambda _e: event_detail_var.set(
            '原始成交：' + json.dumps(day_fill_map.get(day_grid.selection()[0], {}), ensure_ascii=False, default=str)
            if day_grid.selection() else '选择下方成交可查看原始账本字段。'))
        tk.Label(cal, textvariable=event_detail_var, bg='#1f2937', fg='#cbd5e1', justify='left', anchor='w',
                 wraplength=680, font=('Microsoft YaHei UI', 8), padx=10, pady=6).pack(fill='x', padx=12, pady=(0, 5))
        foot = tk.Label(cal, text='净盈亏 = 实际执行价差 − 开仓及平仓手续费；成交价已包含滑点，滑点不重复扣除。未平仓浮盈不计。',
                        bg='#111827', fg='#64748b', font=('Microsoft YaHei UI', 8), anchor='w', wraplength=680)
        foot.pack(fill='x', padx=14, pady=(0, 10))
        def choose_day(day):
            selected_date['value'] = day
            month.update(year=day.year, month=day.month)
            render_calendar()

        def show_day(day):
            date_key = day.isoformat(); entry = day_details.get(date_key)
            for iid in day_grid.get_children(): day_grid.delete(iid)
            day_fill_map.clear()
            event_detail_var.set('选择下方成交可查看原始账本字段。')
            day_fills = calendar_data['transaction_fills'].get(date_key, [])
            if not entry:
                selected_summary.set(f"{date_key}｜" + ('有成交；无可确认的已实现净盈亏。' if day_fills else '无成交记录；当日已实现净盈亏：—'))
            else:
                selected_summary.set(
                    f"{date_key}｜已实现净盈亏：{entry['net']:+,.2f} USD｜完整闭合交易：{entry.get('closed_count',0)}｜"
                    f"盈利：{entry.get('wins',0)}｜亏损：{entry.get('losses',0)}｜手续费（归属平仓日）：{entry['fees']:,.2f} USD"
                )
            source_fills = sorted(day_fills, key=_calendar_timestamp)
            for idx, fill in enumerate(source_fills):
                values = detail_fill_values(fill)
                iid = str(idx); day_fill_map[iid] = fill
                day_grid.insert('', 'end', iid=iid, values=(values[0], values[1], values[3], values[4], values[5], values[6], values[9], values[10]))
        def render_calendar():
            year, mon = month['year'], month['month']; month_label.set(f'{year}年{mon}月')
            for child in grid_frame.winfo_children(): child.destroy()
            current_fills = detail_snapshot.get('fills')
            if current_fills is None:
                if detail_snapshot.get('error'):
                    foot.configure(text='账本读取失败：' + detail_snapshot['error'])
                else:
                    foot.configure(text='正在读取只读成交账本…'); cal.after(250, render_calendar)
                for i in range(6):
                    for j in range(7):
                        tk.Label(grid_frame, text='…', bg='#1f2937', fg='#64748b', height=3).grid(row=i, column=j, sticky='nsew', padx=2, pady=2)
                return
            calc = calculate_pnl_calendar(current_fills, strategy_id, year, mon)
            if strategy_id in PA_STRATEGY_IDS or strategy_id == 'G':
                if len(current_fills) >= 2000:
                    calc['incomplete'].append('只读成交接口上限为2000条，可能缺少更早的配对开仓记录。')
            if strategy_id in PA_STRATEGY_IDS and detail_snapshot.get('metrics') and not detail_snapshot['metrics'].get('position') and calc['open_group_count']:
                warning = f"当前账户为空仓，但有{calc['open_group_count']}笔未配对开仓记录；可能存在缺口删失/账本缺行，不计入日历收益。"
                if warning not in calc['incomplete']: calc['incomplete'].append(warning)
            calc['incomplete'] = list(dict.fromkeys(calc['incomplete']))
            day_details.clear(); day_details.update(calc['daily'])
            calendar_data['transaction_fills'] = calc['transaction_fills']
            first_weekday, days_in_month = month_calendar.monthrange(year, mon)
            offset = first_weekday
            # Explicitly construct the current month and its trailing context to six rows.
            visible_days = [date(year, mon, 1) - timedelta(days=offset - i) for i in range(offset)]
            visible_days += [date(year, mon, day) for day in range(1, days_in_month + 1)]
            tail = 42 - len(visible_days)
            if tail > 0:
                last = date(year, mon, days_in_month)
                visible_days += [last + timedelta(days=i) for i in range(1, tail + 1)]
            for idx, day in enumerate(visible_days):
                entry = calc['daily'].get(day.isoformat())
                is_current_month = day.month == mon
                bg = '#2563eb' if selected_date['value'] == day else '#1f2937' if is_current_month else '#17212f'
                fg = '#e5e7eb' if is_current_month else '#64748b'
                has_fill = bool(calc['transaction_fills'].get(day.isoformat()))
                pnl_text = ('成交·待平' if has_fill else '—') if not entry else f"{entry['net']:+.2f}"
                pnl_color = '#6b7280' if not entry else '#22c55e' if entry['net'] > 0 else '#ef4444' if entry['net'] < 0 else '#d1d5db'
                cell = tk.Frame(grid_frame, bg=bg, highlightbackground='#334155', highlightthickness=1, height=61, cursor='hand2')
                cell.grid(row=idx // 7, column=idx % 7, sticky='nsew', padx=2, pady=2); cell.grid_propagate(False)
                date_label = tk.Label(cell, text=str(day.day), bg=bg, fg=fg, font=('Microsoft YaHei UI', 10, 'bold'))
                date_label.pack(pady=(7, 1))
                pnl_label = tk.Label(cell, text=pnl_text, bg=bg, fg=pnl_color, font=('Microsoft YaHei UI', 8))
                pnl_label.pack()
                for widget in (cell, date_label, pnl_label): widget.bind('<Button-1>', lambda _e, selected=day: choose_day(selected))
                grid_frame.columnconfigure(idx % 7, weight=1, uniform='calendar')
            month_metrics['当月净盈亏'].set(f"{calc['monthly_net']:+,.2f}")
            month_metrics['当月净盈亏']._label_color = '#22c55e' if calc['monthly_net'] >= 0 else '#ef4444'
            month_metrics['胜率'].set(f"{calc['win_rate']:.1%}" if calc['win_rate'] is not None else '—')
            month_metrics['交易天数'].set(str(calc['trading_days']) + ' 天')
            month_metrics['最大单日亏损'].set(f"{calc['max_daily_loss']:,.2f}" if calc['max_daily_loss'] < 0 else '无亏损日')
            if calc['incomplete']:
                foot.configure(text=f"账本有 {len(calc['incomplete'])} 项无法确认；受影响成交不计入收益。" + calc['incomplete'][0])
            else:
                foot.configure(text='净盈亏 = 实际执行价差 − 开仓及平仓手续费；成交价已包含滑点，滑点不重复扣除。未平仓浮盈不计。')
            if selected_date['value'] and selected_date['value'].year == year and selected_date['value'].month == mon:
                show_day(selected_date['value'])
            else:
                for iid in day_grid.get_children(): day_grid.delete(iid)
                day_fill_map.clear()
                event_detail_var.set('选择下方成交可查看原始账本字段。')
                selected_summary.set('点击日期查看当日真实成交与已实现盈亏。')
        render_calendar()

    tk.Button(controls, text='▦  盈亏日历', command=open_pnl_calendar, bg='white', fg='#2563eb', activebackground='#eff6ff',
              relief='solid', bd=1, padx=14, pady=7, font=('Microsoft YaHei UI', 9, 'bold'), cursor='hand2').pack(side='right', padx=(6, 0))
    tk.Button(controls, text='⟳  刷新账户与成交', command=load, bg='#2563eb', fg='white', activebackground='#1d4ed8',
              relief='flat', padx=16, pady=8, font=('Microsoft YaHei UI', 9, 'bold'), cursor='hand2').pack(side='right')
    if strategy_id == 'E': account_kind.trace_add('write', lambda *_: load())
    load()


def main():
    root = tk.Tk(); root.title('BTC 纸盘控制台')
    screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
    width, height = min(1280, screen_width - 40), min(720, screen_height - 80)
    root.geometry(f'{width}x{height}+20+20'); root.minsize(min(1120, width), min(650, height))
    root.grid_columnconfigure(0, weight=1)
    root.grid_rowconfigure(3, weight=1)
    root.iconbitmap(default=str(ROOT / 'frontend/public/btc-lab-logo.ico'))
    style = ttk.Style(root)
    try: style.theme_use('clam')
    except tk.TclError: pass
    style.configure('Strategy.Treeview', rowheight=36, font=('Microsoft YaHei UI', 9), background='#ffffff',
                    fieldbackground='#ffffff', foreground='#172b4d', borderwidth=0, relief='flat')
    style.configure('Strategy.Treeview.Heading', font=('Microsoft YaHei UI', 9, 'bold'), padding=(5, 10),
                    background='#edf2f8', foreground='#172b4d', relief='flat')
    style.configure('Accent.TButton', foreground='#155ac8', font=('Microsoft YaHei UI', 9, 'bold'))
    style.configure('Light.TCombobox', fieldbackground='#ffffff', background='#ffffff', foreground='#172b4d',
                    bordercolor='#d9e2ef', padding=6, arrowsize=12)
    style.map('Light.TCombobox', fieldbackground=[('readonly', '#ffffff')],
              foreground=[('readonly', '#172b4d')], selectbackground=[('readonly', '#ffffff')],
              selectforeground=[('readonly', '#172b4d')])
    style.map('Strategy.Treeview', background=[('selected', '#e8f0ff')], foreground=[('selected', '#102a55')])
    style.configure('Flat.TEntry', fieldbackground='#ffffff', foreground='#172b4d', bordercolor='#d9e2ef', padding=7)
    bg = '#f5f7fb'; navy = '#1e3a5f'; blue = '#2563eb'
    root.configure(bg=bg)

    status = tk.StringVar(value='')
    titlebar = tk.Frame(root, bg=navy, height=0); titlebar.grid(row=0, column=0, sticky='ew')
    titlebar.grid_propagate(False)

    state_snapshot = {'system': {}, 'front': None, 'node': None}
    system_bar = tk.Canvas(root, height=54, bg=bg, highlightthickness=0, bd=0)
    system_bar.grid(row=1, column=0, sticky='ew', padx=10, pady=(7, 5))
    status_label = tk.Label(root, textvariable=status, bg=bg, fg='#64748b',
                            font=('Microsoft YaHei UI', 8), anchor='w')
    def draw_system_icon(canvas, kind, x, y, color, tag=None):
        tags = (tag,) if tag else ()
        if kind == 'node':
            canvas.create_oval(x-4, y-6, x+4, y+2, outline=color, width=1.5, tags=tags)
            canvas.create_line(x, y+2, x, y+7, fill=color, width=1.5, tags=tags)
        elif kind == 'http':
            canvas.create_rectangle(x-6, y-5, x+6, y+5, outline=color, width=1.3, tags=tags)
            canvas.create_text(x, y, text='H', fill=color, font=('Arial', 7, 'bold'), tags=tags)
        elif kind == 'scheduler':
            canvas.create_rectangle(x-5, y-5, x+5, y+5, outline=color, width=1.3, tags=tags)
            canvas.create_line(x-5, y-1, x+5, y-1, fill=color, width=1.2, tags=tags)
            canvas.create_line(x-2, y-7, x-2, y-3, fill=color, width=1.3, tags=tags)
            canvas.create_line(x+2, y-7, x+2, y-3, fill=color, width=1.3, tags=tags)
        elif kind == 'market':
            canvas.create_line(x-6, y+5, x-6, y-5, x+6, y-5, fill=color, width=1.2, tags=tags)
            canvas.create_line(x-4, y+2, x-1, y-1, x+2, y+1, x+5, y-4, fill=color, width=1.7, tags=tags)
        elif kind == 'quote':
            canvas.create_polygon(x-6, y-4, x+1, y-4, x+6, y+1, x+1, y+6, x-6, y+6,
                                  fill='', outline=color, width=1.3, tags=tags)
            canvas.create_oval(x-3, y-2, x-1, y, fill=color, outline=color, tags=tags)
        elif kind == 'ws':
            canvas.create_arc(x-7, y-7, x+7, y+7, start=35, extent=110, style='arc', outline=color, width=1.3, tags=tags)
            canvas.create_arc(x-4, y-4, x+4, y+4, start=35, extent=110, style='arc', outline=color, width=1.4, tags=tags)
            canvas.create_oval(x-1, y+4, x+1, y+6, fill=color, outline=color, tags=tags)
    def draw_system_bar(event=None):
        system_bar.delete('all')
        w = max(100, system_bar.winfo_width()); h = 52
        draw_rounded_rect(system_bar, 1, 1, w - 1, h - 1, 12, '#ffffff', '#e2e8f0')
        badges = system_status_badges(state_snapshot['system'], state_snapshot['front'], state_snapshot['node'])
        if not state_snapshot['system']:
            badges = [{'icon': 'scheduler', 'label': '正在读取系统状态', 'tone': 'neutral', 'raw': 'UNKNOWN'}]
        font = tkfont.Font(root=root, family='Microsoft YaHei UI', size=9, weight='bold')
        x = 16; y = 12; chip_h = 28
        for index, badge in enumerate(badges):
            tone_bg, tone_fg = STATUS_TONES[badge['tone']]
            chip_w = font.measure(badge['label']) + 40
            tag = f"status-chip-{index}"
            draw_rounded_rect(system_bar, x, y, x + chip_w, y + chip_h, 9, tone_bg, tone_bg, tag)
            draw_system_icon(system_bar, badge['icon'], x + 15, y + chip_h / 2, tone_fg, tag)
            system_bar.create_text(x + 28, y + chip_h / 2, anchor='w', text=badge['label'], fill=tone_fg,
                                   font=font, tags=(tag,))
            if badge.get('raw') and badge.get('icon') == 'scheduler':
                system_bar.tag_bind(tag, '<Enter>', lambda e, raw=badge['raw']: show_scheduler_tip(e, raw))
                system_bar.tag_bind(tag, '<Leave>', hide_scheduler_tip)
            x += chip_w + 10
        updated = state_snapshot.get('updated') or '--:--:--'
        system_bar.create_line(w - 153, 12, w - 153, h - 12, fill='#e2e8f0')
        system_bar.create_text(w - 140, h / 2, anchor='w', text='◷  更新 ' + updated,
                               fill='#334155', font=('Microsoft YaHei UI', 9, 'bold'))
    scheduler_tip = {'window': None}
    def show_scheduler_tip(event, raw):
        hide_scheduler_tip()
        tip = tk.Toplevel(root); tip.overrideredirect(True); tip.configure(bg='#172b4d')
        tk.Label(tip, text='调度状态：' + str(raw), bg='#172b4d', fg='white',
                 font=('Microsoft YaHei UI', 9), padx=9, pady=5).pack()
        tip.geometry(f'+{event.x_root + 10}+{event.y_root + 12}')
        scheduler_tip['window'] = tip
    def hide_scheduler_tip(event=None):
        tip = scheduler_tip.get('window')
        if tip and tip.winfo_exists(): tip.destroy()
        scheduler_tip['window'] = None
    system_bar.bind('<Configure>', draw_system_bar)

    cards = tk.Frame(root, bg=bg); cards.grid(row=2, column=0, sticky='ew', padx=7, pady=(0, 7))
    stat_vars = {key: tk.StringVar(value='—') for key in ('total', 'running', 'positions', 'equity')}
    card_specs = (('total', '总策略数', '▱', '#2563eb', '#dbeafe'),
                  ('running', '运行中', '▶', '#16a34a', '#dcfce7'),
                  ('positions', '持仓中', '▣', '#d97706', '#ffedd5'),
                  ('equity', '账户总权益', '$', '#2563eb', '#dbeafe'))
    stat_cards = {}
    for index, (key, label, icon, color, icon_bg) in enumerate(card_specs):
        cards.grid_columnconfigure(index, weight=1, uniform='stat')
        card = tk.Canvas(cards, height=88, bg=bg, highlightthickness=0, bd=0)
        card.grid(row=0, column=index, sticky='ew', padx=6)
        stat_cards[key] = (card, label, icon, color, icon_bg)
        card.bind('<Configure>', lambda event, key=key: draw_stat_card(key))

    def draw_stat_card(key):
        card, label, icon, color, icon_bg = stat_cards[key]
        card.delete('all'); w = max(150, card.winfo_width()); h = 88
        draw_rounded_rect(card, 1, 1, w - 2, h - 2, 12, '#ffffff', '#e2e8f0')
        card.create_oval(16, 18, 66, 68, fill=icon_bg, outline=icon_bg)
        if key == 'total':
            for offset in (7, 0, -7):
                card.create_polygon(41, 34+offset, 52, 40+offset, 41, 46+offset, 30, 40+offset,
                                    fill='', outline=color, width=1.6)
        elif key == 'running':
            card.create_polygon(35, 32, 35, 54, 52, 43, fill=color, outline=color)
        elif key == 'positions':
            card.create_rectangle(31, 40, 51, 54, outline=color, width=2)
            card.create_rectangle(36, 34, 46, 41, outline=color, width=2)
            card.create_line(31, 45, 51, 45, fill=color, width=1.5)
        else:
            card.create_oval(31, 33, 51, 41, outline=color, width=2)
            card.create_arc(31, 37, 51, 47, start=180, extent=180, style='arc', outline=color, width=2)
            card.create_arc(31, 42, 51, 52, start=180, extent=180, style='arc', outline=color, width=2)
            card.create_arc(31, 47, 51, 57, start=180, extent=180, style='arc', outline=color, width=2)
        card.create_text(80, 31, anchor='w', text=label, fill='#64748b', font=('Microsoft YaHei UI', 9))
        card.create_text(80, 58, anchor='w', text=stat_vars[key].get(), fill=color,
                         font=('Microsoft YaHei UI', 19, 'bold'))

    def draw_stat_cards():
        for key in stat_cards: draw_stat_card(key)

    def draw_action_icon(canvas, kind, x, y, color):
        if kind == 'play':
            canvas.create_polygon(x-4, y-7, x-4, y+7, x+7, y, fill=color, outline=color)
        elif kind == 'pause':
            canvas.create_rectangle(x-6, y-7, x-2, y+7, fill=color, outline=color)
            canvas.create_rectangle(x+2, y-7, x+6, y+7, fill=color, outline=color)
        elif kind == 'pool':
            canvas.create_oval(x-7, y-6, x+5, y, outline=color, width=1.5)
            canvas.create_arc(x-7, y-3, x+5, y+5, start=180, extent=180, style='arc', outline=color, width=1.5)
            canvas.create_arc(x-7, y+1, x+5, y+9, start=180, extent=180, style='arc', outline=color, width=1.5)
            canvas.create_line(x+5, y-3, x+5, y+5, fill=color, width=1.5)
        elif kind == 'refresh':
            canvas.create_arc(x-7, y-7, x+7, y+7, start=35, extent=285, style='arc', outline=color, width=1.8)
            canvas.create_polygon(x+4, y-7, x+9, y-6, x+6, y-2, fill=color, outline=color)
        elif kind == 'external':
            canvas.create_rectangle(x-7, y-5, x+3, y+6, outline=color, width=1.4)
            canvas.create_line(x-1, y+2, x+7, y-6, fill=color, width=1.7)
            canvas.create_line(x+2, y-6, x+7, y-6, x+7, y-1, fill=color, width=1.5)

    def make_canvas_button(parent, text, command, width, kind='outline', icon=None):
        canvas = tk.Canvas(parent, width=width, height=38, bg=bg, highlightthickness=0, bd=0,
                           cursor='hand2')
        state = {'text': text, 'kind': kind, 'hover': False, 'pressed': False, 'enabled': True}
        palettes = {
            'primary': ('#2563eb', '#1d4ed8', '#ffffff', '#2563eb'),
            'secondary': ('#eef2f7', '#e2e8f0', '#334155', '#d8e1ed'),
            'outline': ('#ffffff', '#f1f5f9', '#334155', '#dbe3ef'),
            'selected': ('#eff6ff', '#dbeafe', '#1d4ed8', '#2563eb'),
            'segment': ('#f1f5f9', '#e2e8f0', '#334155', '#e2e8f0'),
        }
        def draw(event=None):
            canvas.delete('all')
            base, hover, fg, border = palettes[state['kind']]
            fill = hover if state['hover'] or state['pressed'] else base
            if not state['enabled']:
                fill, fg, border = '#f1f5f9', '#94a3b8', '#e2e8f0'
            draw_rounded_rect(canvas, 1, 1, width - 1, 37, 8, fill, border)
            button_font = ('Microsoft YaHei UI', 9, 'bold')
            if icon:
                text_font = tkfont.Font(root=root, family=button_font[0], size=button_font[1], weight=button_font[2])
                total = 22 + text_font.measure(state['text']) + 6
                left = (width - total) / 2
                draw_action_icon(canvas, icon, left + 8, 19, fg)
                canvas.create_text(left + 25, 19, anchor='w', text=state['text'], fill=fg, font=button_font)
            else:
                canvas.create_text(width / 2, 19, text=state['text'], fill=fg, font=button_font)
        def set_text(value): state['text'] = value; draw()
        def set_kind(value): state['kind'] = value; draw()
        def set_enabled(value): state['enabled'] = bool(value); draw()
        def enter(event): state['hover'] = True; draw()
        def leave(event): state['hover'] = False; state['pressed'] = False; draw()
        def activate(event):
            if not state['enabled']:
                return
            state['pressed'] = True; draw()
            state['pressed'] = False; draw()
            if command:
                command()
        canvas.bind('<Configure>', draw); canvas.bind('<Enter>', enter); canvas.bind('<Leave>', leave)
        canvas.bind('<Button-1>', activate)
        canvas.set_text = set_text; canvas.set_kind = set_kind; canvas.set_enabled = set_enabled
        return canvas

    content = tk.Frame(root, bg='#ffffff', highlightbackground='#e2e8f0', highlightthickness=1)
    content.grid(row=3, column=0, sticky='nsew', padx=10, pady=(0, 7))
    content.grid_rowconfigure(1, weight=1)
    content.grid_columnconfigure(0, weight=1)
    filterbar = tk.Frame(content, bg='white'); filterbar.grid(row=0, column=0, sticky='ew', padx=10, pady=8)
    category = tk.StringVar(value='全部')
    status_filter = tk.StringVar(value='全部状态')
    search = tk.StringVar()
    tab_buttons = {}
    tab_group = tk.Frame(filterbar, bg='#f1f5f9', padx=3, pady=3)
    tab_group.pack(side='left')
    for label, key, width in (('全部策略', '全部', 116), ('原有策略', '原有', 104), ('镜像策略', '镜像', 98), ('PA策略', 'PA', 94)):
        button = make_canvas_button(tab_group, label, lambda value=key: (category.set(value), render_rows()), width,
                                    'selected' if key == '全部' else 'segment')
        button.pack(side='left', padx=2); tab_buttons[key] = button
    ttk.Label(filterbar, text='状态', background='white', foreground='#64748b',
              font=('Microsoft YaHei UI', 9)).pack(side='left', padx=(13, 5))
    status_combo = ttk.Combobox(filterbar, textvariable=status_filter, state='readonly', width=15,
                                style='Light.TCombobox', font=('Microsoft YaHei UI', 9),
                                values=('全部状态', '运行中', '等待C开仓', '已启用待周期', '周期滞后', '已暂停', '未启用', '观察中', '保护待确认', '异常', '故障', '数据过期', '读取失败', '未知'))
    status_combo.pack(side='left'); status_combo.bind('<<ComboboxSelected>>', lambda event: render_rows())
    ttk.Label(filterbar, text='搜索', background='white', foreground='#64748b',
              font=('Microsoft YaHei UI', 9)).pack(side='left', padx=(13, 5))
    search_shell = tk.Frame(filterbar, bg='white', highlightbackground='#d8e1ed', highlightthickness=1)
    search_shell.pack(side='left', ipady=2)
    search_icon = tk.Canvas(search_shell, width=24, height=28, bg='white', highlightthickness=0, bd=0)
    search_icon.pack(side='left', padx=(5, 1))
    search_icon.create_oval(5, 5, 16, 16, outline='#94a3b8', width=1.7)
    search_icon.create_line(14, 14, 21, 21, fill='#94a3b8', width=1.8)
    search_entry = ttk.Entry(search_shell, textvariable=search, width=24, style='Flat.TEntry')
    search_entry.pack(side='left', padx=(0, 5), pady=1)
    search.trace_add('write', lambda *_: render_rows())

    cols = ('control', 'strategy_id', 'name', 'status', 'state', 'amount', 'leverage', 'equity', 'realized', 'unrealized', 'fees', 'action', 'cycle', 'detail')
    table_frame = tk.Frame(content, bg='white'); table_frame.grid(row=1, column=0, sticky='ew', padx=9, pady=(0, 9))
    table_frame.grid_rowconfigure(0, weight=1); table_frame.grid_columnconfigure(0, weight=1)
    table = ttk.Treeview(table_frame, columns=cols, show='headings', height=8, style='Strategy.Treeview')
    titles = ('启停', '策略ID', '策略名称', '状态', '持仓', '持仓金额', '杠杆', '权益', '已实现盈亏', '未实现盈亏', '手续费', '最近动作', '处理周期 / 同步时间（本地）', '策略详情')
    widths = (72, 68, 125, 100, 55, 92, 58, 86, 90, 90, 78, 146, 204, 82)
    for col, title, col_width in zip(cols, titles, widths):
        table.heading(col, text=title)
        table.column(col, width=col_width, minwidth=40 if col not in ('name', 'action', 'cycle') else 70,
                     anchor='e' if col in ('amount', 'equity', 'realized', 'unrealized', 'fees') else 'center', stretch=False)
    ybar = ttk.Scrollbar(table_frame, orient='vertical', command=table.yview)
    xbar = ttk.Scrollbar(table_frame, orient='horizontal', command=table.xview)
    table.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
    table.grid(row=0, column=0, sticky='nsew'); ybar.grid(row=0, column=1, sticky='ns'); xbar.grid(row=1, column=0, sticky='ew')
    table.tag_configure('stripe-even', background='#ffffff', foreground='#172b4d')
    table.tag_configure('stripe-odd', background='#f8fafc', foreground='#172b4d')
    table.tag_configure('row-hover', background='#eef5ff', foreground='#172b4d')
    busy = {'value': False}
    controls = {}
    cached_rows = {'value': {}}
    cell_labels = {}
    switch_widgets = {}
    status_widgets = {}
    detail_widgets = {}
    row_stripes = {}
    hovered_row = {'id': None}
    control_labels = {'RUNNING': '运行中', 'PAUSING': '未完成', 'PAUSED': '已暂停',
                      'OBSERVE_ONLY': '观察中', 'PROTECTION_UNCERTAIN': '保护待确认'}
    def render_rows():
        rows = cached_rows['value']
        selected = filter_console_rows(rows, category.get(), status_filter.get(), search.get())
        labels = {'全部': f"全部策略（{len(rows)}）",
                  '原有': f"原有策略（{sum(1 for key in rows if key in ('A','B','C','D','E'))}）",
                  '镜像': f"镜像策略（{sum(1 for key in rows if key == 'G')}）",
                  'PA': f"PA策略（{sum(1 for key in rows if key in PA_STRATEGY_IDS)}）"}
        for key, button in tab_buttons.items():
            button.set_text(labels[key])
            button.set_kind('selected' if category.get() == key else 'segment')
        for item in table.get_children(): table.delete(item)
        row_stripes.clear()
        for index, row_id in enumerate(selected):
            row = rows[row_id]; controls[row_id] = row
            view = strategy_view(row_id, row)
            stripe = 'stripe-even' if index % 2 == 0 else 'stripe-odd'
            row_stripes[row_id] = stripe
            vals = ('', view['strategy_id'], view['name'], '', view['position'],
                    row.get('amount') or '0.00', row.get('leverage') or '\u672a\u77e5', money_label(row.get('equity')),
                    '', '',
                    money_label(row.get('fees')), view['action'], cycle_display(row), '')
            table.insert('', 'end', iid=row_id, tags=(stripe,), values=vals)
        root.after_idle(draw_overlays)
        summary = console_summary(rows)
        for key, value in summary.items(): stat_vars[key].set(value if key == 'equity' else str(value))
        draw_stat_cards()

    def draw_cell_labels():
        for label in cell_labels.values(): label.destroy()
        cell_labels.clear()
        for row_id in table.get_children():
            row = cached_rows['value'][row_id]
            values = {8: (money_label(row.get('realized')), '#16a34a' if _positive(row.get('realized')) else '#dc2626' if _negative(row.get('realized')) else '#172b4d', 'e'),
                      9: (money_label(row.get('unrealized')), '#14804a' if _positive(row.get('unrealized')) else '#c4322b' if _negative(row.get('unrealized')) else '#20344f', 'e')}
            for column, (value, foreground, anchor) in values.items():
                box = table.bbox(row_id, f'#{column + 1}')
                if not box: continue
                x, y, cell_width, cell_height = box
                label = tk.Label(table, text=value, bg=row_background(row_id),
                                 fg=foreground, anchor=anchor, font=('Microsoft YaHei UI', 9), padx=6)
                label.place(x=x, y=y, width=cell_width, height=cell_height)
                label.bind('<Button-1>', lambda event, key=row_id: open_row_details(key))
                label.bind('<Enter>', lambda event, key=row_id: set_hovered_row(key))
                label.bind('<Leave>', lambda event: root.after_idle(update_hover_from_pointer))
                cell_labels[(row_id, column)] = label

    def draw_status_badges():
        for widget in status_widgets.values(): widget.destroy()
        status_widgets.clear()
        for row_id in table.get_children():
            row = cached_rows['value'][row_id]
            appearance = strategy_status_appearance(strategy_view(row_id, row)['status'])
            tone_bg, tone_fg = STATUS_TONES[appearance['tone']]
            box = table.bbox(row_id, '#4')
            if not box: continue
            x, y, cell_width, cell_height = box
            badge_width = min(cell_width - 8, max(66, tkfont.Font(root=root, family='Microsoft YaHei UI', size=8).measure(appearance['icon'] + ' ' + (row.get('status_label') or '状态未知')) + 18))
            badge_height = 24
            chip = tk.Canvas(table, width=badge_width, height=badge_height, bg=row_background(row_id),
                             highlightthickness=0, bd=0, cursor='hand2')
            chip.place(x=x + max(2, (cell_width - badge_width) // 2), y=y + max(0, (cell_height - badge_height) // 2))
            draw_rounded_rect(chip, 0, 0, badge_width - 1, badge_height - 1, 9, tone_bg, tone_bg)
            chip.create_text(badge_width / 2, badge_height / 2,
                             text=appearance['icon'] + '  ' + (row.get('status_label') or '状态未知'),
                             fill=tone_fg, font=('Microsoft YaHei UI', 8, 'bold'))
            chip.bind('<Button-1>', lambda event, key=row_id: open_row_details(key))
            chip.bind('<Enter>', lambda event, key=row_id: set_hovered_row(key))
            chip.bind('<Leave>', lambda event: root.after_idle(update_hover_from_pointer))
            status_widgets[row_id] = chip

    def row_background(row_id):
        if row_id in table.selection(): return '#e8f0ff'
        if hovered_row['id'] == row_id: return '#eef5ff'
        return '#f8fafc' if row_stripes.get(row_id) == 'stripe-odd' else '#ffffff'

    def set_hovered_row(row_id):
        old = hovered_row['id']
        if old == row_id: return
        hovered_row['id'] = row_id
        for key in (old, row_id):
            if key and key in table.get_children():
                tags = [tag for tag in table.item(key, 'tags') if tag != 'row-hover']
                if key == row_id: tags.append('row-hover')
                table.item(key, tags=tuple(tags))
        root.after_idle(draw_overlays)

    def update_hover_from_pointer(event=None):
        x, y = table.winfo_pointerx() - table.winfo_rootx(), table.winfo_pointery() - table.winfo_rooty()
        set_hovered_row(table.identify_row(y) if 0 <= x < table.winfo_width() else None)

    def draw_switches():
        for widget in switch_widgets.values(): widget.destroy()
        switch_widgets.clear()
        for row_id in table.get_children():
            row = cached_rows['value'][row_id]
            state = console_control_state(row_id, row)
            box = table.bbox(row_id, '#1')
            if not box: continue
            x, y, cell_width, cell_height = box
            width, height = 46, 22
            switch = tk.Canvas(table, width=width, height=height, bg='white',
                               highlightthickness=0, bd=0,
                               cursor='hand2' if state != 'disabled' else 'arrow')
            switch.place(x=x + max(0, (cell_width - width) // 2),
                         y=y + max(0, (cell_height - height) // 2))
            switch.configure(bg=row_background(row_id))
            track = '#3584ff' if state == 'on' else '#e0e5ec' if state == 'disabled' else '#c7ced8'
            switch.create_line(10, 11, 36, 11, fill=track, width=18, capstyle='round')
            knob_x = 27 if state == 'on' else 1
            switch.create_oval(knob_x, 2, knob_x + 18, 20, fill='#ffffff', outline='#d4dce7', width=1)
            if state != 'disabled':
                switch.bind('<Button-1>', lambda event, key=row_id: strategy_click(key))
            switch.bind('<Enter>', lambda event, key=row_id: set_hovered_row(key))
            switch.bind('<Leave>', lambda event: root.after_idle(update_hover_from_pointer))
            switch_widgets[row_id] = switch

    def draw_detail_buttons():
        for widget in detail_widgets.values(): widget.destroy()
        detail_widgets.clear()
        for row_id in table.get_children():
            box = table.bbox(row_id, '#14')
            if not box: continue
            x, y, cell_width, cell_height = box
            width, height = min(62, max(48, cell_width - 8)), 25
            button = tk.Canvas(table, width=width, height=height, bg=row_background(row_id),
                               highlightthickness=0, bd=0, cursor='hand2')
            button.place(x=x + max(2, (cell_width - width) // 2),
                         y=y + max(0, (cell_height - height) // 2))
            draw_rounded_rect(button, 0, 0, width - 1, height - 1, 7, '#2563eb', '#2563eb')
            button.create_text(width / 2, height / 2, text='◉ 查看', fill='white',
                               font=('Microsoft YaHei UI', 8, 'bold'))
            button.bind('<Button-1>', lambda event, key=row_id: show_rule_details(key))
            button.bind('<Enter>', lambda event, key=row_id: set_hovered_row(key))
            button.bind('<Leave>', lambda event: root.after_idle(update_hover_from_pointer))
            detail_widgets[row_id] = button

    def draw_overlays():
        draw_cell_labels()
        draw_status_badges()
        draw_switches()
        draw_detail_buttons()

    def _positive(value):
        try: return Decimal(str(value).replace(',', '')) > 0
        except (InvalidOperation, TypeError, ValueError): return False

    def _negative(value):
        try: return Decimal(str(value).replace(',', '')) < 0
        except (InvalidOperation, TypeError, ValueError): return False

    def on_yview(*args):
        table.yview(*args); root.after_idle(draw_overlays)
    def on_xview(*args):
        table.xview(*args); root.after_idle(draw_overlays)
    ybar.configure(command=on_yview); xbar.configure(command=on_xview)
    table.configure(yscrollcommand=lambda first, last: (ybar.set(first, last), root.after_idle(draw_overlays)),
                    xscrollcommand=lambda first, last: (xbar.set(first, last), root.after_idle(draw_overlays)))
    table.bind('<MouseWheel>', lambda event: (table.yview_scroll(int(-event.delta / 120), 'units'), root.after_idle(draw_overlays)))
    table.bind('<Configure>', lambda event: root.after_idle(draw_overlays), add='+')
    table.bind('<Motion>', lambda event: set_hovered_row(table.identify_row(event.y)))
    table.bind('<Leave>', update_hover_from_pointer)
    table.bind('<<TreeviewSelect>>', lambda event: root.after_idle(draw_overlays))

    refreshing = {'value': False}
    def refresh():
        if refreshing['value']:
            return
        refreshing['value'] = True
        status.set('\u6b63\u5728\u5237\u65b0\u72b6\u6001\u2026')
        status_label.configure(fg='#2563eb')
        def worker():
            try:
                s, rows = snapshot()
                rows = keep_cached_rows(cached_rows['value'], rows)
                try: front = fetch('/', 5175)
                except Exception: front = '不可用'
                node = node_label()
                def show():
                    updated = datetime.now().strftime('%H:%M:%S')
                    state_snapshot.update({'system': s, 'front': front, 'node': node,
                                           'updated': updated})
                    draw_system_bar()
                    status.set(f'\u72b6\u6001\u5df2\u66f4\u65b0 \u00b7 {updated}')
                    status_label.configure(fg='#166534')
                    refreshing['value'] = False
                    cached_rows['value'] = rows
                    controls.clear()
                    table.state(['!disabled'])
                    render_rows()
                    refresh_node_buttons()
                root.after(0, show)
            except Exception as exc:
                msg = str(exc)
                def failed():
                    status.set(f"状态读取失败 · 保留上次成功数据（{datetime.now().strftime('%H:%M:%S')}）：{msg}")
                    status_label.configure(fg='#b42318')
                    refreshing['value'] = False
                root.after(0, failed)
        threading.Thread(target=worker, daemon=True).start()
    def launch():
        if busy['value']: return
        busy['value'] = True
        status.set('正在检查系统及策略状态……')
        status_label.configure(fg='#1d4ed8')
        def worker():
            try:
                msg = recover()
                def show_ok():
                    headline = next((line for line in msg.splitlines() if line.startswith('检查 ')), msg.splitlines()[0])
                    status.set(f"{headline}（{datetime.now().strftime('%H:%M:%S')}）")
                    status_label.configure(fg='#15803d')
                    messagebox.showinfo('一键启动 / 恢复结果', msg)
                root.after(0, show_ok)
            except Exception as exc:
                msg = str(exc)
                def show_err():
                    status.set(f"恢复检查未完成：{msg}")
                    status_label.configure(fg='#b42318')
                    messagebox.showerror('未完成', msg)
                root.after(0, show_err)
            finally:
                busy['value'] = False; root.after(0, refresh)
        threading.Thread(target=worker, daemon=True).start()
    def pause():
        if busy['value']: return
        if not messagebox.askyesno('暂停前后端', '暂停后纸盘调度和持仓止损监控将停止，持仓不会自动平仓。确定暂停吗？'):
            return
        busy['value'] = True; status.set('正在暂停前后端…')
        def worker():
            try:
                ports = listeners()
                for p in ports:
                    if p['port'] == 8012 and (p.get('pid') != _tracked_pid('backend') or str(p.get('exe') or '').lower() != 'python'):
                        raise RuntimeError('8012由非V3进程占用，未停止')
                    if p['port'] == 5175 and (p.get('pid') != _tracked_pid('frontend') or str(p.get('exe') or '').lower() != 'node'):
                        raise RuntimeError('5175由非V3前端进程占用，未停止')
                stopped = subprocess.run(
                    ['powershell.exe', '-NoProfile', '-File', str(ROOT / 'scripts/stop_lab.ps1')],
                    cwd=ROOT, creationflags=NO_WINDOW, timeout=45,
                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                )
                if stopped.returncode != 0:
                    detail = (stopped.stderr or stopped.stdout or '').strip()
                    if not detail:
                        detail = f'停止脚本退出码：{stopped.returncode}'
                    raise RuntimeError(detail[-1200:])
                for _ in range(20):
                    if not listeners(): break
                    time.sleep(0.5)
                else:
                    raise RuntimeError('端口仍在监听，请检查进程；未报告暂停成功')
                def done():
                    controls.clear()
                    status.set('前后端已暂停；纸盘持仓和止损不受实时监控。')
                    status_label.configure(fg='#b42318')
                    messagebox.showinfo('已暂停', '前后端已暂停。纸盘持仓未自动平仓。')
                root.after(0, done)
            except Exception as exc:
                msg = str(exc); root.after(0, lambda: messagebox.showerror('暂停未完成', msg))
            finally:
                busy['value'] = False
        threading.Thread(target=worker, daemon=True).start()
    def strategy_click(agent):
        row = controls.get(agent)
        if agent == 'D':
            if not row or busy['value']:
                return
            enabled = bool(row.get('enabled'))
            title = '暂停D策略' if enabled else '启动D策略'
            action = ('停止新增开仓，并继续监控已有持仓退出' if enabled
                      else '允许D策略从后续自然周期开始判断和模拟开仓')
            if not messagebox.askyesno(title, f'D策略：{action}。确定继续？'):
                return
            busy['value'] = True
            def d_worker():
                try:
                    result = set_d_enabled(not enabled)
                    if result.get('enabled') is not (not enabled):
                        raise RuntimeError('D策略状态未能确认，请刷新状态')
                    msg = 'D策略已启动' if not enabled else 'D策略已暂停；已有持仓继续监控退出'
                    root.after(0, lambda: messagebox.showinfo('D策略控制', msg))
                except Exception as exc:
                    msg = str(exc); root.after(0, lambda: messagebox.showerror('操作未完成', msg))
                finally:
                    busy['value'] = False; root.after(0, refresh)
            threading.Thread(target=d_worker, daemon=True).start()
            return
        if agent in PA_STRATEGY_IDS:
            if not row or row.get('status_label') in ('读取失败', '数据过期', '未登记') or busy['value']:
                return
            enabled = bool(row.get('enabled'))
            raw = row.get('raw') or {}
            version = raw.get('active_version') or raw.get('position_version')
            if not version:
                messagebox.showerror('操作失败', '策略池未提供可控制的活动版本。'); return
            title = '暂停策略' if enabled else '启动策略'
            action = '停止新开仓并保留现有持仓管理' if enabled else '开始后续周期的自动模拟交易'
            if not messagebox.askyesno(title, f"{row.get('name')}（{agent}）：{action}。确定继续？"):
                return
            busy['value'] = True
            def pa_worker():
                try:
                    set_pa_enabled(agent, version, not enabled)
                except Exception as exc:
                    msg = str(exc); root.after(0, lambda: messagebox.showerror('操作未完成', msg))
                finally:
                    busy['value'] = False; root.after(0, refresh)
            threading.Thread(target=pa_worker, daemon=True).start()
            return
        if agent not in ('A', 'B', 'C', 'E') or not row: return
        current = row.get('control')
        action = 'resume' if current == 'PAUSED' else 'pause' if current in ('RUNNING', 'PAUSING') else None
        if not action or busy['value']: return
        if action == 'pause' and not messagebox.askyesno('暂停策略', f'将阻止 {agent} 正式盘新开仓，并按纸盘报价强制平掉正式盘持仓。影子盘继续独立运行。确定继续吗？'):
            return
        busy['value'] = True
        def worker():
            try:
                latest = fetch('/api/v1/strategy-control').get(agent, {}).get('state')
                if latest != current:
                    raise RuntimeError('策略状态已变化，请刷新后重试')
                req = urllib.request.Request(f'http://127.0.0.1:8012/api/v1/strategy-control/{agent}/{action}', method='POST')
                with OPENER.open(req, timeout=30) as response: result = json.load(response)
                msg = f"{agent} 状态：{result.get('state')}；正式：{result.get('formal')}；影子：{result.get('shadow')}"
                root.after(0, lambda: messagebox.showinfo('策略控制', msg))
            except Exception as exc:
                msg = str(exc); root.after(0, lambda: messagebox.showerror('操作未完成', msg))
            finally:
                busy['value'] = False; root.after(0, refresh)
        threading.Thread(target=worker, daemon=True).start()
    def open_trade_details(agent):
        row = controls.get(agent)
        if row:
            open_strategy_details(root, agent, row)
    def row_click(event):
        if table.identify_region(event.x, event.y) != 'cell': return
        agent = table.identify_row(event.y)
        if not agent: return
        column = table.identify_column(event.x)
        if column == '#14':
            target = detail_click_target(agent, column)
            if target: return show_rule_details(target)
        if column == '#1':
            strategy_click(agent)
        elif agent == 'G':
            row = controls.get(agent)
            if row: open_strategy_details(root, agent, row)
        elif agent in PA_STRATEGY_IDS:
            row = controls.get(agent)
            if row: open_strategy_details(root, agent, row)
        elif agent in ('A', 'B', 'C', 'D', 'E'):
            open_trade_details(agent)
    def open_row_details(agent):
        if agent == 'G':
            row = controls.get(agent)
            if row: open_strategy_details(root, agent, row)
        elif agent in PA_STRATEGY_IDS:
            row = controls.get(agent)
            if row: open_strategy_details(root, agent, row)
        elif agent in ('A', 'B', 'C', 'D', 'E'):
            open_trade_details(agent)
    def show_rule_details(agent):
        row = cached_rows['value'].get(agent)
        if row is not None:
            open_strategy_rule_details(root, agent, row)
        return 'break'
    table.bind('<Button-1>', row_click)
    bar = tk.Frame(root, bg=bg); bar.grid(row=4, column=0, sticky='ew', pady=(1, 4))
    bar.grid_columnconfigure(0, weight=1); bar.grid_columnconfigure(1, weight=1)
    button_group = tk.Frame(bar, bg=bg); button_group.grid(row=0, column=0, columnspan=2)
    make_canvas_button(button_group, '一键启动 / 恢复', launch, 180, 'primary', 'play').pack(side='left', padx=5)
    make_canvas_button(button_group, '暂停前后端', pause, 154, 'secondary', 'pause').pack(side='left', padx=5)
    node_buttons = {}
    def choose_node(number):
        if number == 1:
            messagebox.showinfo('行情节点', '当前使用 Binance USDⓈ-M BTCUSDT 公开行情节点1。')
            return
        messagebox.showwarning(
            '备用行情节点未配置',
            'V3部署副本没有第二个Binance REST/WebSocket节点地址；配置前不会切换旧Alpaca凭证或中断当前服务。',
        )
    node_buttons[1] = make_canvas_button(button_group, '节点 1', lambda: choose_node(1), 83, 'segment')
    node_buttons[2] = make_canvas_button(button_group, '备用未配置', lambda: choose_node(2), 110, 'segment')
    node_buttons[1] = make_canvas_button(button_group, '节点 1', lambda: choose_node(1), 83, 'segment')
    node_buttons[2] = make_canvas_button(button_group, '备用未配置', lambda: choose_node(2), 110, 'segment')
    node_buttons[1] = make_canvas_button(button_group, '节点 1', lambda: choose_node(1), 83, 'segment')
    node_buttons[2] = make_canvas_button(button_group, '节点 2', lambda: choose_node(2), 83, 'segment')
    node_buttons[1].pack(side='left', padx=(12, 2)); node_buttons[2].pack(side='left', padx=2)
    make_canvas_button(button_group, '策略池', lambda: open_strategy_pool(root), 112, 'outline', 'pool').pack(side='left', padx=5)
    make_canvas_button(button_group, '策略研究', lambda: __import__('scripts.research_v2_window', fromlist=['open_research_center']).open_research_center(root), 116, 'outline', 'pool').pack(side='left', padx=5)
    make_canvas_button(button_group, '刷新状态', refresh, 126, 'outline', 'refresh').pack(side='left', padx=5)
    make_canvas_button(button_group, '打开K线', lambda: __import__('scripts.kline_window',fromlist=['open_kline']).open_kline(root), 124, 'outline', 'external').pack(side='left', padx=5)
    def live_paper_rows():
        return build_live_paper_rows(cached_rows['value'])

    make_canvas_button(button_group, '实盘跟单', lambda: __import__('scripts.live_copy_window', fromlist=['main']).main(root, paper_rows=live_paper_rows), 108, 'primary', 'external').pack(side='left', padx=5)
    def refresh_node_buttons():
        selected = node_label()
        for number, button in node_buttons.items():
            button.set_kind('selected' if selected == number else 'segment')
    footer = tk.Frame(root, bg=bg); footer.grid(row=5, column=0, padx=14, pady=(0, 7), sticky='ew')
    tk.Label(footer, text='ⓘ  点击策略行查看成交明细；点击启停列控制策略。纸盘 USD_RESEARCH；持仓金额是开仓名义金额，并非保证金。',
             bg=bg, fg='#64748b', anchor='w', font=('Microsoft YaHei UI', 8)).pack(side='left', fill='x', expand=True)
    def show_ops_status():
        popup = tk.Toplevel(root); popup.title('系统运行与备份状态'); popup.geometry('760x520')
        popup.configure(bg='#f5f7fb'); popup.transient(root); popup.lift(); popup.focus_force()
        text = tk.Text(popup, wrap='word', bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 9),
                       relief='flat', padx=14, pady=12)
        text.pack(fill='both', expand=True, padx=12, pady=12)
        text.insert('end', '正在读取后端运行状态与本机备份清单…'); text.configure(state='disabled')
        def worker():
            try:
                system = fetch('/api/v1/system/status')
                health = fetch('/api/v1/health')
                pool_items = fetch('/api/v1/strategy-pool').get('items', [])
                inventory_path = ROOT / 'logs' / 'ledger-backups' / 'inventory.json'
                inventory = json.loads(inventory_path.read_text(encoding='utf-8')) if inventory_path.exists() else {}
                summary = operations_status_summary(system, health, pool_items, inventory,
                                                    bool(os.getenv('BTC_LAB_BACKUP_SECONDARY')))
                content = '\n'.join(f'{key}：{value}' for key, value in summary.items())
            except Exception as exc:
                content = f'状态读取失败：{exc}\n本地数据保留；请检查8012及备份清单。'
            def render():
                if not popup.winfo_exists(): return
                text.configure(state='normal'); text.delete('1.0', 'end'); text.insert('end', content); text.configure(state='disabled')
            root.after(0, render)
        threading.Thread(target=worker, daemon=True).start()
    ttk.Button(footer, text='系统运行与备份', command=show_ops_status).pack(side='right', padx=(8, 0))
    status_label.pack(in_=footer, side='right', anchor='e', padx=(10, 0))
    refresh_node_buttons()

    root.after(500, lambda: style_native_caption(root))
    def tick(): refresh(); root.after(15000, tick)
    tick(); root.mainloop()


if __name__ == '__main__':
    if '--check' in sys.argv:
        s, rows = snapshot(); print(json.dumps({'scheduler': s.get('scheduler'), 'strategies': rows}, ensure_ascii=False))
    else:
        main()

