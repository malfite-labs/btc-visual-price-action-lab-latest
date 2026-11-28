"""BTC Paper Lab V3.1 — Quant Strategy Research Center V3 Desktop UI (Read-Only).

Implements all 8 research modules with light financial styling, non-blocking async loading,
fault isolation, multi-strategy comparison, visual charts, month PnL calendar,
dual-view paginated history, attribution analysis, version/backtest comparison, and CSV/MD export.
"""
from __future__ import annotations

import calendar as _calendar
import csv
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from scripts.research_v3_engine import (
    BEIJING,
    CALCULATION_VERSION,
    UTC,
    ResearchV3Engine,
    compute_attribution,
    compute_metrics,
    compute_multi_compare,
    compute_version_breakdown,
    dec,
    export_research_report,
    fmt_bj,
    parse_utc,
)
from scripts.strategy_pool_ui import CalendarDatePicker

_OPEN_RESEARCH_WINDOW: tk.Toplevel | None = None
_PALETTE = ('#2563eb', '#16a34a', '#dc2626', '#9333ea', '#ea580c')


def _fmt_num(val, digits=2, signed=False) -> str:
    d = dec(val)
    if d is None:
        return '—'
    return f'{d:+,.{digits}f}' if signed else f'{d:,.{digits}f}'


def _fmt_pct(val, signed=False) -> str:
    if val is None:
        return '—'
    try:
        f = float(val)
        return f'{f:+.2f}%' if signed else f'{f:.2f}%'
    except (TypeError, ValueError):
        return '—'


def _fmt_dur(sec: float | None) -> str:
    if sec is None:
        return '—'
    if sec < 3600:
        return f'{sec / 60:.0f} 分钟'
    if sec < 86400:
        return f'{sec / 3600:.1f} 小时'
    return f'{sec / 86400:.1f} 天'


def _draw_single_equity_dd(canvas: tk.Canvas, points: list[dict], initial_cap: float, title: str = ''):
    canvas.delete('all')
    w = max(canvas.winfo_width(), 820)
    h = max(canvas.winfo_height(), 360)
    if len(points) < 2:
        canvas.create_text(
            w // 2, h // 2,
            text='权益观测点不足（<2 个），暂不绘制历史曲线；系统绝不伪造历史权益点。',
            fill='#9a3412', font=('Microsoft YaHei UI', 10, 'bold'),
        )
        return

    equities = [float(dec(p.get('equity')) or Decimal(str(initial_cap))) for p in points]
    dds = [float(dec(p.get('drawdown_pct')) or Decimal(0)) for p in points]
    times = [str(p.get('timestamp') or '') for p in points]

    pad_l, pad_r = 76, 26
    top_y1, top_y2 = 28, int(h * 0.57)
    bot_y1, bot_y2 = int(h * 0.67), h - 30
    plot_w = max(100, w - pad_l - pad_r)

    # Top Equity chart
    canvas.create_rectangle(pad_l, top_y1, w - pad_r, top_y2, fill='white', outline='#cbd5e1')
    canvas.create_text(pad_l + 6, top_y1 - 12, text=title or '权益曲线（已实现重建 + 当前快照）', anchor='w', fill='#1e3a5f', font=('Microsoft YaHei UI', 9, 'bold'))
    lo, hi = min(min(equities), initial_cap), max(max(equities), initial_cap)
    span = max(hi - lo, 1.0)
    lo -= span * 0.08
    hi += span * 0.08
    span = hi - lo
    for i in range(5):
        frac = i / 4.0
        y = top_y2 - frac * (top_y2 - top_y1)
        canvas.create_line(pad_l, y, w - pad_r, y, fill='#f1f5f9')
        canvas.create_text(pad_l - 6, y, text=f'{lo + frac * span:,.0f}', anchor='e', fill='#64748b', font=('Consolas', 8))
    base_y = top_y2 - ((initial_cap - lo) / span) * (top_y2 - top_y1)
    canvas.create_line(pad_l, base_y, w - pad_r, base_y, fill='#94a3b8', dash=(4, 4))

    n = len(equities)
    coords = []
    for i, eq in enumerate(equities):
        x = pad_l + (i / max(1, n - 1)) * plot_w
        y = top_y2 - ((eq - lo) / span) * (top_y2 - top_y1)
        coords.extend((x, y))
    color = '#16a34a' if equities[-1] >= initial_cap else '#dc2626'
    canvas.create_line(*coords, fill=color, width=2)
    canvas.create_text(w - pad_r - 8, top_y1 + 12, text=f'当前: {equities[-1]:,.2f}  |  峰值: {max(equities):,.2f}', anchor='e', fill='#0f172a', font=('Microsoft YaHei UI', 8, 'bold'))

    # Bottom Drawdown chart
    canvas.create_rectangle(pad_l, bot_y1, w - pad_r, bot_y2, fill='white', outline='#cbd5e1')
    canvas.create_text(pad_l + 6, bot_y1 - 12, text='回撤曲线 (%)', anchor='w', fill='#991b1b', font=('Microsoft YaHei UI', 9, 'bold'))
    max_dd = max(max(dds), 0.5)
    for i in range(4):
        frac = i / 3.0
        y = bot_y1 + frac * (bot_y2 - bot_y1)
        canvas.create_line(pad_l, y, w - pad_r, y, fill='#fef2f2')
        canvas.create_text(pad_l - 6, y, text=f'-{frac * max_dd:.2f}%', anchor='e', fill='#64748b', font=('Consolas', 8))
    poly = [pad_l, bot_y1]
    line = []
    for i, dd in enumerate(dds):
        x = pad_l + (i / max(1, n - 1)) * plot_w
        y = bot_y1 + (dd / max_dd) * (bot_y2 - bot_y1)
        poly.extend((x, y))
        line.extend((x, y))
    poly.extend((w - pad_r, bot_y1))
    canvas.create_polygon(*poly, fill='#fecaca', outline='')
    canvas.create_line(*line, fill='#dc2626', width=1.5)
    canvas.create_text(w - pad_r - 8, bot_y1 + 12, text=f'最大回撤: -{max(dds):.2f}%', anchor='e', fill='#b91c1c', font=('Microsoft YaHei UI', 8, 'bold'))

    for idx in (0, n // 2, n - 1):
        x = pad_l + (idx / max(1, n - 1)) * plot_w
        canvas.create_text(x, bot_y2 + 12, text=fmt_bj(times[idx], 10), anchor='center', fill='#475569', font=('Consolas', 8))


def _draw_multi_compare_charts(canvas: tk.Canvas, compare_res: dict):
    canvas.delete('all')
    w = max(canvas.winfo_width(), 860)
    h = max(canvas.winfo_height(), 360)
    items = compare_res.get('strategies') or []
    valid_series = [(it, it.get('normalized_series') or []) for it in items if len(it.get('normalized_series') or []) >= 2]
    if not valid_series:
        canvas.create_text(w // 2, h // 2, text='所选策略在当前共同区间内少于 2 个有效权益点，无法叠加曲线。', fill='#9a3412', font=('Microsoft YaHei UI', 10, 'bold'))
        return

    pad_l, pad_r = 72, 26
    top_y1, top_y2 = 34, int(h * 0.57)
    bot_y1, bot_y2 = int(h * 0.67), h - 28
    plot_w = max(100, w - pad_l - pad_r)

    # Global time bounds
    all_ts = [parse_utc(p['timestamp']).timestamp() for _, s in valid_series for p in s if parse_utc(p['timestamp'])]
    t_min, t_max = min(all_ts), max(all_ts)
    t_span = max(t_max - t_min, 1.0)

    all_ret = [p['return_pct'] for _, s in valid_series for p in s]
    r_lo, r_hi = min(min(all_ret), 0.0), max(max(all_ret), 0.0)
    r_span = max(r_hi - r_lo, 1.0)
    r_lo -= r_span * 0.1
    r_hi += r_span * 0.1
    r_span = r_hi - r_lo

    all_dd = [p['drawdown_pct'] for _, s in valid_series for p in s]
    max_dd = max(max(all_dd), 0.5)

    canvas.create_rectangle(pad_l, top_y1, w - pad_r, top_y2, fill='white', outline='#cbd5e1')
    canvas.create_text(pad_l + 6, top_y1 - 14, text='多策略收益率叠加曲线 (%) · 归一化基准=0%', anchor='w', fill='#1e3a5f', font=('Microsoft YaHei UI', 9, 'bold'))
    for i in range(5):
        frac = i / 4.0
        y = top_y2 - frac * (top_y2 - top_y1)
        canvas.create_line(pad_l, y, w - pad_r, y, fill='#f1f5f9')
        canvas.create_text(pad_l - 6, y, text=f'{r_lo + frac * r_span:+.1f}%', anchor='e', fill='#64748b', font=('Consolas', 8))
    y0 = top_y2 - ((0.0 - r_lo) / r_span) * (top_y2 - top_y1)
    canvas.create_line(pad_l, y0, w - pad_r, y0, fill='#94a3b8', dash=(4, 4))

    canvas.create_rectangle(pad_l, bot_y1, w - pad_r, bot_y2, fill='white', outline='#cbd5e1')
    canvas.create_text(pad_l + 6, bot_y1 - 12, text='多策略回撤叠加曲线 (%)', anchor='w', fill='#991b1b', font=('Microsoft YaHei UI', 9, 'bold'))
    for i in range(4):
        frac = i / 3.0
        y = bot_y1 + frac * (bot_y2 - bot_y1)
        canvas.create_line(pad_l, y, w - pad_r, y, fill='#fef2f2')
        canvas.create_text(pad_l - 6, y, text=f'-{frac * max_dd:.1f}%', anchor='e', fill='#64748b', font=('Consolas', 8))

    # Legend + polylines
    lx = w - pad_r - 8
    for idx, (it, series) in enumerate(valid_series):
        col = _PALETTE[idx % len(_PALETTE)]
        ret_coords, dd_coords = [], []
        for p in series:
            dt = parse_utc(p['timestamp'])
            if dt is None:
                continue
            x = pad_l + ((dt.timestamp() - t_min) / t_span) * plot_w
            yr = top_y2 - ((p['return_pct'] - r_lo) / r_span) * (top_y2 - top_y1)
            yd = bot_y1 + (p['drawdown_pct'] / max_dd) * (bot_y2 - bot_y1)
            ret_coords.extend((x, yr))
            dd_coords.extend((x, yd))
        if len(ret_coords) >= 4:
            canvas.create_line(*ret_coords, fill=col, width=2)
            canvas.create_line(*dd_coords, fill=col, width=1.6)
        lbl = f"■ {it['strategy_id']}"
        canvas.create_text(lx, top_y1 - 14, text=lbl, anchor='e', fill=col, font=('Microsoft YaHei UI', 8, 'bold'))
        lx -= max(85, len(lbl) * 8)

    for frac, ts_val in ((0.0, t_min), (0.5, (t_min + t_max) / 2), (1.0, t_max)):
        x = pad_l + frac * plot_w
        dt_str = datetime.fromtimestamp(ts_val, tz=UTC).astimezone(BEIJING).strftime('%Y-%m-%d')
        canvas.create_text(x, bot_y2 + 12, text=dt_str, anchor='center', fill='#475569', font=('Consolas', 8))


def open_research_center_v3(parent, *, engine: ResearchV3Engine | None = None):
    """Open or focus the singleton Quant Strategy Research Center V3 window."""
    global _OPEN_RESEARCH_WINDOW
    if _OPEN_RESEARCH_WINDOW is not None:
        try:
            if _OPEN_RESEARCH_WINDOW.winfo_exists():
                _OPEN_RESEARCH_WINDOW.deiconify()
                _OPEN_RESEARCH_WINDOW.lift()
                _OPEN_RESEARCH_WINDOW.focus_force()
                return _OPEN_RESEARCH_WINDOW
        except tk.TclError:
            pass
        _OPEN_RESEARCH_WINDOW = None

    eng = engine or ResearchV3Engine()
    win = tk.Toplevel(parent)
    win.title('量化策略研究中心 V3 · 只读研究')
    win.geometry('1380x840')
    win.minsize(1060, 660)
    win.configure(bg='#f5f7fb')
    _OPEN_RESEARCH_WINDOW = win

    def _on_destroy(event):
        global _OPEN_RESEARCH_WINDOW
        if getattr(event, 'widget', None) is win:
            _OPEN_RESEARCH_WINDOW = None

    win.bind('<Destroy>', _on_destroy, add='+')

    state = {
        'snapshot': None,
        'by_id': {},
        'selected_id': None,
        'compare_ids': set(),
        'sort_col': 'strategy_id',
        'sort_desc': False,
        'cal_year': datetime.now(BEIJING).year,
        'cal_month': datetime.now(BEIJING).month,
        'cal_selected_day': None,
        'hist_page': 0,
        'hist_rows': [],
        'loading': False,
    }

    # ==================== ZONE 1: TOP HEADER ====================
    top = tk.Frame(win, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    top.pack(fill='x', padx=10, pady=(8, 4))
    tk.Label(top, text='量化策略研究中心 V3', bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 14, 'bold')).pack(side='left', padx=(12, 8), pady=8)
    tk.Label(top, text='只读研究 · 禁止写交易账本', bg='#dbeafe', fg='#1d4ed8', font=('Microsoft YaHei UI', 8, 'bold'), padx=8, pady=3).pack(side='left', padx=4)
    meta_var = tk.StringVar(value='正在异步读取只读研究数据…')
    tk.Label(top, textvariable=meta_var, bg='white', fg='#475569', font=('Microsoft YaHei UI', 8)).pack(side='right', padx=12)

    # ==================== ZONE 2: GLOBAL FILTER BAR ====================
    fbar = tk.Frame(win, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    fbar.pack(fill='x', padx=10, pady=(0, 4))

    tk.Label(fbar, text='分组:', bg='white', fg='#334155', font=('Microsoft YaHei UI', 8, 'bold')).pack(side='left', padx=(10, 3), pady=6)
    group_var = tk.StringVar(value='全部')
    group_cb = ttk.Combobox(fbar, textvariable=group_var, state='readonly', width=9, values=('全部', '原有策略', '镜像策略', '策略池', 'R系列'))
    group_cb.pack(side='left', padx=(0, 8))

    tk.Label(fbar, text='状态:', bg='white', fg='#334155', font=('Microsoft YaHei UI', 8, 'bold')).pack(side='left', padx=(0, 3))
    status_f_var = tk.StringVar(value='全部状态')
    status_cb = ttk.Combobox(fbar, textvariable=status_f_var, state='readonly', width=10, values=('全部状态', 'RUNNING', 'PAUSED', 'OPEN_LONG', 'FLAT'))
    status_cb.pack(side='left', padx=(0, 8))

    tk.Label(fbar, text='搜索:', bg='white', fg='#334155', font=('Microsoft YaHei UI', 8, 'bold')).pack(side='left', padx=(0, 3))
    search_var = tk.StringVar(value='')
    ttk.Entry(fbar, textvariable=search_var, width=16).pack(side='left', padx=(0, 8))

    tk.Label(fbar, text='区间:', bg='white', fg='#334155', font=('Microsoft YaHei UI', 8, 'bold')).pack(side='left', padx=(0, 3))
    range_var = tk.StringVar(value='全部有效历史')
    range_cb = ttk.Combobox(fbar, textvariable=range_var, state='readonly', width=12, values=('全部有效历史', '近7天', '近30天', '近90天', '自定义'))
    range_cb.pack(side='left', padx=(0, 6))

    today_bj = datetime.now(BEIJING).date()
    start_picker = CalendarDatePicker(fbar, today_bj - timedelta(days=30))
    end_picker = CalendarDatePicker(fbar, today_bj)
    start_picker.pack(side='left', padx=2)
    tk.Label(fbar, text='~', bg='white', fg='#64748b').pack(side='left')
    end_picker.pack(side='left', padx=(2, 8))

    def _get_time_bounds() -> tuple[datetime | None, datetime | None]:
        mode = range_var.get()
        now_u = datetime.now(UTC)
        if mode == '全部有效历史':
            return None, None
        if mode == '近7天':
            return now_u - timedelta(days=7), None
        if mode == '近30天':
            return now_u - timedelta(days=30), None
        if mode == '近90天':
            return now_u - timedelta(days=90), None
        sd = start_picker.get_date()
        ed = end_picker.get_date()
        s_dt = datetime(sd.year, sd.month, sd.day, 0, 0, tzinfo=BEIJING).astimezone(UTC)
        e_dt = datetime(ed.year, ed.month, ed.day, 23, 59, 59, tzinfo=BEIJING).astimezone(UTC)
        return s_dt, e_dt

    tk.Button(
        fbar, text='应用筛选', command=lambda: render_all(),
        bg='#eff6ff', fg='#1d4ed8', activebackground='#dbeafe', relief='flat',
        padx=10, pady=3, font=('Microsoft YaHei UI', 8, 'bold'), cursor='hand2',
    ).pack(side='left', padx=4)

    tk.Button(
        fbar, text='⟳ 刷新数据', command=lambda: refresh(force=True),
        bg='#2563eb', fg='white', activebackground='#1d4ed8', activeforeground='white',
        relief='flat', padx=12, pady=3, font=('Microsoft YaHei UI', 8, 'bold'), cursor='hand2',
    ).pack(side='right', padx=(4, 10), pady=5)

    # ==================== ZONE 3: SUMMARY KPI STRIP ====================
    kpi_strip = tk.Frame(win, bg='#f5f7fb')
    kpi_strip.pack(fill='x', padx=10, pady=(0, 4))
    sum_vars = {}
    for idx, (k, title, fg) in enumerate((
        ('total', '当前策略总数', '#172b4d'),
        ('traded', '已产生交易策略', '#1d4ed8'),
        ('profit', '盈利策略数', '#16a34a'),
        ('loss', '亏损策略数', '#dc2626'),
        ('flat', '盈亏持平策略', '#475569'),
        ('insufficient', '样本不足/待核查', '#b45309'),
    )):
        c = tk.Frame(kpi_strip, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
        c.grid(row=0, column=idx, sticky='nsew', padx=(0 if idx == 0 else 4, 0))
        tk.Label(c, text=title, bg='white', fg='#64748b', font=('Microsoft YaHei UI', 8), anchor='w').pack(fill='x', padx=8, pady=(5, 0))
        v = tk.StringVar(value='—')
        sum_vars[k] = v
        tk.Label(c, textvariable=v, bg='white', fg=fg, font=('Microsoft YaHei UI', 12, 'bold'), anchor='w').pack(fill='x', padx=8, pady=(0, 5))
        kpi_strip.columnconfigure(idx, weight=1, uniform='sum')

    # ==================== ZONE 4: 8 RESEARCH TABS ====================
    nb = ttk.Notebook(win)
    nb.pack(fill='both', expand=True, padx=10, pady=(2, 4))

    tab_overview = tk.Frame(nb, bg='#f5f7fb')
    tab_detail = tk.Frame(nb, bg='#f5f7fb')
    tab_compare = tk.Frame(nb, bg='#f5f7fb')
    tab_equity = tk.Frame(nb, bg='#f5f7fb')
    tab_calendar = tk.Frame(nb, bg='#f5f7fb')
    tab_history = tk.Frame(nb, bg='#f5f7fb')
    tab_attrib = tk.Frame(nb, bg='#f5f7fb')
    tab_ver_bt = tk.Frame(nb, bg='#f5f7fb')

    for frame, title in (
        (tab_overview, '策略总览'),
        (tab_detail, '策略详情'),
        (tab_compare, '策略对比'),
        (tab_equity, '权益与回撤'),
        (tab_calendar, '盈亏日历'),
        (tab_history, '历史交易'),
        (tab_attrib, '交易分析'),
        (tab_ver_bt, '版本与回测对照'),
    ):
        nb.add(frame, text=title)

    # ---------- TAB 1: OVERVIEW ----------
    ov_cols = (
        ('cmp', '对比', 44, 'center'),
        ('strategy_id', '策略 ID', 158, 'w'),
        ('strategy_name', '中文名称', 175, 'w'),
        ('version', '版本', 74, 'center'),
        ('state', '运行状态', 92, 'center'),
        ('run_days', '运行天数', 64, 'e'),
        ('initial', '初始资金', 86, 'e'),
        ('equity', '当前权益', 90, 'e'),
        ('net_pnl', '区间净盈亏', 92, 'e'),
        ('ret_pct', '收益率%', 78, 'e'),
        ('closed', '闭合笔数', 68, 'e'),
        ('win_rate', '胜率%', 68, 'e'),
        ('pf', '盈利因子', 84, 'center'),
        ('max_dd', '最大回撤%', 78, 'e'),
        ('fees', '累计手续费', 82, 'e'),
        ('avg_pnl', '平均每笔盈亏', 90, 'e'),
        ('quality', '数据质量', 124, 'w'),
    )
    ov_wrap = tk.Frame(tab_overview, bg='#f5f7fb')
    ov_wrap.pack(fill='both', expand=True, padx=4, pady=4)
    ov_tree = ttk.Treeview(ov_wrap, columns=tuple(c[0] for c in ov_cols), show='headings', height=18)
    ov_tree.tag_configure('profit', foreground='#15803d')
    ov_tree.tag_configure('loss', foreground='#b91c1c')
    ov_tree.tag_configure('warn', foreground='#9a3412')

    def _sort_overview(col_id: str):
        if col_id == 'cmp':
            return
        if state['sort_col'] == col_id:
            state['sort_desc'] = not state['sort_desc']
        else:
            state['sort_col'] = col_id
            state['sort_desc'] = True
        render_overview()

    for cid, title, width, anchor in ov_cols:
        ov_tree.heading(cid, text=title, command=lambda c=cid: _sort_overview(c))
        ov_tree.column(cid, width=width, minwidth=44, anchor=anchor, stretch=True)
    ov_ys = ttk.Scrollbar(ov_wrap, orient='vertical', command=ov_tree.yview)
    ov_xs = ttk.Scrollbar(ov_wrap, orient='horizontal', command=ov_tree.xview)
    ov_tree.configure(yscrollcommand=ov_ys.set, xscrollcommand=ov_xs.set)
    ov_tree.grid(row=0, column=0, sticky='nsew')
    ov_ys.grid(row=0, column=1, sticky='ns')
    ov_xs.grid(row=1, column=0, sticky='ew')
    ov_wrap.rowconfigure(0, weight=1)
    ov_wrap.columnconfigure(0, weight=1)

    tk.Label(
        tab_overview,
        text='提示：点击第一列勾选 2–5 套策略进入「策略对比」；双击任意策略行打开「策略详情」。不同资金模型与时间窗口仅作客观展示，不可直接排名。',
        bg='#f5f7fb', fg='#64748b', font=('Microsoft YaHei UI', 8), anchor='w',
    ).pack(fill='x', padx=6, pady=(0, 4))

    def _on_ov_click(event):
        iid = ov_tree.identify_row(event.y)
        col = ov_tree.identify_column(event.x)
        if not iid:
            return
        state['selected_id'] = iid
        if col == '#1':
            if iid in state['compare_ids']:
                state['compare_ids'].remove(iid)
            else:
                if len(state['compare_ids']) >= 5:
                    messagebox.showinfo('最多选择 5 套策略', '首期策略对比最多同时选择 5 套策略，请先取消已勾选策略。', parent=win)
                    return
                state['compare_ids'].add(iid)
            render_overview()

    def _on_ov_dblclick(event):
        iid = ov_tree.identify_row(event.y)
        if iid:
            state['selected_id'] = iid
            detail_sid_var.set(iid)
            nb.select(tab_detail)
            render_detail()

    ov_tree.bind('<Button-1>', _on_ov_click)
    ov_tree.bind('<Double-1>', _on_ov_dblclick)

    # ---------- TAB 2: STRATEGY DETAIL ----------
    det_top = tk.Frame(tab_detail, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    det_top.pack(fill='x', padx=6, pady=(6, 4))
    tk.Label(det_top, text='选择研究策略:', bg='white', fg='#334155', font=('Microsoft YaHei UI', 9, 'bold')).pack(side='left', padx=(10, 4), pady=6)
    detail_sid_var = tk.StringVar(value='')
    detail_cb = ttk.Combobox(det_top, textvariable=detail_sid_var, state='readonly', width=34)
    detail_cb.pack(side='left', padx=4)
    detail_cb.bind('<<ComboboxSelected>>', lambda _e: render_detail())
    det_head_var = tk.StringVar(value='')
    tk.Label(det_top, textvariable=det_head_var, bg='white', fg='#475569', font=('Microsoft YaHei UI', 8)).pack(side='left', padx=10)

    det_kpi_frame = tk.Frame(tab_detail, bg='#f5f7fb')
    det_kpi_frame.pack(fill='x', padx=6, pady=2)
    det_kpis: dict[str, tk.StringVar] = {}
    det_defs = (
        ('equity', '当前权益'), ('net_pnl', '区间净盈亏'), ('ret_pct', '净收益率'),
        ('real_unreal', '已实现 / 未实现'), ('fees', '累计手续费'), ('position', '当前持仓'),
        ('closed', '完整闭合笔数'), ('win_rate', '胜率'), ('pf', '盈利因子'),
        ('max_dd', '最大回撤'), ('avg_wl', '平均盈利 / 亏损'), ('avg_net', '平均每笔净盈亏'),
        ('max_wl', '最大单笔盈 / 亏'), ('consec', '最大连胜 / 连亏'), ('avg_hold', '平均持仓时长'),
        ('coverage', '数据覆盖 / 质量'),
    )
    for idx, (k, lbl) in enumerate(det_defs):
        r_i, c_i = divmod(idx, 8)
        card = tk.Frame(det_kpi_frame, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
        card.grid(row=r_i, column=c_i, sticky='nsew', padx=2, pady=2)
        tk.Label(card, text=lbl, bg='white', fg='#64748b', font=('Microsoft YaHei UI', 7), anchor='w').pack(fill='x', padx=6, pady=(4, 0))
        v = tk.StringVar(value='—')
        det_kpis[k] = v
        tk.Label(card, textvariable=v, bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 9, 'bold'), anchor='w').pack(fill='x', padx=6, pady=(0, 4))
        det_kpi_frame.columnconfigure(c_i, weight=1, uniform='dkpi')

    det_body = tk.PanedWindow(tab_detail, orient='horizontal', bg='#f5f7fb', sashwidth=6)
    det_body.pack(fill='both', expand=True, padx=6, pady=4)
    det_canvas = tk.Canvas(det_body, bg='#f8fafc', highlightthickness=1, highlightbackground='#dbe5f4')
    det_body.add(det_canvas, width=780)
    det_right = tk.Frame(det_body, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    det_body.add(det_right, width=540)
    tk.Label(det_right, text='策略说明、数据质量与最近闭合交易', bg='white', fg='#1e3a5f', font=('Microsoft YaHei UI', 9, 'bold'), anchor='w').pack(fill='x', padx=10, pady=(8, 4))
    det_text = tk.Text(det_right, wrap='word', bg='white', fg='#1e293b', font=('Microsoft YaHei UI', 8), relief='flat', padx=10, pady=4)
    det_text.pack(fill='both', expand=True)

    # ---------- TAB 3: MULTI-STRATEGY COMPARE ----------
    cmp_top = tk.Frame(tab_compare, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    cmp_top.pack(fill='x', padx=6, pady=(6, 4))
    common_win_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(cmp_top, text='对齐共同有效研究区间', variable=common_win_var, command=lambda: render_compare()).pack(side='left', padx=10, pady=6)
    cmp_warn_var = tk.StringVar(value='请在「策略总览」第一列勾选 2–5 套策略，或点击右侧按钮快速选择默认策略对比。')
    tk.Label(cmp_top, textvariable=cmp_warn_var, bg='white', fg='#b45309', font=('Microsoft YaHei UI', 8, 'bold')).pack(side='left', padx=10)

    def _quick_pick_compare():
        recs = (state['snapshot'] or {}).get('strategies') or []
        picks = [r['base']['strategy_id'] for r in recs if r['metrics']['closed_count'] > 0][:4]
        if len(picks) < 2:
            picks = [r['base']['strategy_id'] for r in recs][:4]
        state['compare_ids'] = set(picks)
        render_overview()
        render_compare()

    ttk.Button(cmp_top, text='快速对比有成交策略', command=_quick_pick_compare).pack(side='right', padx=10, pady=4)

    cmp_canvas = tk.Canvas(tab_compare, bg='#f8fafc', height=310, highlightthickness=1, highlightbackground='#dbe5f4')
    cmp_canvas.pack(fill='both', expand=True, padx=6, pady=4)
    cmp_cols = ('sid', 'name', 'model', 'closed', 'ret', 'wr', 'pf', 'avg', 'dd', 'fees', 'long_pnl', 'short_pnl')
    cmp_tree = ttk.Treeview(tab_compare, columns=cmp_cols, show='headings', height=7)
    for cid, t, w in (
        ('sid', '策略 ID', 145), ('name', '中文名称', 155), ('model', '资金模型', 160), ('closed', '闭合笔数', 70),
        ('ret', '收益率%', 78), ('wr', '胜率%', 68), ('pf', '盈利因子', 80), ('avg', '平均每笔', 82),
        ('dd', '最大回撤%', 78), ('fees', '手续费', 76), ('long_pnl', '做多净盈亏', 88), ('short_pnl', '做空净盈亏', 88),
    ):
        cmp_tree.heading(cid, text=t)
        cmp_tree.column(cid, width=w, anchor='e' if cid not in ('sid', 'name', 'model') else 'w')
    cmp_tree.pack(fill='x', padx=6, pady=(0, 6))

    # ---------- TAB 4: EQUITY & DRAWDOWN ----------
    eq_top = tk.Frame(tab_equity, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    eq_top.pack(fill='x', padx=6, pady=(6, 4))
    tk.Label(eq_top, text='选择策略:', bg='white', fg='#334155', font=('Microsoft YaHei UI', 9, 'bold')).pack(side='left', padx=(10, 4), pady=6)
    eq_sid_var = tk.StringVar(value='')
    eq_cb = ttk.Combobox(eq_top, textvariable=eq_sid_var, state='readonly', width=34)
    eq_cb.pack(side='left', padx=4)
    eq_cb.bind('<<ComboboxSelected>>', lambda _e: render_equity_tab())
    eq_basis_var = tk.StringVar(value='')
    tk.Label(eq_top, textvariable=eq_basis_var, bg='white', fg='#64748b', font=('Microsoft YaHei UI', 8)).pack(side='left', padx=12)

    eq_kpi_row = tk.Frame(tab_equity, bg='#f5f7fb')
    eq_kpi_row.pack(fill='x', padx=6, pady=2)
    eq_kpis = {}
    for idx, (k, lbl) in enumerate((
        ('peak', '历史峰值权益'), ('dd_amt', '最大回撤金额'), ('dd_pct', '最大回撤比例'),
        ('dd_start', '回撤开始时间'), ('dd_trough', '回撤最低点'), ('dd_rec', '回撤恢复时间'), ('in_dd', '当前回撤状态'),
    )):
        c = tk.Frame(eq_kpi_row, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
        c.grid(row=0, column=idx, sticky='nsew', padx=2)
        tk.Label(c, text=lbl, bg='white', fg='#64748b', font=('Microsoft YaHei UI', 7), anchor='w').pack(fill='x', padx=6, pady=(4, 0))
        v = tk.StringVar(value='—')
        eq_kpis[k] = v
        tk.Label(c, textvariable=v, bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 9, 'bold'), anchor='w').pack(fill='x', padx=6, pady=(0, 4))
        eq_kpi_row.columnconfigure(idx, weight=1, uniform='eqk')
    eq_canvas = tk.Canvas(tab_equity, bg='#f8fafc', highlightthickness=1, highlightbackground='#dbe5f4')
    eq_canvas.pack(fill='both', expand=True, padx=6, pady=4)

    # ---------- TAB 5: PNL CALENDAR ----------
    cal_top = tk.Frame(tab_calendar, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    cal_top.pack(fill='x', padx=6, pady=(6, 4))
    tk.Label(cal_top, text='策略/范围:', bg='white', fg='#334155', font=('Microsoft YaHei UI', 8, 'bold')).pack(side='left', padx=(10, 4), pady=6)
    cal_sid_var = tk.StringVar(value='全部策略汇总')
    cal_cb = ttk.Combobox(cal_top, textvariable=cal_sid_var, state='readonly', width=28)
    cal_cb.pack(side='left', padx=4)
    cal_cb.bind('<<ComboboxSelected>>', lambda _e: render_calendar_tab())
    ttk.Button(cal_top, text='◀ 上月', command=lambda: shift_cal_month(-1)).pack(side='left', padx=6)
    cal_month_lbl = tk.StringVar(value='')
    tk.Label(cal_top, textvariable=cal_month_lbl, bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 10, 'bold'), width=12).pack(side='left')
    ttk.Button(cal_top, text='下月 ▶', command=lambda: shift_cal_month(1)).pack(side='left', padx=6)
    tk.Label(cal_top, text='口径：北京时间（UTC+8）按实际平仓退出时间归属每日已实现净盈亏', bg='white', fg='#64748b', font=('Microsoft YaHei UI', 8)).pack(side='right', padx=10)

    cal_sum_var = tk.StringVar(value='')
    tk.Label(tab_calendar, textvariable=cal_sum_var, bg='#eff6ff', fg='#1e3a8a', font=('Microsoft YaHei UI', 9, 'bold'), anchor='w', padx=10, pady=5).pack(fill='x', padx=6, pady=(0, 4))

    cal_split = tk.PanedWindow(tab_calendar, orient='vertical', bg='#f5f7fb', sashwidth=6)
    cal_split.pack(fill='both', expand=True, padx=6, pady=(0, 6))
    cal_grid_wrap = tk.Frame(cal_split, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    cal_split.add(cal_grid_wrap, height=340)
    cal_day_wrap = tk.Frame(cal_split, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    cal_split.add(cal_day_wrap, height=200)
    cal_day_hdr = tk.StringVar(value='点击日历中任意日期查看当日交易详情')
    tk.Label(cal_day_wrap, textvariable=cal_day_hdr, bg='white', fg='#1e3a5f', font=('Microsoft YaHei UI', 9, 'bold'), anchor='w', padx=10, pady=5).pack(fill='x')
    cal_day_tree = ttk.Treeview(cal_day_wrap, columns=('sid', 'side', 'entry_t', 'exit_t', 'qty', 'entry_p', 'exit_p', 'fees', 'net', 'reason'), show='headings', height=6)
    for cid, t, w in (
        ('sid', '策略', 130), ('side', '方向', 58), ('entry_t', '开仓时间', 142), ('exit_t', '平仓时间', 142),
        ('qty', '数量', 82), ('entry_p', '开仓价', 86), ('exit_p', '平仓价', 86), ('fees', '手续费', 74), ('net', '净盈亏', 88), ('reason', '退出原因', 130),
    ):
        cal_day_tree.heading(cid, text=t)
        cal_day_tree.column(cid, width=w, anchor='e' if cid in ('qty', 'entry_p', 'exit_p', 'fees', 'net') else 'w')
    cal_day_tree.pack(fill='both', expand=True, padx=6, pady=(0, 6))

    # ---------- TAB 6: HISTORY TRADES (DUAL VIEW + PAGINATION + CSV) ----------
    hist_top = tk.Frame(tab_history, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    hist_top.pack(fill='x', padx=6, pady=(6, 4))
    hist_view_var = tk.StringVar(value='完整闭合交易记录')
    ttk.Combobox(hist_top, textvariable=hist_view_var, state='readonly', width=16, values=('完整闭合交易记录', '逐笔成交记录')).pack(side='left', padx=(8, 6), pady=6)
    hist_sid_var = tk.StringVar(value='全部策略')
    hist_sid_cb = ttk.Combobox(hist_top, textvariable=hist_sid_var, state='readonly', width=22)
    hist_sid_cb.pack(side='left', padx=4)
    hist_side_var = tk.StringVar(value='全部方向')
    ttk.Combobox(hist_top, textvariable=hist_side_var, state='readonly', width=9, values=('全部方向', 'LONG', 'SHORT')).pack(side='left', padx=4)
    hist_pnl_var = tk.StringVar(value='全部盈亏')
    ttk.Combobox(hist_top, textvariable=hist_pnl_var, state='readonly', width=9, values=('全部盈亏', '仅盈利', '仅亏损')).pack(side='left', padx=4)
    hist_reason_var = tk.StringVar(value='全部原因')
    ttk.Combobox(hist_top, textvariable=hist_reason_var, state='readonly', width=11, values=('全部原因', '止盈', '止损', '策略信号退出', '时间退出', '其他退出')).pack(side='left', padx=4)
    ttk.Button(hist_top, text='筛选', command=lambda: render_history_tab(reset_page=True)).pack(side='left', padx=4)

    def _export_history_csv():
        rows = state.get('hist_rows') or []
        if not rows:
            messagebox.showinfo('无记录', '当前筛选结果为空，无需导出。', parent=win)
            return
        dest = filedialog.asksaveasfilename(parent=win, title='导出历史交易 CSV', initialfile='research_v3_trades.csv', defaultextension='.csv', filetypes=[('CSV', '*.csv')])
        if not dest:
            return
        with open(dest, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        messagebox.showinfo('导出成功', f'已导出 {len(rows)} 条记录至：\n{dest}', parent=win)

    tk.Button(hist_top, text='导出 CSV', command=_export_history_csv, bg='#2563eb', fg='white', relief='flat', padx=10, pady=3, font=('Microsoft YaHei UI', 8, 'bold'), cursor='hand2').pack(side='right', padx=8)

    hist_wrap = tk.Frame(tab_history, bg='#f5f7fb')
    hist_wrap.pack(fill='both', expand=True, padx=6, pady=2)
    hist_cols = ('sid', 'name', 'ver', 'entry_t', 'exit_t', 'side', 'entry_p', 'exit_p', 'qty', 'dur', 'gross', 'fees', 'net', 'reason', 'ev')
    hist_tree = ttk.Treeview(hist_wrap, columns=hist_cols, show='headings', height=16)
    hist_tree.tag_configure('profit', foreground='#15803d', background='#f0fdf4')
    hist_tree.tag_configure('loss', foreground='#b91c1c', background='#fef2f2')
    for cid, t, w in (
        ('sid', '策略ID', 130), ('name', '策略名称', 140), ('ver', '开仓版本', 74), ('entry_t', '开仓/成交时间', 142),
        ('exit_t', '平仓时间', 142), ('side', '方向', 62), ('entry_p', '开仓/成交价', 88), ('exit_p', '平仓价', 88),
        ('qty', '数量', 82), ('dur', '持仓时长', 78), ('gross', '毛盈亏', 82), ('fees', '手续费', 72),
        ('net', '净盈亏', 88), ('reason', '退出/成交原因', 118), ('ev', '证据状态', 110),
    ):
        hist_tree.heading(cid, text=t)
        hist_tree.column(cid, width=w, anchor='e' if cid in ('entry_p', 'exit_p', 'qty', 'gross', 'fees', 'net') else 'w')
    hys = ttk.Scrollbar(hist_wrap, orient='vertical', command=hist_tree.yview)
    hxs = ttk.Scrollbar(hist_wrap, orient='horizontal', command=hist_tree.xview)
    hist_tree.configure(yscrollcommand=hys.set, xscrollcommand=hxs.set)
    hist_tree.grid(row=0, column=0, sticky='nsew')
    hys.grid(row=0, column=1, sticky='ns')
    hxs.grid(row=1, column=0, sticky='ew')
    hist_wrap.rowconfigure(0, weight=1)
    hist_wrap.columnconfigure(0, weight=1)

    hist_foot = tk.Frame(tab_history, bg='#f5f7fb')
    hist_foot.pack(fill='x', padx=6, pady=4)
    hist_page_var = tk.StringVar(value='')
    tk.Label(hist_foot, textvariable=hist_page_var, bg='#f5f7fb', fg='#475569', font=('Microsoft YaHei UI', 8)).pack(side='left')
    ttk.Button(hist_foot, text='下一页 ▶', command=lambda: (state.__setitem__('hist_page', state['hist_page'] + 1), render_history_page())).pack(side='right', padx=3)
    ttk.Button(hist_foot, text='◀ 上一页', command=lambda: (state.__setitem__('hist_page', max(0, state['hist_page'] - 1)), render_history_page())).pack(side='right', padx=3)

    # ---------- TAB 7: ATTRIBUTION ANALYSIS ----------
    attr_top = tk.Frame(tab_attrib, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    attr_top.pack(fill='x', padx=6, pady=(6, 4))
    tk.Label(attr_top, text='归因对象:', bg='white', fg='#334155', font=('Microsoft YaHei UI', 9, 'bold')).pack(side='left', padx=(10, 4), pady=6)
    attr_sid_var = tk.StringVar(value='全部策略汇总')
    attr_cb = ttk.Combobox(attr_top, textvariable=attr_sid_var, state='readonly', width=28)
    attr_cb.pack(side='left', padx=4)
    attr_cb.bind('<<ComboboxSelected>>', lambda _e: render_attribution_tab())
    tk.Label(attr_top, text='注：归因分析仅描述历史统计关联，不代表因果关系；滑点已含于成交价中，净盈亏不重复扣减。', bg='white', fg='#64748b', font=('Microsoft YaHei UI', 8)).pack(side='right', padx=10)

    attr_nb = ttk.Notebook(tab_attrib)
    attr_nb.pack(fill='both', expand=True, padx=6, pady=4)
    attr_trees = {}
    for key, title in (
        ('direction', '多空方向分析'),
        ('exit_reason', '退出原因分析'),
        ('entry_time', '开仓时段与星期（北京时间）'),
        ('holding_duration', '持仓时长分析'),
        ('cost', '交易成本分析'),
    ):
        f = tk.Frame(attr_nb, bg='white')
        attr_nb.add(f, text=title)
        if key == 'cost':
            txt = tk.Text(f, wrap='word', bg='white', fg='#0f172a', font=('Microsoft YaHei UI', 10), relief='flat', padx=16, pady=14)
            txt.pack(fill='both', expand=True)
            attr_trees[key] = txt
        else:
            tv = ttk.Treeview(f, columns=('bucket', 'count', 'wins', 'losses', 'wr', 'net', 'avg', 'pf'), show='headings', height=12)
            for cid, lbl, w in (
                ('bucket', '维度分组', 180), ('count', '闭合笔数', 90), ('wins', '盈利笔数', 90), ('losses', '亏损笔数', 90),
                ('wr', '胜率%', 95), ('net', '净盈亏 (USD)', 125), ('avg', '平均每笔净盈亏', 125), ('pf', '盈利因子', 110),
            ):
                tv.heading(cid, text=lbl)
                tv.column(cid, width=w, anchor='w' if cid == 'bucket' else 'e')
            tv.pack(fill='both', expand=True, padx=8, pady=8)
            attr_trees[key] = tv

    # ---------- TAB 8: VERSION & BACKTEST COMPARISON ----------
    vb_top = tk.Frame(tab_ver_bt, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    vb_top.pack(fill='x', padx=6, pady=(6, 4))
    tk.Label(vb_top, text='选择策略:', bg='white', fg='#334155', font=('Microsoft YaHei UI', 9, 'bold')).pack(side='left', padx=(10, 4), pady=6)
    vb_sid_var = tk.StringVar(value='')
    vb_cb = ttk.Combobox(vb_top, textvariable=vb_sid_var, state='readonly', width=34)
    vb_cb.pack(side='left', padx=4)
    vb_cb.bind('<<ComboboxSelected>>', lambda _e: render_ver_bt_tab())
    tk.Label(vb_top, text='严格隔离：纸盘自然运行数据与已保存历史回测结果分开展示，绝不混合计算盈利。', bg='#fef3c7', fg='#92400e', font=('Microsoft YaHei UI', 8, 'bold'), padx=8, pady=3).pack(side='right', padx=10)

    tk.Label(tab_ver_bt, text='一、历史版本绩效分解（按开仓版本归属，不重归属至最新版）', bg='#f5f7fb', fg='#1e3a5f', font=('Microsoft YaHei UI', 9, 'bold'), anchor='w').pack(fill='x', padx=8, pady=(4, 2))
    ver_tree = ttk.Treeview(tab_ver_bt, columns=('ver', 'enabled', 'start', 'end', 'closed', 'net', 'wr', 'pf'), show='headings', height=6)
    for cid, t, w in (('ver', '策略版本', 120), ('enabled', '启用/上传时间', 155), ('start', '首笔退出时间', 155), ('end', '末笔退出时间', 155), ('closed', '闭合笔数', 90), ('net', '净盈亏', 110), ('wr', '胜率%', 90), ('pf', '盈利因子', 100)):
        ver_tree.heading(cid, text=t)
        ver_tree.column(cid, width=w, anchor='w' if cid in ('ver', 'enabled', 'start', 'end') else 'e')
    ver_tree.pack(fill='x', padx=6, pady=(0, 8))

    tk.Label(tab_ver_bt, text='二、已保存历史回测结果（独立回测账本 strategy-pool-backtests.sqlite3）与纸盘自然运行对照', bg='#f5f7fb', fg='#1e3a5f', font=('Microsoft YaHei UI', 9, 'bold'), anchor='w').pack(fill='x', padx=8, pady=(2, 2))
    bt_tree = ttk.Treeview(tab_ver_bt, columns=('src', 'sid', 'ver', 'range', 'bars', 'closed', 'net', 'ret', 'wr', 'pf', 'dd', 'fees'), show='headings', height=10)
    for cid, t, w in (
        ('src', '数据来源', 120), ('sid', '策略', 150), ('ver', '版本', 70), ('range', '覆盖区间（北京）', 220),
        ('bars', 'K线数', 68), ('closed', '闭合笔数', 70), ('net', '净盈亏', 92), ('ret', '收益率%', 78),
        ('wr', '胜率%', 68), ('pf', '盈利因子', 84), ('dd', '最大回撤%', 78), ('fees', '手续费', 80),
    ):
        bt_tree.heading(cid, text=t)
        bt_tree.column(cid, width=w, anchor='w' if cid in ('src', 'sid', 'ver', 'range') else 'e')
    bt_tree.pack(fill='both', expand=True, padx=6, pady=(0, 6))

    # ==================== ZONE 5: BOTTOM STATUS & EXPORT BAR ====================
    foot = tk.Frame(win, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    foot.pack(fill='x', padx=10, pady=(0, 8))
    status_var = tk.StringVar(value=f'计算引擎：{CALCULATION_VERSION} · 只读研究模式 · 不修改任何交易账本')
    tk.Label(foot, textvariable=status_var, bg='white', fg='#475569', font=('Microsoft YaHei UI', 8), anchor='w').pack(side='left', padx=10, pady=6)

    def _do_export(mode: str):
        snap = state.get('snapshot')
        if not snap:
            return
        target = filedialog.askdirectory(parent=win, title='选择研究报告导出目录（请勿选在项目目录内部）')
        if not target:
            return
        s_dt, e_dt = _get_time_bounds()
        try:
            if mode == 'single':
                sid = state.get('selected_id') or detail_sid_var.get()
                rec = state['by_id'].get(sid)
                if not rec:
                    raise RuntimeError('请先选择一条策略')
                paths = export_research_report(output_dir=target, records=[rec], mode=f'single_{sid}', start_dt=s_dt, end_dt=e_dt, snapshot_time=snap['snapshot_time'])
            else:
                recs = [state['by_id'][k] for k in state['compare_ids'] if k in state['by_id']] if (mode == 'compare' and len(state['compare_ids']) >= 2) else snap['strategies']
                cmp_res = compute_multi_compare(recs, common_window=common_win_var.get(), start_dt=s_dt, end_dt=e_dt) if mode == 'compare' else None
                paths = export_research_report(output_dir=target, records=recs, mode=mode, start_dt=s_dt, end_dt=e_dt, snapshot_time=snap['snapshot_time'], compare_result=cmp_res)
            messagebox.showinfo('研究报告导出成功', '已生成 CSV 与 Markdown 研究报告：\n\n' + '\n'.join(str(p) for p in paths), parent=win)
        except Exception as exc:
            messagebox.showerror('导出失败', str(exc), parent=win)

    tk.Button(foot, text='导出总览/对比报告 (CSV+MD)', command=lambda: _do_export('compare' if len(state['compare_ids']) >= 2 else 'overview'), bg='#2563eb', fg='white', relief='flat', padx=10, pady=4, font=('Microsoft YaHei UI', 8, 'bold'), cursor='hand2').pack(side='right', padx=(4, 10), pady=4)
    ttk.Button(foot, text='导出单策略报告 (CSV+MD)', command=lambda: _do_export('single')).pack(side='right', padx=4, pady=4)

    # ==================== RENDER FUNCTIONS ====================
    def _filtered_records() -> list[dict]:
        snap = state.get('snapshot')
        if not snap:
            return []
        grp = group_var.get()
        st_f = status_f_var.get()
        q = search_var.get().strip().casefold()
        out = []
        for r in snap['strategies']:
            b = r['base']
            if grp != '全部' and b['strategy_group'] != grp:
                continue
            if st_f != '全部状态' and str(b['state']).upper() != st_f.upper():
                continue
            if q and q not in (b['strategy_id'] + ' ' + b['strategy_name']).casefold():
                continue
            out.append(r)
        return out

    def render_overview():
        recs = _filtered_records()
        s_dt, e_dt = _get_time_bounds()
        computed = []
        traded = profit = loss = flat = insuff = 0
        for r in recs:
            init_c = dec(r['account']['initial_capital']) or Decimal('10000')
            cur_eq = dec(r['account']['current_equity'])
            m = compute_metrics(r['closed_trades'], initial_capital=init_c, current_equity=cur_eq, start_dt=s_dt, end_dt=e_dt, all_closed_trades=r['closed_trades'], equity_points=r['equity_series'])
            pnl_d = dec(m['interval_net_pnl']) or Decimal(0)
            if m['closed_count'] > 0:
                traded += 1
            if pnl_d > 0:
                profit += 1
            elif pnl_d < 0:
                loss += 1
            else:
                flat += 1
            if m['closed_count'] < 30 or r['data_quality']['missing_data']:
                insuff += 1
            computed.append((r, m))

        sum_vars['total'].set(str(len(recs)))
        sum_vars['traded'].set(str(traded))
        sum_vars['profit'].set(str(profit))
        sum_vars['loss'].set(str(loss))
        sum_vars['flat'].set(str(flat))
        sum_vars['insufficient'].set(str(insuff))

        # Sort
        sc = state['sort_col']
        def _sk(pair):
            r, m = pair
            if sc == 'strategy_id': return r['base']['strategy_id']
            if sc == 'strategy_name': return r['base']['strategy_name']
            if sc == 'closed': return m['closed_count']
            if sc == 'net_pnl': return float(dec(m['interval_net_pnl']) or Decimal(0))
            if sc == 'ret_pct': return float(m['net_return_pct']) if m['net_return_pct'] is not None else -1e9
            if sc == 'win_rate': return float(m['win_rate']) if m['win_rate'] is not None else -1.0
            if sc == 'max_dd': return float(m['max_drawdown_pct']) if m['max_drawdown_pct'] is not None else -1.0
            if sc == 'equity': return float(dec(r['account']['current_equity']) or Decimal(0))
            return r['base']['strategy_id']

        computed.sort(key=_sk, reverse=state['sort_desc'])
        for iid in ov_tree.get_children():
            ov_tree.delete(iid)
        for r, m in computed:
            b, a, q = r['base'], r['account'], r['data_quality']
            sid = b['strategy_id']
            pnl_d = dec(m['interval_net_pnl']) or Decimal(0)
            tag = 'warn' if q['missing_data'] else ('profit' if pnl_d > 0 else 'loss' if pnl_d < 0 else '')
            vals = (
                '☑' if sid in state['compare_ids'] else '☐',
                sid, b['strategy_name'], b['strategy_version'], b['state'],
                str(b['running_days']) if b['running_days'] is not None else '—',
                _fmt_num(a['initial_capital']), _fmt_num(a['current_equity']),
                _fmt_num(m['interval_net_pnl'], signed=True), _fmt_pct(m['net_return_pct'], signed=True),
                str(m['closed_count']), _fmt_pct(m['win_rate']), m['profit_factor_label'],
                _fmt_pct(m['max_drawdown_pct']), _fmt_num(m['fees']), _fmt_num(m['avg_net_pnl'], signed=True),
                q['completeness'],
            )
            ov_tree.insert('', 'end', iid=sid, values=vals, tags=(tag,) if tag else ())

    def render_detail():
        sid = detail_sid_var.get() or state.get('selected_id')
        r = state['by_id'].get(sid)
        if not r:
            return
        s_dt, e_dt = _get_time_bounds()
        b, a, q = r['base'], r['account'], r['data_quality']
        init_c = dec(a['initial_capital']) or Decimal('10000')
        m = compute_metrics(r['closed_trades'], initial_capital=init_c, current_equity=dec(a['current_equity']), start_dt=s_dt, end_dt=e_dt, all_closed_trades=r['closed_trades'], equity_points=r['equity_series'])
        det_head_var.set(f"{b['strategy_name']}  |  分组：{b['strategy_group']}  |  版本：{b['strategy_version']}  |  状态：{b['state']}  |  资金模型：{a['capital_model']}")
        det_kpis['equity'].set(_fmt_num(a['current_equity']))
        det_kpis['net_pnl'].set(_fmt_num(m['interval_net_pnl'], signed=True))
        det_kpis['ret_pct'].set(_fmt_pct(m['net_return_pct'], signed=True))
        det_kpis['real_unreal'].set(f"{_fmt_num(a['realized_pnl'], signed=True)} / {_fmt_num(a['unrealized_pnl'], signed=True)}")
        det_kpis['fees'].set(_fmt_num(m['fees']))
        pos = a.get('current_position')
        det_kpis['position'].set('持仓中' if pos else '空仓')
        det_kpis['closed'].set(f"{m['closed_count']} 笔（{m['win_count']}胜/{m['loss_count']}负）")
        det_kpis['win_rate'].set(_fmt_pct(m['win_rate']))
        det_kpis['pf'].set(m['profit_factor_label'])
        det_kpis['max_dd'].set(_fmt_pct(m['max_drawdown_pct']))
        det_kpis['avg_wl'].set(f"{_fmt_num(m['avg_win'], signed=True)} / {_fmt_num(m['avg_loss'], signed=True)}")
        det_kpis['avg_net'].set(_fmt_num(m['avg_net_pnl'], signed=True))
        det_kpis['max_wl'].set(f"{_fmt_num(m['max_single_win'], signed=True)} / {_fmt_num(m['max_single_loss'], signed=True)}")
        det_kpis['consec'].set(f"{m['max_consec_wins']} 胜 / {m['max_consec_losses']} 负")
        det_kpis['avg_hold'].set(_fmt_dur(m['avg_holding_sec']))
        det_kpis['coverage'].set(q['completeness'])

        _draw_single_equity_dd(det_canvas, r['equity_series'], float(init_c), title=f"{b['strategy_name']} 权益与回撤曲线")
        det_text.configure(state='normal')
        det_text.delete('1.0', 'end')
        lines = [
            f"策略 ID：{b['strategy_id']}   |   中文名称：{b['strategy_name']}",
            f"策略说明：{b.get('description') or '—'}",
            f"数据来源：{q['data_source']}",
            f"覆盖时间：{fmt_bj(q['coverage_start'])} ~ {fmt_bj(q['coverage_end'])}",
            f"数据质量：{q['completeness']}" + ('' if not q['missing_data'] else '\n  待核查项：' + '；'.join(q['missing_data'][:5])),
            f"在途未平仓：{a['active_open_count']} 笔（未计入已平仓胜率）",
            '',
            f"最近闭合交易（共 {len(r['closed_trades'])} 笔，显示最新 15 笔）：",
        ]
        if not r['closed_trades']:
            lines.append('  暂无完整闭合交易记录。')
        else:
            for t in reversed(r['closed_trades'][-15:]):
                lines.append(f"  • {fmt_bj(t['exit_time'], 16)} | {t['side']:5s} | 净盈亏 {_fmt_num(t['net_pnl'], signed=True)} | 费用 {_fmt_num(t['fees'])} | {t['exit_reason']}")
        det_text.insert('1.0', '\n'.join(lines))
        det_text.configure(state='disabled')

    def render_compare():
        recs = [state['by_id'][k] for k in sorted(state['compare_ids']) if k in state['by_id']]
        for iid in cmp_tree.get_children():
            cmp_tree.delete(iid)
        if len(recs) < 2:
            cmp_warn_var.set('请至少勾选 2 套策略（最多 5 套）以进行横向对比，或点击「快速对比有成交策略」。')
            cmp_canvas.delete('all')
            cmp_canvas.create_text(420, 140, text='请在「策略总览」勾选 2–5 套策略进行对比', fill='#64748b', font=('Microsoft YaHei UI', 11))
            return
        s_dt, e_dt = _get_time_bounds()
        res = compute_multi_compare(recs, common_window=common_win_var.get(), start_dt=s_dt, end_dt=e_dt)
        if res['warning']:
            cmp_warn_var.set(res['warning'])
        else:
            cmp_warn_var.set(f"共同区间：{fmt_bj(res['common_start'], 16) if res['common_start'] else '全历史'} ~ {fmt_bj(res['common_end'], 16) if res['common_end'] else '最新'} · 同资金模型对比")
        _draw_multi_compare_charts(cmp_canvas, res)
        for it in res['strategies']:
            m = it['metrics']
            d = it['direction']
            cmp_tree.insert('', 'end', values=(
                it['strategy_id'], it['strategy_name'], it['capital_model'], m['closed_count'],
                _fmt_pct(m['net_return_pct'], signed=True), _fmt_pct(m['win_rate']), m['profit_factor_label'],
                _fmt_num(m['avg_net_pnl'], signed=True), _fmt_pct(m['max_drawdown_pct']), _fmt_num(m['fees']),
                _fmt_num(d['LONG']['net_pnl'], signed=True), _fmt_num(d['SHORT']['net_pnl'], signed=True),
            ))

    def render_equity_tab():
        sid = eq_sid_var.get() or state.get('selected_id')
        r = state['by_id'].get(sid)
        if not r:
            return
        dd = r['drawdown_summary']
        init_c = float(dec(r['account']['initial_capital']) or Decimal('10000'))
        eq_basis_var.set(f"口径：{dd.get('basis', '—')}  |  有效观测点：{dd.get('points', 0)}")
        eq_kpis['peak'].set(_fmt_num(dd.get('peak_equity')))
        eq_kpis['dd_amt'].set(_fmt_num(dd.get('max_drawdown_amount')))
        eq_kpis['dd_pct'].set(_fmt_pct(dd.get('max_drawdown_pct')))
        eq_kpis['dd_start'].set(fmt_bj(dd.get('dd_start'), 16))
        eq_kpis['dd_trough'].set(fmt_bj(dd.get('dd_trough'), 16))
        eq_kpis['dd_rec'].set(fmt_bj(dd.get('dd_recovery'), 16) if dd.get('dd_recovery') else '尚未恢复')
        eq_kpis['in_dd'].set('回撤中' if dd.get('in_drawdown') else '已创峰值/无回撤')
        _draw_single_equity_dd(eq_canvas, r['equity_series'], init_c, title=f"{r['base']['strategy_name']}（{sid}）权益与回撤分析")

    def shift_cal_month(delta: int):
        m = state['cal_month'] + delta
        y = state['cal_year']
        if m < 1: m, y = 12, y - 1
        elif m > 12: m, y = 1, y + 1
        state['cal_year'], state['cal_month'] = y, m
        render_calendar_tab()

    def render_calendar_tab():
        y, mo = state['cal_year'], state['cal_month']
        cal_month_lbl.set(f'{y} 年 {mo:02d} 月')
        target = cal_sid_var.get()
        recs = list(state['by_id'].values()) if target == '全部策略汇总' else ([state['by_id'][target]] if target in state['by_id'] else [])
        by_day: dict[str, list[dict]] = {}
        for r in recs:
            for t in r['closed_trades']:
                et = parse_utc(t.get('exit_time'))
                if et is None:
                    continue
                bj = et.astimezone(BEIJING)
                if bj.year == y and bj.month == mo:
                    by_day.setdefault(bj.date().isoformat(), []).append(t)

        # Monthly summary
        day_nets = {d: sum((dec(t['net_pnl']) or Decimal(0) for t in ts), Decimal(0)) for d, ts in by_day.items()}
        month_net = sum(day_nets.values(), Decimal(0))
        win_days = sum(1 for v in day_nets.values() if v > 0)
        loss_days = sum(1 for v in day_nets.values() if v < 0)
        flat_days = sum(1 for v in day_nets.values() if v == 0)
        days_in_m = _calendar.monthrange(y, mo)[1]
        max_win_d = max((v for v in day_nets.values() if v > 0), default=None)
        max_loss_d = min((v for v in day_nets.values() if v < 0), default=None)
        cal_sum_var.set(
            f"月累计净盈亏：{_fmt_num(month_net, signed=True)} USD  |  盈利日：{win_days}  |  亏损日：{loss_days}  |  持平日：{flat_days}  |  无交易日：{days_in_m - len(day_nets)}  |  "
            f"最大单日盈利：{_fmt_num(max_win_d, signed=True)}  |  最大单日亏损：{_fmt_num(max_loss_d, signed=True)}"
        )

        for w in cal_grid_wrap.winfo_children():
            w.destroy()
        for ci, wd in enumerate(('周一', '周二', '周三', '周四', '周五', '周六', '周日')):
            tk.Label(cal_grid_wrap, text=wd, bg='#f1f5f9', fg='#334155', font=('Microsoft YaHei UI', 8, 'bold'), pady=4).grid(row=0, column=ci, sticky='ew')
            cal_grid_wrap.columnconfigure(ci, weight=1, uniform='cal')

        def _show_day(d_key: str):
            state['cal_selected_day'] = d_key
            ts = by_day.get(d_key, [])
            for iid in cal_day_tree.get_children():
                cal_day_tree.delete(iid)
            d_net = sum((dec(t['net_pnl']) or Decimal(0) for t in ts), Decimal(0))
            d_fee = sum((dec(t['fees']) or Decimal(0) for t in ts), Decimal(0))
            d_w = sum(1 for t in ts if (dec(t['net_pnl']) or Decimal(0)) > 0)
            d_l = sum(1 for t in ts if (dec(t['net_pnl']) or Decimal(0)) < 0)
            cal_day_hdr.set(f"{d_key} 单日交易详情  |  闭合 {len(ts)} 笔（{d_w}胜/{d_l}负）  |  当日净盈亏：{_fmt_num(d_net, signed=True)} USD  |  手续费：{_fmt_num(d_fee)}")
            for t in ts:
                cal_day_tree.insert('', 'end', values=(
                    t['strategy_id'], t['side'], fmt_bj(t['entry_time'], 16), fmt_bj(t['exit_time'], 16),
                    t['quantity'], _fmt_num(t['entry_price']), _fmt_num(t['exit_price']),
                    _fmt_num(t['fees']), _fmt_num(t['net_pnl'], signed=True), t['exit_reason'],
                ))

        first_wd, _ = _calendar.monthrange(y, mo)
        r_i, c_i = 1, first_wd
        today_d = datetime.now(BEIJING).date()
        for day_num in range(1, days_in_m + 1):
            d_obj = date(y, mo, day_num)
            d_key = d_obj.isoformat()
            ts = by_day.get(d_key, [])
            net = day_nets.get(d_key)
            if d_obj > today_d:
                bg, fg, sub = '#f8fafc', '#94a3b8', '未来'
            elif not ts:
                bg, fg, sub = '#ffffff', '#64748b', '无交易'
            elif net > 0:
                bg, fg, sub = '#dcfce7', '#15803d', f'{net:+.2f}\n({len(ts)}笔)'
            elif net < 0:
                bg, fg, sub = '#fee2e2', '#b91c1c', f'{net:+.2f}\n({len(ts)}笔)'
            else:
                bg, fg, sub = '#f1f5f9', '#334155', f'0.00\n({len(ts)}笔)'
            cell = tk.Frame(cal_grid_wrap, bg=bg, highlightbackground='#cbd5e1', highlightthickness=1, cursor='hand2')
            cell.grid(row=r_i, column=c_i, sticky='nsew', padx=1, pady=1)
            cal_grid_wrap.rowconfigure(r_i, weight=1)
            l1 = tk.Label(cell, text=str(day_num), bg=bg, fg='#0f172a', font=('Microsoft YaHei UI', 8, 'bold'), anchor='nw')
            l1.pack(fill='x', padx=4, pady=(2, 0))
            l2 = tk.Label(cell, text=sub, bg=bg, fg=fg, font=('Microsoft YaHei UI', 8), justify='center')
            l2.pack(expand=True)
            for w_c in (cell, l1, l2):
                w_c.bind('<Button-1>', lambda _e, dk=d_key: _show_day(dk))
            c_i += 1
            if c_i >= 7:
                c_i = 0
                r_i += 1

        if by_day:
            pick_day = state['cal_selected_day'] if state['cal_selected_day'] in by_day else sorted(by_day.keys())[-1]
            _show_day(pick_day)

    def render_history_tab(reset_page: bool = False):
        if reset_page:
            state['hist_page'] = 0
        s_dt, e_dt = _get_time_bounds()
        view_mode = hist_view_var.get()
        sid_f = hist_sid_var.get()
        side_f = hist_side_var.get()
        pnl_f = hist_pnl_var.get()
        reason_f = hist_reason_var.get()
        recs = list(state['by_id'].values()) if sid_f == '全部策略' else ([state['by_id'][sid_f]] if sid_f in state['by_id'] else [])
        rows = []
        if view_mode == '完整闭合交易记录':
            for r in recs:
                sname = r['base']['strategy_name']
                for t in r['closed_trades']:
                    et = parse_utc(t['exit_time'])
                    if s_dt and et and et < s_dt: continue
                    if e_dt and et and et > e_dt: continue
                    if side_f != '全部方向' and t['side'] != side_f: continue
                    nd = dec(t['net_pnl']) or Decimal(0)
                    if pnl_f == '仅盈利' and nd <= 0: continue
                    if pnl_f == '仅亏损' and nd >= 0: continue
                    if reason_f != '全部原因' and t.get('exit_reason_category') != reason_f: continue
                    rows.append({
                        'sid': t['strategy_id'], 'name': sname, 'ver': t['opening_strategy_version'],
                        'entry_t': fmt_bj(t['entry_time'], 16), 'exit_t': fmt_bj(t['exit_time'], 16),
                        'side': t['side'], 'entry_p': _fmt_num(t['entry_price']), 'exit_p': _fmt_num(t['exit_price']),
                        'qty': t['quantity'], 'dur': _fmt_dur(t.get('holding_duration_sec')),
                        'gross': _fmt_num(t['gross_pnl'], signed=True), 'fees': _fmt_num(t['fees']),
                        'net': _fmt_num(t['net_pnl'], signed=True), 'net_num': float(nd),
                        'reason': t['exit_reason'], 'ev': t['close_evidence_status'],
                    })
            rows.sort(key=lambda x: x['exit_t'], reverse=True)
        else:
            for r in recs:
                sid = r['base']['strategy_id']
                sname = r['base']['strategy_name']
                for f in r['fills']:
                    ts = f.get('occurred_at') or f.get('created_at') or f.get('cycle') or ''
                    dt = parse_utc(ts)
                    if s_dt and dt and dt < s_dt: continue
                    if e_dt and dt and dt > e_dt: continue
                    side = str(f.get('position_direction') or f.get('direction') or f.get('side') or '—').upper()
                    if side_f != '全部方向' and side != side_f: continue
                    rows.append({
                        'sid': sid, 'name': sname, 'ver': str(f.get('strategy_version') or f.get('version') or '—'),
                        'entry_t': fmt_bj(ts, 16), 'exit_t': '—', 'side': side,
                        'entry_p': _fmt_num(f.get('price')), 'exit_p': '—',
                        'qty': str(f.get('quantity') or f.get('qty') or '—'), 'dur': '—',
                        'gross': '—', 'fees': _fmt_num(f.get('fee')), 'net': _fmt_num(f.get('pnl') or f.get('net_pnl'), signed=True),
                        'net_num': float(dec(f.get('pnl') or f.get('net_pnl')) or Decimal(0)),
                        'reason': str(f.get('reason') or f.get('position_effect') or f.get('effect') or '—'),
                        'ev': 'RAW_FILL',
                    })
            rows.sort(key=lambda x: x['entry_t'], reverse=True)
        state['hist_rows'] = rows
        render_history_page()

    def render_history_page():
        rows = state.get('hist_rows') or []
        ps = 100
        total_p = max(1, (len(rows) + ps - 1) // ps)
        p = max(0, min(state['hist_page'], total_p - 1))
        state['hist_page'] = p
        for iid in hist_tree.get_children():
            hist_tree.delete(iid)
        chunk = rows[p * ps:(p + 1) * ps]
        for r in chunk:
            tag = 'profit' if r['net_num'] > 0 else 'loss' if r['net_num'] < 0 else ''
            hist_tree.insert('', 'end', values=tuple(r[c] for c in hist_cols), tags=(tag,) if tag else ())
        hist_page_var.set(f'共 {len(rows)} 条记录 · 第 {p + 1}/{total_p} 页（每页 100 条）')

    def render_attribution_tab():
        target = attr_sid_var.get()
        recs = list(state['by_id'].values()) if target == '全部策略汇总' else ([state['by_id'][target]] if target in state['by_id'] else [])
        s_dt, e_dt = _get_time_bounds()
        trades = []
        for r in recs:
            for t in r['closed_trades']:
                et = parse_utc(t['exit_time'])
                if s_dt and et and et < s_dt: continue
                if e_dt and et and et > e_dt: continue
                trades.append(t)
        attr = compute_attribution(trades)

        def _fill_tv(tv, data_map):
            for iid in tv.get_children():
                tv.delete(iid)
            for k, st in data_map.items():
                tv.insert('', 'end', values=(
                    k, st['count'], st['wins'], st['losses'], _fmt_pct(st['win_rate']),
                    _fmt_num(st['net_pnl'], signed=True), _fmt_num(st['avg_pnl'], signed=True), st['profit_factor'],
                ))

        _fill_tv(attr_trees['direction'], {'做多 (LONG)': attr['direction']['LONG'], '做空 (SHORT)': attr['direction']['SHORT']})
        _fill_tv(attr_trees['exit_reason'], attr['exit_reason'])
        time_merged = {**{f'时段 {k}': v for k, v in attr['entry_hour_bj'].items()}, **{f'星期 {k}': v for k, v in attr['entry_weekday_bj'].items()}}
        _fill_tv(attr_trees['entry_time'], time_merged)
        _fill_tv(attr_trees['holding_duration'], attr['holding_duration'])

        cost_txt = attr_trees['cost']
        c = attr['cost']
        cost_txt.configure(state='normal')
        cost_txt.delete('1.0', 'end')
        cost_txt.insert('1.0', (
            f"归因范围：{target}（共 {len(trades)} 笔完整闭合交易）\n\n"
            f"• 毛交易收益（成交价差）：{_fmt_num(c['gross_pnl'], signed=True)} USD\n"
            f"• 累计手续费成本：{_fmt_num(c['total_fees'])} USD\n"
            f"• 记录滑点参考额：{_fmt_num(c['recorded_slippage'])} USD\n"
            f"• 净交易收益（扣手续费后）：{_fmt_num(c['net_pnl'], signed=True)} USD\n\n"
            f"成本口径说明：{c['slippage_note']}\n\n"
            f"严谨性提示：{attr['disclaimer']}"
        ))
        cost_txt.configure(state='disabled')

    def render_ver_bt_tab():
        sid = vb_sid_var.get() or state.get('selected_id')
        r = state['by_id'].get(sid)
        for iid in ver_tree.get_children():
            ver_tree.delete(iid)
        for iid in bt_tree.get_children():
            bt_tree.delete(iid)
        if r:
            vrows = compute_version_breakdown(r['closed_trades'], r['base'].get('versions_history'))
            for v in vrows:
                ver_tree.insert('', 'end', values=(
                    v['version'], fmt_bj(v['enabled_at'], 16), fmt_bj(v['trade_start'], 16), fmt_bj(v['trade_end'], 16),
                    v['closed_count'], _fmt_num(v['net_pnl'], signed=True), _fmt_pct(v['win_rate']), v['profit_factor'],
                ))
            # Paper Live row for reference (strictly labeled PAPER_LIVE)
            m = r['metrics']
            bt_tree.insert('', 'end', values=(
                '纸盘自然运行', f"{r['base']['strategy_name']} ({sid})", r['base']['strategy_version'],
                f"{fmt_bj(r['data_quality']['coverage_start'], 10)} ~ {fmt_bj(r['data_quality']['coverage_end'], 10)}",
                '自然周期', m['closed_count'], _fmt_num(m['interval_net_pnl'], signed=True),
                _fmt_pct(m['net_return_pct'], signed=True), _fmt_pct(m['win_rate']), m['profit_factor_label'],
                _fmt_pct(m['max_drawdown_pct']), _fmt_num(m['fees']),
            ))
        # Saved backtests from strategy-pool-backtests.sqlite3 (strictly isolated!)
        bts = (state.get('snapshot') or {}).get('backtest_reports') or []
        for b in bts:
            bt_tree.insert('', 'end', values=(
                '历史回测(独立账本)', f"{b['strategy_name']} ({b['strategy_id']})", b['version'],
                f"{fmt_bj(b['start'], 10)} ~ {fmt_bj(b['end'], 10)}",
                b['bar_count'], b['closed_trade_count'], _fmt_num(b['net_pnl'], signed=True),
                _fmt_pct(b['net_return_pct'], signed=True), _fmt_pct(b['win_rate']), b['profit_factor_label'],
                _fmt_pct(b['max_drawdown_pct']), _fmt_num(b['fees']),
            ))

    def render_all():
        if not state.get('snapshot'):
            return
        render_overview()
        idx = nb.index(nb.select())
        if idx == 1: render_detail()
        elif idx == 2: render_compare()
        elif idx == 3: render_equity_tab()
        elif idx == 4: render_calendar_tab()
        elif idx == 5: render_history_tab()
        elif idx == 6: render_attribution_tab()
        elif idx == 7: render_ver_bt_tab()

    nb.bind('<<NotebookTabChanged>>', lambda _e: render_all())

    def apply_snapshot(snap: dict):
        if not win.winfo_exists():
            return
        state['loading'] = False
        state['snapshot'] = snap
        state['by_id'] = {r['base']['strategy_id']: r for r in snap['strategies']}
        sids = list(state['by_id'].keys())
        detail_cb.configure(values=sids)
        eq_cb.configure(values=sids)
        vb_cb.configure(values=sids)
        cal_cb.configure(values=['全部策略汇总'] + sids)
        hist_sid_cb.configure(values=['全部策略'] + sids)
        attr_cb.configure(values=['全部策略汇总'] + sids)

        if sids and (not state['selected_id'] or state['selected_id'] not in state['by_id']):
            state['selected_id'] = sids[0]
            detail_sid_var.set(sids[0])
            eq_sid_var.set(sids[0])
            vb_sid_var.set(sids[0])

        err_txt = f"  |  部分接口告警({len(snap['errors'])}): " + '; '.join(snap['errors'][:2]) if snap['errors'] else '  |  全部只读源正常'
        cache_txt = '（缓存命中）' if snap.get('from_cache') else f"（加载耗时 {snap.get('load_ms', 0)}ms）"
        meta_var.set(f"快照：{fmt_bj(snap['snapshot_time'])} 北京时间 {cache_txt}  |  策略 {len(sids)} 套  |  已保存回测 {len(snap.get('backtest_reports') or [])} 份{err_txt}")
        status_var.set(f"{snap['atomicity_note']}  |  引擎：{CALCULATION_VERSION}")
        render_all()

    def refresh(force: bool = False):
        if state['loading']:
            return
        state['loading'] = True
        meta_var.set('正在并发读取只读研究数据（不阻塞界面）…')

        def worker():
            try:
                snap = eng.load_snapshot(force=force)
                if win.winfo_exists():
                    win.after(0, lambda: apply_snapshot(snap))
            except Exception as exc:
                msg = str(exc)
                def _on_err():
                    state['loading'] = False
                    meta_var.set(f'读取异常：{msg}（可点击右上角「刷新数据」重试）')
                if win.winfo_exists():
                    win.after(0, _on_err)

        threading.Thread(target=worker, daemon=True).start()

    # Expose helpers for automated verification & screenshot capture
    win._research_v3_state = state
    win._research_v3_nb = nb
    win._research_v3_vars = {
        'detail_sid': detail_sid_var, 'eq_sid': eq_sid_var, 'cal_sid': cal_sid_var,
        'hist_sid': hist_sid_var, 'attr_sid': attr_sid_var, 'vb_sid': vb_sid_var,
        'attr_nb': attr_nb, 'quick_compare': _quick_pick_compare,
        'render_all': render_all, 'refresh': refresh,
    }

    refresh(force=False)
    return win
