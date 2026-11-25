"""Tk three-step wizard for local, pending live strategy configuration."""
import tkinter as tk
from time import monotonic
from dataclasses import asdict
from decimal import Decimal
from tkinter import ttk

from scripts.live_center import make_binding
from scripts.live_ui_style import BG, INK, initialize, metric


def open_strategy_wizard(owner, *, strategies, paper, data, snapshots, save_data, query_account, on_saved, preflight):
    existing = getattr(owner, '_strategy_wizard', None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return existing
    window = tk.Toplevel(owner)
    owner._strategy_wizard = window
    window.title('实盘策略配置向导')
    window.transient(owner)
    window.geometry(f'{min(1600, window.winfo_screenwidth()-70)}x{min(900, window.winfo_screenheight()-95)}')
    window.minsize(800, 540)
    initialize(window)
    tk.Label(window, text='实盘策略配置向导', bg='#17458c', fg='white', anchor='w', padx=16, pady=9,
             font=('Microsoft YaHei UI', 17, 'bold')).pack(fill='x', padx=22, pady=(14, 8))
    steps = tk.Frame(window, bg=BG)
    steps.pack(fill='x', padx=22)
    labels = []
    for index, title in enumerate(('选择策略', '分配资金', '确认配置'), 1):
        label = tk.Label(steps, text=f'{index}  {title}', padx=18, pady=8, font=('Microsoft YaHei UI', 10, 'bold'))
        label.pack(side='left', fill='x', expand=True, padx=3)
        labels.append(label)
    frame = ttk.Frame(window, padding=14, style='Live.TFrame')
    message = tk.StringVar()
    footer = ttk.Frame(window, padding=(22, 9), style='Live.TFrame')
    footer.pack(side='bottom', fill='x')
    ttk.Label(window, textvariable=message, wraplength=740).pack(side='bottom', fill='x', padx=22)
    frame.pack(fill='both', expand=True, padx=22, pady=14)

    existing_by_strategy = {row['strategy_id']: row for row in data['bindings'].values()}
    draft = data.get('draft', {})
    selected = set(draft.get('selected', ())) & set(strategies)
    forms = {key: dict(value) for key, value in draft.get('forms', {}).items()}
    for sid, row in existing_by_strategy.items():
        forms.setdefault(sid, {'account': row['account_id'], 'version': row['strategy_version'],
                               'budget': row['budget']['usdt'], 'leverage': str(row['budget']['planned_leverage'])})
    account_labels = {f"{value['name']} · {key[:8]}": key for key, value in data['accounts'].items()}
    label_by_account = {key: label for label, key in account_labels.items()}
    step = 0
    controls = {}
    checks = {}
    assessors = {}
    moving = False
    completing = False

    def ordered():
        return [sid for sid in strategies if sid in selected]

    def save_draft():
        collect()
        try:
            save_data({'draft': {'selected': ordered(), 'forms': forms}})
            message.set('草稿已保存在当前 Windows 用户配置。')
            return True
        except OSError:
            message.set('草稿保存失败，请检查本机加密配置。')
            return False

    def collect():
        for sid, (account, version, budget, leverage) in controls.items():
            forms[sid] = {'account': account_labels.get(account.get(), ''), 'version': paper.get(sid, {}).get('live_rule', {}).get('version', ''),
                          'budget': budget.get(), 'leverage': leverage.get()}

    def records():
        collect()
        result = {}
        for sid in ordered():
            values = forms.get(sid, {})
            account = values.get('account')
            if account not in data['accounts']:
                raise ValueError(f'{sid}：请选择已验证的 Binance 子账户。')
            if account in result:
                raise ValueError('同一交易账户仅允许绑定一套待启用策略。主账户预算没有独立资金隔离。')
            old = data['bindings'].get(account)
            if old and old['strategy_id'] not in selected:
                raise ValueError(f'{sid}：该账户已分配给 {old["strategy_id"]}。')
            snapshot = snapshots.get(account)
            if not snapshot:
                raise ValueError(f'{sid}：请先扫描该账户的可用资金。')
            if not values.get('version'):
                raise ValueError(f'{sid}：版本读取失败。')
            binding = make_binding(account, sid, values.get('version', ''), values.get('budget', ''),
                                   values.get('leverage', ''), snapshot['available'])
            row = asdict(binding)
            row['budget']['usdt'] = str(binding.budget.usdt)
            result[account] = row
        return result

    def fingerprint(sid):
        values = forms[sid]
        rule = paper.get(sid, {}).get('live_rule', {})
        return (sid, rule.get('sha256'), *(values[name] for name in ('account', 'version', 'budget', 'leverage')))

    def current_result(sid, *, fresh=False):
        observed = checks.get(sid)
        if observed and observed[0] == fingerprint(sid) and (not fresh or monotonic() - observed[2] <= 60):
            return observed[1]
        return None

    def result_text(result):
        text = f"{result['state']} · {result['reason']}"
        if 'quantity' in result:
            text += (f"\n可用 {result['available']} USDT · 预算 {result['budget']} USDT · 允许上限 {result['allowed_leverage']}×"
                     f"\n价格 {result['price']} · 最小数量 {result['quantity']} BTC · 保证金 {result['margin']:.2f} USDT"
                     f" · 费用 {result['fees']:.2f} · 预留 {result['reserve']:.2f} · 风险 {result['risk_status']}")
        return text

    def approved():
        collect()
        return all((current_result(sid) or {}).get('state') == 'PASS' for sid in ordered())

    def next_step():
        nonlocal moving
        if moving or not selected:
            if not moving:
                message.set('请至少选择一套策略。')
            return
        if step == 0:
            if save_draft():
                render(1)
            return
        collect()
        pending = iter(sid for sid in ordered() if current_result(sid, fresh=True) is None)
        moving = True
        submit.state(['disabled'])

        def advance():
            nonlocal moving
            if not window.winfo_exists():
                return
            sid = next(pending, None)
            if sid is not None:
                message.set(f'{sid}：正在重新核验当前配置与资金…')
                assessors[sid](advance)
                return
            moving = False
            submit.state(['!disabled'])
            if save_draft():
                render(2)

        advance()

    def finish():
        nonlocal completing
        if completing:
            return
        if not approved():
            message.set('账户与配置检查未通过，请返回资金页核对。')
            return
        try:
            accounts = iter(records())
        except ValueError as exc:
            message.set(str(exc))
            return
        completing = True
        submit.configure(state='disabled')

        def done():
            nonlocal completing
            if not window.winfo_exists():
                return
            try:
                account = next(accounts, None)
                if account is not None:
                    if query_account(account, done, message, failed) is False:
                        failed()
                    return
                if not approved():
                    raise ValueError("配置发生变化，请返回重新核验。")
                configured = records()
                preserved = {key: row for key, row in data['bindings'].items() if row['strategy_id'] not in selected}
                save_data({'bindings': {**preserved, **configured}, 'draft': {}})
            except (ValueError, OSError) as exc:
                message.set(str(exc) if isinstance(exc, ValueError) else '本机配置保存失败。')
                failed()
                return
            window.destroy()
            on_saved()

        def failed():
            nonlocal completing
            completing = False
            if window.winfo_exists():
                submit.configure(state='normal')

        done()

    def render(target):
        nonlocal step, submit
        if completing or moving:
            return
        collect()
        controls.clear()
        step = target
        message.set('')
        for area in (frame, footer):
            for child in area.winfo_children():
                child.destroy()
        for index, label in enumerate(labels):
            label.configure(bg='#175cf2' if index == step else '#e2ebfa', fg='white' if index == step else '#334766')
        if step == 0:
            left = ttk.LabelFrame(frame, text='纸盘策略', padding=10, style='Live.TLabelframe')
            right = ttk.LabelFrame(frame, text='已选策略与账户', padding=10, style='Live.TLabelframe')
            right.pack(side='right', fill='y')
            left.pack(side='left', fill='both', expand=True, padx=(0, 10))
            chosen = ttk.Label(right, style='Live.TLabel')
            chosen.pack(anchor='w', pady=(0, 8))
            selection_list = ttk.Treeview(right, columns=('strategy', 'account'), show='headings', style='Live.Treeview', height=9)
            for key, name in zip(selection_list['columns'], ('策略', '子账户')):
                selection_list.heading(key, text=name)
                selection_list.column(key, width=94)
            bind_choice = ttk.Combobox(right, state='readonly', values=tuple(account_labels), width=23)
            bind_choice.pack(side='bottom', fill='x', pady=8)
            selection_list.pack(fill='both', expand=True)

            def set_account(_event):
                ids = selection_list.selection()
                if ids:
                    forms.setdefault(ids[0], {})['account'] = account_labels.get(bind_choice.get(), '')
                    selected_rows()

            def selected_rows():
                chosen.configure(text=f'已选择 {len(selected)} 套策略')
                for sid in selection_list.get_children():
                    selection_list.delete(sid)
                for sid in ordered():
                    selection_list.insert('', 'end', iid=sid, values=(sid, label_by_account.get(forms.get(sid, {}).get('account'), '未绑定')))
                bind_choice.set('')

            selection_list.bind('<<TreeviewSelect>>', lambda _event: bind_choice.set(
                label_by_account.get(forms.get(selection_list.selection()[0], {}).get('account'), '') if selection_list.selection() else ''))
            bind_choice.bind('<<ComboboxSelected>>', set_account)
            filters = ttk.Frame(left, style='Live.TFrame')
            filters.pack(fill='x', pady=(0, 10))
            category = ttk.Combobox(filters, state='readonly', values=('全部', '原有策略', '镜像策略', 'PA策略', '已运行', '未配置实盘'))
            category.set('全部')
            category.pack(side='left')
            search = tk.StringVar()
            ttk.Entry(filters, width=14, textvariable=search).pack(side='left', padx=6)
            table = ttk.Treeview(left, columns=('pick', 'id', 'name', 'status', 'position', 'equity', 'cycle', 'configured'), show='headings', selectmode='none', style='Live.Treeview')
            for key, title, width in zip(table['columns'], ('勾选', '策略ID', '策略名称', '纸盘状态', '纸盘持仓', '纸盘权益', '最近周期', '实盘配置'), (50, 62, 165, 90, 80, 85, 130, 85)):
                table.heading(key, text=title)
                table.column(key, width=width, minwidth=width, stretch=False)
            horizontal = ttk.Scrollbar(left, orient='horizontal', command=table.xview)
            scroll = ttk.Scrollbar(left, command=table.yview)
            table.configure(xscrollcommand=horizontal.set, yscrollcommand=scroll.set)
            horizontal.pack(side='bottom', fill='x')
            scroll.pack(side='right', fill='y')
            table.pack(fill='both', expand=True)

            def rows():
                value = category.get()
                for sid in table.get_children():
                    table.delete(sid)
                for sid in strategies:
                    row = paper.get(sid, {})
                    state = row.get('status_label', '未读取')
                    if search.get().strip().casefold() not in f"{sid} {row.get('name', '')}".casefold():
                        continue
                    if ((value == '原有策略' and sid not in 'ABCDE') or (value == '镜像策略' and sid != 'G')
                            or (value == 'PA策略' and not sid.startswith('PA-'))
                            or (value == '已运行' and state != '运行中')
                            or (value == '未配置实盘' and sid in existing_by_strategy)):
                        continue
                    table.insert('', 'end', iid=sid, values=('☑' if sid in selected else '☐', sid, row.get('name', sid), state,
                                 row.get('state', '—'), row.get('equity') if row.get('equity') is not None else '—',
                                 row.get('cycle', '—'), '已配置' if sid in existing_by_strategy else '—'))
                next_button.configure(state='normal' if selected else 'disabled')
                selected_rows()

            def toggle(event):
                sid = table.identify_row(event.y)
                if sid:
                    checks.pop(sid, None)
                    selected.symmetric_difference_update((sid,))
                    rows()

            def select_page():
                selected.update(table.get_children())
                rows()

            ttk.Button(filters, text='全选当前页', command=select_page, style='Live.TButton').pack(side='left')
            category.bind('<<ComboboxSelected>>', lambda _event: rows())
            search.trace_add('write', lambda *_args: rows())
            table.bind('<ButtonRelease-1>', toggle)
        elif step == 1:
            ttk.Label(frame, text='分配实盘预算与杠杆', style='Live.TLabel', font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w', pady=(0, 9))
            totals = ttk.Frame(frame, style='Live.TFrame')
            totals.pack(fill='x', pady=(0, 9))
            metrics = [tk.StringVar(value='—') for _ in range(4)]
            show_callbacks = []

            def update_totals():
                accounts = {account_labels.get(fields[0].get()) for fields in controls.values()}
                scanned = [snapshots[key]['available'] for key in accounts if key in snapshots]
                budgets = []
                for _, _, amount, leverage in controls.values():
                    try:
                        value, multiple = Decimal(amount.get()), int(leverage.get())
                    except (ArithmeticError, ValueError):
                        continue
                    if value.is_finite() and value > 0 and 1 <= multiple <= 5:
                        budgets.append((value, multiple))
                metrics[0].set(f'{sum(scanned):,.2f}' if scanned else '—')
                metrics[1].set(f'{sum(value for value, _ in budgets):,.2f}')
                metrics[2].set(f'{sum(value * multiple for value, multiple in budgets):,.2f}')
                metrics[3].set(f'{max(multiple for _, multiple in budgets)}×' if budgets else '—')
            for index, (name, value) in enumerate(zip(('已扫描可用 USDT', '计划预算 USDT', '预计名义 USDT', '最高计划杠杆'), metrics)):
                metric(totals, name, value).grid(row=0, column=index, sticky='nsew', padx=(0, 5))
                totals.columnconfigure(index, weight=1)
            rule = ttk.LabelFrame(frame, text='分配规则', padding=12, style='Live.TLabelframe')
            rule.pack(side='right', fill='y', padx=(8, 0))
            tk.Label(rule, text='预算不得超过合约可用余额\n杠杆取计划、策略和交易所的较低值\n系统上限 5×\n账户与配置检查通过后可继续\n程序不会自动追加保证金',
                     justify='left', fg=INK, bg='#fbfdff', font=('Microsoft YaHei UI', 10)).pack(anchor='w')
            work = ttk.Frame(frame, style='Live.TFrame')
            work.pack(fill='both', expand=True)
            canvas = tk.Canvas(work, highlightthickness=0, bg=BG)
            scroll = ttk.Scrollbar(work, command=canvas.yview)
            canvas.configure(yscrollcommand=scroll.set)
            scroll.pack(side='right', fill='y')
            canvas.pack(fill='both', expand=True)
            cards = ttk.Frame(canvas, style='Live.TFrame')
            embed = canvas.create_window(0, 0, window=cards, anchor='nw')
            cards.bind('<Configure>', lambda _e: canvas.configure(scrollregion=canvas.bbox('all')))
            canvas.bind('<Configure>', lambda e: canvas.itemconfigure(embed, width=e.width))
            for sid in ordered():
                row = paper.get(sid, {})
                values = forms.get(sid, {})
                card = ttk.LabelFrame(cards, padding=9, style='Live.TLabelframe', text=f'{sid} · {row.get("name", sid)} · 纸盘 {row.get("status_label", "未读取")}')
                card.pack(fill='x', padx=2, pady=(0, 7))
                account = ttk.Combobox(card, values=tuple(account_labels), width=19, state='readonly')
                account.set(label_by_account.get(values.get('account'), ''))
                if not account.get() and len(ordered()) == 1 and len(account_labels) == 1:
                    account_id = next(iter(account_labels.values()))
                    binding = data['bindings'].get(account_id)
                    if not binding or binding['strategy_id'] == sid:
                        account.set(next(iter(account_labels)))
                amount_var = tk.StringVar()
                leverage_var = tk.StringVar()
                version, budget = ttk.Entry(card, width=12), ttk.Entry(card, width=12, textvariable=amount_var)
                rule = row.get('live_rule', {})
                version.insert(0, rule.get('version') or '版本读取失败')
                version.configure(state='readonly')
                if rule.get('version'):
                    ttk.Label(card, text=f"上限 {rule.get('max_leverage', '—')}× · 风险比例 {rule.get('risk_fraction', '—')} · 规则 {rule.get('sha256', '')[:12]}", style='Live.TLabel').grid(column=0, row=5, columnspan=4, sticky='w', pady=5)
                elif rule.get('error'):
                    ttk.Label(card, text=rule['error'], style='Live.TLabel').grid(column=0, row=5, columnspan=4, sticky='w')
                budget.insert(0, values.get('budget', ''))
                leverage = ttk.Spinbox(card, from_=1, to=5, width=4, textvariable=leverage_var)
                leverage.set(values.get('leverage', '5'))
                controls[sid] = account, version, budget, leverage
                for index, (title, field) in enumerate(zip(('账户', '策略版本', '预算 USDT', '杠杆'), controls[sid])):
                    ttk.Label(card, text=title).grid(column=index, row=0, sticky='w')
                    card.columnconfigure(index, weight=1)
                    field.grid(column=index, row=1, sticky='ew', padx=(0, 9), ipady=4)
                seen = ttk.Label(card)
                seen.grid(column=0, row=3, columnspan=5, sticky='w', pady=(5, 0))

                def show(control=account, output=seen):
                    snapshot = snapshots.get(account_labels.get(control.get()))
                    output.configure(text=(f"可用 {snapshot['available']} USDT · 读取已验证 · 合约交易权限 "
                                           + {True: '已开通', False: '未开通'}.get(snapshot['permissions'].get('enableFutures'), '未知')
                                           + f" · 更新 {snapshot.get('scanned_at', '—')}\n钱包 {snapshot.get('wallet', '—')} · 权益 {snapshot.get('equity', '—')} · 占用初始保证金 {snapshot.get('initial_margin') or '—'} USDT")
                                     if snapshot else '可用资金及权限待扫描')
                    update_totals()

                def scan(control=account, refresh=show):
                    account_id = account_labels.get(control.get())
                    if not account_id:
                        message.set('请先选择已绑定的子账户。')
                        return
                    def scanned():
                        if control.winfo_exists():
                            refresh()
                            message.set('余额已更新。')
                    query_account(account_id, scanned, message)

                account.bind('<<ComboboxSelected>>', lambda _event, refresh=show: refresh())
                ttk.Button(card, text='扫描余额', command=scan, style='Live.TButton').grid(column=0, row=2, sticky='w', pady=(6, 0))
                result_label = ttk.Label(card, style='Live.TLabel', justify='left', wraplength=640)
                result_label.grid(column=0, row=4, columnspan=5, sticky='w')
                if sid not in ('A', 'C'):
                    result_label.configure(text='UNVERIFIED · 预检适配器未完成。')
                collect()
                previous = current_result(sid)
                if previous:
                    result_label.configure(text=result_text(previous))

                def assess(done=lambda: None, current_sid=sid, output=result_label, refresh=show):
                    collect()
                    values = forms[current_sid]
                    key = fingerprint(current_sid)
                    checks.pop(current_sid, None)
                    output.configure(text='正在核验余额、BTCUSDT 规则和策略限制…')

                    def display(result):
                        if not output.winfo_exists():
                            return
                        collect()
                        if fingerprint(current_sid) == key:
                            checks[current_sid] = (key, result, monotonic())
                            output.configure(text=result_text(result))
                            refresh()
                        done()

                    if values['account'] not in data['accounts'] or not values['version']:
                        display({'state': 'UNVERIFIED', 'reason': '请先关联账户并确认活动版本可读取。'})
                    elif preflight(values['account'], current_sid, values['version'], values['budget'],
                                   values['leverage'], display, message) is False:
                        display({'state': 'UNVERIFIED', 'reason': '已有查询在进行，请稍后重试。'})

                assessors[sid] = assess

                def invalidate(*_args, current_sid=sid, output=result_label):
                    if checks.pop(current_sid, None) is not None:
                        output.configure(text='UNVERIFIED · 配置已变化，将在下一步重新预检。')
                    update_totals()

                account.bind('<<ComboboxSelected>>', invalidate, add='+')
                ttk.Button(card, text='账户与配置检查', command=assess, style='LivePrimary.TButton').grid(column=1, row=2, columnspan=3, sticky='w', pady=(6, 0))
                show_callbacks.append(show)
                amount_var.trace_add('write', invalidate)
                leverage_var.trace_add('write', invalidate)
                card.amount_var = amount_var
                card.leverage_var = leverage_var
            for show in show_callbacks:
                show()
            keys = iter(dict.fromkeys(account_labels.get(fields[0].get()) for fields in controls.values()))
            def refresh_bound():
                if not window.winfo_exists() or step != 1:
                    return
                for key in keys:
                    if key:
                        def refresh_next():
                            if window.winfo_exists() and step == 1:
                                for refresh in show_callbacks:
                                    refresh()
                                if not moving and message.get() == "正在查询只读账户…":
                                    message.set("可用余额已更新。")
                                window.after_idle(refresh_bound)
                        query_account(key, refresh_next, message, refresh_next)
                        return
            window.after_idle(refresh_bound)
        else:
            verified = approved()
            ttk.Label(frame, style='Live.TLabel', wraplength=820,
                      text=('账户与配置检查：PASS · 15 分钟周期 · 执行器、交易所止损保护与恢复待验收。' if verified else
                            '尚未通过账户与配置检查，当前配置不能启用真实交易。')).pack(anchor='w', pady=(0, 12))
            table = ttk.Treeview(frame, columns=('id','account','version','budget','lev'), show='headings', height=7, style='Live.Treeview')
            for name, title in zip(table['columns'], ('策略','交易账户','活动版本','预算 USDT','计划杠杆')):
                table.heading(name, text=title)
                table.column(name, width=120, minwidth=65)
            table.pack(fill='x')
            for sid in ordered():
                values = forms[sid]
                account = data['accounts'].get(values['account'], {})
                table.insert('', 'end', values=(sid, account.get('name','未绑定'), values['version'] or '版本读取失败',
                                               values['budget'], values['leverage']))
                result = current_result(sid) or {'state':'UNVERIFIED', 'reason':'预检已过期或配置已变化。'}
                ttk.Label(frame, style='Live.TLabel', wraplength=780,
                          text=f"{sid} · {result_text(result)}").pack(anchor='w', pady=6)
        ttk.Button(footer, text='上一步' if step else '关闭', style='Live.TButton', command=lambda: render(step-1) if step else close()).pack(side='left')
        ttk.Button(footer, text='保存草稿', style='Live.TButton', command=save_draft).pack(side='left', padx=8)
        submit = ttk.Button(footer, text='保存配置' if step == 2 else ('下一步：分配资金' if step == 0 else '下一步：确认配置'),
                            command=finish if step == 2 else next_step, style='LivePrimary.TButton')
        submit.pack(side='right')
        if step == 2 and not verified:
            submit.state(['disabled'])
        if step == 0:
            next_button = submit
            rows()

    def close():
        if completing:
            window.destroy()
        elif save_draft():
            window.destroy()

    submit = None
    window.protocol('WM_DELETE_WINDOW', close)
    render(0)
    return window
