"""Read-only evidence display. Production order submission is unavailable."""
import tkinter as tk
from tkinter import ttk

from scripts.live_ui_style import initialize


def evidence_rows(account, snapshot, binding, preflight, *, simulated=False):
    def state(value):
        return 'UNVERIFIED' if value is None else 'PASS' if value else 'FAIL'

    verified = snapshot if not simulated else None
    verified_preflight = preflight if verified else None
    conflicts = None if verified is None else bool(verified['positions'] or verified['orders']
                                                   or verified['conditional'] or verified.get('other_activity'))
    return (
        ('API 已绑定', state(bool(account) if not simulated else None), '本机加密账户登记'),
        ('真实账户认证', state(True if verified else None), '当前会话的账户读取结果'),
        ('资金可用', state(verified['available'] >= binding['budget'] if verified and binding else None), '合约余额与所选预算比较'),
        ('策略版本有效', state(verified_preflight.get('version_verified') if verified_preflight else None), '已核对版本和原始规则'),
        ('最小订单可行', state(verified_preflight.get('fits') if verified_preflight else None), '交易所规则及费用预检'),
        ('账户与配置检查', verified_preflight.get('state', 'UNVERIFIED') if verified_preflight else 'UNVERIFIED', '收到信号后才执行订单级风险检查'),
        ('无仓位冲突', state(not conflicts if conflicts is not None else None), '读取账户仓位与挂单'),
        ('独立执行器已验收', 'UNVERIFIED', '离线模拟不构成实盘执行验收'),
        ('交易所止损及恢复已验收', 'UNVERIFIED', '实盘保护与恢复未验收'),
    )


def open_start_check(owner, rows, *, monitor=None, refresh=None, ready=False):
    existing = getattr(owner, '_start_check', None)
    if existing is not None and existing.winfo_exists():
        existing.destroy()
    window = tk.Toplevel(owner)
    owner._start_check = window
    window.transient(owner)
    window.title('BTC 实盘策略启动检查')
    window.geometry('820x570')
    initialize(window)
    tk.Label(window, text='实盘启动检查', bg='#17458c', fg='white', anchor='w',
             font=('Microsoft YaHei UI', 16, 'bold'), padx=14, pady=10).pack(fill='x')
    feedback = tk.StringVar(value=('已保存受支持的配置。启动后核验正式版本与已收盘 K 线。' if ready else '请先保存受支持的策略配置。'))
    ttk.Label(window, text='只读监测准备状态', style='Live.TLabel').pack(anchor='w', padx=14, pady=(8, 0))
    ttk.Label(window, textvariable=feedback, style='Live.TLabel', wraplength=720).pack(anchor='w', padx=14)
    ttk.Label(window, text='真实交易启动准备状态', style='Live.TLabel').pack(anchor='w', padx=14, pady=(8, 0))
    footer = ttk.Frame(window, padding=(14, 10), style='Live.TFrame')
    footer.pack(side='bottom', fill='x')
    table = ttk.Treeview(window, columns=('check', 'status', 'detail'), show='headings', style='Live.Treeview')
    for column, title, width in zip(table['columns'], ('检查项', '状态', '证据'), (220, 120, 350)):
        table.heading(column, text=title)
        table.column(column, width=width)
    for color, foreground in (('PASS', '#067331'), ('FAIL', '#a93c2e'), ('UNVERIFIED', '#9a500b')):
        table.tag_configure(color, foreground=foreground)
    for label, status, detail in rows:
        table.insert('', 'end', values=(label, status, detail), tags=(status,))
    table.pack(fill='both', expand=True, padx=14, pady=14)
    if monitor:
        ttk.Button(footer, text='启动只读信号监测', command=lambda: feedback.set(monitor()),
                   style='Live.TButton').pack(side='left', padx=14, pady=(0, 14))
    if refresh:
        ttk.Button(footer, text='刷新账户与配置检查', style='Live.TButton',
                   command=lambda: (feedback.set('正在刷新账户与配置检查…'), refresh())).pack(side='left', padx=8)
    ttk.Button(footer, text='启动实盘策略', style='LivePrimary.TButton', state='disabled').pack(side='right', padx=14, pady=(0, 14))
    return window
