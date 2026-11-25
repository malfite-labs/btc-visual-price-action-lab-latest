"""Native desktop view; no paper or live execution imports."""
import json
import queue
import threading
import tkinter as tk
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from time import monotonic
from tkinter import messagebox, ttk
from uuid import uuid4

from scripts.live_center import AccountReadError, BinanceReader, Configuration, LiveAccount
from scripts.live_strategy_wizard import open_strategy_wizard
from scripts.live_ui_style import initialize, metric
from scripts.live_start_check import evidence_rows, open_start_check
from scripts import kline_window


STRATEGIES = ('A', 'B', 'C', 'D', 'E', 'G', *(f'PA-{i:02d}' for i in range(1, 11)))


def open_live_center(root, *, configuration=None, reader_factory=BinanceReader, demo=False, paper_rows=None):
    if demo and (configuration is None or reader_factory is BinanceReader):
        raise ValueError('演示须使用独立配置与测试数据读取器')
    existing = getattr(root, '_live_center', None)
    if existing is not None and existing.winfo_exists():
        existing.deiconify()
        existing.lift()
        return existing
    storage = configuration or Configuration()
    window = tk.Toplevel(root)
    root._live_center = window
    window.transient(root)
    window.title('BTC 实盘交易中心')
    window.geometry(f'{min(1600, window.winfo_screenwidth()-60)}x{min(900, window.winfo_screenheight()-90)}')
    window.minsize(780, 590)
    initialize(window)
    body = ttk.Frame(window, padding=18, style='Live.TFrame')
    monitor_status = tk.StringVar(value='只读信号监测 · 已停止｜真实订单关闭')
    monitor_bar = ttk.Frame(window, padding=(18, 6), style='Live.TFrame')
    monitor_bar.pack(side='bottom', fill='x')
    ttk.Label(monitor_bar, textvariable=monitor_status, style='Live.TLabel',
              justify='left', wraplength=900).pack(side='left', fill='x', expand=True)
    body.pack(fill='both', expand=True)
    tk.Label(body, text='BTC 实盘交易中心', bg='#17458c', fg='white', padx=14, pady=9,
             anchor='w', font=('Microsoft YaHei UI', 17, 'bold')).pack(fill='x')
    ttk.Label(body, style='Live.TLabel', text=('隔离演示数据 · 不连接交易所' if demo else 'Binance USDⓈ-M · 账户只读  /  真实交易未启用')).pack(anchor='w', pady=(6, 12))
    status = tk.StringVar(value='未连接 · 未配置')
    overview = tk.StringVar(value='可用 USDT —    账户权益 —    待启用预算 —')
    tk.Label(body, textvariable=status, bg='#e6efff', fg='#1b4a96',
             anchor='w', padx=10, pady=5, wraplength=740).pack(fill='x', pady=(0, 5))
    ttk.Label(body, style='Live.TLabel', textvariable=overview, font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w')
    cards = ttk.Frame(body, style='Live.TFrame')
    cards.pack(fill='x', pady=(10, 0))
    metrics = []
    for index, title in enumerate(('已配置策略', '账户待验证', '账户异常', '总权益（USDT）')):
        value = tk.StringVar(value='—')
        card = metric(cards, title, value)
        card.grid(row=0, column=index, sticky='nsew', padx=(0, 6))
        cards.columnconfigure(index, weight=1, uniform='account-metrics')
        metrics.append(value)
    toolbar = ttk.Frame(body, style='Live.TFrame')
    toolbar.pack(side='bottom', fill='x', pady=(12, 0))
    account_bar = ttk.Frame(toolbar, style='Live.TFrame')
    account_bar.pack(fill='x', pady=(0, 6))
    action_bar = ttk.Frame(toolbar, style='Live.TFrame')
    action_bar.pack(fill='x')
    choice = ttk.Combobox(account_bar, state='readonly', width=25)
    choice.pack(side='left', padx=(0, 8))
    def open_live_chart():
        native_live_chart = getattr(kline_window, 'open_live_kline', None)
        if native_live_chart:
            window._live_chart = native_live_chart(window)
            return
        current = getattr(window, '_live_chart', None)
        if current is not None and current.winfo_exists():
            current.lift()
            return
        chart = tk.Toplevel(window)
        chart.transient(window)
        chart.title('BTC · 实盘K线')
        chart.geometry('860x550')
        initialize(chart)
        ttk.Label(chart, text='BTCUSDT · 实盘行情与指标窗口', padding=16, style='Live.TLabel').pack(anchor='w')
        ttk.Label(chart, text='暂无实盘成交记录', padding=16, style='Live.TLabel').pack(anchor='w')
        window._live_chart = chart

    ttk.Button(account_bar, text='打开实盘K线', style='Live.TButton', command=open_live_chart).pack(side='right')
    pending = ttk.LabelFrame(body, text='策略绑定 · 待启用', padding=10, style='Live.TLabelframe')
    pending.pack(fill='both', expand=True, pady=(10, 9))
    pending_text = tk.StringVar(value='暂无实盘策略运行；暂无待启用绑定。')
    ttk.Label(pending, textvariable=pending_text, justify='left', style='Live.TLabel').pack(anchor='w')
    filters = ttk.Frame(pending, style='Live.TFrame')
    filters.pack(fill='x', pady=6)
    search = tk.StringVar()
    state_filter = ttk.Combobox(filters, state='readonly', values=('全部状态', '待启用', '账户待验证', '账户异常'), width=14)
    state_filter.set('全部状态')
    state_filter.pack(side='left', padx=(0, 10))
    ttk.Label(filters, text='搜索策略', style='Live.TLabel').pack(side='left', padx=(0, 6))
    ttk.Entry(filters, textvariable=search, width=24).pack(side='left')
    columns = ('enabled', 'id', 'name', 'account', 'state', 'position', 'notional', 'leverage', 'budget', 'equity', 'realized', 'unrealized', 'fees', 'action', 'cycle')
    table = ttk.Treeview(pending, show='headings', columns=columns, height=7, style='Live.Treeview')
    for key, title in zip(columns, ('启停', '策略 ID', '策略名称', '交易账户', '状态', '持仓', '持仓金额', '计划杠杆', '预算', '权益', '已实现', '未实现', '费用', '最近动作', '周期时间')):
        table.heading(key, text=title)
        table.column(key, width=83, minwidth=60, stretch=False)
    track = ttk.Scrollbar(pending, orient='horizontal', command=table.xview)
    scroll = ttk.Scrollbar(pending, orient='vertical', command=table.yview)
    table.configure(xscrollcommand=track.set, yscrollcommand=scroll.set)
    track.pack(side='bottom', fill='x')
    scroll.pack(side='right', fill='y')
    table.pack(fill='both', expand=True)
    detail = tk.Text(body, height=3, wrap='word', bg='#fbfdff', fg='#152443', font=('Microsoft YaHei UI', 9))
    detail.pack(side='bottom', fill='x', before=pending)
    detail.configure(state='disabled')

    def set_details(lines):
        detail.configure(state='normal')
        detail.delete('1.0', 'end')
        detail.insert('1.0', '\n'.join(lines))
        detail.configure(state='disabled')

    try:
        data = storage.load()
    except (OSError, ValueError):
        status.set('本机加密配置无法读取，请检查 Windows 用户与配置文件。')
        return window
    snapshots = {}
    snapshot_times = {}
    checks = {}
    account_errors = set()
    labels = {}
    actions = []
    busy = False

    def save_data(next_data):
        if not demo:
            storage.save({**data, **next_data})
        data.update(next_data)

    def selected():
        return labels.get(choice.get())

    def show_account():
        account_id = selected()
        account = data['accounts'].get(account_id)
        snapshot = snapshots.get(account_id)
        binding = data['bindings'].get(account_id)
        allocated = binding['budget']['usdt'] if binding else '—'
        pending_text.set('双击配置可查看对应只读账户。' if data['bindings'] else '未配置 · 请选择已有策略并配置资金')
        metrics[0].set(str(len(data['bindings'])))
        metrics[1].set(str(len(data['accounts'].keys() - snapshots.keys() - account_errors)))
        metrics[2].set(str(len(account_errors)))
        metrics[3].set(f"{sum(item['equity'] for item in snapshots.values()):,.2f} · {len(snapshots)}/{len(data['accounts'])}" if snapshots else '—')
        for iid in table.get_children():
            table.delete(iid)
        paper = paper_rows() if paper_rows else {}
        for key, value in data['bindings'].items():
            sid = value['strategy_id']
            name = paper.get(sid, {}).get('name', sid)
            info = snapshots.get(key)
            state = '账户异常' if key in account_errors else ('待启用' if info else '账户待验证')
            if state_filter.get() not in ('全部状态', state) or search.get().strip().casefold() not in f'{sid} {name}'.casefold():
                continue
            table.insert('', 'end', iid=key, values=('锁定', sid, name, data['accounts'][key]['name'],
                         state, '—', '—', f"{value['budget']['planned_leverage']}×", value['budget']['usdt'],
                         info['equity'] if info else '—', '—', '—', '—', '配置已保存', '—'))
        if not snapshot:
            status.set(f"{account['name']} · {'账户异常' if account_id in account_errors else '账户待验证'}" if account else '未连接 · 未配置')
            overview.set(f'可用 USDT —    账户权益 —    待启用预算 {allocated}')
            set_details([])
            return
        mode = '主账户' if account.get('account_type') == 'main' else '子账户'
        status.set(f"{account['name']} · {mode} · {'隔离演示' if demo else 'Binance实盘已连接'} · 认证通过 · {snapshot['scanned_at']}")
        overview.set(f"可用 USDT {snapshot['available']}    USDT 资产权益 {snapshot['equity']}    待启用预算 {allocated}")
        risk = snapshot['config']
        permissions = snapshot['permissions']
        rows = [f"USDT 合约钱包余额 {snapshot.get('wallet', '—')}",
                f"持仓模式 {'双向' if snapshot['hedge'] else '单向'}  /  多资产模式 {risk.get('multiAssetsMargin', '未知')}",
                f"API 权限 · 合约 {permissions.get('enableFutures', '未知')} · 提现 {permissions.get('enableWithdrawals', '未知')} · IP 限制 {permissions.get('ipRestrict', '未知')}"]
        if mode == '主账户':
            rows.insert(0, '当前为主账户模式，资金限制由程序预算和交易所保证金机制共同管理，并非独立子账户硬隔离。')
            if snapshot['positions'] or snapshot['orders'] or snapshot['conditional'] or snapshot.get('other_activity'):
                rows.insert(1, '主账户已有合约持仓或挂单，可能存在资金或仓位冲突。')
            if 'spot_usdt' in snapshot:
                spot = snapshot['spot_usdt']
                rows.append(f"现货钱包 USDT：{spot if spot is not None else '未验证'} · 不计入合约可用余额")
        rows.extend(f"BTCUSDT 配置 · {row.get('marginType', '未知')} · 杠杆 {row.get('leverage', '未知')}×" for row in snapshot['symbol'])
        rows.append('BTCUSDT 持仓')
        rows.extend(f"  {row.get('positionSide')} · 数量 {row.get('positionAmt')} · 开仓价 {row.get('entryPrice')}" for row in snapshot['positions'])
        if not snapshot['positions']:
            rows.append('  无')
        rows.append('BTCUSDT 未完成订单（普通 / 条件）')
        for order in snapshot['orders'] + snapshot['conditional']:
            rows.append(f"  #{order.get('orderId') or order.get('algoId')} · {order.get('side')} · "
                        f"{order.get('type') or order.get('orderType')} · {order.get('origQty') or order.get('quantity')} · "
                        f"价格 {order.get('price')} · 触发价 {order.get('triggerPrice', '—')}")
        if not snapshot['orders'] and not snapshot['conditional']:
            rows.append('  无')
        set_details(rows)

    def refresh_choices(account_id):
        labels.clear()
        labels.update({f"{account['name']} · {'主账户' if account.get('account_type') == 'main' else '子账户'} · {key[:8]}": key
                       for key, account in data['accounts'].items()})
        choice.configure(values=tuple(labels))
        choice.set(next((label for label, key in labels.items() if key == account_id), ''))
        show_account()

    def query(api_key, secret, callback, error_var, on_error=lambda: None, operation=None):
        nonlocal busy
        if busy:
            error_var.set('已有账户查询在进行，请稍后。')
            return False
        busy = True
        for action in actions:
            action.configure(state='disabled')
        error_var.set('正在查询只读账户…')
        result = queue.SimpleQueue()

        def work():
            try:
                reader = reader_factory(api_key, secret)
                result.put(((operation(reader) if operation else reader.snapshot()), None))
            except AccountReadError as exc:
                result.put((None, str(exc)))
            except Exception:
                result.put((None, '账户查询失败，请核对凭据格式和网络。'))

        def poll():
            nonlocal busy
            try:
                snapshot, error = result.get_nowait()
            except queue.Empty:
                window.after(75, poll)
                return
            busy = False
            for action in actions:
                if action.winfo_exists():
                    action.configure(state='normal')
            if error:
                on_error()
                error_var.set('账户异常 · ' + error)
                return
            try:
                snapshot['scanned_at'] = datetime.now().strftime('%H:%M:%S')
                callback(snapshot)
            except (ValueError, OSError) as exc:
                error_var.set(str(exc) if isinstance(exc, ValueError) else 'Windows 加密配置保存失败')
                on_error()

        threading.Thread(target=work, daemon=True).start()
        window.after(75, poll)
        return True

    def bind_account():
        dialog = tk.Toplevel(window)
        dialog.title('绑定交易所账户 · Binance')
        dialog.transient(window)
        initialize(dialog)
        tk.Label(dialog, text='绑定 Binance 账户', bg='#17458c', fg='white', anchor='w', padx=16, pady=9,
                 font=('Microsoft YaHei UI', 14, 'bold')).pack(fill='x')
        form = ttk.Frame(dialog, padding=18, style='Live.TFrame')
        form.pack(fill='both', expand=True)
        entries = []
        kind = ttk.Combobox(form, state='readonly', values=('Binance主账户', 'Binance子账户'), width=36)
        kind.current(0)
        kind.grid(row=0, column=1, sticky='ew', padx=8, pady=4)
        ttk.Label(form, text='账户类型', style='Live.TLabel').grid(row=0, column=0, sticky='w')
        for i, text in enumerate(('账户备注', 'API Key', 'API Secret'), 1):
            ttk.Label(form, text=text, style='Live.TLabel').grid(row=i, column=0, sticky='w', pady=4)
            entry = ttk.Entry(form, width=38, show='●' if i > 1 else '')
            entry.grid(row=i, column=1, padx=8, pady=4)
            entries.append(entry)
        message = tk.StringVar(value='先验证连接，再使用当前 Windows 用户加密保存。')
        ttk.Label(form, textvariable=message, style='Live.TLabel', wraplength=440).grid(row=4, columnspan=2, sticky='w', pady=8)
        verified = None

        def inputs():
            name, key, secret = (entry.get().strip() for entry in entries)
            return (name, key, secret, ('main', 'subaccount')[kind.current()])

        def submit():
            nonlocal verified
            verified = None
            save_button.state(['disabled'])
            values = inputs()
            name, key, secret, mode = values
            if not name or not key or not secret or not key.isascii() or not secret.isascii():
                message.set('请填写名称和 ASCII 格式的 API 凭据。')
                return

            def accepted(snapshot):
                nonlocal verified
                if not dialog.winfo_exists() or inputs() != values:
                    return
                verified = values, snapshot
                save_button.state(['!disabled'])
                permission = {True: '已开通', False: '未开通'}.get(snapshot['permissions'].get('enableFutures'), '未验证')
                message.set(f"验证成功 · 合约交易权限 {permission}\n"
                            f"钱包 {snapshot.get('wallet', '—')} USDT · 可用 {snapshot['available']} USDT\n"
                            f"BTCUSDT 持仓 {len(snapshot['positions'])} · 普通挂单 {len(snapshot['orders'])} · 条件挂单 {len(snapshot['conditional'])}")

            query(key, secret, accepted, message, operation=lambda client: read_snapshot(client, mode))

        def save():
            if not verified or verified[0] != inputs():
                message.set('凭据或账户类型已修改，请重新验证。')
                return
            (name, key, secret, mode), snapshot = verified
            alias = snapshot['exchange_alias']
            if any(account['exchange_alias'] == alias for account in data['accounts'].values()):
                message.set('此 Binance 合约账户已绑定。轮换密钥前请先断开原绑定。')
                return
            account_id = uuid4().hex
            account = LiveAccount(account_id, name, key, secret, alias, mode)
            try:
                save_data({'accounts': {**data['accounts'], account_id: asdict(account)}})
            except OSError:
                message.set('Windows 加密配置保存失败。')
                return
            snapshots[account_id] = snapshot
            snapshot_times[account_id] = monotonic()
            dialog.destroy()
            refresh_choices(account_id)

        ttk.Button(form, text='验证连接', style='Live.TButton', command=submit).grid(row=5, column=0, sticky='w')
        save_button = ttk.Button(form, text='加密保存', style='LivePrimary.TButton', command=save, state='disabled')
        save_button.grid(row=5, column=1, sticky='e')
        return dialog

    def read_snapshot(client, mode):
        snapshot = client.snapshot()
        if mode == 'main' and snapshot['available'] < 20:
            snapshot['spot_usdt'] = client.spot_usdt()
        return snapshot

    def query_account(key, callback, error_var, on_error=lambda: None):
        account = data['accounts'][key]
        def accepted(snapshot):
            if snapshot['exchange_alias'] != account['exchange_alias']:
                raise ValueError('交易所返回的账户与已绑定身份不一致。')
            account_errors.discard(key)
            snapshots[key] = snapshot
            snapshot_times[key] = monotonic()
            show_account()
            callback()
        def failed():
            account_errors.add(key)
            snapshots.pop(key, None)
            show_account()
            on_error()
        return query(account['api_key'], account['api_secret'], accepted, error_var, failed,
                     operation=lambda client: read_snapshot(client, account.get('account_type', 'subaccount')))

    def refresh_account():
        key = selected()
        if key is not None:
            query_account(key, lambda: None, status)

    def preflight(key, sid, version, budget, leverage, callback, error_var, *, signal_bars=None):
        account = data['accounts'][key]
        mode = account.get('account_type', 'subaccount')
        def check(client):
            snapshot = read_snapshot(client, mode)
            if snapshot['exchange_alias'] != account['exchange_alias']:
                raise AccountReadError('交易所返回的账户身份不一致。')
            rule = (paper_rows() if paper_rows else {}).get(sid, {}).get('live_rule', {})
            if rule.get('version') != version:
                return {'state': 'UNVERIFIED', 'reason': '活动版本读取失败或已变化，请重开向导。'}
            if sid in ('A', 'C'):
                from scripts.live_strategy_a import StrategyAAdapter
                from scripts.live_strategy_c import StrategyCAdapter
                adapter = StrategyCAdapter() if sid == 'C' else StrategyAAdapter()
                if adapter.sha256 != rule.get('sha256') or str(adapter.risk_fraction) != str(rule.get('risk_fraction')) or adapter.max_leverage != int(rule['max_leverage']):
                    return {'state': 'UNVERIFIED', 'reason': f'{sid} 活动规则或风控标识与本机源码不一致。'}
            params = dict(strategy_id=sid, version=version, budget=budget,
                          leverage=leverage, account_type=mode)
            result = (client.configuration_preflight(snapshot, **params) if signal_bars is None else
                      client.order_preflight(snapshot, **params, closed_bars=signal_bars))
            return {**result, 'snapshot': snapshot}
        def checked(result):
            snapshot = result.pop('snapshot', None)
            if snapshot is not None:
                snapshots[key] = snapshot
                snapshot_times[key] = monotonic()
            if signal_bars is None:
                checks[key] = ((sid, version, Decimal(budget), int(leverage)), result, monotonic()) if result.get('version_verified') else None
            callback(result)
        return query(account['api_key'], account['api_secret'], checked, error_var, operation=check,
              on_error=lambda: callback({'state': 'UNVERIFIED', 'reason': '账户只读复核失败。'}))

    monitors = {}

    def stop_monitor():
        monitors.clear()
        monitor_status.set('只读信号监测 · 已停止｜真实订单关闭')

    ttk.Button(monitor_bar, text='停止监测', command=stop_monitor,
               style='Live.TButton').pack(side='right')

    def start_monitor(key, config):
        from datetime import UTC, timedelta
        from pathlib import Path
        from urllib.request import urlopen
        from scripts.live_strategy_a import StrategyAAdapter
        from scripts.live_strategy_c import StrategyCAdapter

        if monitors:
            return '监测已在运行'
        sid = config['strategy_id']
        try:
            adapter = StrategyCAdapter() if sid == 'C' else StrategyAAdapter()
            rule = (paper_rows() if paper_rows else {}).get(sid, {}).get('live_rule', {})
            if not (adapter.version == config['strategy_version'] == rule.get('version')
                    and adapter.sha256 == rule.get('sha256')):
                raise ValueError('已保存版本或规则哈希与正式活动规则不一致')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            monitor_status.set(f'只读监测 · 启动失败｜{exc}｜真实订单关闭')
            return '启动失败：请核对已保存版本、活动规则和本机规则文件。'

        state = {'cycle': None, 'signal': '—', 'risk': '不适用', 'status': '等待数据'}
        monitors[key] = state

        def current():
            return window.winfo_exists() and monitors.get(key) is state and data['bindings'].get(key) == config

        def display():
            cycle = state['cycle'].astimezone().isoformat(timespec='seconds') if state['cycle'] else '—'
            monitor_status.set(
                f"{sid} {adapter.version} · 只读监测｜{state['status']}｜真实订单关闭"
                f"\n最近处理 {cycle}｜信号 {state['signal']}｜风控 {state['risk']}｜关闭实盘中心即停止"
            )

        def record(cycle, signal, risk):
            row = {'cycle_id': cycle.astimezone(UTC).isoformat(), 'close_at': cycle.isoformat(),
                   'strategy_id': sid, 'version': adapter.version, 'sha256': adapter.sha256,
                   'valid_candle': True, 'signal': signal, 'risk': risk, 'duplicate': False}
            try:
                path = Path(__file__).resolve().parents[1] / '.run' / 'live-signal-monitor.jsonl'
                with path.open('a', encoding='utf8') as output:
                    output.write(json.dumps(row, ensure_ascii=False) + '\n')
            except OSError:
                state['status'] = '监测中 · 周期日志写入失败'
            display()

        def poll_cycle():
            if not current():
                return
            queue_out = queue.SimpleQueue()
            previous_cycle = state['cycle']

            def read_cycle():
                try:
                    with urlopen('http://127.0.0.1:8012/api/v1/market/candles?limit=289', timeout=4) as response:
                        payload = json.load(response)
                    if payload['source'] != 'BINANCE_USDM_PERP' or payload['interval'] != '15m':
                        raise ValueError('行情来源或周期不符')
                    bars = tuple(bar for bar in payload['items'] if bar.get('is_closed') is True)[-288:]
                    cycle = datetime.fromisoformat(bars[-1]['close_at'])
                    now = datetime.now(UTC)
                    if not cycle <= now < cycle + timedelta(minutes=15):
                        raise ValueError('等待最新已收盘周期')
                    if previous_cycle is not None and cycle <= previous_cycle:
                        queue_out.put((cycle, None, None, None))
                        return
                    signal = adapter.evaluate(closed_bars=bars, cycle_at=cycle)
                    queue_out.put((cycle, signal, bars, None))
                except (OSError, KeyError, ValueError, TypeError, IndexError, ArithmeticError) as exc:
                    queue_out.put((None, None, None, str(exc) if isinstance(exc, ValueError) else type(exc).__name__))

            def receive():
                if not current():
                    return
                try:
                    cycle, signal, bars, error = queue_out.get_nowait()
                except queue.Empty:
                    window.after(100, receive)
                    return
                window.after(5000, poll_cycle)
                if error:
                    state.update(status=f'等待数据 · {error}', signal='ERROR', risk='不适用')
                    display()
                    return
                if state['cycle'] is not None and cycle <= state['cycle']:
                    if state['signal'] == 'ERROR':
                        state.update(status='监测中 · 等待下一周期', signal='—')
                        display()
                    return
                first = state['cycle'] is None
                state.update(cycle=cycle, status='监测中 · 等待下一周期',
                             signal='NO_SIGNAL' if signal.action == 'WAIT' else 'SIGNAL', risk='不适用')
                if first:
                    state['status'] = '监测中 · 启动基线，等待新收盘'
                    display()
                    return
                if signal.action == 'WAIT':
                    record(cycle, 'NO_SIGNAL', None)
                    return
                budget = config['budget']
                state.update(status='监测中 · 查询单笔风险', risk='查询中')
                display()

                def report(result):
                    if current() and state['cycle'] == cycle:
                        state.update(status='监测中 · 等待下一周期', risk=result['state'])
                        record(cycle, {'direction': signal.direction, 'stop': signal.stop_basis},
                               {'state': result['state'], 'reason': result['reason']})

                if preflight(key, sid, config['strategy_version'], budget['usdt'],
                             budget['planned_leverage'], report, tk.StringVar(master=window),
                             signal_bars=bars) is False:
                    report({'state': 'UNVERIFIED', 'reason': '账户查询正在进行'})

            threading.Thread(target=read_cycle, daemon=True).start()
            window.after(100, receive)

        display()
        poll_cycle()
        return '只读监测中 · 等待行情或下一周期'

    def startup_check():
        key = selected()
        original = data['bindings'].get(key)
        binding = None
        report = None
        ready = False
        if original:
            binding = {**original, 'budget': Decimal(original['budget']['usdt'])}
            sid = original['strategy_id']
            context = (sid, original['strategy_version'], binding['budget'], original['budget']['planned_leverage'])
            cached = checks.get(key)
            if cached and cached[0] == context:
                report = cached[1] if monotonic() - cached[2] <= 60 else None
            ready = sid in ('A', 'C') and not demo
        fresh = snapshots.get(key) if monotonic() - snapshot_times.get(key, float('-inf')) <= 60 else None
        def refresh():
            if not ready:
                return
            preflight(key, sid, original['strategy_version'], original['budget']['usdt'],
                      original['budget']['planned_leverage'], lambda _result: startup_check(), status)
        return open_start_check(window, evidence_rows(data['accounts'].get(key), fresh, binding, report, simulated=demo),
                                monitor=(lambda: start_monitor(key, original)) if ready else None,
                                refresh=refresh if ready else None, ready=ready)

    def disconnect():
        key = selected()
        if key is None or not messagebox.askyesno('断开绑定', '移除本机密钥、关联策略预算与草稿账户选择？此操作不会撤销币安侧密钥。', parent=window):
            return
        draft = data.get('draft', {})
        forms = {sid: {**row, 'account': ''} if row.get('account') == key else row
                 for sid, row in draft.get('forms', {}).items()}
        try:
            save_data({'accounts': {k: v for k, v in data['accounts'].items() if k != key},
                       'bindings': {k: v for k, v in data['bindings'].items() if k != key},
                       'draft': {**draft, 'forms': forms}})
        except OSError:
            status.set('断开绑定失败，原配置已保留。')
            return
        snapshots.pop(key, None)
        account_errors.discard(key)
        refresh_choices(next(iter(data['accounts']), None))

    def allocate():
        return open_strategy_wizard(window, strategies=STRATEGIES, paper=paper_rows() if paper_rows else {},
                                    data=data, snapshots=snapshots, save_data=save_data, query_account=query_account,
                                    on_saved=show_account, preflight=preflight)

    def view_binding(_event):
        rows = table.selection()
        if not rows:
            return
        key = rows[0]
        binding = data['bindings'][key]
        refresh_choices(key)
        info = snapshots.get(key)
        dialog = tk.Toplevel(window)
        dialog.transient(window)
        dialog.title(f"{binding['strategy_id']} · 配置详情")
        ttk.Label(dialog, padding=20, justify='left', text='\n'.join((
            f"账户：{data['accounts'][key]['name']}",
            f"策略版本：{binding['strategy_version']}",
            f"预算：{binding['budget']['usdt']} USDT · 计划上限 {binding['budget']['planned_leverage']}×",
            f"API：{'已核验' if info else '待扫描'} · 账户权益：{info['equity'] if info else '—'}",
            "策略运行、盈亏及周期：待启用",
            "账户持仓与挂单：见实盘中心只读账户明细",
            f"实盘K线：{'已打开' if getattr(window, '_live_chart', None) and window._live_chart.winfo_exists() else '未打开'}",
        ))).pack()

    table.bind('<Double-Button-1>', view_binding)

    for label, command in (('绑定交易所账户', bind_account), ('刷新只读账户', refresh_account), ('选择已有策略', allocate), ('断开绑定', disconnect), ('启动检查', startup_check)):
        button = ttk.Button(action_bar, text=label, command=command,
                            style='LivePrimary.TButton' if label == '选择已有策略' else 'Live.TButton')
        button.pack(side='left', padx=(0, 8))
        actions.append(button)
    choice.bind('<<ComboboxSelected>>', lambda _event: show_account())
    state_filter.bind('<<ComboboxSelected>>', lambda _event: show_account())
    search.trace_add('write', lambda *_args: show_account())
    refresh_choices(next(iter(data['accounts']), None))
    return window
