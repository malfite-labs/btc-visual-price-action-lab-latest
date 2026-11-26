"""Unified dual-environment (Demo & Production) native copy-trading center."""
import ctypes
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk
from uuid import uuid4

from scripts.live_copy_env import (
    DEMO_ENV,
    MAX_COPY_LEVERAGE,
    PROD_ENV,
    VALID_ENVS,
    config_ledger_dir,
    copy_config_payload,
    env_paths,
    list_production_accounts,
    make_exchange,
)
from scripts.local_dashboard import build_live_paper_rows, strategy_display_name, strategy_sort_key

BEIJING = timezone(timedelta(hours=8))

PALETTE = {
    'bg': '#f4f7fc',
    'header_bg': '#e4eefc',
    'header_border': '#cdddf6',
    'card_bg': '#ffffff',
    'card_border': '#d6e2f5',
    'info_bg': '#eef5ff',
    'info_border': '#c7dcf8',
    'text': '#13284c',
    'muted': '#5b6f8f',
    'primary': '#1d5ef0',
    'primary_hover': '#164bc4',
    'accent_red': '#c62828',
    'gain': '#15803d',
    'loss': '#b91c1c',
    'selected_bg': '#dbeafe',
    'selected_fg': '#0f2447',
    'pill_green_bg': '#dcfce7',
    'pill_green_fg': '#15803d',
    'pill_blue_bg': '#dbeafe',
    'pill_blue_fg': '#1d4ed8',
    'pill_amber_bg': '#fef3c7',
    'pill_amber_fg': '#b45309',
    'pill_gray_bg': '#f1f5f9',
    'pill_gray_fg': '#64748b',
}


def money(value):
    if value is None:
        return '—'
    try:
        rounded = Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        return f'{rounded if rounded else abs(rounded):,.2f} USDT'
    except (InvalidOperation, ValueError, TypeError):
        return '—'


def pnl_money(value):
    if value is None:
        return '—'
    try:
        rounded = Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        if rounded > 0:
            return f'+{rounded:,.2f} USDT'
        return f'{rounded if rounded else abs(rounded):,.2f} USDT'
    except (InvalidOperation, ValueError, TypeError):
        return '—'


def pnl_tag(value):
    return 'gain' if value is not None and value > 0 else 'loss' if value is not None and value < 0 else 'flat'


def pnl_color(value):
    return {'gain': PALETTE['gain'], 'loss': PALETTE['loss'], 'flat': PALETTE['text']}[pnl_tag(value)]


def color_rows(tree):
    for name in ('gain', 'loss', 'flat'):
        tree.tag_configure(name, foreground={'gain': PALETTE['gain'], 'loss': PALETTE['loss'], 'flat': PALETTE['text']}[name])


def split_strategy_label(sid, rows=None):
    """Split unified display label into (short_code, display_name) for 2-column table."""
    label = rows[sid] if (rows and sid in rows) else strategy_display_name(sid)
    if ' · ' in label:
        code, name = label.split(' · ', 1)
        return code, name
    return label, label


event_reasons = {
    'MANUAL_PAUSE': '手动暂停新开仓',
    'PAUSED': '暂停来源未记录',
    'STOP_PENDING': '正在安全停止',
    'STARTUP_RECONCILIATION': '启动对账',
    'NETWORK_RECOVERY': '网络恢复对账',
    'BUDGET_LIMIT': '超出预算',
    'FILL_OUTSIDE_60_SECOND_WINDOW': '成交超出 60 秒时限或时间超前',
    'PAPER_POSITION_ALREADY_CLOSED': '纸盘关联仓位已平仓',
    'ACTIVE_FOLLOWER_POSITION': '已有活动跟单仓位',
    'MINIMUM_ORDER_REQUIREMENTS': '未满足最小委托要求',
    'NO_ACTIVE_LINKED_POSITION': '没有活动关联仓位',
    'NO_ASSOCIATED_POSITION': '没有关联仓位',
    'UNSUPPORTED_EXECUTION_ACTION:PARTIAL_CLOSE': '当前执行器未执行部分退出',
    'ORIGINAL_OPEN_NOT_FOLLOWED': '原开仓未跟单，无需平仓',
}


def paper_event_label(fill):
    action = {'OPEN': '开', 'FULL_CLOSE': '平', 'PARTIAL_CLOSE': '部分平', 'ADD': '加'}
    return action.get(fill['action'], fill['action']) + {'LONG': '多', 'SHORT': '空'}.get(fill['direction'], '')


def demo_copy_data(directory, *, chart=False):
    """Read exact fills and paper links scoped to one configuration."""
    data = dict(active=[], trades=[], triggers=[], chart=[], today=None, total=None, action='等待成交')
    ledger = directory / 'c-v31-demo-execution.sqlite3'
    links, events = {}, {}
    inbox = directory / 'demo-execution-inbox.sqlite3'
    if inbox.is_file():
        with closing(sqlite3.connect(inbox.as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
            has_details = db.execute("SELECT 1 FROM sqlite_master WHERE name='skipped_event_details'").fetchone()
            detail_rows = dict(
                (event, (reason, stamp))
                for event, reason, stamp in db.execute('SELECT event,reason,decided_at FROM skipped_event_details')
            ) if has_details else {}
            links = {
                json.loads(raw)['client_order_id']: trade
                for trade, raw in db.execute('SELECT trade,intent FROM linked_orders')
            }
            for event, raw, result, plan, status in db.execute(
                """SELECT i.event_id,i.payload,e.result,i.result,i.status FROM inbox i
                   LEFT JOIN executed_events e ON e.event=i.event_id ORDER BY i.cursor"""
            ):
                row = json.loads(raw)
                plan_result = json.loads(plan or '{}')
                reason, _ = detail_rows.get(event, (plan_result.get('reason'), None))
                if (
                    result == 'SKIPPED'
                    and reason == 'NO_ASSOCIATED_POSITION'
                    and row['action'] in ('FULL_CLOSE', 'PARTIAL_CLOSE')
                    and row['trade_id'] not in links.values()
                ):
                    reason = 'ORIGINAL_OPEN_NOT_FOLLOWED'
                if result == 'SKIPPED_MIN_ORDER' and not reason:
                    reason = 'MINIMUM_ORDER_REQUIREMENTS'
                if result == 'SKIPPED' and not reason:
                    reason = '旧版本未记录原因'
                data['triggers'].append((event, row, status, result, reason))
                if row['action'] == 'OPEN' and result in ('PROTECTED', 'FOLLOW_PAPER', 'RECOVERED'):
                    events[row['trade_id'], '开仓'] = event.split(':', 1)[-1]
                if row['action'] == 'FULL_CLOSE' and result == 'CLOSED':
                    events[row['trade_id'], '平仓'] = event.split(':', 1)[-1]
    if not ledger.is_file():
        return data
    with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
        for client_id, state, raw in db.execute('SELECT id,state,evidence FROM cycles ORDER BY rowid'):
            e = json.loads(raw)
            trade = links.get(client_id)
            direction = e.get('intent', {}).get('direction')
            qty = Decimal(e.get('filled', '0'))
            if state in ('FOLLOW_PAPER', 'PROTECTED', 'PARTIAL_PROTECTED') and qty > 0 and trade:
                data['active'].append((direction, qty, Decimal(e['average'])))
                data['action'] = f"已开仓 #{e.get('order_id', '—')}"
            elif state not in ('CLOSED', 'NO_FILL'):
                data['action'] = '执行状态待核对'
            for effect, receipt_key in [('开仓', 'entry_trades'), ('平仓', 'exit_trades')]:
                groups = {}
                for fill in e.get(receipt_key, []):
                    if fill.get('orderId') is not None and fill.get('time') is not None:
                        key = (fill['orderId'], fill['time']) if chart else (fill['orderId'], None)
                        groups.setdefault(key, []).append(fill)
                for index, ((oid, _), fills) in enumerate(
                    sorted(groups.items(), key=lambda pair: max(int(fill['time']) for fill in pair[1]))
                ):
                    q = sum(Decimal(x['qty']) for x in fills)
                    amount = sum(Decimal(x['qty']) * Decimal(x['price']) for x in fills)
                    fee = (
                        sum(Decimal(x['commission']) for x in fills)
                        if all(x.get('commissionAsset') == 'USDT' for x in fills)
                        else None
                    )
                    stamp = datetime.fromtimestamp(max(int(x['time']) for x in fills) / 1000, BEIJING)
                    net = e.get('net_pnl_before_funding_usdt') if effect == '平仓' and index == len(groups) - 1 else None
                    if chart and trade and direction in ('LONG', 'SHORT'):
                        opening = effect == '开仓'
                        side = 'BUY' if opening == (direction == 'LONG') else 'SELL'
                        if all(
                            f['side'] == side and f.get('symbol') == 'BTCUSDT' and f.get('positionSide') in (direction, 'BOTH')
                            for f in fills
                        ):
                            data['chart'].append(
                                dict(
                                    paper_trade_id=trade,
                                    order_id=str(oid),
                                    fill_timestamp=stamp.isoformat(),
                                    action='OPEN' if opening else 'CLOSE',
                                    position_side=direction,
                                    execution_side=side,
                                    filled_price=str(amount / q),
                                    filled_quantity=str(q),
                                    filled_notional_usdt=str(amount),
                                    fee_usdt=None if fee is None else str(fee),
                                    realized_pnl_usdt=str(sum(Decimal(f['realizedPnl']) for f in fills))
                                    if all(f.get('realizedPnl') is not None for f in fills)
                                    else None,
                                    net_pnl_before_funding_usdt=net,
                                )
                            )
                    if net is not None:
                        net = Decimal(net)
                        data['total'] = (data['total'] or Decimal(0)) + net
                        if stamp.date() == datetime.now(BEIJING).date():
                            data['today'] = (data['today'] or Decimal(0)) + net
                    data['trades'].append(
                        (
                            stamp,
                            effect,
                            fills[0]['side'],
                            direction,
                            q,
                            amount / q,
                            amount,
                            fee,
                            net,
                            events.get((trade, effect), '—'),
                            oid,
                        )
                    )
                    if effect == '平仓':
                        data['action'] = f'已平仓 #{oid}'
    data['trades'].sort(key=lambda row: row[0], reverse=True)
    return data


ROOT = env_paths(DEMO_ENV).root
CONFIG = ROOT / 'copy-v2-draft.json'
RUNTIME = env_paths(DEMO_ENV).runtime


def main(owner=None, *, paper_rows=None, initial_env=DEMO_ENV, env_roots=None):
    if paper_rows is None:
        paper_rows = build_live_paper_rows
    existing = getattr(owner, '_copy_v2_window', None)
    if existing is not None and existing.winfo_exists():
        existing.deiconify()
        existing.lift()
        existing.focus_set()
        return existing

    root = tk.Tk() if owner is None else tk.Toplevel(owner)
    if owner is not None:
        owner._copy_v2_window = root
    root.title('BTC 实盘跟单中心')
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    win_w, win_h = min(1360, max(1060, sw - 80)), min(840, max(680, sh - 100))
    root.geometry(f'{win_w}x{win_h}')
    root.minsize(min(1020, sw - 40), min(640, sh - 80))
    root.configure(bg=PALETTE['bg'])

    scaling = float(root.tk.call('tk', 'scaling'))
    row_height = max(32, int(scaling * 24))
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('TFrame', background=PALETTE['bg'])
    style.configure('Card.TFrame', background=PALETTE['card_bg'])
    style.configure('Info.TFrame', background=PALETTE['info_bg'])
    style.configure('TLabel', background=PALETTE['bg'], foreground=PALETTE['text'], font=('Microsoft YaHei UI', 9))
    style.configure('Card.TLabel', background=PALETTE['card_bg'], foreground=PALETTE['text'], font=('Microsoft YaHei UI', 9))
    style.configure('Muted.TLabel', background=PALETTE['card_bg'], foreground=PALETTE['muted'], font=('Microsoft YaHei UI', 8))
    style.configure('Info.TLabel', background=PALETTE['info_bg'], foreground=PALETTE['text'], font=('Microsoft YaHei UI', 9))
    style.configure('TButton', padding=(12, 7), font=('Microsoft YaHei UI', 9), background='#ffffff', foreground=PALETTE['text'], bordercolor='#cbd9f0')
    style.map('TButton', background=[('active', '#edf3fe'), ('disabled', '#f1f5f9')], foreground=[('disabled', '#94a3b8')])
    style.configure('Primary.TButton', padding=(14, 7), font=('Microsoft YaHei UI', 9, 'bold'), background=PALETTE['primary'], foreground='#ffffff', bordercolor=PALETTE['primary'])
    style.map('Primary.TButton', background=[('active', PALETTE['primary_hover']), ('disabled', '#93c5fd')], foreground=[('disabled', '#ffffff')])
    style.configure('Copy.Treeview', background='#ffffff', fieldbackground='#ffffff', foreground=PALETTE['text'], rowheight=row_height, font=('Microsoft YaHei UI', 9), borderwidth=0)
    style.configure('Copy.Treeview.Heading', background='#e8f0fc', foreground=PALETTE['text'], font=('Microsoft YaHei UI', 9, 'bold'), padding=(6, 7), relief='flat')
    style.map('Copy.Treeview', background=[('selected', PALETTE['selected_bg'])], foreground=[('selected', PALETTE['selected_fg'])])
    style.configure('TCombobox', padding=(6, 4), font=('Microsoft YaHei UI', 9))
    style.configure('TEntry', padding=(6, 4), font=('Microsoft YaHei UI', 9))

    project = Path(__file__).resolve().parents[1]
    supported = {'A', 'B', 'C', 'G'}
    active_states = (
        'MONITORING', 'PAUSED', 'DEMO_RUNNING', 'DEMO_PAUSED', 'DEMO_STOP_PENDING',
        'EXCHANGE_UNKNOWN', 'DEMO_RECONNECTING', 'DEMO_NETWORK_RECOVERY', 'DEMO_RECONCILING',
    )
    state_labels_demo = {
        'DEMO_RUNNING': 'Demo跟单运行中',
        'MONITORING': '只读监测中',
        'PAUSED': '已暂停新开仓',
        'DEMO_PAUSED': '已暂停新开仓',
        'DEMO_STOP_PENDING': '等待安全停止',
        'STOPPED': '已停止',
        'RECONCILE_REQUIRED': '对账异常·暂停开仓',
        'EXCHANGE_UNKNOWN': '交易所状态待确认',
        'PROTECTION_INCOMPLETE': '止损保护异常',
        'DEMO_RECONNECTING': '网络重连中',
        'DEMO_NETWORK_RECOVERY': '网络恢复中·暂停新开仓',
        'DEMO_RECONCILING': '对账中',
    }
    state_labels_prod = {
        **state_labels_demo,
        'DEMO_RUNNING': '正式实盘运行中（真实资金）',
        'MONITORING': '正式只读监测中',
        'STOPPED': '正式下单关闭',
        'RECONCILE_REQUIRED': '对账异常',
    }

    def state_label(env_code, st):
        mapping = state_labels_prod if env_code == PROD_ENV else state_labels_demo
        return mapping.get(st, '状态待确认')

    def display_strategy(sid, rows=None):
        if not sid:
            return '未知'
        if rows and sid in rows:
            return rows[sid]
        return strategy_display_name(sid)

    def adapter_status(sid):
        if sid in supported:
            return True, ''
        if sid.startswith('pa-'):
            from scripts.live_copy_v2 import paper_fills
            try:
                if paper_fills(project / 'data/strategy-pool.sqlite3', strategy=sid):
                    return True, ''
                if sid == 'pa-trend-false-break-reclaim':
                    return True, '统一流水适配已隔离验证，尚无自然成交'
                return False, '正式账本暂无可核实成交'
            except (ValueError, ArithmeticError, KeyError, OSError, sqlite3.Error) as exc:
                if str(exc).startswith('PA_FULL_CLOSE_MISMATCH:'):
                    number = str(exc).split(':', 1)[1]
                    return False, f'第 {number} 条平仓的买卖方向、数量或版本与开仓不符'
                return False, f'成交关联未核实（{exc}）'
        if sid in ('PA-09', 'PA-10'):
            return False, '独立账本成交缺少方向及生命周期关联；平仓后持仓快照无法还原历史方向'
        return False, '尚无经过验证的开平仓及交易关联适配器'

    def env_defaults(env_code):
        p = env_paths(env_code, base_root=(env_roots or {}).get(env_code))
        prod_accounts = list_production_accounts() if env_code == PROD_ENV else []
        acc_label = f"Binance USDⓈ-M · {prod_accounts[0]['name']}" if prod_accounts else p.default_account_label
        acc_id = prod_accounts[0]['account_id'] if prod_accounts else None
        return {
            'strategy': '',
            'mode': '定额跟单',
            'fixed': '20',
            'budget': '100',
            'leverage': '5',
            'stop': '',
            'tp': '',
            'emergency': '',
            'maximum': '',
            'slippage': '',
            'account': acc_label,
            'account_id': acc_id or '',
            'follow_tp': '1',
            'follow_sl': '1',
            'custom_tp': '0',
            'custom_sl': '0',
            'flatten_on_stop': '0',
        }

    def env_policy(env_code):
        return dict(
            environment=env_code,
            execution_mode='PRODUCTION' if env_code == PROD_ENV else 'DEMO',
            copy_mode='FIXED',
            production_order_enabled=False,
        )

    class EnvContext:
        def __init__(self, env_code):
            self.env = env_code
            self.paths = env_paths(env_code, base_root=(env_roots or {}).get(env_code))
            self.configs = {}
            self.selected_id = None
            self.account_line = tk.StringVar(root, value='读取中…')
            self.account_updated = tk.StringVar(root, value='正在查询账户余额')
            self.active_strategy_line = tk.StringVar(root, value='暂无活动策略')
            self.active_strategy_sub = tk.StringVar(root, value='● 未运行')
            self.service_line = tk.StringVar(root, value='真实资金交易关闭' if env_code == PROD_ENV else '已停止')
            self.service_sub = tk.StringVar(root, value='生产下单默认关闭' if env_code == PROD_ENV else '最近对账 —')
            self.pnl_value = tk.StringVar(root, value='暂无记录')
            self.pnl_sub = tk.StringVar(root, value='已扣手续费 · 不含资金费')
            self.status_note = tk.StringVar(root, value='')
            self.queue_note = tk.StringVar(root, value='')
            self.positions = {}
            self.mark_received = 0
            self.last_mark_request = 0
            self.balance_mode = None
            self.transition = False
            self.queue_check = None
            self.chart_requested_strategy = None
            self.preflight_cache = None
            self.allowed_leverages = tuple(range(1, MAX_COPY_LEVERAGE + 1))
            self.exchange_max_leverage = None
            self.actual_leverage = None
            self.load_configs()

        def persist(self):
            self.paths.root.mkdir(parents=True, exist_ok=True)
            tmp = self.paths.configs.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.configs, ensure_ascii=False, indent=2), encoding='utf8')
            tmp.replace(self.paths.configs)

        def saved_config_matches(self, cid, config):
            try:
                return (
                    self.configs.get(cid) == config
                    and json.loads(self.paths.configs.read_text(encoding='utf8')).get(cid) == config
                )
            except (OSError, ValueError):
                return False

        def load_configs(self):
            if self.paths.configs.exists():
                try:
                    raw = json.loads(self.paths.configs.read_text(encoding='utf8'))
                    if isinstance(raw, dict):
                        self.configs = raw
                        return
                except (OSError, ValueError):
                    self.configs = {}
                    return
            if self.env != DEMO_ENV:
                self.configs = {}
                return
            # Legacy Demo migration (only when Demo copy-configs.json does not exist yet)
            legacy = {}
            draft_path = self.paths.root / 'copy-v2-draft.json'
            for path in (self.paths.root / 'copy-presets').glob('*.json'):
                try:
                    row = json.loads(path.read_text(encoding='utf8'))
                    legacy[row['strategy']] = row
                except (OSError, ValueError, KeyError):
                    continue
            if draft_path.exists():
                try:
                    d = json.loads(draft_path.read_text(encoding='utf8'))
                    if d.get('strategy'):
                        legacy[d['strategy']] = d
                except (OSError, ValueError):
                    pass
            base_def = env_defaults(DEMO_ENV)
            for sid, row in legacy.items():
                cid = uuid4().hex
                self.configs[cid] = {
                    **base_def, **row, 'copy_config_id': cid,
                    'created_at': datetime.now().isoformat(), **env_policy(DEMO_ENV), 'archived_at': None,
                }
            old = self.paths.ledgers / 'demo-execution-inbox.sqlite3'
            if old.is_file():
                with closing(sqlite3.connect(old.as_uri() + '?mode=ro', uri=True)) as db:
                    source, fixed, budget = db.execute('SELECT source,fixed,budget FROM subscription').fetchone()
                for cid, row in self.configs.items():
                    sid = row['strategy']
                    if (
                        (source.endswith('#' + sid) or (sid == 'C' and source.endswith('paper.sqlite3')))
                        and Decimal(row['fixed']) == Decimal(fixed)
                        and Decimal(row['budget']) == Decimal(budget)
                    ):
                        row['legacy_demo_ledger'] = True
            if self.configs:
                self.persist()

        def ledger_dir(self, cid):
            return config_ledger_dir(self.env, cid, self.configs[cid], self.configs, base_root=(env_roots or {}).get(self.env))

        def send_command(self, command):
            self.paths.runtime.mkdir(parents=True, exist_ok=True)
            pending = self.paths.runtime / f'command.ui-{os.getpid()}.tmp'
            pending.write_text(command, encoding='utf8')
            pending.replace(self.paths.runtime / 'command.txt')

        def read_switch(self):
            try:
                value = json.loads(self.paths.switch_queue.read_text(encoding='utf8'))
                if (
                    not isinstance(value, dict)
                    or not isinstance(value.get('target'), dict)
                    or not {'from_strategy', 'state', 'from_id', 'target_id', 'account', 'source', 'extra'} <= value.keys()
                    or 'strategy' not in value['target']
                ):
                    raise ValueError('Invalid queue')
                return value
            except FileNotFoundError:
                return None
            except (OSError, ValueError, TypeError):
                return dict(
                    state='BLOCKED', from_strategy='未知', target={'strategy': '未知'},
                    note='排队记录无法核对，请取消后重新确认',
                )

        def save_switch(self, value):
            self.paths.runtime.mkdir(parents=True, exist_ok=True)
            pending = self.paths.switch_queue.with_suffix(f'.{os.getpid()}.tmp')
            pending.write_text(json.dumps(value, ensure_ascii=False), encoding='utf8')
            pending.replace(self.paths.switch_queue)

        @contextmanager
        def switch_lock(self):
            import msvcrt
            self.paths.runtime.mkdir(parents=True, exist_ok=True)
            with (self.paths.runtime / 'switch.lock').open('a+b') as claim:
                claim.seek(0)
                msvcrt.locking(claim.fileno(), msvcrt.LK_NBLCK, 1)
                yield

    contexts = {DEMO_ENV: EnvContext(DEMO_ENV), PROD_ENV: EnvContext(PROD_ENV)}
    active_env = initial_env if initial_env in VALID_ENVS else DEMO_ENV
    current_page = 0
    editing_env = active_env
    draft_cid = None
    kline_env_state = {'environment': 'production' if active_env == PROD_ENV else 'demo', 'seq': 0}
    fields = {k: tk.StringVar(root, value=v) for k, v in env_defaults(active_env).items()}
    table = None
    pnl_cells = {}
    controls = {}
    pnl_widget = None
    active_strat_sub_widget = None
    queue_cancel_btn = None
    tab_buttons = {}
    detail_windows = {}
    pool = ThreadPoolExecutor(max_workers=2)
    root.bind('<Destroy>', lambda e: pool.shutdown(wait=False) if e.widget is root else None)

    def ctx(env_code=None):
        return contexts[env_code or active_env]

    def service_pid_alive(pid):
        if not pid:
            return False
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        handle = kernel.OpenProcess(0x00100000, False, int(pid))
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)

    def running(env_code=None):
        c = ctx(env_code)
        try:
            row = json.loads((c.paths.runtime / 'status.json').read_text(encoding='utf8'))
            pid = row.get('pid')
            if row['state'] in (*active_states, 'RECONCILE_REQUIRED', 'PROTECTION_INCOMPLETE') and pid:
                if not service_pid_alive(pid):
                    row['state'] = 'STOPPED'
                    prior = row.get('reason')
                    row['reason'] = '后台已退出。重新启动前须核对原配置及交易所敞口。' + (' 上次状态：' + prior if prior else '')
            elif row['state'] in active_states and time.time() - row['time'] > 60:
                row['state'] = 'STOPPED'
            return row
        except (OSError, ValueError, KeyError, TypeError):
            return {'state': 'STOPPED', 'environment': c.env}

    def selected_config(env_code=None):
        c = ctx(env_code)
        if c.env == active_env and table is not None and table.winfo_exists() and table.selection():
            return table.selection()[0]
        return c.selected_id

    def refresh_account(env_code=None, *, force=True):
        c = ctx(env_code)
        if c.balance_mode == 'READING':
            return
        if not force and c.account_line.get() not in ('读取中…', '读取失败', ''):
            return
        c.balance_mode = 'READING'

        def query():
            exchange, _, _ = make_exchange(c.env, allow_writes=False, base_root=(env_roots or {}).get(c.env))
            exchange.synchronize()
            asset = next((a for a in exchange.account()['assets'] if a['asset'] == 'USDT'), None)
            if asset is None:
                raise ValueError('账户响应缺少 USDT')
            lev_info = (None, None, None)
            try:
                allowed, ex_max = exchange.allowed_leverage_range()
                sym_cfg = exchange._request('GET', '/fapi/v1/symbolConfig', {'symbol': 'BTCUSDT'})
                act_lev = int(sym_cfg[0]['leverage']) if sym_cfg else None
                lev_info = (allowed, ex_max, act_lev)
            except Exception:
                pass
            return money(asset['availableBalance']), lev_info

        if c.account_line.get() in ('', '读取失败'):
            c.account_line.set('读取中…')
            c.account_updated.set('正在查询账户余额')
        future = pool.submit(query)

        def finish():
            if not root.winfo_exists():
                return
            if not future.done():
                root.after(150, finish)
                return
            c.balance_mode = None
            try:
                bal, (allowed, ex_max, act_lev) = future.result()
                c.account_line.set(bal)
                c.account_updated.set(f"更新于 {datetime.now():%H:%M:%S}")
                if allowed:
                    c.allowed_leverages = allowed
                    c.exchange_max_leverage = ex_max
                    c.actual_leverage = act_lev
            except Exception as exc:
                c.account_line.set('读取失败')
                c.account_updated.set(str(exc)[:28] or '点击刷新账户重试')

        root.after(150, finish)

    def refresh_marks(env_code=None):
        c = ctx(env_code)
        if time.monotonic() - c.last_mark_request < 15:
            return
        c.last_mark_request = time.monotonic()

        def query():
            exchange, _, _ = make_exchange(c.env, allow_writes=False, base_root=(env_roots or {}).get(c.env))
            exchange.synchronize()
            rows = exchange._request('GET', '/fapi/v3/positionRisk', {'symbol': 'BTCUSDT'})
            return {
                ('LONG' if Decimal(r['positionAmt']) > 0 else 'SHORT'): r
                for r in rows
                if r['symbol'] == 'BTCUSDT' and Decimal(r['positionAmt'])
            }

        future = pool.submit(query)

        def finish():
            if not root.winfo_exists():
                return
            if not future.done():
                root.after(150, finish)
                return
            try:
                c.positions = future.result()
                c.mark_received = time.monotonic()
            except Exception:
                c.positions = {}
                c.mark_received = 0

        root.after(150, finish)

    def metrics(c, data, *, owned):
        if not data['active']:
            return '—', '—', None, None, '—'
        if len(data['active']) != 1:
            return '待核对', '价格待更新', None, None, '—'
        direction, qty, entry = data['active'][0]
        name = {'LONG': '做多', 'SHORT': '做空'}.get(direction, '待核对')
        row = c.positions.get(direction) if owned and time.monotonic() - c.mark_received <= 30 else None
        if row and abs(Decimal(row['positionAmt'])) == qty and Decimal(row['entryPrice']) == entry:
            mark = Decimal(row['markPrice'])
            if mark.is_finite() and mark > 0:
                return name, money(qty * mark), Decimal(row['unRealizedProfit']), Decimal(row['initialMargin']), f'{qty:f} BTC'
        return name, '价格待更新', None, None, f'{qty:f} BTC'

    def make_chart_snapshot(env_code):
        c = ctx(env_code)
        current = running(env_code)
        items, positions, errors = [], [], []
        registered = {}
        rows_cache = paper_rows()
        expected_mode = c.paths.mode_code
        for cid, config in list(c.configs.items()):
            raw_strategy = config['strategy']
            label = display_strategy(raw_strategy, rows_cache)
            registered[raw_strategy] = label
            try:
                directory = c.ledger_dir(cid)
                data = demo_copy_data(directory, chart=True)
                items.extend(dict(fill, strategy_id=label, copy_config_id=cid) for fill in data['chart'])
                if (
                    current.get('mode') == expected_mode
                    and current.get('copy_config_id') == cid
                    and current.get('strategy', '') == raw_strategy
                ):
                    fresh_marks = time.monotonic() - c.mark_received <= 30
                    fresh_reconciliation = (
                        current.get('state') in ('DEMO_RUNNING', 'DEMO_PAUSED')
                        and 0 <= time.time() - current.get('last_reconciled_at', 0) <= 45
                    )
                    for side, qty, price in data['active']:
                        row = c.positions.get(side) if fresh_marks else None
                        matches = row and abs(Decimal(row['positionAmt'])) == qty and Decimal(row['entryPrice']) == price
                        if matches or (not fresh_marks and fresh_reconciliation):
                            positions.append(
                                dict(
                                    strategy_id=label,
                                    copy_config_id=cid,
                                    position_side=side,
                                    quantity=str(qty),
                                    price=str(price),
                                    unrealized_pnl=row.get('unRealizedProfit') if matches else None,
                                )
                            )
            except (OSError, sqlite3.Error, ValueError, KeyError, ArithmeticError, TypeError):
                errors.append(label)
        ordered_strategies = [registered[sid] for sid in sorted(registered, key=strategy_sort_key)]
        return dict(
            environment='production' if env_code == PROD_ENV else 'demo',
            strategies=ordered_strategies,
            items=items,
            positions=positions,
            errors=errors,
            selected_strategy=c.chart_requested_strategy,
        )

    def open_selected_kline():
        from scripts.kline_window import open_live_kline
        c = ctx()
        config = c.configs.get(selected_config(), {})
        raw_sid = config.get('strategy')
        c.chart_requested_strategy = display_strategy(raw_sid, paper_rows()) if raw_sid else None
        kline_env_state['environment'] = 'production' if c.env == PROD_ENV else 'demo'
        kline_env_state['seq'] += 1
        open_live_kline(
            root,
            demo_snapshot=lambda: make_chart_snapshot(DEMO_ENV),
            production_snapshot=lambda: make_chart_snapshot(PROD_ENV),
            env_state=lambda: dict(kline_env_state),
        )

    def demo_ledger_busy(env_code, cid):
        c = ctx(env_code)
        cfg = c.configs.get(cid, {})
        if not cfg:
            return False
        directory = c.ledger_dir(cid)
        inbox = directory / 'demo-execution-inbox.sqlite3'
        ledger = directory / 'c-v31-demo-execution.sqlite3'
        try:
            linked = set()
            if inbox.exists():
                with closing(sqlite3.connect(inbox.as_uri() + '?mode=ro', uri=True)) as db:
                    linked = {json.loads(raw)['client_order_id'] for raw, in db.execute('SELECT intent FROM linked_orders')}
            if not ledger.exists():
                return bool(linked)
            with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True)) as db:
                cycles = dict(db.execute('SELECT id,state FROM cycles'))
                writes = db.execute('SELECT count(*) FROM writes').fetchone()[0]
            return set(cycles) != linked or bool(writes and not cycles) or any(
                x not in ('CLOSED', 'NO_FILL') for x in cycles.values()
            )
        except (OSError, sqlite3.Error, ValueError, KeyError):
            return True

    def busy(env_code, cid):
        c = ctx(env_code)
        row = running(env_code)
        cfg = c.configs.get(cid, {})
        if (
            (
                row.get('copy_config_id') == cid
                or (not row.get('copy_config_id') and row.get('strategy') == cfg.get('strategy') and bool(cfg))
            )
            and row['state'] in (*active_states, 'RECONCILE_REQUIRED', 'PROTECTION_INCOMPLETE')
        ):
            return True
        return demo_ledger_busy(env_code, cid)

    def row_status(env_code, cid, row):
        c = ctx(env_code)
        cfg = c.configs[cid]
        if row.get('copy_config_id') == cid and row.get('strategy') == cfg['strategy']:
            st = row['state']
            if st == 'DEMO_RUNNING':
                return '正式运行中' if env_code == PROD_ENV else 'Demo运行中'
            return state_label(env_code, st)
        pending = c.read_switch()
        if pending and pending.get('target_id') == cid:
            return '等待切换' if pending['state'] != 'BLOCKED' else '切换待核对'
        return '已保存'

    def preflight_env(env_code, config, *, allow_existing_exposure=False):
        exchange, _, _ = make_exchange(env_code, account_id=config.get('account_id') or None,
                                       allow_writes=False, base_root=(env_roots or {}).get(env_code))
        report = exchange.preflight_report(
            planned_leverage=int(config['leverage']),
            fixed_margin=Decimal(config['fixed']),
            budget=Decimal(config['budget']),
            allow_existing_exposure=allow_existing_exposure,
        )
        if report['status'] != 'PASS':
            raise ValueError('；'.join(report['blockers']))
        return report['account_fingerprint'], report

    def current_env_identity(env_code, config=None):
        exchange, _, _ = make_exchange(env_code, account_id=(config or {}).get('account_id') or None,
                                       allow_writes=False, base_root=(env_roots or {}).get(env_code))
        exchange.synchronize()
        aliases = [r.get('accountAlias') for r in exchange._request('GET', '/fapi/v3/balance') if r.get('asset') == 'USDT']
        if len(aliases) != 1 or not isinstance(aliases[0], str) or not aliases[0].strip():
            raise ValueError('无法核实账户身份。')
        return sha256(aliases[0].encode()).hexdigest()[:8]

    def check_demo_cursor(env_code, cid, sid, source):
        from scripts.live_copy_v2 import paper_fills
        c = ctx(env_code)
        inbox = c.ledger_dir(cid) / 'demo-execution-inbox.sqlite3'
        if not inbox.is_file():
            return
        with closing(sqlite3.connect(inbox.as_uri() + '?mode=ro', uri=True)) as db:
            expected = str(source.resolve()) if sid == 'C' else f'{source.resolve()}#{sid}'
            row = db.execute('SELECT source,cursor FROM subscription WHERE id=1').fetchone()
            if row is None or row[0] != expected or paper_fills(source, strategy=sid) < row[1]:
                raise ValueError('原成交游标与所选策略或账本不一致。')
            pending = db.execute(
                """SELECT i.payload FROM inbox i LEFT JOIN executed_events e ON e.event=i.event_id
                   WHERE e.event IS NULL"""
            ).fetchall()
        now = datetime.now(timezone.utc)

        def recent_open(action, at):
            return action == 'OPEN' and (now - at).total_seconds() <= 60

        if any(
            recent_open(fill['action'], datetime.fromisoformat(fill['occurred_at']))
            for raw, in pending
            for fill in (json.loads(raw),)
        ):
            raise ValueError('原跟单账本还有近期未处理开仓，避免补单，请等该成交超过安全时限后再试。')
        cursor = row[1]
        while fills := paper_fills(source, cursor, strategy=sid):
            if any(recent_open(fill.action, fill.occurred_at) for fill in fills):
                raise ValueError('存在启用前的近期纸盘开仓，避免补单，请待其超过安全时限后再试。')
            cursor = fills[-1].cursor

    def spawn_follower(env_code, cid, config, source, extra):
        import msvcrt
        c = ctx(env_code)
        c.paths.runtime.mkdir(parents=True, exist_ok=True)
        with (c.paths.runtime / 'active.lock').open('a+b') as lock:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError('当前环境已有跟单进程持有运行锁，请等待安全停止。') from exc
            c.send_command('run')
        cmd = [
            sys.executable, '-m', 'scripts.live_copy_service', str(source),
            str(c.paths.runtime / f"plans-{config['strategy']}-{time.time_ns()}.sqlite3"),
            '--fixed', config['fixed'], '--strategy', config['strategy'], '--config-id', cid,
            '--budget', config['budget'], '--runtime-dir', str(c.paths.runtime),
            '--environment', env_code,
        ]
        if config.get('account_id'):
            cmd += ['--account-id', config['account_id']]
        if (env_roots or {}).get(env_code):
            cmd += ['--data-dir', str((env_roots or {})[env_code])]
        cmd += extra
        with (c.paths.runtime / 'service.log').open('ab') as log:
            process = subprocess.Popen(
                cmd, cwd=project, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            )
        return process

    def show_preflight_dialog(env_code=None):
        c = ctx(env_code)
        dialog = tk.Toplevel(root)
        dialog.title(f"{c.paths.env_title} · 账户只读预检")
        dialog.geometry('640x480')
        dialog.configure(bg=PALETTE['bg'])
        dialog.transient(root)
        text = tk.Text(dialog, wrap='word', bg='#ffffff', fg=PALETTE['text'], font=('Microsoft YaHei UI', 9), padx=14, pady=12, relief='flat')
        text.pack(fill='both', expand=True, padx=14, pady=(14, 8))
        text.insert('end', '正在通过 Binance 只读接口核验账户权限、持仓模式、逐仓、杠杆与最小下单规格…\n')
        text.configure(state='disabled')
        ttk.Button(dialog, text='关闭', command=dialog.destroy).pack(anchor='e', padx=14, pady=(0, 12))

        def run_probe():
            sel_cfg = c.configs.get(selected_config(c.env) or '', {})
            probe_lev = int(sel_cfg.get('leverage') or MAX_COPY_LEVERAGE)
            probe_fixed = Decimal(sel_cfg.get('fixed') or '20')
            probe_budget = Decimal(sel_cfg.get('budget') or '100')
            exchange, acc_label, _ = make_exchange(c.env, allow_writes=False, base_root=(env_roots or {}).get(c.env))
            report = exchange.preflight_report(planned_leverage=probe_lev, fixed_margin=probe_fixed, budget=probe_budget)
            return acc_label, report

        future = pool.submit(run_probe)

        def done():
            if not dialog.winfo_exists():
                return
            if not future.done():
                dialog.after(150, done)
                return
            text.configure(state='normal')
            text.delete('1.0', 'end')
            try:
                acc_label, r = future.result()
                c.preflight_cache = r
                lines = [
                    f"执行账户：{acc_label}",
                    f"交易环境：{c.paths.env_title}（只读查询，未修改任何账户设置，未发送任何订单）",
                    f"账户校验标识：{r['account_fingerprint']}（别名 {r['account_alias']}）",
                    f"预检结论：{r['status']}",
                    f"合约交易权限（canTrade）：{'已开通' if r['can_trade'] else '未开通'}",
                    f"持仓模式：{r['position_mode']}（保留用户原模式，不自动修改）",
                    f"保证金模式：{r['margin_type']} · 当前实际杠杆：{r['actual_leverage']}×（系统上限 {MAX_COPY_LEVERAGE}×，本次预检计划杠杆 {r['planned_leverage']}×）",
                    f"USDT 可用余额：{money(r['available_usdt'])} · 钱包余额：{money(r['wallet_usdt'])}",
                    f"当前敞口：BTC仓位 {r['btc_positions']} / 其他仓位 {r['other_positions']} / 普通挂单 {r['open_orders']} / Algo条件单 {r['open_algos']}",
                    f"BTCUSDT 规格：现价 {r['price']} · 最小数量 {r['min_qty']} · 步长 {r['step_size']} · 价格步长 {r['tick_size']} · 最小名义 {r['min_notional']} USDT",
                    f"Taker 费率：{r['taker_fee']} · 按{r['planned_leverage']}×估算最小单保证金：≥ {r['est_min_margin_usdt']} USDT",
                    '',
                    '阻断项：' if r['blockers'] else '无阻断项。正式真实资金交易仍需用户单独确认授权后方可启用。',
                ]
                for item in r['blockers']:
                    lines.append(f'  • {item}')
                text.insert('end', '\n'.join(lines))
            except Exception as exc:
                text.insert('end', f'预检读取失败：{exc}')
            text.configure(state='disabled')

        dialog.after(150, done)

    def queue_confirmation(env_code, previous, cid, config, source, extra, details_text):
        c = ctx(env_code)
        future = pool.submit(current_env_identity, env_code, config)

        def ready():
            if not root.winfo_exists():
                return
            if not future.done():
                root.after(150, ready)
                return
            try:
                identity = future.result()
                prior = c.read_switch()
                dialog = tk.Toplevel(root)
                dialog.title(f'切换{c.paths.env_title}跟单策略')
                dialog.configure(bg=PALETTE['card_bg'])
                dialog.transient(root)
                decision = []

                def accept():
                    decision.append(True)
                    dialog.destroy()

                replacement = f"\n将替换已排队的 {display_strategy(prior['target']['strategy'])} 配置。" if prior else ''
                body_box = tk.Frame(dialog, bg=PALETTE['card_bg'], padx=20, pady=16)
                body_box.pack(fill='both', expand=True)
                tk.Label(body_box, text='确认安全切换跟单策略', bg=PALETTE['card_bg'], fg=PALETTE['text'],
                         font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w', pady=(0, 8))
                tk.Label(
                    body_box,
                    bg=PALETTE['card_bg'],
                    fg=PALETTE['text'],
                    justify='left',
                    wraplength=520,
                    font=('Microsoft YaHei UI', 9),
                    text=(
                        f"当前正在运行：{display_strategy(previous['strategy'])} · {previous.get('copy_config_id')}\n"
                        f"准备启用：{display_strategy(config['strategy'])} · {cid}\n"
                        f"交易环境：{c.paths.env_title}（账户校验标识 {identity}）\n"
                        f"参数摘要：{details_text}\n\n"
                        f"确认后将安全切换跟单策略：暂停原策略新增开仓，继续管理已有关联仓位。"
                        f"如当前仍有持仓，将在平仓及对账完成后自动启用新策略。{replacement}"
                    ),
                ).pack(anchor='w')
                btn_row = tk.Frame(dialog, bg=PALETTE['bg'], padx=16, pady=10)
                btn_row.pack(fill='x')
                ttk.Button(btn_row, text='取消并保存', command=dialog.destroy).pack(side='right')
                ttk.Button(btn_row, text='确认切换', style='Primary.TButton', command=accept).pack(side='right', padx=(0, 8))
                dialog.grab_set()
                root.wait_window(dialog)
                if not decision:
                    return
                row = running(env_code)
                with c.switch_lock():
                    if (
                        row.get('copy_config_id') != previous.get('copy_config_id')
                        or row.get('mode') != c.paths.mode_code
                        or c.read_switch() != prior
                        or not c.saved_config_matches(cid, config)
                        or (prior and prior['state'] in ('STARTING', 'STOPPING'))
                    ):
                        raise ValueError('确认期间活动服务或配置已变化，请重新确认。')
                    c.save_switch(
                        dict(
                            from_id=previous['copy_config_id'],
                            from_strategy=previous['strategy'],
                            target_id=cid,
                            target=config,
                            source=str(source),
                            extra=extra,
                            account=identity,
                            state='WAITING',
                        )
                    )
                    c.send_command('pause')
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                messagebox.showerror('等待切换', str(exc), parent=root)

        root.after(150, ready)

    def cancel_switch(env_code=None):
        c = ctx(env_code)
        pending = c.read_switch()
        if pending and messagebox.askyesno(
            '取消等待切换',
            '仅移除当前环境的待切换目标。原服务继续管理已有仓位，并保持当前暂停状态。',
            parent=root,
        ):
            try:
                with c.switch_lock():
                    if (
                        c.read_switch() != pending
                        or pending['state'] in ('STARTING', 'STOPPING')
                        or running(c.env)['state'] == 'DEMO_STOP_PENDING'
                    ):
                        raise ValueError('切换已经被认领或状态已变化，请检查服务状态。')
                    c.paths.switch_queue.unlink(missing_ok=True)
            except (OSError, ValueError) as exc:
                messagebox.showerror('等待切换', str(exc), parent=root)

    def switch_to_env(env_code, cid, config, source, extra, previous, confirmation, *, allow_existing_exposure=False):
        c = ctx(env_code)
        c.transition = True

        def finish(error=None):
            c.transition = False
            if error:
                messagebox.showerror('跟单服务', str(error), parent=root)

        def ownership(row):
            return tuple(row.get(k) for k in ('pid', 'mode', 'strategy', 'copy_config_id'))

        original = ownership(previous)

        def launch(confirmed):
            try:
                row = running(env_code)
                if row['state'] != 'STOPPED' or service_pid_alive(row.get('pid')):
                    raise ValueError('当前环境已有跟单服务或待对账状态，请核对后再试。')
                if c.configs.get(cid) != config:
                    raise ValueError('核对期间配置已更改，请重新确认。')
                if any(demo_ledger_busy(env_code, k) for k in c.configs if k != cid):
                    raise ValueError('当前环境其他配置仍有未结束交易或未知委托。')
                check_demo_cursor(env_code, cid, config['strategy'], source)
                process = spawn_follower(env_code, cid, config, source, extra)
                root.after(150, lambda: wait_for_start(process, time.monotonic() + 20))
            except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
                finish(exc)

        def wait_for_start(process, deadline):
            row = running(env_code)
            if (
                row.get('pid') != previous.get('pid')
                and row.get('mode') == c.paths.mode_code
                and row.get('copy_config_id') == cid
                and row.get('strategy') == config['strategy']
            ):
                if row['state'] == 'DEMO_RUNNING':
                    finish()
                elif row['state'] in ('RECONCILE_REQUIRED', 'EXCHANGE_UNKNOWN', 'PROTECTION_INCOMPLETE', 'STOPPED'):
                    finish(row.get('reason') or state_label(env_code, row['state']))
                elif time.monotonic() >= deadline:
                    finish()
                else:
                    root.after(150, lambda: wait_for_start(process, deadline))
            elif process.poll() is not None or time.monotonic() >= deadline:
                finish(f'{c.paths.env_title} 未发布启动状态，请查看服务日志。')
            else:
                root.after(150, lambda: wait_for_start(process, deadline))

        def wait_for_stop(deadline, confirmed):
            row = running(env_code)
            if ownership(row) != original:
                finish('跟单服务已更换，取消切换。')
            elif row['state'] == 'STOPPED' and not service_pid_alive(row.get('pid')):
                recheck = pool.submit(preflight_env, env_code, config)

                def checked_after_stop():
                    if not recheck.done():
                        root.after(150, checked_after_stop)
                        return
                    try:
                        if recheck.result()[0] != confirmed[0]:
                            raise ValueError('账户在切换期间发生变化，请重新确认。')
                        launch(confirmed)
                    except Exception as exc:
                        finish(exc)

                root.after(150, checked_after_stop)
            elif time.monotonic() >= deadline:
                finish('只读跟单尚未安全退出，未启动新服务。')
            else:
                root.after(150, lambda: wait_for_stop(deadline, confirmed))

        future = pool.submit(preflight_env, env_code, config, allow_existing_exposure=allow_existing_exposure)

        def checked():
            if not root.winfo_exists():
                return
            if not future.done():
                root.after(150, checked)
                return
            try:
                confirmed = future.result()
                account_fp = confirmed[0]
                row = running(env_code)
                if ownership(row) != original:
                    raise ValueError('核对期间跟单服务已更换，请重新确认。')
                title_txt = '明确启用 Binance 正式实盘（真实资金）' if env_code == PROD_ENV else '明确启用 Binance Demo'
                if not messagebox.askyesno(
                    title_txt,
                    f"账户：{config.get('account', c.paths.default_account_label)}（校验标识 {account_fp}）\n" + confirmation,
                    parent=root,
                ):
                    finish()
                    return
                row = running(env_code)
                if ownership(row) != original or c.configs.get(cid) != config:
                    raise ValueError('确认期间服务或配置已更换，请重新核对。')
                if previous['state'] in ('MONITORING', 'PAUSED'):
                    if row['state'] not in ('MONITORING', 'PAUSED'):
                        raise ValueError('只读服务状态已经变化，请重新确认。')
                    c.send_command('stop')
                    wait_for_stop(time.monotonic() + 12, confirmed)
                else:
                    launch(confirmed)
            except Exception as exc:
                finish(exc)

        root.after(150, checked)

    def advance_switch_for(env_code):
        c = ctx(env_code)
        try:
            pending = c.read_switch()
            if not pending:
                return
            row = running(env_code)
            if pending['state'] == 'STARTING':
                if row.get('copy_config_id') == pending['target_id'] and row.get('state') == 'DEMO_RUNNING':
                    with c.switch_lock():
                        if c.read_switch() == pending:
                            c.paths.switch_queue.unlink(missing_ok=True)
                elif time.time() - pending.get('started_at', time.time()) > 60:
                    with c.switch_lock():
                        if c.read_switch() == pending:
                            c.save_switch({**pending, 'state': 'BLOCKED', 'note': '启动回执待核对，请检查后台，系统未重复启动'})
                return
            if pending['state'] not in ('WAITING', 'STOPPING') or c.queue_check is not None or c.transition:
                return
            if row.get('copy_config_id') != pending['from_id'] or row.get('mode') != c.paths.mode_code:
                return
            if row['state'] == 'DEMO_RUNNING' and pending['state'] == 'WAITING':
                with c.switch_lock():
                    if c.read_switch() == pending:
                        c.send_command('pause')
                return
            paused = row['state'] == 'DEMO_PAUSED' and pending['state'] == 'WAITING'
            stopped = row['state'] == 'STOPPED' and not service_pid_alive(row.get('pid'))
            if not paused and not stopped:
                if pending['state'] == 'STOPPING' and row['state'] == 'DEMO_PAUSED':
                    with c.switch_lock():
                        if c.read_switch() == pending:
                            c.send_command('stop')
                return
            new_id = pending['target_id']
            config = pending['target']
            if not c.saved_config_matches(new_id, config) or config.get('archived_at'):
                with c.switch_lock():
                    if c.read_switch() == pending:
                        c.save_switch({**pending, 'state': 'BLOCKED', 'note': '目标配置已改变，请取消并重新确认'})
                return
            if any(demo_ledger_busy(env_code, k) for k in c.configs):
                return
            c.queue_check = pool.submit(preflight_env, env_code, config)

            def checked():
                if not root.winfo_exists():
                    return
                if not c.queue_check.done():
                    root.after(150, checked)
                    return
                candidate, c.queue_check = c.queue_check, None
                try:
                    if c.read_switch() != pending:
                        return
                    identity, _ = candidate.result()
                    if identity != pending['account']:
                        raise ValueError('账户已更换，请取消并重新确认。')
                    live = running(env_code)
                    if (
                        live.get('copy_config_id') != pending['from_id']
                        or live['state'] != row['state']
                        or live.get('pid') != row.get('pid')
                        or (stopped and service_pid_alive(live.get('pid')))
                        or not c.saved_config_matches(new_id, config)
                    ):
                        return
                    if any(demo_ledger_busy(env_code, k) for k in c.configs):
                        return
                    source = Path(pending['source'])
                    check_demo_cursor(env_code, new_id, config['strategy'], source)
                    with c.switch_lock():
                        if c.read_switch() != pending:
                            return
                        if paused:
                            c.save_switch({**pending, 'state': 'STOPPING', 'note': '原仓已对账，等待服务安全退出'})
                            c.send_command('stop')
                            return
                        c.save_switch({**pending, 'state': 'STARTING', 'started_at': time.time(), 'note': '已认领启动，等待后台确认'})
                        spawn_follower(env_code, new_id, config, source, pending['extra'])
                except Exception as exc:
                    try:
                        with c.switch_lock():
                            current = c.read_switch()
                            if current == pending:
                                c.save_switch({**pending, 'note': str(exc)})
                            elif current and current.get('state') == 'STARTING' and current.get('target_id') == new_id:
                                c.save_switch({**current, 'state': 'BLOCKED', 'note': str(exc)})
                    except OSError:
                        return

            root.after(150, checked)
        except (OSError, ValueError):
            pass

    def advance_all_switches():
        if not root.winfo_exists():
            return
        for env_code in VALID_ENVS:
            advance_switch_for(env_code)
        root.after(1500, advance_all_switches)

    def optional_exit(v, toggle, key):
        if v.get(toggle) != '1':
            return None
        value = Decimal(v.get(key) or '0')
        if not value.is_finite() or not 0 < value < 100:
            raise ValueError('开启独立止盈或止损后，请填写大于 0 且小于 100 的百分比。')
        return value

    def control(command, env_code=None, cid_override=None):
        c = ctx(env_code)
        try:
            c.paths.runtime.mkdir(parents=True, exist_ok=True)
            if c.transition:
                return
            previous = running(c.env)
            current = previous['state']
            if command in ('resume', 'run') and c.read_switch():
                raise ValueError('当前环境已有等待切换，请先取消排队；原仓仍按安全停止流程管理。')
            if command in ('run', 'enable'):
                cid = cid_override or selected_config(c.env)
                if cid not in c.configs or c.configs[cid].get('archived_at'):
                    raise ValueError('请选择一条未归档配置。')
                v = c.configs[cid]
                can_switch = (
                    command == 'enable'
                    and current in ('MONITORING', 'PAUSED')
                    and previous.get('mode') == 'DRY_RUN'
                    and previous.get('copy_config_id') == cid
                    and previous.get('strategy') == v['strategy']
                )
                can_queue = (
                    command == 'enable'
                    and current in ('DEMO_RUNNING', 'DEMO_PAUSED', 'DEMO_STOP_PENDING')
                    and previous.get('mode') == c.paths.mode_code
                    and previous.get('copy_config_id')
                    and previous.get('copy_config_id') != cid
                )
                if current != 'STOPPED' and not can_switch and not can_queue:
                    mode_txt = {'DRY_RUN': '只读', 'DEMO': 'Binance Demo', 'PRODUCTION': 'Binance 正式实盘'}.get(
                        previous.get('mode'), '未知模式'
                    )
                    raise ValueError(
                        f"当前跟单策略 {display_strategy(previous.get('strategy'))} · {mode_txt} · "
                        f"{state_label(c.env, current)}。请先安全停止或完成对账。"
                    )
                if v['mode'] != '定额跟单':
                    raise ValueError('定比跟单暂未开放。')
                sid = v['strategy']
                ready, reason = adapter_status(sid)
                if not ready:
                    raise ValueError(reason)
                source = project / 'data' / (
                    'strategy-pool.sqlite3' if sid.startswith('pa-')
                    else 'g/mirror.sqlite3' if sid == 'G'
                    else 'paper.sqlite3'
                )
                if not source.is_file():
                    raise ValueError('正式纸盘账本不可用。')
                extra = []
                if command == 'enable':
                    other_busy = any(demo_ledger_busy(c.env, k) for k in c.configs if k != cid)
                    resuming_own = demo_ledger_busy(c.env, cid)
                    if not can_queue and other_busy:
                        raise ValueError('当前环境其他配置仍有未结束交易或未知委托，请先完成对账。')
                    stop = optional_exit(v, 'custom_sl', 'stop')
                    target = optional_exit(v, 'custom_tp', 'tp')
                    risk = Decimal(v['budget']) * Decimal('.02')
                    warning = '\n未设置独立止损，纸盘或网络中断时可能无法及时平仓。' if stop is None else ''
                    resume_note = '\n检测到该配置存在未平关联仓位，确认后将自动对账并继续管理已有仓位退出（不补开历史开仓）。' if resuming_own else ''
                    order_flag = '--authorized-production-orders' if c.env == PROD_ENV else '--authorized-demo-orders'
                    env_warn = (
                        '⚠️ 注意：当前为【正式实盘真实资金】环境，确认后将跟随新纸盘成交提交真实订单！'
                        if c.env == PROD_ENV
                        else '确认后只跟随新纸盘成交，在 Binance Demo 提交虚拟订单。'
                    )
                    confirmation = (
                        f"策略：{display_strategy(sid)}\n配置 ID：{cid}\n"
                        f"每笔保证金：{money(v['fixed'])}\n总预算：{money(v['budget'])}\n杠杆上限：{v['leverage']}×\n"
                        '退出方式：跟随纸盘止盈止损\n'
                        f"独立止损：{str(stop) + '%（按价格变化）' if stop is not None else '关闭'}\n"
                        f"独立止盈：{str(target) + '%（按价格变化）' if target is not None else '关闭'}\n"
                        + ('确认后安全停止当前只读监测。\n' if can_switch else '')
                        + env_warn + resume_note + warning
                    )
                    extra = [order_flag, '--leverage', v['leverage'], '--risk-cap', str(risk)]
                    if stop is not None:
                        extra += ['--stop-fraction', str(stop / 100)]
                    if target is not None:
                        extra += ['--target-fraction', str(target / 100)]
                    if c.env == DEMO_ENV and v.get('legacy_demo_ledger'):
                        extra += ['--legacy-demo-ledger']
                    if can_queue:
                        queue_confirmation(
                            c.env, previous, cid, dict(v), source, extra,
                            f"每笔 {money(v['fixed'])} · 预算 {money(v['budget'])} · {v['leverage']}×",
                        )
                    elif c.read_switch():
                        raise ValueError('当前环境已有等待切换，请先取消或等待队列完成。')
                    else:
                        switch_to_env(c.env, cid, dict(v), source, extra, previous, confirmation, allow_existing_exposure=resuming_own)
                else:
                    spawn_follower(c.env, cid, v, source, extra)
                return
            if command in ('run', 'enable') or current not in active_states:
                if command not in ('run', 'enable'):
                    messagebox.showinfo('跟单服务', f'当前{c.paths.env_title}没有运行中的跟单后台，无需执行该操作。', parent=root)
                return
            if command == 'toggle_pause':
                command = 'run' if current in ('PAUSED', 'DEMO_PAUSED') else 'pause'
            elif command == 'resume':
                if current not in ('PAUSED', 'DEMO_PAUSED'):
                    return
                command = 'run'
            c.send_command(command)
            root.after(200, sync_view)
        except (OSError, KeyError, ValueError, ArithmeticError) as exc:
            messagebox.showerror('跟单服务', str(exc), parent=root)

    def place_pnl_cells(_event=None):
        if table is None or not table.winfo_exists():
            return
        for cid, label in pnl_cells.items():
            box = table.bbox(cid, '今日盈亏')
            if box:
                x, y, width, height = box
                label.configure(background=PALETTE['selected_bg'] if cid in table.selection() else '#ffffff')
                label.place(x=x, y=y, width=width, height=height)
            else:
                label.place_forget()

    def update_tab_pills():
        for env_code, btn in tab_buttons.items():
            if not btn.winfo_exists():
                continue
            r = running(env_code)
            st = r['state']
            is_active = env_code == active_env
            prefix = 'Demo 模拟跟单' if env_code == DEMO_ENV else '正式实盘跟单'
            if st in active_states and r.get('strategy'):
                code, _ = split_strategy_label(r['strategy'])
                suffix = f' · {code} 运行中' if st == 'DEMO_RUNNING' else f' · {code} 已暂停'
            elif env_code == PROD_ENV:
                suffix = ' · 未启用'
            else:
                suffix = ''
            btn.configure(
                text=prefix + suffix,
                bg=PALETTE['primary'] if is_active else '#ffffff',
                fg='#ffffff' if is_active else PALETTE['text'],
                activebackground=PALETTE['primary_hover'] if is_active else '#edf3fe',
                activeforeground='#ffffff' if is_active else PALETTE['text'],
            )

    def sync_view():
        if not root.winfo_exists():
            return
        update_tab_pills()
        c = ctx()
        row = running(c.env)
        queued = c.read_switch()
        status = row['state']
        is_live_mode = row.get('mode') == c.paths.mode_code
        when = row.get('last_connected_at') or row.get('last_reconciled_at')

        # Card 2: Current Active Strategy
        if status in active_states and row.get('strategy'):
            c.active_strategy_line.set(display_strategy(row['strategy']))
            sub_txt = '● 运行中' if status == 'DEMO_RUNNING' else f"● {state_label(c.env, status)}"
            c.active_strategy_sub.set(sub_txt)
            if active_strat_sub_widget is not None and active_strat_sub_widget.winfo_exists():
                active_strat_sub_widget.configure(foreground=PALETTE['gain'] if status == 'DEMO_RUNNING' else '#b45309')
        else:
            c.active_strategy_line.set('暂无活动策略')
            c.active_strategy_sub.set('● 真实资金交易关闭' if c.env == PROD_ENV else '● 未运行')
            if active_strat_sub_widget is not None and active_strat_sub_widget.winfo_exists():
                active_strat_sub_widget.configure(foreground=PALETTE['muted'])

        # Card 3: Service Status
        c.service_line.set(state_label(c.env, status))
        if when:
            c.service_sub.set(f"最近对账 {datetime.fromtimestamp(when):%H:%M:%S}")
        elif c.env == PROD_ENV:
            pf = c.preflight_cache
            if pf:
                c.service_sub.set(f"预检：{pf['status']} · {pf['position_mode']} / {pf['actual_leverage']}×")
            else:
                c.service_sub.set('生产下单默认关闭')
        else:
            c.service_sub.set('最近对账 —')

        # Info / Queue bar text
        if queued:
            c.status_note.set(
                f"等待切换：{display_strategy(queued['from_strategy'])} 平仓后切换至 {display_strategy(queued['target']['strategy'])} · "
                f"{queued.get('note', '等待原仓退出及对账')}"
            )
        elif c.transition:
            c.status_note.set(f'正在核对 {c.paths.env_title} 账户或等待跟单服务切换…')
        elif status in active_states and row.get('strategy'):
            c.status_note.set(
                f"当前运行：{display_strategy(row['strategy'])}（{c.paths.env_title}）  |  "
                f"选择其他配置点击启用可进入安全排队切换。"
                + (f" · {row['reason']}" if row.get('reason') else '')
            )
        elif c.env == PROD_ENV:
            pf = c.preflight_cache
            if pf and pf.get('blockers'):
                c.status_note.set(f"正式实盘预检阻断：{pf['blockers'][0]}  |  真实资金下单默认关闭。")
            else:
                c.status_note.set('Binance 正式实盘环境（真实资金交易默认关闭）。保存配置不自动启动，须预检通过并经独立确认后方可启用。')
        else:
            c.status_note.set(
                (row.get('reason') + '  |  ' if row.get('reason') else '')
                + 'Binance Demo 模拟资金跟单。同一环境最多运行一套策略，保存配置不会自动启动。'
            )

        if queue_cancel_btn is not None and queue_cancel_btn.winfo_exists():
            if queued:
                queue_cancel_btn.pack(side='right', padx=(8, 0))
                queue_cancel_btn.configure(
                    state='disabled' if queued['state'] in ('STARTING', 'STOPPING') or status == 'DEMO_STOP_PENDING' else 'normal'
                )
            else:
                queue_cancel_btn.pack_forget()

        # Table refresh
        if table is not None and table.winfo_exists():
            env_total = Decimal(0)
            has_total = False
            rows_cache = paper_rows()
            for cid in table.get_children():
                if cid not in c.configs:
                    continue
                table.set(cid, '状态', row_status(c.env, cid, row))
                owned = is_live_mode and row.get('copy_config_id') == cid and row.get('strategy') == c.configs[cid]['strategy']
                try:
                    data = demo_copy_data(c.ledger_dir(cid))
                    if owned and data['active']:
                        refresh_marks(c.env)
                    direction, notional, _, _, _ = metrics(c, data, owned=owned)
                    total = data['today']
                    recent = data['triggers'][-1] if data['triggers'] else None
                    if recent:
                        _, fill, _, _, reason = recent
                        prefix_act = '正式 ' if c.env == PROD_ENV else 'Demo '
                        action = f"纸盘 {paper_event_label(fill)} · {event_reasons.get(reason, reason) or prefix_act + data['action']}"
                    else:
                        action = data['action']
                except (OSError, sqlite3.Error, ValueError, ArithmeticError, TypeError):
                    direction, notional, action, total = '待核对', '—', '执行账本读取失败', None
                table.set(cid, '持仓方向', direction)
                table.set(cid, '持仓金额(USDT)', notional)
                table.set(cid, '今日盈亏', pnl_money(total))
                table.set(cid, '最近动作', action)
                emphasis = (
                    'env-active' if owned and status == 'DEMO_RUNNING'
                    else 'copy-warning' if queued and queued.get('target_id') == cid
                    else 'copy-warning' if row.get('copy_config_id') == cid and status in (
                        'DEMO_PAUSED', 'PAUSED', 'RECONCILE_REQUIRED', 'PROTECTION_INCOMPLETE'
                    )
                    else ''
                )
                table.item(cid, tags=(emphasis,) if emphasis else ())
                if cid not in pnl_cells:
                    label = tk.Label(table, anchor='e', padx=6, bd=0, font=('Microsoft YaHei UI', 9, 'bold'))
                    label.bind('<Button-1>', lambda _e, key=cid: table.selection_set(key))
                    label.bind('<Double-Button-1>', lambda _e, key=cid: trade_details(c.env, key))
                    label.bind('<Button-3>', lambda e, key=cid: context_menu(e, table, False, cid=key))
                    pnl_cells[cid] = label
                pnl_cells[cid].configure(text=pnl_money(total), foreground=pnl_color(total))
                if total is not None:
                    has_total = True
                    env_total += total
            c.pnl_value.set(pnl_money(env_total) if has_total else '暂无记录')
            if pnl_widget is not None and pnl_widget.winfo_exists():
                pnl_widget.configure(foreground=pnl_color(env_total if has_total else None))
            place_pnl_cells()

        # Footer button states (strictly driven by current env's real backend state)
        cid = selected_config(c.env)
        same = cid is not None and row.get('copy_config_id') == cid
        for name, button in controls.items():
            if not button.winfo_exists():
                continue
            if name == 'enable':
                enabled = (
                    not c.transition
                    and not (same and is_live_mode and status in active_states)
                    and status in ('STOPPED', 'MONITORING', 'PAUSED', 'DEMO_RUNNING', 'DEMO_PAUSED', 'DEMO_STOP_PENDING')
                )
                button.configure(state='normal' if enabled else 'disabled')
            elif name == 'toggle_pause':
                is_paused = status in ('PAUSED', 'DEMO_PAUSED')
                button.configure(text='▶ 恢复跟单' if is_paused else 'Ⅱ 暂停新开仓')
                enabled = not c.transition and status in ('MONITORING', 'DEMO_RUNNING', 'PAUSED', 'DEMO_PAUSED')
                button.configure(state='normal' if enabled else 'disabled')
            elif name == 'stop':
                enabled = not c.transition and status in active_states and status != 'DEMO_STOP_PENDING'
                button.configure(state='normal' if enabled else 'disabled')

    def update_state():
        if not root.winfo_exists():
            return
        sync_view()
        root.after(1000, update_state)

    def details(env_code, cid):
        c = ctx(env_code)
        v = c.configs[cid]
        label = display_strategy(v['strategy'])
        tech = f"\n内部策略 ID：{v['strategy']} · 配置 ID：{cid}" if label != v['strategy'] else f"\n配置 ID：{cid}"
        messagebox.showinfo(
            '查看跟单配置',
            f"{label} · {money(v['fixed'])} / 笔 · {v['leverage']}×\n"
            f"总预算：{money(v['budget'])} · 交易环境：{c.paths.env_title}\n"
            f"执行账户：{v.get('account', c.paths.default_account_label)}\n"
            f"创建时间：{v['created_at']}{tech}",
            parent=root,
        )

    def trade_details(env_code, cid):
        c = ctx(env_code)
        v = c.configs[cid]
        key = (env_code, cid, v['strategy'])
        existing_win = detail_windows.get(key)
        if existing_win is not None and existing_win.winfo_exists():
            existing_win.deiconify()
            existing_win.lift()
            existing_win.focus_set()
            return
        window = tk.Toplevel(root)
        detail_windows[key] = window
        env_short = '正式实盘' if env_code == PROD_ENV else 'Demo'
        window.title(f"{display_strategy(v['strategy'])} · {env_short}跟单交易明细")
        window.geometry('1260x580')
        window.configure(bg=PALETTE['bg'])
        summary = ttk.Label(window, padding=12, justify='left')
        summary.pack(fill='x')
        unreal_label = ttk.Label(window, padding=(12, 0, 12, 8))
        unreal_label.pack(fill='x')
        tabs = ttk.Notebook(window)
        tabs.pack(fill='both', expand=True, padx=12)
        trigger_panel = ttk.Frame(tabs)
        panel = ttk.Frame(tabs)
        tabs.add(trigger_panel, text='纸盘触发记录')
        tabs.add(panel, text=f'{env_short} 交易所实际成交')
        headings = ('触发时间（北京）', '动作', '纸盘交易 ID', '消费状态', '执行结果', '原因', '纸盘成交 ID')
        trigger = ttk.Treeview(trigger_panel, columns=headings, show='headings')
        scroll = ttk.Scrollbar(trigger_panel, orient='vertical', command=trigger.yview)
        cross = ttk.Scrollbar(trigger_panel, orient='horizontal', command=trigger.xview)
        trigger.configure(yscrollcommand=scroll.set, xscrollcommand=cross.set)
        scroll.pack(side='right', fill='y')
        cross.pack(side='bottom', fill='x')
        trigger.pack(fill='both', expand=True)
        for column, width in zip(headings, (160, 65, 280, 80, 110, 250, 265)):
            trigger.heading(column, text=column)
            trigger.column(column, width=width, stretch=False)
        columns = (
            '成交／判定时间（北京）', '开仓／平仓', '买卖', '持仓方向', '数量 BTC', '均价 USDT',
            '金额', '手续费', '已实现净盈亏', '纸盘成交 ID', f'{env_short} 订单ID', '执行结果',
        )
        tree = ttk.Treeview(panel, columns=columns, show='headings')
        sy = ttk.Scrollbar(panel, orient='vertical', command=tree.yview)
        sx = ttk.Scrollbar(panel, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side='right', fill='y')
        sx.pack(side='bottom', fill='x')
        tree.pack(fill='both', expand=True)
        color_rows(tree)
        for column, width in zip(columns, (205, 86, 55, 82, 100, 130, 115, 115, 120, 325, 160, 140)):
            tree.heading(column, text=column)
            tree.column(column, width=width, stretch=False)
        raw_note = ttk.Label(window, padding=8)
        raw_note.pack(fill='x')
        exact = {}

        def draw():
            try:
                data = demo_copy_data(c.ledger_dir(cid))
                row = running(env_code)
                owned = row.get('mode') == c.paths.mode_code and row.get('copy_config_id') == cid and row.get('strategy') == v['strategy']
                direction, notional, unreal, margin, qty = metrics(c, data, owned=owned)
                summary.configure(
                    text=(
                        f"{display_strategy(v['strategy'])} · {c.paths.env_title} · 配置 {cid} · {v['mode']} · "
                        f"每笔 {money(v['fixed'])} · 总预算 {money(v['budget'])} · {v['leverage']}× · {row_status(env_code, cid, row)}\n"
                        f"持仓 {direction} {qty} · 名义金额 {notional} · 初始保证金 {money(margin)}\n"
                        f"今日已平仓 {pnl_money(data['today'])} · 累计已实现 {pnl_money(data['total'])}（已扣交易费，资金费前）"
                    ),
                    foreground=pnl_color(data['total']),
                )
                unreal_label.configure(text=f'当前未实现 {pnl_money(unreal)}', foreground=pnl_color(unreal))
                tree.delete(*tree.get_children())
                exact.clear()
                for stamp, effect, side, direction, qty, price, amount, fee, net, event, oid in data['trades']:
                    iid = tree.insert(
                        '', 'end',
                        values=(
                            stamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3], effect,
                            {'BUY': '买', 'SELL': '卖'}.get(side, side),
                            {'LONG': '做多', 'SHORT': '做空'}.get(direction, '—'),
                            f'{qty:f}', f'{price:f}', money(amount), money(fee), pnl_money(net), event, oid, '已成交',
                        ),
                        tags=(pnl_tag(net),),
                    )
                    exact[iid] = (fee, net)
                trigger.delete(*trigger.get_children())
                outcomes = {
                    'SKIPPED': '已跳过', 'SKIPPED_MIN_ORDER': '已跳过', 'NO_FILL': '未成交',
                    'RECOVERED': '已恢复', 'CLOSED': '已平仓', 'FOLLOW_PAPER': '已跟单', 'PROTECTED': '已跟单',
                }
                for event, fill, status_v, result_v, reason_v in reversed(data['triggers']):
                    when_txt = datetime.fromisoformat(fill['occurred_at']).astimezone(BEIJING).strftime('%Y-%m-%d %H:%M:%S')
                    trigger.insert(
                        '', 'end',
                        values=(
                            when_txt, paper_event_label(fill), fill['trade_id'],
                            '已消费' if status_v == 'PLANNED' else '待消费',
                            outcomes.get(result_v, result_v or '尚无执行回执'),
                            event_reasons.get(reason_v, reason_v) or '—', event,
                        ),
                    )
                raw_note.configure(text='选中成交可查看原始精度' if exact else f'暂无{env_short}实际成交')
            except (OSError, sqlite3.Error, ValueError, ArithmeticError, TypeError):
                raw_note.configure(text='读取跟单回执失败，请刷新')

        def refresh_win():
            refresh_marks(env_code)
            draw()

        def on_select(_event):
            if tree.selection():
                fee, net = exact.get(tree.selection()[0], (None, None))
                raw_note.configure(text=f"原始精度 USDT  手续费 {fee if fee is not None else '—'}  已实现净盈亏 {net if net is not None else '—'}")

        tree.bind('<<TreeviewSelect>>', on_select)
        ttk.Button(window, text='刷新', command=refresh_win).pack(anchor='e', padx=12, pady=8)
        refresh_win()
        window.after(2500, lambda: draw() if window.winfo_exists() else None)

    def copy_to_other_env(source_env, cid):
        target_env = PROD_ENV if source_env == DEMO_ENV else DEMO_ENV
        src_ctx = ctx(source_env)
        dst_ctx = ctx(target_env)
        src_cfg = src_ctx.configs.get(cid)
        if not src_cfg:
            return
        t_defaults = env_defaults(target_env)
        new_cid = uuid4().hex
        copied = copy_config_payload(
            src_cfg,
            target_environment=target_env,
            new_cid=new_cid,
            created_at=datetime.now().isoformat(),
            account_label=t_defaults['account'],
            account_id=t_defaults['account_id'] or None,
        )
        dst_ctx.configs[new_cid] = copied
        dst_ctx.persist()
        dst_ctx.selected_id = new_cid
        messagebox.showinfo(
            '配置已复制',
            f"已将「{display_strategy(copied['strategy'])}」的参数复制到【{dst_ctx.paths.env_title}】\n"
            f"新配置 ID：{new_cid[:10]}（状态：已保存，未自动启动）\n"
            f"历史订单、账本、游标与交易授权均未复制。",
            parent=root,
        )

    def new_config():
        nonlocal editing_env, draft_cid
        c = ctx()
        editing_env = c.env
        draft_cid = None
        for key, value in env_defaults(c.env).items():
            fields[key].set(value)
        render(1)

    def edit(env_code, cid):
        nonlocal editing_env, draft_cid
        c = ctx(env_code)
        if busy(env_code, cid):
            messagebox.showerror('编辑配置', '请先安全停止并完成对账。', parent=root)
            return
        editing_env = env_code
        draft_cid = cid
        c.selected_id = cid
        defs = env_defaults(env_code)
        for key, value in fields.items():
            value.set(c.configs[cid].get(key, defs.get(key, '')))
        render(1)

    def archive(env_code, cid):
        c = ctx(env_code)
        if busy(env_code, cid):
            messagebox.showerror('归档', '配置正在运行或仍有关联仓位、未决委托。', parent=root)
            return
        c.configs[cid]['archived_at'] = datetime.now().isoformat(timespec='seconds')
        c.persist()
        render(0)

    def restore(env_code, cid, window):
        c = ctx(env_code)
        c.configs[cid]['archived_at'] = None
        c.persist()
        window.destroy()
        render(0)

    def context_menu(event, tree, archived, window=None, *, cid=None, env_code=None):
        e_code = env_code or active_env
        cid = cid or tree.identify_row(event.y)
        if not cid:
            return
        tree.selection_set(cid)
        menu = tk.Menu(tree, tearoff=False)
        menu.add_command(label='查看配置', command=lambda: details(e_code, cid))
        menu.add_command(label='查看交易明细', command=lambda: trade_details(e_code, cid))
        if archived:
            menu.add_command(label='恢复配置', command=lambda: restore(e_code, cid, window))
        else:
            menu.add_command(label='编辑配置', command=lambda: edit(e_code, cid))
            copy_label = '复制到正式实盘' if e_code == DEMO_ENV else '复制到 Demo 模拟'
            menu.add_command(label=copy_label, command=lambda: copy_to_other_env(e_code, cid))
            menu.add_separator()
            menu.add_command(label='只读诊断（不下单）', command=lambda: control('run', e_code, cid))
            menu.add_command(label='查看账户只读预检', command=lambda: show_preflight_dialog(e_code))
            menu.add_separator()
            menu.add_command(label='归档', command=lambda: archive(e_code, cid))
        menu.tk_popup(event.x_root, event.y_root)

    def archives():
        c = ctx()
        window = tk.Toplevel(root)
        window.title(f'{c.paths.env_title} · 归档库')
        window.geometry('660x360')
        tree = ttk.Treeview(window, columns=('策略', '每笔 USDT', '杠杆', '归档时间'), show='headings', style='Copy.Treeview')
        for col in tree['columns']:
            tree.heading(col, text=col)
            tree.column(col, width=210 if col == '策略' else 140)
        for cid, v in c.configs.items():
            if v.get('archived_at'):
                tree.insert('', 'end', iid=cid, values=(display_strategy(v['strategy']), money(v['fixed']), v['leverage'] + '×', v['archived_at']))
        tree.pack(fill='both', expand=True, padx=10, pady=10)
        tree.bind('<Button-3>', lambda e: context_menu(e, tree, True, window, env_code=c.env))

    def show_records():
        c = ctx()
        cid = selected_config(c.env)
        if cid not in c.configs:
            messagebox.showinfo('跟单记录', '请先在列表中选择一条跟单配置。', parent=root)
            return
        trade_details(c.env, cid)

    def values():
        return {key: value.get() for key, value in fields.items()}

    def save(*, destination=0, enable_after=False):
        nonlocal draft_cid
        c = ctx(editing_env)
        try:
            v = values()
            if v['strategy'] not in paper_rows() or v['mode'] != '定额跟单':
                raise ValueError('请选择有效的纸盘策略。')
            budget = Decimal(v['budget'])
            if not budget.is_finite() or budget <= 0 or not 1 <= int(v['leverage']) <= MAX_COPY_LEVERAGE:
                raise ValueError(f'总预算须大于 0，计划杠杆须为 1–{MAX_COPY_LEVERAGE}×。')
            if not 0 < Decimal(v['fixed']) <= budget:
                raise ValueError('每笔保证金须大于 0 且不超过总资金上限。')
            optional_exit(v, 'custom_sl', 'stop')
            optional_exit(v, 'custom_tp', 'tp')
            target_cid = draft_cid if draft_cid else uuid4().hex
            prior = c.configs.get(target_cid, {})
            defs = env_defaults(c.env)
            v['account'] = defs['account']
            v['account_id'] = defs['account_id']
            if target_cid in c.configs and busy(c.env, target_cid):
                raise ValueError('请先安全停止并完成对账，再编辑此配置。')
            if (
                prior
                and v['strategy'] != prior['strategy']
                and (prior.get('legacy_demo_ledger') or (c.ledger_dir(target_cid) / 'demo-execution-inbox.sqlite3').is_file())
            ):
                raise ValueError('已关联账本的配置不能更换策略，请新建配置。')
            c.configs[target_cid] = {
                **prior, **v, **env_policy(c.env),
                'copy_config_id': target_cid,
                'created_at': prior.get('created_at', datetime.now().isoformat()),
                'archived_at': None,
            }
            c.persist()
            c.selected_id = target_cid
            draft_cid = None
            render(destination)
            if enable_after:
                control('enable', c.env, target_cid)
        except (ValueError, ArithmeticError, OSError) as exc:
            messagebox.showerror('保存失败', str(exc) or '请检查预算、单笔保证金、杠杆和百分比。', parent=root)

    def switch_environment(target_env):
        nonlocal active_env
        if target_env == active_env:
            return
        active_env = target_env
        # Never starts, pauses, or stops any service; only changes view!
        render(0)
        refresh_account(active_env, force=False)

    # Top persistent header frame
    header = tk.Frame(root, bg=PALETTE['header_bg'], padx=20, pady=12, highlightbackground=PALETTE['header_border'], highlightthickness=1)
    header.pack(fill='x')

    brand = tk.Frame(header, bg=PALETTE['header_bg'])
    brand.pack(side='left')
    badge = tk.Canvas(brand, width=42, height=42, bg=PALETTE['header_bg'], highlightthickness=0)
    badge.pack(side='left', padx=(0, 12))
    badge.create_oval(2, 2, 40, 40, fill='#f59e0b', outline='#d97706', width=1)
    badge.create_text(21, 21, text='₿', fill='#ffffff', font=('Segoe UI', 19, 'bold'))
    title_col = tk.Frame(brand, bg=PALETTE['header_bg'])
    title_col.pack(side='left')
    tk.Label(title_col, text='BTC 实盘跟单中心', bg=PALETTE['header_bg'], fg='#0f2447',
             font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w')
    tk.Label(title_col, text='Binance USDⓈ-M · 跟随纸盘已成交订单', bg=PALETTE['header_bg'], fg='#4b6182',
             font=('Microsoft YaHei UI', 9)).pack(anchor='w')

    # Top-right dual-environment pill tabs
    tab_shell = tk.Frame(header, bg='#ffffff', padx=4, pady=4, highlightbackground='#c5d7f2', highlightthickness=1)
    tab_shell.pack(side='right')
    for env_code, default_txt in ((DEMO_ENV, 'Demo 模拟跟单'), (PROD_ENV, '正式实盘跟单')):
        btn = tk.Button(
            tab_shell, text=default_txt, bd=0, relief='flat', cursor='hand2',
            padx=16, pady=7, font=('Microsoft YaHei UI', 9, 'bold'),
            command=lambda e=env_code: switch_environment(e),
        )
        btn.pack(side='left', padx=2)
        tab_buttons[env_code] = btn

    page = tk.Frame(root, bg=PALETTE['bg'], padx=20, pady=12)
    page.pack(fill='both', expand=True)

    def render_steps(parent, active_step):
        bar = tk.Frame(parent, bg=PALETTE['bg'])
        bar.pack(fill='x', pady=(0, 10))
        inner = tk.Frame(bar, bg=PALETTE['bg'])
        inner.pack(anchor='center')
        steps_info = [('1', '选择策略'), ('2', '跟单设置'), ('3', '高级设置'), ('4', '确认启用')]
        for idx, (num, label) in enumerate(steps_info, 1):
            done = idx < active_step
            current_s = idx == active_step
            if current_s and active_step == 4:
                pill = tk.Frame(inner, bg='#e0ecff', padx=10, pady=3)
                pill.pack(side='left')
                tk.Label(pill, text=num, bg=PALETTE['primary'], fg='#ffffff', width=2, font=('Microsoft YaHei UI', 8, 'bold')).pack(side='left', padx=(0, 6))
                tk.Label(pill, text=label, bg='#e0ecff', fg=PALETTE['primary'], font=('Microsoft YaHei UI', 9, 'bold')).pack(side='left')
            else:
                bg_c = PALETTE['primary'] if (done or current_s) else '#cbd5e1'
                fg_t = PALETTE['text'] if (done or current_s) else '#94a3b8'
                tk.Label(inner, text='✓' if done else num, bg=bg_c, fg='#ffffff', width=2, font=('Microsoft YaHei UI', 8, 'bold')).pack(side='left', padx=(0, 5))
                tk.Label(inner, text=label, bg=PALETTE['bg'], fg=fg_t, font=('Microsoft YaHei UI', 9, 'bold' if current_s else 'normal')).pack(side='left')
            if idx < 4:
                tk.Frame(inner, bg='#cbd9f0', height=2, width=42).pack(side='left', padx=10)

    def render(index):
        nonlocal current_page, table, pnl_widget, active_strat_sub_widget, queue_cancel_btn
        current_page = index
        table = None
        pnl_widget = None
        active_strat_sub_widget = None
        queue_cancel_btn = None
        controls.clear()
        pnl_cells.clear()
        for widget in page.winfo_children():
            widget.destroy()

        c = ctx()
        update_tab_pills()

        footer = tk.Frame(page, bg=PALETTE['bg'], pady=10)
        footer.pack(side='bottom', fill='x')
        body = tk.Frame(page, bg=PALETTE['bg'])
        body.pack(fill='both', expand=True)

        if index == 0:
            # 1. Top 4 aligned stat cards
            stats = tk.Frame(body, bg=PALETTE['bg'])
            stats.pack(fill='x', pady=(0, 12))
            stats.grid_rowconfigure(0, weight=1)
            card_specs = [
                ('Demo 可用 USDT' if c.env == DEMO_ENV else '正式可用 USDT', c.account_line, c.account_updated, '💳', '#eaf2ff', PALETTE['primary'], False),
                ('当前跟单策略', c.active_strategy_line, c.active_strategy_sub, '◎', '#f1ecff', '#6d28d9', 'active_sub'),
                ('服务状态', c.service_line, c.service_sub, '▶', '#e8f8ee', PALETTE['gain'], False),
                ('今日已平仓净盈亏（资金费前）', c.pnl_value, c.pnl_sub, '📊', '#e8f8ee', PALETTE['gain'], 'pnl'),
            ]
            for col_idx, (title_t, val_var, sub_var, icon_ch, icon_bg, icon_fg, tag_kind) in enumerate(card_specs):
                stats.grid_columnconfigure(col_idx, weight=1, uniform='stat_card')
                card = tk.Frame(
                    stats, bg=PALETTE['card_bg'], padx=14, pady=12,
                    highlightbackground=PALETTE['card_border'], highlightthickness=1,
                )
                card.grid(row=0, column=col_idx, sticky='nsew', padx=(0 if col_idx == 0 else 6, 0 if col_idx == 3 else 6))
                card.grid_columnconfigure(1, weight=1)
                icon_box = tk.Label(card, text=icon_ch, bg=icon_bg, fg=icon_fg, width=3, height=1, font=('Segoe UI Emoji', 13))
                icon_box.grid(row=0, column=0, rowspan=3, sticky='n', padx=(0, 10), pady=2)
                tk.Label(card, text=title_t, bg=PALETTE['card_bg'], fg=PALETTE['muted'], font=('Microsoft YaHei UI', 8), anchor='w').grid(row=0, column=1, sticky='ew')
                val_lbl = tk.Label(card, textvariable=val_var, bg=PALETTE['card_bg'], fg=PALETTE['text'], font=('Microsoft YaHei UI', 14, 'bold'), anchor='w')
                val_lbl.grid(row=1, column=1, sticky='ew', pady=(2, 2))
                sub_lbl = tk.Label(card, textvariable=sub_var, bg=PALETTE['card_bg'], fg=PALETTE['muted'], font=('Microsoft YaHei UI', 8), anchor='w')
                sub_lbl.grid(row=2, column=1, sticky='ew')
                if tag_kind == 'pnl':
                    pnl_widget = val_lbl
                elif tag_kind == 'active_sub':
                    active_strat_sub_widget = sub_lbl

            # 2. Strategy Config List Card
            list_card = tk.Frame(
                body, bg=PALETTE['card_bg'], padx=14, pady=12,
                highlightbackground=PALETTE['card_border'], highlightthickness=1,
            )
            list_card.pack(fill='both', expand=True, pady=(0, 10))
            hdr = tk.Frame(list_card, bg=PALETTE['card_bg'])
            hdr.pack(fill='x', pady=(0, 8))
            tk.Label(hdr, text='策略配置列表', bg=PALETTE['card_bg'], fg=PALETTE['text'],
                     font=('Microsoft YaHei UI', 11, 'bold')).pack(side='left')
            env_badge_bg = PALETTE['pill_blue_bg'] if c.env == DEMO_ENV else '#fee2e2'
            env_badge_fg = PALETTE['pill_blue_fg'] if c.env == DEMO_ENV else PALETTE['accent_red']
            tk.Label(
                hdr, text=f"{c.paths.env_title} · 共 {sum(not x.get('archived_at') for x in c.configs.values())} 条配置",
                bg=env_badge_bg, fg=env_badge_fg, padx=8, pady=2, font=('Microsoft YaHei UI', 8, 'bold'),
            ).pack(side='left', padx=(10, 0))
            if c.env == PROD_ENV:
                ttk.Button(hdr, text='正式账户只读预检', command=lambda: show_preflight_dialog(PROD_ENV)).pack(side='right')

            columns = ('编号', '策略名称', '配置 ID', '模式', '单笔保证金', '杠杆', '状态', '持仓方向', '持仓金额(USDT)', '今日盈亏', '最近动作')
            table_wrap = tk.Frame(list_card, bg=PALETTE['card_bg'])
            table_wrap.pack(fill='both', expand=True)
            table = ttk.Treeview(table_wrap, columns=columns, show='headings', height=7, style='Copy.Treeview')
            ys = ttk.Scrollbar(table_wrap, orient='vertical', command=table.yview)
            table.configure(yscrollcommand=ys.set)
            ys.pack(side='right', fill='y')
            table.pack(side='left', fill='both', expand=True)

            widths = {
                '编号': 62, '策略名称': 148, '配置 ID': 96, '模式': 76, '单笔保证金': 96,
                '杠杆': 52, '状态': 112, '持仓方向': 68, '持仓金额(USDT)': 110, '今日盈亏': 102, '最近动作': 220,
            }
            for col in columns:
                table.heading(col, text=col)
                anchor = 'e' if col in ('单笔保证金', '持仓金额(USDT)', '今日盈亏') else 'w' if col in ('策略名称', '最近动作') else 'center'
                table.column(col, width=widths[col], minwidth=48, anchor=anchor, stretch=(col == '最近动作'))

            table.tag_configure('env-active', foreground=PALETTE['accent_red'], font=('Microsoft YaHei UI', 9, 'bold'))
            table.tag_configure('copy-warning', foreground='#a15c09')

            rows_cache = paper_rows()
            cur_run = running(c.env)
            for cid, v in c.configs.items():
                if not v.get('archived_at'):
                    code, sname = split_strategy_label(v['strategy'], rows_cache)
                    table.insert(
                        '', 'end', iid=cid,
                        values=(
                            code, sname, cid[:10], v['mode'], money(v['fixed']),
                            v['leverage'] + '×', row_status(c.env, cid, cur_run), '—', '—', '—', '等待成交',
                        ),
                    )
            if c.selected_id in table.get_children():
                table.selection_set(c.selected_id)

            def on_select_row(_e):
                if table.selection():
                    c.selected_id = table.selection()[0]
                place_pnl_cells()
                sync_view()

            table.bind('<Button-3>', lambda e: context_menu(e, table, False, env_code=c.env))
            table.bind('<<TreeviewSelect>>', on_select_row, add='+')
            table.bind('<Configure>', place_pnl_cells, add='+')

            def on_row_double_click(event):
                cid_hit = table.identify_row(event.y)
                if cid_hit in c.configs and table.identify_region(event.x, event.y) in ('cell', 'tree'):
                    table.selection_set(cid_hit)
                    trade_details(c.env, cid_hit)
                    return 'break'

            table.bind('<Double-Button-1>', on_row_double_click)

            sub_bar = tk.Frame(list_card, bg=PALETTE['card_bg'], pady=6)
            sub_bar.pack(fill='x')
            ttk.Button(sub_bar, text='📁 归档库', command=archives).pack(side='left')
            tk.Label(sub_bar, text='提示：左键选中 · 双击查看交易明细 · 右键编辑/复制配置/只读诊断',
                     bg=PALETTE['card_bg'], fg=PALETTE['muted'], font=('Microsoft YaHei UI', 8)).pack(side='right')

            # 3. Info / Queue Banner
            info_bar = tk.Frame(
                body, bg=PALETTE['info_bg'], padx=14, pady=9,
                highlightbackground=PALETTE['info_border'], highlightthickness=1,
            )
            info_bar.pack(fill='x')
            tk.Label(info_bar, text='ⓘ', bg=PALETTE['info_bg'], fg=PALETTE['primary'], font=('Microsoft YaHei UI', 10, 'bold')).pack(side='left', padx=(0, 8))
            tk.Label(info_bar, textvariable=c.status_note, bg=PALETTE['info_bg'], fg=PALETTE['text'],
                     font=('Microsoft YaHei UI', 9), wraplength=920, justify='left').pack(side='left', fill='x', expand=True)
            queue_cancel_btn = ttk.Button(info_bar, text='取消等待切换', command=lambda: cancel_switch(c.env))

            # 4. Simplified Bottom Action Bar (Section 13)
            ttk.Button(footer, text='＋ 新建跟单', style='Primary.TButton', command=new_config).pack(side='left', padx=(0, 6))
            enable_txt = '▶ 启用正式实盘' if c.env == PROD_ENV else '▶ 启用Demo'
            controls['enable'] = ttk.Button(footer, text=enable_txt, command=lambda: control('enable', c.env))
            controls['enable'].pack(side='left', padx=4)
            controls['toggle_pause'] = ttk.Button(footer, text='Ⅱ 暂停新开仓', command=lambda: control('toggle_pause', c.env))
            controls['toggle_pause'].pack(side='left', padx=4)
            controls['stop'] = ttk.Button(footer, text='■ 停止跟单', command=lambda: control('stop', c.env))
            controls['stop'].pack(side='left', padx=4)
            ttk.Button(footer, text='↻ 刷新账户', command=lambda: refresh_account(c.env)).pack(side='left', padx=4)
            ttk.Button(footer, text='📄 查看记录', command=show_records).pack(side='left', padx=4)
            ttk.Button(footer, text='📈 打开实盘K线', command=open_selected_kline).pack(side='right')
            sync_view()
            return

        # Step 1 (选择策略 + 跟单设置) or Step 2 (高级设置 + 确认启用)
        render_steps(body, 2 if index == 1 else 4)
        split = tk.Frame(body, bg=PALETTE['bg'])
        split.pack(fill='both', expand=True)
        split.grid_columnconfigure(0, weight=1, uniform='cfg_col')
        split.grid_columnconfigure(1, weight=1, uniform='cfg_col')
        split.grid_rowconfigure(0, weight=1)

        left_card = tk.Frame(split, bg=PALETTE['card_bg'], padx=18, pady=14, highlightbackground=PALETTE['card_border'], highlightthickness=1)
        right_card = tk.Frame(split, bg=PALETTE['card_bg'], padx=18, pady=14, highlightbackground=PALETTE['card_border'], highlightthickness=1)
        left_card.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        right_card.grid(row=0, column=1, sticky='nsew', padx=(8, 0))

        e_ctx = ctx(editing_env)

        if index == 1:
            tk.Label(left_card, text='选择纸盘策略', bg=PALETTE['card_bg'], fg=PALETTE['text'],
                     font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w', pady=(0, 8))
            search_var = tk.StringVar(root, value='')
            search_row = tk.Frame(left_card, bg='#f8fafd', padx=8, pady=4, highlightbackground='#cbd9f0', highlightthickness=1)
            search_row.pack(fill='x', pady=(0, 8))
            tk.Label(search_row, text='🔍', bg='#f8fafd', fg=PALETTE['muted']).pack(side='left', padx=(0, 6))
            search_entry = tk.Entry(search_row, textvariable=search_var, bd=0, bg='#f8fafd', fg=PALETTE['text'], font=('Microsoft YaHei UI', 9))
            search_entry.pack(side='left', fill='x', expand=True)

            rows = paper_rows()
            ordered_sids = tuple(rows.keys())
            if not fields['strategy'].get() and ordered_sids:
                fields['strategy'].set('C' if 'C' in rows else ordered_sids[0])
            existing_sids = {cfg['strategy'] for cfg in e_ctx.configs.values() if not cfg.get('archived_at')}
            adapter_cache = {sid: adapter_status(sid) for sid in ordered_sids}

            # Scrollable canvas of 16 strategy cards
            list_outer = tk.Frame(left_card, bg=PALETTE['card_bg'])
            list_outer.pack(fill='both', expand=True)
            canvas = tk.Canvas(list_outer, bg=PALETTE['card_bg'], highlightthickness=0)
            vscroll = ttk.Scrollbar(list_outer, orient='vertical', command=canvas.yview)
            inner_list = tk.Frame(canvas, bg=PALETTE['card_bg'])
            inner_win = canvas.create_window((0, 0), window=inner_list, anchor='nw')
            canvas.configure(yscrollcommand=vscroll.set)
            vscroll.pack(side='right', fill='y')
            canvas.pack(side='left', fill='both', expand=True)
            inner_list.bind('<Configure>', lambda _e: canvas.configure(scrollregion=canvas.bbox('all')))
            canvas.bind('<Configure>', lambda e: canvas.itemconfigure(inner_win, width=e.width))

            def _on_mousewheel(event):
                canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')

            canvas.bind('<MouseWheel>', _on_mousewheel)

            def render_strategy_cards(*_args):
                for w in inner_list.winfo_children():
                    w.destroy()
                q = search_var.get().strip().casefold()
                selected_sid = fields['strategy'].get()
                for sid in ordered_sids:
                    label = rows[sid]
                    if q and q not in (sid + ' ' + label).casefold():
                        continue
                    is_sel = sid == selected_sid
                    ready, reason = adapter_cache[sid]
                    if not ready:
                        badge_txt, b_bg, b_fg = '待适配', PALETTE['pill_gray_bg'], PALETTE['pill_gray_fg']
                    elif sid in existing_sids:
                        badge_txt, b_bg, b_fg = '已有配置', PALETTE['pill_blue_bg'], PALETTE['pill_blue_fg']
                    else:
                        badge_txt, b_bg, b_fg = '可启用', PALETTE['pill_green_bg'], PALETTE['pill_green_fg']

                    c_bg = '#eef5ff' if is_sel else '#ffffff'
                    c_bd = PALETTE['primary'] if is_sel else '#dce6f5'
                    item = tk.Frame(inner_list, bg=c_bg, padx=10, pady=7, highlightbackground=c_bd, highlightthickness=1, cursor='hand2')
                    item.pack(fill='x', pady=3)
                    top_r = tk.Frame(item, bg=c_bg)
                    top_r.pack(fill='x')
                    radio = tk.Label(top_r, text='◉' if is_sel else '○', bg=c_bg,
                                     fg=PALETTE['primary'] if is_sel else '#94a3b8', font=('Segoe UI', 11))
                    radio.pack(side='left', padx=(0, 8))
                    title_l = tk.Label(top_r, text=label, bg=c_bg, fg=PALETTE['text'],
                                       font=('Microsoft YaHei UI', 9, 'bold' if is_sel else 'normal'))
                    title_l.pack(side='left')
                    pill = tk.Label(top_r, text=badge_txt, bg=b_bg, fg=b_fg, padx=8, pady=1, font=('Microsoft YaHei UI', 8, 'bold'))
                    pill.pack(side='right')

                    if is_sel:
                        detail_txt = (
                            f"ⓘ 可启用{e_ctx.paths.env_title}定额跟单" + (f" · {reason}" if reason else '')
                            if ready else f"ⓘ 待适配，可保存草稿：{reason}"
                        )
                        sub_l = tk.Label(item, text=detail_txt, bg=c_bg, fg=PALETTE['primary'] if ready else '#b45309',
                                         font=('Microsoft YaHei UI', 8), wraplength=420, justify='left')
                        sub_l.pack(anchor='w', padx=(24, 0), pady=(3, 0))
                        sub_l.bind('<MouseWheel>', _on_mousewheel)

                    def choose(_e, chosen=sid):
                        fields['strategy'].set(chosen)
                        render_strategy_cards()

                    for widget in (item, top_r, radio, title_l, pill):
                        widget.bind('<Button-1>', choose)
                        widget.bind('<MouseWheel>', _on_mousewheel)

            search_var.trace_add('write', render_strategy_cards)
            render_strategy_cards()

            # Right Card: 跟单设置
            tk.Label(right_card, text='跟单设置', bg=PALETTE['card_bg'], fg=PALETTE['text'],
                     font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w', pady=(0, 12))

            def form_row(label_txt, key, choices=None, readonly_text=None):
                row_f = tk.Frame(right_card, bg=PALETTE['card_bg'])
                row_f.pack(fill='x', pady=6)
                tk.Label(row_f, text=label_txt, bg=PALETTE['card_bg'], fg=PALETTE['text'], width=22, anchor='w',
                         font=('Microsoft YaHei UI', 9)).pack(side='left')
                if readonly_text is not None:
                    box = tk.Label(row_f, text=readonly_text, bg='#eef5ff', fg=PALETTE['primary'], anchor='w',
                                   padx=10, pady=6, font=('Microsoft YaHei UI', 9, 'bold'),
                                   highlightbackground='#c7dcf8', highlightthickness=1)
                    box.pack(side='left', fill='x', expand=True)
                elif choices is not None:
                    cb = ttk.Combobox(row_f, textvariable=fields[key], values=choices, state='readonly')
                    cb.pack(side='left', fill='x', expand=True)
                else:
                    ent = ttk.Entry(row_f, textvariable=fields[key])
                    ent.pack(side='left', fill='x', expand=True)

            fields['mode'].set('定额跟单')
            defs = env_defaults(editing_env)
            fields['account'].set(defs['account'])
            fields['account_id'].set(defs['account_id'])
            form_row('跟单模式', 'mode', ('定额跟单',))
            form_row('每笔跟单保证金（USDT）', 'fixed')
            form_row('总资金上限（USDT）', 'budget')

            # Fixed opening leverage selector: displays 1倍..N倍 while persisting numeric string in fields['leverage']
            allowed_nums = tuple(e_ctx.allowed_leverages) if e_ctx.allowed_leverages else tuple(range(1, MAX_COPY_LEVERAGE + 1))
            lev_max_label = allowed_nums[-1]
            lev_labels = tuple(f'{n}倍' for n in allowed_nums)
            cur_lev_str = fields['leverage'].get() or '5'
            if cur_lev_str not in tuple(str(n) for n in allowed_nums):
                cur_lev_str = str(min(int(cur_lev_str) if cur_lev_str.isdigit() else 5, lev_max_label))
                fields['leverage'].set(cur_lev_str)
            lev_choice = tk.StringVar(root, value=f'{cur_lev_str}倍')
            lev_row = tk.Frame(right_card, bg=PALETTE['card_bg'])
            lev_row.pack(fill='x', pady=6)
            tk.Label(lev_row, text=f'固定开仓杠杆（最高 {lev_max_label}×）', bg=PALETTE['card_bg'], fg=PALETTE['text'],
                     width=22, anchor='w', font=('Microsoft YaHei UI', 9)).pack(side='left')
            lev_cb = ttk.Combobox(lev_row, textvariable=lev_choice, values=lev_labels, state='readonly')
            lev_cb._choice_var = lev_choice
            lev_cb.pack(side='left', fill='x', expand=True)

            # Live opening notional preview & Binance actual leverage match box
            preview_box = tk.Frame(
                right_card, bg=PALETTE['info_bg'], padx=12, pady=9,
                highlightbackground=PALETTE['info_border'], highlightthickness=1,
            )
            preview_box.pack(fill='x', pady=(4, 6))
            preview_main = tk.Label(preview_box, bg=PALETTE['info_bg'], fg=PALETTE['text'], anchor='w',
                                    font=('Microsoft YaHei UI', 10, 'bold'))
            preview_main.pack(fill='x')
            preview_formula = tk.Label(preview_box, bg=PALETTE['info_bg'], fg=PALETTE['muted'], anchor='w',
                                       font=('Microsoft YaHei UI', 8))
            preview_formula.pack(fill='x', pady=(2, 0))
            preview_note = tk.Label(
                preview_box, bg=PALETTE['info_bg'], fg=PALETTE['muted'], anchor='w',
                font=('Microsoft YaHei UI', 8),
                text='计划名义价值，实际成交受交易所数量精度、价格及最小订单限制影响。',
            )
            preview_note.pack(fill='x', pady=(1, 4))

            match_row = tk.Frame(preview_box, bg=PALETTE['info_bg'])
            match_row.pack(fill='x')
            actual_lev_lbl = tk.Label(match_row, bg=PALETTE['info_bg'], fg=PALETTE['text'], anchor='w',
                                      font=('Microsoft YaHei UI', 8, 'bold'))
            actual_lev_lbl.pack(side='left')
            match_badge_lbl = tk.Label(match_row, bg=PALETTE['info_bg'], anchor='w',
                                       font=('Microsoft YaHei UI', 8, 'bold'), padx=8)
            match_badge_lbl.pack(side='left')

            def update_leverage_preview(*_args):
                if not preview_box.winfo_exists():
                    return
                idx_l = lev_cb.current()
                if 0 <= idx_l < len(allowed_nums):
                    fields['leverage'].set(str(allowed_nums[idx_l]))
                lev_int = int(fields['leverage'].get()) if fields['leverage'].get().isdigit() else 5
                try:
                    fx = Decimal(fields['fixed'].get().strip())
                    if fx.is_finite() and fx > 0:
                        notional = (fx * lev_int).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        fx_fmt = fx.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        preview_main.configure(text=f'预计每笔开仓金额：{notional:,.2f} USDT')
                        preview_formula.configure(text=f'计算方式：{fx_fmt:,.2f} USDT保证金 × {lev_int}倍杠杆')
                    else:
                        raise ValueError()
                except (InvalidOperation, ValueError):
                    preview_main.configure(text='预计每笔开仓金额：—')
                    preview_formula.configure(text=f'计算方式：请输入有效保证金 × {lev_int}倍杠杆')
                act_l = e_ctx.actual_leverage
                if act_l is None:
                    actual_lev_lbl.configure(text='币安当前实际杠杆：暂未获取')
                    match_badge_lbl.configure(text='', fg=PALETTE['muted'])
                elif act_l == lev_int:
                    actual_lev_lbl.configure(text=f'币安当前实际杠杆：{act_l}×')
                    match_badge_lbl.configure(text='● 参数一致', fg=PALETTE['gain'])
                else:
                    actual_lev_lbl.configure(text=f'币安当前实际杠杆：{act_l}×')
                    match_badge_lbl.configure(text='● 需要调整币安杠杆，启用前须保持一致', fg='#c2410c')

            lev_cb.bind('<<ComboboxSelected>>', update_leverage_preview)
            fixed_trace_id = fields['fixed'].trace_add('write', update_leverage_preview)
            preview_box.bind('<Destroy>', lambda _e: fields['fixed'].trace_remove('write', fixed_trace_id) if _e.widget is preview_box else None, add='+')
            update_leverage_preview()

            tk.Label(
                right_card, bg=PALETTE['card_bg'], fg=PALETTE['muted'], anchor='w', justify='left',
                wraplength=450, font=('Microsoft YaHei UI', 8),
                text='每次跟随纸盘开仓时，按照固定保证金×固定杠杆计算委托金额，不跟随纸盘策略自身的杠杆倍数。',
            ).pack(fill='x', pady=(0, 6))

            env_display = '⚗ Binance Demo（虚拟资金）' if editing_env == DEMO_ENV else '⚡ Binance 正式实盘（真实资金）'
            form_row('交易环境', 'environment', readonly_text=env_display)
            form_row('执行账户', 'account', (defs['account'],))

            ttk.Button(footer, text='← 返回主页', command=lambda: render(0)).pack(side='left')
            def go_next():
                try:
                    fixed, budget = Decimal(fields['fixed'].get()), Decimal(fields['budget'].get())
                    if not fixed.is_finite() or not budget.is_finite() or not 0 < fixed <= budget:
                        raise ValueError()
                    if fields['strategy'].get() not in rows:
                        raise ValueError()
                except (ValueError, ArithmeticError, InvalidOperation):
                    messagebox.showerror('跟单配置', '请选择有效策略，并填写有效的保证金及预算。', parent=root)
                    return
                render(2)

            ttk.Button(footer, text='下一步：高级设置', style='Primary.TButton', command=go_next).pack(side='right')
            ttk.Button(footer, text='保存配置', command=lambda: save(destination=0, enable_after=False)).pack(side='right', padx=(0, 8))
            return

        # Step 2: 高级设置 & 确认配置
        tk.Label(left_card, text='高级设置', bg=PALETTE['card_bg'], fg=PALETTE['text'],
                 font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w', pady=(0, 10))
        tk.Label(left_card, text='退出方式  |  默认跟随纸盘止盈止损（FOLLOW_PAPER）', bg=PALETTE['card_bg'],
                 fg=PALETTE['muted'], font=('Microsoft YaHei UI', 9)).pack(anchor='w', pady=(0, 10))

        pct_entries = {}
        for toggle_key, input_key, title_t, input_label in (
            ('custom_tp', 'tp', '独立止盈（选填 · 按成交价涨跌幅 %）', '自定义止盈 %（可空）'),
            ('custom_sl', 'stop', '独立止损（选填 · 按成交价涨跌幅 %）', '自定义止损 %（可空）'),
        ):
            t_row = tk.Frame(left_card, bg=PALETTE['card_bg'])
            t_row.pack(fill='x', pady=4)
            is_on = fields[toggle_key].get() == '1'
            chk_btn = tk.Button(
                t_row, text='☑' if is_on else '☐', bd=0, relief='flat', cursor='hand2',
                bg=PALETTE['card_bg'], fg=PALETTE['primary'] if is_on else '#94a3b8',
                font=('Segoe UI', 13, 'bold'),
            )
            chk_btn.pack(side='left', padx=(0, 6))
            tk.Label(t_row, text=title_t, bg=PALETTE['card_bg'], fg=PALETTE['text'], font=('Microsoft YaHei UI', 9, 'bold')).pack(side='left')

            in_row = tk.Frame(left_card, bg=PALETTE['card_bg'])
            in_row.pack(fill='x', pady=(0, 8), padx=(26, 0))
            tk.Label(in_row, text=input_label, bg=PALETTE['card_bg'], fg=PALETTE['muted'], width=20, anchor='w',
                     font=('Microsoft YaHei UI', 9)).pack(side='left')
            ent = ttk.Entry(in_row, textvariable=fields[input_key], width=18, state='normal' if is_on else 'disabled')
            ent.pack(side='left', fill='x', expand=True)
            tk.Label(in_row, text='%', bg=PALETTE['card_bg'], fg=PALETTE['muted'], padx=6).pack(side='left')
            pct_entries[toggle_key] = (chk_btn, ent)

            def make_toggle(tk_k=toggle_key, btn_w=chk_btn, ent_w=ent):
                def _toggle():
                    nxt = '0' if fields[tk_k].get() == '1' else '1'
                    fields[tk_k].set(nxt)
                    on = nxt == '1'
                    btn_w.configure(text='☑' if on else '☐', fg=PALETTE['primary'] if on else '#94a3b8')
                    ent_w.configure(state='normal' if on else 'disabled')
                return _toggle

            chk_btn.configure(command=make_toggle())

        unavail = tk.Frame(left_card, bg='#f8fafd', padx=10, pady=8, highlightbackground='#e2e8f0', highlightthickness=1)
        unavail.pack(fill='x', pady=(8, 10))
        tk.Label(
            unavail, bg='#f8fafd', fg=PALETTE['muted'], justify='left', wraplength=440, font=('Microsoft YaHei UI', 8),
            text=(
                '暂不可用项（当前执行器未启用，避免误导）：\n'
                '• 停止时全部平仓：默认停止时仅停止新开仓并保留必要持仓管理。\n'
                '• 自定义单笔预算上限 % / 最大滑点 %：暂未开放。'
            ),
        ).pack(anchor='w')

        adv_note = tk.Frame(left_card, bg=PALETTE['info_bg'], padx=10, pady=8, highlightbackground=PALETTE['info_border'], highlightthickness=1)
        adv_note.pack(fill='x')
        tk.Label(
            adv_note, bg=PALETTE['info_bg'], fg=PALETTE['text'], justify='left', wraplength=440, font=('Microsoft YaHei UI', 8),
            text='ⓘ 未勾选独立止盈止损时仅使用 FOLLOW_PAPER，不强制创建 Algo 条件单；勾选后成交即挂载对应条件单，百分比按成交均价涨跌幅计算。',
        ).pack(anchor='w')

        # Right Card: 确认配置
        tk.Label(right_card, text='确认配置', bg=PALETTE['card_bg'], fg=PALETTE['text'],
                 font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w', pady=(0, 10))
        v = values()
        code, sname = split_strategy_label(v['strategy'])
        exit_summary_var = tk.StringVar(root)

        def sync_exit_summary(*_args):
            tp_v = fields['tp'].get().strip()
            sl_v = fields['stop'].get().strip()
            tp_t = f"{tp_v}%（按成交价）" if fields['custom_tp'].get() == '1' and tp_v else ('待填写' if fields['custom_tp'].get() == '1' else '关闭')
            sl_t = f"{sl_v}%（按成交价）" if fields['custom_sl'].get() == '1' and sl_v else ('待填写' if fields['custom_sl'].get() == '1' else '关闭')
            exit_summary_var.set(f'跟随纸盘 · 止盈 {tp_t} · 止损 {sl_t}')

        sync_exit_summary()
        for watch_k in ('custom_tp', 'custom_sl', 'tp', 'stop'):
            fields[watch_k].trace_add('write', sync_exit_summary)

        env_mode_txt = 'Binance 正式实盘（真实资金）' if editing_env == PROD_ENV else 'Binance Demo（虚拟资金）'
        perm_txt = (
            '正式真实下单默认关闭（需预检通过及独立授权）'
            if editing_env == PROD_ENV
            else '虚拟资金跟单 · 生产下单关闭'
        )
        lev_int = int(v['leverage']) if str(v['leverage']).isdigit() else 5
        try:
            est_notional_txt = money(Decimal(v['fixed']) * lev_int)
        except (InvalidOperation, ValueError):
            est_notional_txt = '—'
        act_lev = e_ctx.actual_leverage
        act_lev_txt = f'{act_lev}×' if act_lev is not None else '暂未获取'
        if act_lev is None:
            lev_match_txt, lev_match_fg = '暂未获取', PALETTE['muted']
        elif act_lev == lev_int:
            lev_match_txt, lev_match_fg = '参数一致', PALETTE['gain']
        else:
            lev_match_txt, lev_match_fg = '需要调整币安杠杆，启用前须保持一致', '#c2410c'

        summary_rows = [
            ('策略编号', code),
            ('策略名称', sname),
            ('跟单模式', v['mode']),
            ('每笔跟单保证金', money(v['fixed'])),
            ('总资金上限', money(v['budget'])),
            ('固定开仓杠杆', f"{lev_int}×"),
            ('预计每笔开仓金额', est_notional_txt),
            ('币安当前实际杠杆', act_lev_txt),
            ('杠杆匹配状态', lev_match_txt),
            ('交易环境', env_mode_txt),
            ('执行账户', v.get('account', e_ctx.paths.default_account_label)),
            ('退出与独立止盈/止损', exit_summary_var),
            ('当前可用资金', e_ctx.account_line),
            ('交易权限状态', perm_txt),
        ]
        sum_box = tk.Frame(right_card, bg=PALETTE['card_bg'], highlightbackground='#e2e8f0', highlightthickness=1)
        sum_box.pack(fill='x', pady=(0, 10))
        sum_box._exit_var = exit_summary_var
        for idx_r, (k_txt, v_txt) in enumerate(summary_rows):
            r_bg = '#f8fafd' if idx_r % 2 == 0 else '#ffffff'
            rf = tk.Frame(sum_box, bg=r_bg, padx=10, pady=4)
            rf.pack(fill='x')
            tk.Label(rf, text=k_txt, bg=r_bg, fg=PALETTE['muted'], width=18, anchor='w', font=('Microsoft YaHei UI', 9)).pack(side='left')
            if k_txt == '杠杆匹配状态':
                v_fg = lev_match_fg
            elif editing_env == PROD_ENV and k_txt in ('交易环境', '交易权限状态'):
                v_fg = PALETTE['accent_red']
            else:
                v_fg = PALETTE['text']
            lbl_opts = {'textvariable': v_txt} if isinstance(v_txt, tk.StringVar) else {'text': v_txt}
            tk.Label(rf, **lbl_opts, bg=r_bg, fg=v_fg, anchor='w', font=('Microsoft YaHei UI', 9, 'bold' if k_txt in ('策略名称', '预计每笔开仓金额', '当前可用资金', '杠杆匹配状态') else 'normal')).pack(side='left', fill='x', expand=True)

        rules_box = tk.Frame(right_card, bg=PALETTE['info_bg'], padx=12, pady=9, highlightbackground=PALETTE['info_border'], highlightthickness=1)
        rules_box.pack(fill='x')
        tk.Label(rules_box, text='ⓘ 启用规则', bg=PALETTE['info_bg'], fg=PALETTE['primary'], font=('Microsoft YaHei UI', 9, 'bold')).pack(anchor='w', pady=(0, 4))
        tk.Label(
            rules_box, bg=PALETTE['info_bg'], fg=PALETTE['text'], justify='left', wraplength=440, font=('Microsoft YaHei UI', 8),
            text=(
                '• 同一交易环境一次仅运行一套策略；Demo 与正式实盘相互独立。\n'
                '• 若当前环境已有运行配置，点击启用将进入安全排队切换。\n'
                '• 启用后可在主界面随时暂停新开仓、恢复或安全停止跟单。'
            ),
        ).pack(anchor='w')

        ttk.Button(footer, text='← 上一步', command=lambda: render(1)).pack(side='left')
        enable_btn_txt = '✓ 启用正式实盘' if editing_env == PROD_ENV else '✓ 启用Demo跟单'
        ttk.Button(footer, text=enable_btn_txt, style='Primary.TButton', command=lambda: save(destination=0, enable_after=True)).pack(side='right')
        ttk.Button(footer, text='返回主页面', command=lambda: render(0)).pack(side='right', padx=(0, 8))
        ttk.Button(footer, text='保存配置', command=lambda: save(destination=0, enable_after=False)).pack(side='right', padx=(0, 8))

    render(0)
    update_state()
    refresh_account(DEMO_ENV)
    refresh_account(PROD_ENV)
    advance_all_switches()

    def on_close():
        if any(contexts[e].read_switch() for e in VALID_ENVS):
            root.withdraw()
        else:
            root.destroy()

    root.protocol('WM_DELETE_WINDOW', on_close)
    if owner is None:
        root.mainloop()
    return root


if __name__ == '__main__':
    main()
