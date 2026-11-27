"""Unified Strategy Pool UI for BTC Paper Lab V3.1.

Covers:
- BUG-001: Separate Strategy ID, Chinese Name, and Version columns.
- BUG-002: Right-click context menu for target-bound strategy updates with ID/diff/position protection.
- BUG-003: Unified detail & account panel launch for any registered strategy (including Chan & future uploads).
- BUG-004: Calendar date picker backtest config, 3-month validation, non-blocking progress/cancel, visual report & charts.
- BUG-005: Reusable visual Trade Fills Table with sorting, filtering, pagination, CSV export, and optional JSON diagnostic.
"""

from __future__ import annotations

import calendar as _calendar
import csv
import json
import threading
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

BEIJING = timezone(timedelta(hours=8), name='Asia/Shanghai')
ROOT = Path(__file__).resolve().parents[1]

# Track open singleton windows to prevent duplicate popups
_OPEN_WINDOWS: dict[str, tk.Toplevel] = {}


def _focus_or_track(key: str, factory):
    existing = _OPEN_WINDOWS.get(key)
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return existing
        except tk.TclError:
            pass
        _OPEN_WINDOWS.pop(key, None)
    win = factory()
    _OPEN_WINDOWS[key] = win

    def _cleanup(event):
        if getattr(event, 'widget', None) is win:
            _OPEN_WINDOWS.pop(key, None)

    win.bind('<Destroy>', _cleanup, add='+')
    return win


def _safe_decimal(value, default='0') -> Decimal:
    try:
        d = Decimal(str(value).replace(',', '').strip())
        return d if d.is_finite() else Decimal(default)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _fmt_money(value, signed=False) -> str:
    if value is None or value == '' or value == '—':
        return '—'
    d = _safe_decimal(value)
    return f'{d:+,.2f}' if signed else f'{d:,.2f}'


def _fmt_price(value) -> str:
    if value is None or value == '':
        return '—'
    d = _safe_decimal(value)
    return f'{d:,.2f}'


def _fmt_qty(value) -> str:
    if value is None or value == '':
        return '—'
    d = _safe_decimal(value)
    return f'{d:.6f}'.rstrip('0').rstrip('.') or '0'


def _fmt_local_time(raw) -> str:
    if not raw:
        return '—'
    text = str(raw).strip()
    try:
        dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if dt.tzinfo is not None:
            dt = dt.astimezone(BEIJING)
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        return text


def _reason_cn(reason: str) -> str:
    code = str(reason or '').upper().strip()
    mapping = {
        'ENTRY': '开仓',
        'ENTRY_NEXT_OPEN': '开仓（次根开盘）',
        'STOP': '止损平仓',
        'STOP_QUOTE': '止损平仓（报价）',
        'TARGET': '止盈平仓',
        'TARGET_QUOTE': '止盈平仓（报价）',
        'TIME': '超时平仓',
        'RULE': '规则平仓',
    }
    return mapping.get(code, reason or '—')


def _reason_category(reason: str) -> str:
    code = str(reason or '').upper().strip()
    if code in ('ENTRY', 'ENTRY_NEXT_OPEN'):
        return '开仓'
    if 'TARGET' in code:
        return '止盈'
    if 'STOP' in code:
        return '止损'
    if 'TIME' in code:
        return '超时'
    if 'RULE' in code:
        return '规则'
    return '其他'


def normalize_fills_for_table(raw_fills: list[dict], *, strategy_id: str, strategy_name: str, source_kind: str = 'paper') -> list[dict]:
    """Normalize paper or backtest fills into the 13-column display schema without fabricating missing fields."""
    # Sort chronologically ascending first to track real open/close state accurately for paper fills
    def _sort_key(f):
        return str(f.get('cycle') or f.get('created_at') or f.get('occurred_at') or '')

    ordered = sorted(raw_fills or [], key=_sort_key)
    open_pos = None
    rows = []
    for f in ordered:
        side = str(f.get('side') or '').upper()
        reason = str(f.get('reason') or '')
        reason_u = reason.upper()
        qty = _safe_decimal(f.get('qty') or f.get('quantity'))
        price = _safe_decimal(f.get('price'))
        fee = _safe_decimal(f.get('fee'))
        slippage = _safe_decimal(f.get('slippage'))
        notional = _safe_decimal(f.get('notional')) if f.get('notional') is not None else abs(price * qty)
        version = str(f.get('version') or f.get('strategy_version') or '—')
        order_id = str(f.get('order_id') or f.get('idempotency_key') or f.get('fill_id') or '—')
        ts_raw = f.get('cycle') or f.get('created_at') or f.get('occurred_at') or ''
        local_ts = _fmt_local_time(ts_raw)
        day_key = local_ts[:10] if len(local_ts) >= 10 else ''

        # Determine exact trade action without guessing from BUY/SELL alone
        explicit_action = f.get('action')
        realized_pnl = f.get('net_pnl')
        if explicit_action in ('开多', '平多', '开空', '平空'):
            action = explicit_action
            if action in ('开多', '开空'):
                realized_pnl_val = None
            else:
                realized_pnl_val = _safe_decimal(realized_pnl) if realized_pnl is not None else None
        elif reason_u in ('ENTRY', 'ENTRY_NEXT_OPEN'):
            direction = str(f.get('direction') or ('LONG' if side == 'BUY' else 'SHORT')).upper()
            action = '开多' if direction == 'LONG' else '开空'
            open_pos = {'direction': direction, 'entry': price, 'qty': qty, 'entry_fee': fee}
            realized_pnl_val = None
        elif reason_u in ('STOP', 'STOP_QUOTE', 'TARGET', 'TARGET_QUOTE', 'TIME', 'RULE') or 'CLOSE' in reason_u:
            direction = str(f.get('direction') or (open_pos['direction'] if open_pos else ('LONG' if side == 'SELL' else 'SHORT'))).upper()
            action = '平多' if direction == 'LONG' else '平空'
            if realized_pnl is not None and realized_pnl != '':
                realized_pnl_val = _safe_decimal(realized_pnl)
            elif open_pos is not None and open_pos['direction'] == direction:
                sign = Decimal(1) if direction == 'LONG' else Decimal(-1)
                gross = (price - open_pos['entry']) * qty * sign
                realized_pnl_val = gross - open_pos['entry_fee'] - fee
                open_pos = None
            else:
                realized_pnl_val = None
        else:
            action = '—'
            realized_pnl_val = _safe_decimal(realized_pnl) if realized_pnl not in (None, '') else None

        rows.append({
            'time': local_ts,
            'raw_time': str(ts_raw),
            'day': day_key,
            'strategy_id': str(f.get('strategy_id') or strategy_id),
            'strategy_name': str(f.get('strategy_name') or strategy_name),
            'version': version,
            'action': action,
            'side': side or '—',
            'price': _fmt_price(price),
            'price_num': float(price),
            'qty': _fmt_qty(qty),
            'qty_num': float(qty),
            'notional': _fmt_money(notional),
            'notional_num': float(notional),
            'fee': _fmt_money(fee),
            'fee_num': float(fee),
            'slippage': _fmt_money(slippage),
            'slippage_num': float(slippage),
            'pnl': _fmt_money(realized_pnl_val, signed=True) if realized_pnl_val is not None else '—',
            'pnl_num': float(realized_pnl_val) if realized_pnl_val is not None else None,
            'reason': _reason_cn(reason),
            'reason_cat': _reason_category(reason),
            'order_id': order_id,
            'source_kind': source_kind,
            'raw': f,
        })
    # Default display order: newest first
    rows.reverse()
    return rows


def add_calendar_months(d: date, months: int) -> date:
    month_idx = d.month - 1 + months
    year = d.year + month_idx // 12
    month = month_idx % 12 + 1
    day = min(d.day, _calendar.monthrange(year, month)[1])
    return date(year, month, day)


class CalendarDatePicker(tk.Frame):
    """Native Tkinter calendar date selector (zero external dependencies)."""

    def __init__(self, parent, initial: date, *, on_change=None, min_date: date | None = None, max_date: date | None = None):
        super().__init__(parent, bg='white', highlightbackground='#cbd5e1', highlightthickness=1)
        self._value = initial
        self._view_year = initial.year
        self._view_month = initial.month
        self._on_change = on_change
        self._min_date = min_date
        self._max_date = max_date
        self._popup: tk.Toplevel | None = None

        self._var = tk.StringVar(value=initial.isoformat())
        self._entry = tk.Label(
            self, textvariable=self._var, bg='white', fg='#0f172a',
            font=('Microsoft YaHei UI', 9, 'bold'), padx=10, pady=5, width=12, anchor='w', cursor='hand2',
        )
        self._entry.pack(side='left', fill='x', expand=True)
        self._btn = tk.Button(
            self, text='📅 选择', command=self._toggle_popup,
            bg='#eff6ff', fg='#1d4ed8', activebackground='#dbeafe', activeforeground='#1e40af',
            relief='flat', padx=8, pady=4, font=('Microsoft YaHei UI', 8, 'bold'), cursor='hand2',
        )
        self._btn.pack(side='right')
        self._entry.bind('<Button-1>', lambda _e: self._toggle_popup())

    def get_date(self) -> date:
        return self._value

    def set_date(self, new_date: date):
        self._value = new_date
        self._view_year = new_date.year
        self._view_month = new_date.month
        self._var.set(new_date.isoformat())
        if self._on_change:
            self._on_change(new_date)

    def _toggle_popup(self):
        if self._popup is not None and self._popup.winfo_exists():
            self._popup.destroy()
            self._popup = None
            return
        pop = tk.Toplevel(self)
        pop.title('选择日期')
        pop.transient(self.winfo_toplevel())
        pop.resizable(False, False)
        pop.configure(bg='white')
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 4
        pop.geometry(f'276x268+{x}+{y}')
        self._popup = pop

        header = tk.Frame(pop, bg='#1e3a5f')
        header.pack(fill='x')
        month_lbl = tk.StringVar()
        tk.Button(header, text='◀', command=lambda: shift(-1), bg='#1e3a5f', fg='white', relief='flat', padx=10, pady=5, cursor='hand2').pack(side='left')
        tk.Label(header, textvariable=month_lbl, bg='#1e3a5f', fg='white', font=('Microsoft YaHei UI', 9, 'bold')).pack(side='left', expand=True)
        tk.Button(header, text='▶', command=lambda: shift(1), bg='#1e3a5f', fg='white', relief='flat', padx=10, pady=5, cursor='hand2').pack(side='right')

        wk = tk.Frame(pop, bg='#f1f5f9')
        wk.pack(fill='x')
        for idx, name in enumerate(('一', '二', '三', '四', '五', '六', '日')):
            tk.Label(wk, text=name, bg='#f1f5f9', fg='#475569', font=('Microsoft YaHei UI', 8, 'bold'), width=4, pady=3).grid(row=0, column=idx, sticky='ew')
            wk.columnconfigure(idx, weight=1)

        grid_frame = tk.Frame(pop, bg='white', padx=4, pady=4)
        grid_frame.pack(fill='both', expand=True)

        def pick(d: date):
            self.set_date(d)
            if pop.winfo_exists():
                pop.destroy()
            self._popup = None

        def render():
            month_lbl.set(f'{self._view_year} 年 {self._view_month:02d} 月')
            for w in grid_frame.winfo_children():
                w.destroy()
            first_wd, days_in_m = _calendar.monthrange(self._view_year, self._view_month)
            row_i, col_i = 0, first_wd
            for day_num in range(1, days_in_m + 1):
                d = date(self._view_year, self._view_month, day_num)
                is_sel = d == self._value
                bg = '#2563eb' if is_sel else 'white'
                fg = 'white' if is_sel else '#0f172a'
                btn = tk.Button(
                    grid_frame, text=str(day_num), width=3, pady=3,
                    bg=bg, fg=fg, activebackground='#dbeafe', activeforeground='#1e3a5f',
                    relief='flat', font=('Microsoft YaHei UI', 8, 'bold' if is_sel else 'normal'),
                    cursor='hand2', command=lambda dd=d: pick(dd),
                )
                btn.grid(row=row_i, column=col_i, padx=1, pady=1, sticky='nsew')
                grid_frame.columnconfigure(col_i, weight=1)
                col_i += 1
                if col_i >= 7:
                    col_i = 0
                    row_i += 1

        def shift(delta: int):
            m = self._view_month + delta
            y = self._view_year
            if m < 1:
                m, y = 12, y - 1
            elif m > 12:
                m, y = 1, y + 1
            self._view_year, self._view_month = y, m
            render()

        render()
        pop.focus_force()


class TradeFillsTableFrame(tk.Frame):
    """Reusable BUG-005 visual Trade Fills table with sorting, filtering, pagination, CSV export, and JSON diagnostic."""

    COLUMNS = (
        ('time', '成交时间', 148, 'w'),
        ('strategy_name', '策略名称', 150, 'w'),
        ('version', '策略版本', 74, 'center'),
        ('action', '交易动作', 74, 'center'),
        ('side', '交易方向', 74, 'center'),
        ('price', '成交价格', 98, 'e'),
        ('qty', '成交数量(BTC)', 104, 'e'),
        ('notional', '名义价值', 98, 'e'),
        ('fee', '手续费', 80, 'e'),
        ('slippage', '滑点成本', 80, 'e'),
        ('pnl', '已实现盈亏', 100, 'e'),
        ('reason', '交易原因', 118, 'w'),
        ('order_id', '订单编号', 195, 'w'),
    )
    PAGE_SIZE = 100

    def __init__(self, parent, rows: list[dict], *, strategy_id: str, strategy_name: str, source_kind: str = 'paper'):
        super().__init__(parent, bg='#f8fafc')
        self._all_rows = list(rows or [])
        self._filtered: list[dict] = list(self._all_rows)
        self._strategy_id = strategy_id
        self._strategy_name = strategy_name
        self._source_kind = source_kind
        self._page = 0
        self._sort_col = 'time'
        self._sort_desc = True
        self._iid_map: dict[str, dict] = {}

        # Filter bar
        bar = tk.Frame(self, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
        bar.pack(fill='x', padx=8, pady=(6, 4))

        badge_text = '纸盘实时成交账本' if source_kind == 'paper' else '历史回测成交（独立回测账本）'
        badge_bg = '#dbeafe' if source_kind == 'paper' else '#fef3c7'
        badge_fg = '#1d4ed8' if source_kind == 'paper' else '#92400e'
        tk.Label(bar, text=badge_text, bg=badge_bg, fg=badge_fg, font=('Microsoft YaHei UI', 8, 'bold'), padx=8, pady=3).pack(side='left', padx=(8, 10), pady=6)

        tk.Label(bar, text='方向:', bg='white', fg='#475569', font=('Microsoft YaHei UI', 8)).pack(side='left')
        self._side_var = tk.StringVar(value='全部')
        ttk.Combobox(bar, textvariable=self._side_var, state='readonly', width=7, values=('全部', 'BUY', 'SELL')).pack(side='left', padx=(3, 8))

        tk.Label(bar, text='原因:', bg='white', fg='#475569', font=('Microsoft YaHei UI', 8)).pack(side='left')
        self._reason_var = tk.StringVar(value='全部')
        ttk.Combobox(bar, textvariable=self._reason_var, state='readonly', width=8, values=('全部', '开仓', '止盈', '止损', '超时', '规则', '其他')).pack(side='left', padx=(3, 8))

        tk.Label(bar, text='起始日:', bg='white', fg='#475569', font=('Microsoft YaHei UI', 8)).pack(side='left')
        self._from_var = tk.StringVar(value='')
        ttk.Entry(bar, textvariable=self._from_var, width=11).pack(side='left', padx=(3, 6))
        tk.Label(bar, text='结束日:', bg='white', fg='#475569', font=('Microsoft YaHei UI', 8)).pack(side='left')
        self._to_var = tk.StringVar(value='')
        ttk.Entry(bar, textvariable=self._to_var, width=11).pack(side='left', padx=(3, 8))

        ttk.Button(bar, text='筛选', command=self.apply_filters).pack(side='left', padx=2)
        ttk.Button(bar, text='重置', command=self.reset_filters).pack(side='left', padx=2)

        tk.Button(
            bar, text='导出 CSV', command=self.export_csv,
            bg='#2563eb', fg='white', activebackground='#1d4ed8', activeforeground='white',
            relief='flat', padx=10, pady=3, font=('Microsoft YaHei UI', 8, 'bold'), cursor='hand2',
        ).pack(side='right', padx=(4, 8), pady=5)
        ttk.Button(bar, text='高级诊断 JSON', command=self.show_raw_json).pack(side='right', padx=4)

        # Table area
        body = tk.Frame(self, bg='#f8fafc')
        body.pack(fill='both', expand=True, padx=8, pady=4)
        col_ids = tuple(c[0] for c in self.COLUMNS)
        self._tree = ttk.Treeview(body, columns=col_ids, show='headings', height=14)
        self._tree.tag_configure('profit', foreground='#15803d', background='#f0fdf4')
        self._tree.tag_configure('loss', foreground='#b91c1c', background='#fef2f2')
        self._tree.tag_configure('even', foreground='#0f172a', background='#ffffff')
        self._tree.tag_configure('odd', foreground='#0f172a', background='#f8fafc')

        for cid, title, width, anchor in self.COLUMNS:
            self._tree.heading(cid, text=title, command=lambda c=cid: self._toggle_sort(c))
            self._tree.column(cid, width=width, minwidth=60, anchor=anchor, stretch=True)

        ys = ttk.Scrollbar(body, orient='vertical', command=self._tree.yview)
        xs = ttk.Scrollbar(body, orient='horizontal', command=self._tree.xview)
        self._tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self._tree.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        xs.grid(row=1, column=0, sticky='ew')
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self._empty_lbl = tk.Label(
            body, text='暂无成交记录', bg='white', fg='#64748b',
            font=('Microsoft YaHei UI', 11), pady=30,
        )

        # Footer / Pagination
        foot = tk.Frame(self, bg='#f8fafc')
        foot.pack(fill='x', padx=8, pady=(2, 6))
        self._summary_var = tk.StringVar(value='')
        tk.Label(foot, textvariable=self._summary_var, bg='#f8fafc', fg='#475569', font=('Microsoft YaHei UI', 8)).pack(side='left')
        self._next_btn = ttk.Button(foot, text='下一页 ▶', command=lambda: self._goto_page(self._page + 1))
        self._next_btn.pack(side='right', padx=3)
        self._prev_btn = ttk.Button(foot, text='◀ 上一页', command=lambda: self._goto_page(self._page - 1))
        self._prev_btn.pack(side='right', padx=3)

        self._render_page()

    def set_rows(self, rows: list[dict]):
        self._all_rows = list(rows or [])
        self.apply_filters()

    def reset_filters(self):
        self._side_var.set('全部')
        self._reason_var.set('全部')
        self._from_var.set('')
        self._to_var.set('')
        self.apply_filters()

    def apply_filters(self):
        side_f = self._side_var.get()
        reason_f = self._reason_var.get()
        from_f = self._from_var.get().strip()
        to_f = self._to_var.get().strip()
        result = []
        for r in self._all_rows:
            if side_f != '全部' and r['side'] != side_f:
                continue
            if reason_f != '全部' and r['reason_cat'] != reason_f:
                continue
            if from_f and r['day'] and r['day'] < from_f:
                continue
            if to_f and r['day'] and r['day'] > to_f:
                continue
            result.append(r)
        self._filtered = result
        self._page = 0
        self._sort_filtered()
        self._render_page()

    def _toggle_sort(self, col: str):
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = True
        self._sort_filtered()
        self._render_page()

    def _sort_filtered(self):
        col = self._sort_col
        num_map = {'price': 'price_num', 'qty': 'qty_num', 'notional': 'notional_num', 'fee': 'fee_num', 'slippage': 'slippage_num', 'pnl': 'pnl_num'}
        if col in num_map:
            nk = num_map[col]
            self._filtered.sort(key=lambda r: (r[nk] is not None, r[nk] if r[nk] is not None else 0.0), reverse=self._sort_desc)
        else:
            self._filtered.sort(key=lambda r: str(r.get(col) or ''), reverse=self._sort_desc)

    def _goto_page(self, page: int):
        total_pages = max(1, (len(self._filtered) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._page = max(0, min(page, total_pages - 1))
        self._render_page()

    def _render_page(self):
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        self._iid_map.clear()
        total = len(self._filtered)
        if total == 0:
            self._empty_lbl.place(relx=0.5, rely=0.5, anchor='center')
            self._summary_var.set(f'共 0 条记录（总账本 {len(self._all_rows)} 条）')
            self._prev_btn.configure(state='disabled')
            self._next_btn.configure(state='disabled')
            return
        self._empty_lbl.place_forget()
        start = self._page * self.PAGE_SIZE
        end = min(total, start + self.PAGE_SIZE)
        for idx, r in enumerate(self._filtered[start:end]):
            tag = 'profit' if (r['pnl_num'] is not None and r['pnl_num'] > 0) else 'loss' if (r['pnl_num'] is not None and r['pnl_num'] < 0) else ('even' if idx % 2 == 0 else 'odd')
            vals = tuple(r[c[0]] for c in self.COLUMNS)
            iid = f'row_{start + idx}'
            self._iid_map[iid] = r
            self._tree.insert('', 'end', iid=iid, values=vals, tags=(tag,))
        total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._summary_var.set(
            f'共 {total} 条匹配记录（总计 {len(self._all_rows)} 条） · 当前第 {self._page + 1}/{total_pages} 页 · 显示第 {start + 1}–{end} 条'
        )
        self._prev_btn.configure(state='normal' if self._page > 0 else 'disabled')
        self._next_btn.configure(state='normal' if (self._page + 1) < total_pages else 'disabled')

    def export_csv(self, dest_path: str | None = None):
        if dest_path is None:
            prefix = 'paper_fills' if self._source_kind == 'paper' else 'backtest_fills'
            dest_path = filedialog.asksaveasfilename(
                parent=self.winfo_toplevel(),
                title='导出成交明细 CSV',
                initialfile=f'{prefix}_{self._strategy_id}.csv',
                defaultextension='.csv',
                filetypes=[('CSV 表格文件', '*.csv')],
            )
        if not dest_path:
            return None
        headers = [c[1] for c in self.COLUMNS]
        keys = [c[0] for c in self.COLUMNS]
        with open(dest_path, 'w', encoding='utf-8-sig', newline='') as fh:
            writer = csv.writer(fh)
            writer.writerow(headers)
            for r in self._filtered:
                writer.writerow([r[k] for k in keys])
        if dest_path:
            messagebox.showinfo('导出成功', f'已导出 {len(self._filtered)} 条成交记录至：\n{dest_path}', parent=self.winfo_toplevel())
        return dest_path

    def show_raw_json(self):
        sel = self._tree.selection()
        if sel and sel[0] in self._iid_map:
            payload = self._iid_map[sel[0]]['raw']
            title = f'{self._strategy_id} · 单笔成交原始 JSON（高级诊断）'
        else:
            payload = [r['raw'] for r in self._filtered[:200]]
            title = f'{self._strategy_id} · 原始成交 JSON（前 {len(payload)} 条 · 高级诊断）'
        win = tk.Toplevel(self.winfo_toplevel())
        win.title(title)
        win.geometry('840x540')
        txt = tk.Text(win, wrap='none', font=('Consolas', 9), bg='#0f172a', fg='#e2e8f0')
        sy = ttk.Scrollbar(win, orient='vertical', command=txt.yview)
        sx = ttk.Scrollbar(win, orient='horizontal', command=txt.xview)
        txt.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        txt.grid(row=0, column=0, sticky='nsew')
        sy.grid(row=0, column=1, sticky='ns')
        sx.grid(row=1, column=0, sticky='ew')
        win.rowconfigure(0, weight=1)
        win.columnconfigure(0, weight=1)
        txt.insert('1.0', json.dumps(payload, ensure_ascii=False, indent=2))
        txt.configure(state='disabled')


def open_fills_table_window(parent, *, strategy_id: str, strategy_name: str, raw_fills: list[dict], source_kind: str = 'paper'):
    """Open a standalone resizable BUG-005 visual Trade Fills Table window."""
    key = f'fills:{source_kind}:{strategy_id}'

    def _build():
        win = tk.Toplevel(parent)
        win.title(f'{strategy_name}（{strategy_id}）· {"纸盘成交明细" if source_kind == "paper" else "回测成交明细"}')
        win.geometry('1280x640')
        win.minsize(920, 460)
        win.configure(bg='#f8fafc')
        rows = normalize_fills_for_table(raw_fills, strategy_id=strategy_id, strategy_name=strategy_name, source_kind=source_kind)
        table = TradeFillsTableFrame(win, rows, strategy_id=strategy_id, strategy_name=strategy_name, source_kind=source_kind)
        table.pack(fill='both', expand=True)
        return win

    return _focus_or_track(key, _build)


def _draw_curve_charts(canvas: tk.Canvas, curve: list[dict], initial_equity: float):
    """Draw Equity Curve (top) and Drawdown % Curve (bottom) on a native Tkinter Canvas."""
    canvas.delete('all')
    w = max(canvas.winfo_width(), 860)
    h = max(canvas.winfo_height(), 420)
    if not curve:
        canvas.create_text(w // 2, h // 2, text='暂无权益曲线数据', fill='#64748b', font=('Microsoft YaHei UI', 11))
        return

    # Downsample if very large for crisp rendering
    pts = curve
    if len(pts) > 900:
        step = len(pts) / 900.0
        pts = [curve[int(i * step)] for i in range(900)]
        if pts[-1] is not curve[-1]:
            pts.append(curve[-1])

    equities = [float(_safe_decimal(p.get('equity'), '10000')) for p in pts]
    dds = [float(_safe_decimal(p.get('drawdown_pct'), '0')) * 100.0 for p in pts]
    cycles = [str(p.get('cycle') or '') for p in pts]

    pad_l, pad_r = 76, 26
    top_y1, top_y2 = 28, int(h * 0.58)
    bot_y1, bot_y2 = int(h * 0.67), h - 34
    plot_w = max(100, w - pad_l - pad_r)

    # Top panel: Equity Curve
    canvas.create_rectangle(pad_l, top_y1, w - pad_r, top_y2, fill='#ffffff', outline='#cbd5e1')
    canvas.create_text(pad_l + 6, top_y1 - 12, text='权益曲线 (USD_RESEARCH)', anchor='w', fill='#1e3a5f', font=('Microsoft YaHei UI', 9, 'bold'))
    min_eq = min(min(equities), initial_equity)
    max_eq = max(max(equities), initial_equity)
    span_eq = max(max_eq - min_eq, 1.0)
    min_eq -= span_eq * 0.08
    max_eq += span_eq * 0.08
    span_eq = max_eq - min_eq

    # Horizontal grid lines & labels
    for i in range(5):
        frac = i / 4.0
        y = top_y2 - frac * (top_y2 - top_y1)
        val = min_eq + frac * span_eq
        canvas.create_line(pad_l, y, w - pad_r, y, fill='#f1f5f9')
        canvas.create_text(pad_l - 6, y, text=f'{val:,.0f}', anchor='e', fill='#64748b', font=('Consolas', 8))

    # Baseline $10,000
    base_y = top_y2 - ((initial_equity - min_eq) / span_eq) * (top_y2 - top_y1)
    canvas.create_line(pad_l, base_y, w - pad_r, base_y, fill='#94a3b8', dash=(4, 4))

    n = len(equities)
    eq_coords = []
    for i, eq in enumerate(equities):
        x = pad_l + (i / max(1, n - 1)) * plot_w
        y = top_y2 - ((eq - min_eq) / span_eq) * (top_y2 - top_y1)
        eq_coords.extend((x, y))
    if len(eq_coords) >= 4:
        line_color = '#16a34a' if equities[-1] >= initial_equity else '#dc2626'
        canvas.create_line(*eq_coords, fill=line_color, width=2)
    canvas.create_text(
        w - pad_r - 6, top_y1 + 12,
        text=f'期末权益: {equities[-1]:,.2f}  |  最高: {max(equities):,.2f}  |  最低: {min(equities):,.2f}',
        anchor='e', fill='#0f172a', font=('Microsoft YaHei UI', 8, 'bold'),
    )

    # Bottom panel: Drawdown % Curve
    canvas.create_rectangle(pad_l, bot_y1, w - pad_r, bot_y2, fill='#ffffff', outline='#cbd5e1')
    canvas.create_text(pad_l + 6, bot_y1 - 12, text='回撤曲线 (%)', anchor='w', fill='#991b1b', font=('Microsoft YaHei UI', 9, 'bold'))
    max_dd = max(max(dds), 0.5)
    for i in range(4):
        frac = i / 3.0
        y = bot_y1 + frac * (bot_y2 - bot_y1)
        val = frac * max_dd
        canvas.create_line(pad_l, y, w - pad_r, y, fill='#fef2f2')
        canvas.create_text(pad_l - 6, y, text=f'-{val:.2f}%', anchor='e', fill='#64748b', font=('Consolas', 8))

    dd_poly = [pad_l, bot_y1]
    dd_line = []
    for i, dd in enumerate(dds):
        x = pad_l + (i / max(1, n - 1)) * plot_w
        y = bot_y1 + (dd / max_dd) * (bot_y2 - bot_y1)
        dd_poly.extend((x, y))
        dd_line.extend((x, y))
    dd_poly.extend((w - pad_r, bot_y1))
    if len(dd_poly) >= 6:
        canvas.create_polygon(*dd_poly, fill='#fecaca', outline='')
    if len(dd_line) >= 4:
        canvas.create_line(*dd_line, fill='#dc2626', width=1.5)
    canvas.create_text(w - pad_r - 6, bot_y1 + 12, text=f'最大回撤: -{max(dds):.2f}%', anchor='e', fill='#b91c1c', font=('Microsoft YaHei UI', 8, 'bold'))

    # X-axis date ticks
    for tick_i in (0, n // 4, n // 2, (3 * n) // 4, n - 1):
        if 0 <= tick_i < n:
            x = pad_l + (tick_i / max(1, n - 1)) * plot_w
            lbl = _fmt_local_time(cycles[tick_i])[:10]
            canvas.create_text(x, bot_y2 + 14, text=lbl, anchor='center', fill='#475569', font=('Consolas', 8))


def open_backtest_report_window(parent, report: dict):
    """Open the standardized BUG-004 visual backtest report window with KPI cards, Equity/Drawdown charts, and Fills table."""
    sid = str(report.get('strategy_id') or '—')
    sname = str(report.get('strategy_name') or sid)
    ver = str(report.get('strategy_version') or '—')
    win = tk.Toplevel(parent)
    win.title(f'历史回测绩效报告 · {sname}（{sid} v{ver}）')
    win.geometry('1260x800')
    win.minsize(980, 620)
    win.configure(bg='#f5f7fb')

    # Header banner
    head = tk.Frame(win, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
    head.pack(fill='x', padx=12, pady=(10, 6))
    start_s = _fmt_local_time(report.get('actual_start') or report.get('start'))[:16]
    end_s = _fmt_local_time(report.get('actual_end') or report.get('end'))[:16]
    tk.Label(
        head, text=f'{sname}  ·  ID: {sid}  ·  版本: v{ver}',
        bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 13, 'bold'), anchor='w',
    ).pack(fill='x', padx=14, pady=(10, 2))
    tk.Label(
        head,
        text=(
            f'回测区间（北京时间）：{start_s} ~ {end_s}  |  标的：{report.get("symbol", "BTC/USD")} ({report.get("timeframe", "15m")})'
            f'  |  数据源：{report.get("data_source", "BINANCE_USDM_PERP")}  |  K线：{report.get("bar_count", 0)} 根（预热 {report.get("warmup_bar_count", 0)} 根）'
            f'  |  耗时：{report.get("elapsed_ms", 0)} ms'
        ),
        bg='white', fg='#475569', font=('Microsoft YaHei UI', 8), anchor='w',
    ).pack(fill='x', padx=14, pady=(0, 10))

    # KPI Cards Grid (2 rows x 6 cols)
    kpi_frame = tk.Frame(win, bg='#f5f7fb')
    kpi_frame.pack(fill='x', padx=12, pady=(0, 6))
    net_pnl = _safe_decimal(report.get('net_pnl'))
    net_ret = float(report.get('net_return_pct') or 0.0)
    win_rate = float(report.get('win_rate') or 0.0) * 100.0
    max_dd_pct = float(report.get('max_drawdown_pct') if report.get('max_drawdown_pct') is not None else float(_safe_decimal(report.get('max_drawdown'))) * 100.0)
    max_dd_amt = _safe_decimal(report.get('max_drawdown_amount'))
    pnl_color = '#16a34a' if net_pnl > 0 else '#dc2626' if net_pnl < 0 else '#172b4d'
    open_pos = report.get('open_position')
    open_pos_txt = f"{open_pos.get('direction', 'LONG')} {_fmt_qty(open_pos.get('qty'))} BTC" if isinstance(open_pos, dict) else '空仓'
    gaps = report.get('data_gaps') or []

    cards = [
        ('初始资金', f"{_fmt_money(report.get('initial_equity', '10000'))} USD", '#172b4d'),
        ('最终权益', f"{_fmt_money(report.get('final_equity'))} USD", pnl_color),
        ('净收益率', f'{net_ret:+.2f}%', pnl_color),
        ('净盈亏（含成本）', f'{_fmt_money(net_pnl, signed=True)} USD', pnl_color),
        ('已实现 / 未实现盈亏', f"{_fmt_money(report.get('realized_net_pnl'), signed=True)} / {_fmt_money(report.get('unrealized_net_pnl'), signed=True)}", '#172b4d'),
        ('最大回撤', f'-{max_dd_pct:.2f}%（-{_fmt_money(max_dd_amt)}）', '#dc2626' if max_dd_pct > 0 else '#172b4d'),
        ('成交 / 平仓次数', f"{report.get('fill_count', len(report.get('trades') or []))} 笔 / {report.get('closed_trade_count', 0)} 次平仓", '#172b4d'),
        ('盈利 / 亏损次数', f"{report.get('win_count', 0)} 胜 / {report.get('loss_count', 0)} 负", '#172b4d'),
        ('胜率（已平仓口径）', f'{win_rate:.1f}%' if report.get('closed_trade_count') else '—（无平仓）', '#172b4d'),
        ('盈利因子', str(report.get('profit_factor_label') or '—'), '#172b4d'),
        ('累计手续费 / 滑点', f"{_fmt_money(report.get('fees'))} / {_fmt_money(report.get('slippage_total'))}", '#172b4d'),
        ('数据缺口 / 期末持仓', f'{len(gaps)} 处缺口 · {open_pos_txt}', '#b45309' if gaps else '#172b4d'),
    ]
    for idx, (label, val, fg) in enumerate(cards):
        r_i, c_i = divmod(idx, 6)
        card = tk.Frame(kpi_frame, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
        card.grid(row=r_i, column=c_i, sticky='nsew', padx=3, pady=3)
        tk.Label(card, text=label, bg='white', fg='#64748b', font=('Microsoft YaHei UI', 8), anchor='w').pack(fill='x', padx=8, pady=(6, 1))
        tk.Label(card, text=val, bg='white', fg=fg, font=('Microsoft YaHei UI', 10, 'bold'), anchor='w').pack(fill='x', padx=8, pady=(0, 6))
        kpi_frame.columnconfigure(c_i, weight=1, uniform='kpi')

    # Tabs: Charts | Trades | Integrity & Cost Notes
    nb = ttk.Notebook(win)
    nb.pack(fill='both', expand=True, padx=12, pady=(4, 10))

    tab_chart = tk.Frame(nb, bg='#f8fafc')
    nb.add(tab_chart, text='权益与回撤曲线')
    chart_canvas = tk.Canvas(tab_chart, bg='#f8fafc', highlightthickness=0)
    chart_canvas.pack(fill='both', expand=True, padx=8, pady=8)
    init_eq_f = float(_safe_decimal(report.get('initial_equity', '10000'), '10000'))
    curve = report.get('equity_curve') or []
    chart_canvas.bind('<Configure>', lambda _e: _draw_curve_charts(chart_canvas, curve, init_eq_f))
    win.after(120, lambda: _draw_curve_charts(chart_canvas, curve, init_eq_f))

    tab_trades = tk.Frame(nb, bg='#f8fafc')
    nb.add(tab_trades, text=f'回测成交明细（{len(report.get("trades") or [])}）')
    norm_trades = normalize_fills_for_table(report.get('trades') or [], strategy_id=sid, strategy_name=sname, source_kind='backtest')
    TradeFillsTableFrame(tab_trades, norm_trades, strategy_id=sid, strategy_name=sname, source_kind='backtest').pack(fill='both', expand=True)

    tab_info = tk.Frame(nb, bg='white')
    nb.add(tab_info, text='数据完整性与成本说明')
    info_txt = tk.Text(tab_info, wrap='word', bg='white', fg='#1e293b', font=('Microsoft YaHei UI', 9), relief='flat', padx=14, pady=12)
    info_txt.pack(fill='both', expand=True)
    cm = report.get('cost_model') or {}
    notes = [
        f'策略名称：{sname}（ID: {sid}，版本: v{ver}）',
        f'数据源与标的：{report.get("data_source", "BINANCE_USDM_PERP")} · {report.get("symbol", "BTC/USD")} · {report.get("timeframe", "15m")}',
        f'实际使用K线：{report.get("bar_count", 0)} 根（起始 {report.get("actual_start")}，结束 {report.get("actual_end")}），预热K线 {report.get("warmup_bar_count", 0)} 根（不产生回测前成交）',
        f'重复K线剔除：{report.get("duplicate_bars_removed", 0)} 根',
        f'数据缺口检查：共 {len(gaps)} 处缺口' + ('' if not gaps else '\n  ' + '\n  '.join(f"{g.get('from')} -> {g.get('to')}（缺 {g.get('missing_bars', '?')} 根）" for g in gaps[:20])),
        f'交易成本模型：手续费率 {cm.get("fee_rate", "0.0005")}，不利滑点 {cm.get("slippage_rate", "0.0002")}',
        f'未计入成本项：{cm.get("unmodeled_costs", "未计入永续合约资金费率及盘口冲击成本")}',
        f'同根K线冲突规则：{report.get("same_bar_rule", "同根K线同时触及止损与止盈时，按保守规则优先触发止损")}（本区间冲突次数：{report.get("same_bar_stop_target_ambiguities", 0)}）',
        f'期末持仓与估值口径：{"存在未平仓位，未实现盈亏按期末收盘价扣除预计平仓手续费与滑点计算" if open_pos else "期末空仓，最终权益 = 初始资金 + 已实现净盈亏"}',
        f'账本隔离说明：本次回测使用独立临时内存账本并归档至 strategy-pool-backtests.sqlite3，未修改任何纸盘、Demo 或正式实盘账户。',
    ]
    info_txt.insert('1.0', '\n\n'.join(notes))
    info_txt.configure(state='disabled')
    return win


def open_backtest_dialog(parent, item: dict, *, api_json):
    """Open the BUG-004 Calendar Date Picker backtest config dialog with non-blocking progress & cancel."""
    sid = item['id']
    sname = item.get('name') or sid
    key = f'backtest:{sid}'

    def _build():
        win = tk.Toplevel(parent)
        win.title(f'历史回测配置 · {sname}（{sid}）')
        win.geometry('660x540')
        win.minsize(600, 480)
        win.configure(bg='#f5f7fb')

        # Top strategy info card
        card = tk.Frame(win, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
        card.pack(fill='x', padx=14, pady=(12, 8))
        tk.Label(card, text=f'{sname}', bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 12, 'bold'), anchor='w').pack(fill='x', padx=12, pady=(10, 2))
        tk.Label(card, text=f'策略 ID：{sid}   ·   初始资金：10,000 USD_RESEARCH   ·   K线周期：15分钟', bg='white', fg='#475569', font=('Microsoft YaHei UI', 9), anchor='w').pack(fill='x', padx=12, pady=(0, 8))

        # Version & Data range
        form = tk.Frame(win, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
        form.pack(fill='x', padx=14, pady=(0, 8))

        versions_list = [item.get('active_version') or item.get('latest_version') or '1.0.0']
        try:
            v_items = api_json(f'/api/v1/strategy-pool/{sid}/versions').get('items', [])
            if v_items:
                versions_list = [v['version'] for v in v_items]
        except Exception:
            pass
        ver_var = tk.StringVar(value=item.get('active_version') or versions_list[-1])

        r0 = tk.Frame(form, bg='white')
        r0.pack(fill='x', padx=12, pady=(10, 6))
        tk.Label(r0, text='策略版本：', bg='white', fg='#334155', font=('Microsoft YaHei UI', 9, 'bold'), width=12, anchor='w').pack(side='left')
        ttk.Combobox(r0, textvariable=ver_var, state='readonly', width=18, values=versions_list).pack(side='left')

        range_var = tk.StringVar(value='正在读取可用历史数据范围…')
        cost_var = tk.StringVar(value='手续费 0.05% · 不利滑点 2 bps · 同根K线止损优先 · 未计入资金费率')
        r1 = tk.Frame(form, bg='white')
        r1.pack(fill='x', padx=12, pady=4)
        tk.Label(r1, text='可用数据：', bg='white', fg='#334155', font=('Microsoft YaHei UI', 9, 'bold'), width=12, anchor='w').pack(side='left')
        tk.Label(r1, textvariable=range_var, bg='white', fg='#1d4ed8', font=('Microsoft YaHei UI', 8), anchor='w', wraplength=480, justify='left').pack(side='left', fill='x', expand=True)

        r2 = tk.Frame(form, bg='white')
        r2.pack(fill='x', padx=12, pady=4)
        tk.Label(r2, text='执行规则：', bg='white', fg='#334155', font=('Microsoft YaHei UI', 9, 'bold'), width=12, anchor='w').pack(side='left')
        tk.Label(r2, textvariable=cost_var, bg='white', fg='#475569', font=('Microsoft YaHei UI', 8), anchor='w', wraplength=480, justify='left').pack(side='left', fill='x', expand=True)

        # Date pickers
        today_bj = datetime.now(BEIJING).date()
        default_start = today_bj - timedelta(days=7)

        r3 = tk.Frame(form, bg='white')
        r3.pack(fill='x', padx=12, pady=(10, 4))
        tk.Label(r3, text='开始日期：', bg='white', fg='#334155', font=('Microsoft YaHei UI', 9, 'bold'), width=12, anchor='w').pack(side='left')
        start_picker = CalendarDatePicker(r3, default_start)
        start_picker.pack(side='left', padx=(0, 14))
        tk.Label(r3, text='结束日期：', bg='white', fg='#334155', font=('Microsoft YaHei UI', 9, 'bold'), width=10, anchor='w').pack(side='left')
        end_picker = CalendarDatePicker(r3, today_bj)
        end_picker.pack(side='left')

        # Quick range presets
        r4 = tk.Frame(form, bg='white')
        r4.pack(fill='x', padx=12, pady=(6, 6))
        tk.Label(r4, text='快捷区间：', bg='white', fg='#64748b', font=('Microsoft YaHei UI', 8), width=12, anchor='w').pack(side='left')

        def set_preset(days=None, months=None):
            end_d = datetime.now(BEIJING).date()
            if months:
                start_d = add_calendar_months(end_d, -months)
            else:
                start_d = end_d - timedelta(days=days)
            start_picker.set_date(start_d)
            end_picker.set_date(end_d)

        for lbl, kwargs in (('近 1 天', {'days': 1}), ('近 7 天', {'days': 7}), ('近 30 天', {'days': 30}), ('最大 3 个月', {'months': 3})):
            ttk.Button(r4, text=lbl, command=lambda kw=kwargs: set_preset(**kw)).pack(side='left', padx=3)

        tk.Label(
            form,
            text='时区说明：日期按北京时间 (UTC+8) 选择；系统内部自动转换为 UTC 并对齐 15 分钟 K 线边界，最长支持 3 个自然月（超限将直接提示，不静默截断）。',
            bg='white', fg='#64748b', font=('Microsoft YaHei UI', 8), anchor='w', wraplength=600, justify='left',
        ).pack(fill='x', padx=12, pady=(2, 10))

        # Live Progress Panel
        prog_card = tk.Frame(win, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
        prog_card.pack(fill='x', padx=14, pady=(0, 8))
        stage_var = tk.StringVar(value='就绪：请选择回测区间后点击「开始回测」。')
        detail_var = tk.StringVar(value='')
        tk.Label(prog_card, textvariable=stage_var, bg='white', fg='#1e3a5f', font=('Microsoft YaHei UI', 9, 'bold'), anchor='w').pack(fill='x', padx=12, pady=(8, 4))
        pbar = ttk.Progressbar(prog_card, mode='determinate', maximum=100)
        pbar.pack(fill='x', padx=12, pady=2)
        tk.Label(prog_card, textvariable=detail_var, bg='white', fg='#64748b', font=('Microsoft YaHei UI', 8), anchor='w').pack(fill='x', padx=12, pady=(2, 8))

        # State
        state = {'running': False, 'job_id': None, 'poll_after': None}

        # Buttons
        btns = tk.Frame(win, bg='#f5f7fb')
        btns.pack(fill='x', padx=14, pady=(2, 10))

        def load_range_async():
            def worker():
                try:
                    info = api_json('/api/v1/strategy-pool/backtest/range')
                    min_s = _fmt_local_time(info.get('archive_min_open_at'))[:16]
                    max_s = _fmt_local_time(info.get('archive_max_close_at'))[:16]
                    cnt = info.get('archive_bar_count', 0)
                    cm = info.get('cost_model') or {}
                    msg = f'本地已归档 {min_s} ~ {max_s}（{cnt} 根），支持自动补齐最近 3 个自然月 Binance 真实 15m 历史 K 线'
                    cmsg = f"手续费 {float(cm.get('fee_rate', '0.0005'))*100:.2f}% · 不利滑点 {cm.get('adverse_slippage_bps', '2')} bps · 同根K线止损优先 · 独立账本隔离"
                    if win.winfo_exists():
                        win.after(0, lambda: (range_var.set(msg), cost_var.set(cmsg)))
                except Exception as exc:
                    if win.winfo_exists():
                        win.after(0, lambda: range_var.set(f'历史数据范围查询失败：{exc}'))
            threading.Thread(target=worker, daemon=True).start()

        def cancel_job():
            jid = state.get('job_id')
            if state['running'] and jid:
                stage_var.set('正在取消回测任务并释放资源…')
                def _do_cancel():
                    try:
                        api_json(f'/api/v1/strategy-pool/backtest/jobs/{jid}/cancel', method='POST', payload={})
                    except Exception:
                        pass
                threading.Thread(target=_do_cancel, daemon=True).start()
            else:
                win.destroy()

        def poll_job():
            if not win.winfo_exists() or not state['running'] or not state['job_id']:
                return
            jid = state['job_id']
            def _fetch():
                try:
                    res = api_json(f'/api/v1/strategy-pool/backtest/jobs/{jid}')
                except Exception as exc:
                    res = {'status': 'FAILED', 'error': str(exc)}
                if not win.winfo_exists():
                    return
                def _apply():
                    st = res.get('status')
                    proc = int(res.get('processed_bars') or 0)
                    tot = max(1, int(res.get('total_bars') or 1))
                    pct = min(100.0, (proc / tot) * 100.0)
                    pbar['value'] = pct
                    stage_var.set(str(res.get('stage') or st))
                    cur_t = _fmt_local_time(res.get('current_cycle')) if res.get('current_cycle') else '—'
                    elapsed_s = (int(res.get('elapsed_ms') or 0)) / 1000.0
                    detail_var.set(f'已处理 K 线：{proc} / {tot} ({pct:.0f}%)  |  当前时间：{cur_t}  |  已运行：{elapsed_s:.1f}s')
                    if st == 'RUNNING':
                        state['poll_after'] = win.after(250, poll_job)
                    else:
                        state['running'] = False
                        state['job_id'] = None
                        start_btn.configure(state='normal')
                        cancel_btn.configure(text='关闭')
                        if st == 'DONE' and res.get('report'):
                            pbar['value'] = 100
                            stage_var.set(f'回测完成（耗时 {elapsed_s:.2f}s），正在打开绩效报告。')
                            open_backtest_report_window(parent, res['report'])
                        elif st == 'CANCELED':
                            stage_var.set('回测已取消，资源已释放。')
                        else:
                            err = res.get('error') or '未知错误'
                            stage_var.set('回测未完成：' + str(err))
                            messagebox.showerror('回测失败', str(err), parent=win)
                win.after(0, _apply)
            threading.Thread(target=_fetch, daemon=True).start()

        def start_backtest():
            if state['running']:
                return
            s_date = start_picker.get_date()
            e_date = end_picker.get_date()
            now_bj = datetime.now(BEIJING)
            if e_date < s_date:
                messagebox.showwarning('日期区间无效', '结束日期不能早于开始日期，请重新选择。', parent=win)
                return
            if e_date > now_bj.date():
                messagebox.showwarning('日期区间无效', '结束日期不能晚于当前可用最新日期。', parent=win)
                return
            if e_date > add_calendar_months(s_date, 3):
                messagebox.showwarning('超过最大回测跨度', '最大回测时间跨度限制为三个自然月，请缩短日期区间（系统不会静默截断）。', parent=win)
                return

            # Convert Beijing dates to 15-min aligned UTC timestamps
            begin_bj = datetime(s_date.year, s_date.month, s_date.day, 0, 0, tzinfo=BEIJING)
            if e_date == now_bj.date():
                end_bj = now_bj.replace(minute=(now_bj.minute // 15) * 15, second=0, microsecond=0)
            else:
                end_bj = datetime(e_date.year, e_date.month, e_date.day, 23, 45, tzinfo=BEIJING) + timedelta(minutes=15)
            if end_bj <= begin_bj:
                messagebox.showwarning('日期区间无效', '所选日期区间无已收盘 15 分钟 K 线，请调整起始日期。', parent=win)
                return

            state['running'] = True
            start_btn.configure(state='disabled')
            cancel_btn.configure(text='取消回测')
            pbar['value'] = 5
            stage_var.set('正在提交历史回测任务…')
            detail_var.set('')

            payload = {
                'version': ver_var.get(),
                'start': begin_bj.astimezone(timezone.utc).isoformat(),
                'end': end_bj.astimezone(timezone.utc).isoformat(),
                'allow_backfill': True,
            }

            def _submit():
                try:
                    res = api_json(f'/api/v1/strategy-pool/{sid}/backtest/jobs', method='POST', payload=payload)
                    jid = res.get('job_id')
                    if not jid:
                        raise RuntimeError('未返回回测任务 ID')
                    state['job_id'] = jid
                    if win.winfo_exists():
                        win.after(100, poll_job)
                except Exception as exc:
                    if win.winfo_exists():
                        def _fail():
                            state['running'] = False
                            start_btn.configure(state='normal')
                            cancel_btn.configure(text='取消')
                            stage_var.set('提交回测失败：' + str(exc))
                            messagebox.showerror('回测失败', str(exc), parent=win)
                        win.after(0, _fail)

            threading.Thread(target=_submit, daemon=True).start()

        start_btn = tk.Button(
            btns, text='开始回测', command=start_backtest,
            bg='#2563eb', fg='white', activebackground='#1d4ed8', activeforeground='white',
            relief='flat', padx=18, pady=6, font=('Microsoft YaHei UI', 9, 'bold'), cursor='hand2',
        )
        start_btn.pack(side='right', padx=(6, 0))
        cancel_btn = ttk.Button(btns, text='取消', command=cancel_job)
        cancel_btn.pack(side='right')

        def _on_close():
            if state['running'] and state.get('job_id'):
                cancel_job()
            win.destroy()

        win.protocol('WM_DELETE_WINDOW', _on_close)
        load_range_async()
        return win

    return _focus_or_track(key, _build)


def open_update_strategy_dialog(parent, item: dict, *, api_json, on_updated=None):
    """Open the BUG-002 target-locked Strategy Version Update dialog (invoked from right-click menu)."""
    sid = item['id']
    sname = item.get('name') or f'名称待完善（{sid}）'
    cur_ver = item.get('active_version') or item.get('latest_version') or '—'
    key = f'update:{sid}'

    def _build():
        win = tk.Toplevel(parent)
        win.title(f'更新策略版本 · {sname}（{sid}）')
        win.geometry('720x560')
        win.minsize(640, 480)
        win.configure(bg='#f5f7fb')

        head = tk.Frame(win, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
        head.pack(fill='x', padx=14, pady=(12, 8))
        tk.Label(head, text=f'目标更新策略：{sname}', bg='white', fg='#172b4d', font=('Microsoft YaHei UI', 12, 'bold'), anchor='w').pack(fill='x', padx=12, pady=(10, 2))
        tk.Label(head, text=f'策略 ID：{sid}   |   当前版本：{cur_ver}   |   仅允许上传 strategy_id="{sid}" 的新版本文件', bg='white', fg='#1d4ed8', font=('Microsoft YaHei UI', 9), anchor='w').pack(fill='x', padx=12, pady=(0, 10))

        pick_bar = tk.Frame(win, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
        pick_bar.pack(fill='x', padx=14, pady=(0, 8))
        file_var = tk.StringVar(value='尚未选择新版本策略 JSON 文件')
        tk.Label(pick_bar, textvariable=file_var, bg='white', fg='#475569', font=('Microsoft YaHei UI', 9), anchor='w').pack(side='left', fill='x', expand=True, padx=12, pady=10)

        preview_box = tk.Text(win, wrap='word', bg='white', fg='#0f172a', font=('Microsoft YaHei UI', 9), relief='solid', bd=1, padx=12, pady=10)
        preview_box.pack(fill='both', expand=True, padx=14, pady=(0, 8))
        preview_box.insert('1.0', '请点击上方「选择新版本文件」按钮，系统将先校验策略 ID、版本号及规则合法性，并展示新旧版本差异与持仓保护提示。\n\n• 原策略账户、历史权益、历史成交记录、当前持仓与已有止盈止损将完整保留。\n• 若策略 ID 不一致或文件无效，更新将被拒绝，原可运行版本不受影响。')
        preview_box.configure(state='disabled')

        activate_var = tk.BooleanVar(value=True)
        chk = ttk.Checkbutton(win, text='更新成功后将新版本设为后续新开仓的生效版本（已有持仓仍保持原开仓版本及原止盈止损）', variable=activate_var)
        chk.pack(anchor='w', padx=14, pady=(0, 6))

        state = {'raw': None, 'validated': False, 'preview': None}

        def set_preview(text, fg='#0f172a'):
            preview_box.configure(state='normal', fg=fg)
            preview_box.delete('1.0', 'end')
            preview_box.insert('1.0', text)
            preview_box.configure(state='disabled')

        def choose_file():
            path = filedialog.askopenfilename(parent=win, title=f'选择 {sid} 新版本策略文件', filetypes=[('JSON 策略', '*.json')])
            if not path:
                return
            file_var.set(path)
            state['raw'] = None
            state['validated'] = False
            confirm_btn.configure(state='disabled')
            try:
                raw = Path(path).read_bytes()
                qs = urllib.parse.urlencode({'expected_strategy_id': sid})
                res = api_json(f'/api/v1/strategy-pool/upload/validate?{qs}', method='POST', raw=raw)
                state['raw'] = raw
                state['validated'] = True
                state['preview'] = res
                changes = (res.get('version_diff') or {}).get('changes') or []
                lines = [
                    '✅ 校验通过（尚未写入数据库，请确认后执行更新）',
                    f"目标策略 ID：{res.get('strategy_id')}（匹配一致）",
                    f"中文名称：{res.get('name')}",
                    f"版本变更：{res.get('previous_version') or cur_ver}  ➜  {res.get('version')}",
                    f"文件 SHA-256：{res.get('sha256')}",
                    f"持仓保护：{res.get('position_protection_note')}",
                    '',
                    f'主要变更字段（共 {len(changes)} 项）：',
                ]
                if changes:
                    for ch in changes:
                        old_s = json.dumps(ch.get('old'), ensure_ascii=False)
                        new_s = json.dumps(ch.get('new'), ensure_ascii=False)
                        if len(old_s) > 120:
                            old_s = old_s[:120] + '…'
                        if len(new_s) > 120:
                            new_s = new_s[:120] + '…'
                        lines.append(f"  • {ch['field']}:\n      旧值: {old_s}\n      新值: {new_s}")
                else:
                    lines.append('  （无字段变更）')
                set_preview('\n'.join(lines), fg='#14532d')
                confirm_btn.configure(state='normal')
            except Exception as exc:
                set_preview(f'❌ 校验未通过，已拒绝更新（原策略继续正常运行）：\n\n{exc}', fg='#b91c1c')

        def confirm_update():
            if not state['validated'] or state['raw'] is None:
                return
            try:
                qs = urllib.parse.urlencode({'expected_strategy_id': sid, 'activate': 'true' if activate_var.get() else 'false'})
                res = api_json(f'/api/v1/strategy-pool/upload?{qs}', method='POST', raw=state['raw'])
                messagebox.showinfo(
                    '策略更新成功',
                    f"策略 {res.get('name')}（{sid}）已更新至版本 {res.get('version')}。\n\n{res.get('position_protection_note', '')}",
                    parent=win,
                )
                if on_updated:
                    on_updated()
                win.destroy()
            except Exception as exc:
                messagebox.showerror('更新失败（原版本已保留）', str(exc), parent=win)

        tk.Button(
            pick_bar, text='选择新版本文件…', command=choose_file,
            bg='#eff6ff', fg='#1d4ed8', activebackground='#dbeafe', relief='flat',
            padx=12, pady=5, font=('Microsoft YaHei UI', 9, 'bold'), cursor='hand2',
        ).pack(side='right', padx=10, pady=6)

        btns = tk.Frame(win, bg='#f5f7fb')
        btns.pack(fill='x', padx=14, pady=(2, 12))
        confirm_btn = tk.Button(
            btns, text='确认更新', command=confirm_update, state='disabled',
            bg='#2563eb', fg='white', activebackground='#1d4ed8', activeforeground='white',
            relief='flat', padx=18, pady=6, font=('Microsoft YaHei UI', 9, 'bold'), cursor='hand2',
        )
        confirm_btn.pack(side='right', padx=(6, 0))
        ttk.Button(btns, text='取消', command=win.destroy).pack(side='right')
        return win

    return _focus_or_track(key, _build)


def open_strategy_pool_window(root, *, api_json, open_rule_details, open_account_details, fallback_name_fn=None, leverage_label_fn=None):
    """Open the upgraded V3.1 Strategy Pool window (BUG-001/002/003/004/005)."""
    def _build():
        window = tk.Toplevel(root)
        window.title('可扩展策略池 · BTC Paper Lab V3.1')
        window.geometry('1320x660')
        window.minsize(1040, 520)
        window.configure(bg='#f5f7fb')

        header = tk.Frame(window, bg='white', highlightbackground='#dbe5f4', highlightthickness=1)
        header.pack(fill='x', padx=10, pady=(10, 6))
        tk.Label(
            header,
            text='仅模拟盘 · BTC/USD · 15分钟 · 初始资金 $10,000 · 杠杆上限 5x · 单笔风险上限 2%   |   提示：双击策略查看账户与成交，右键点击策略行进行「更新策略」等专属操作',
            bg='white', fg='#334155', font=('Microsoft YaHei UI', 9), anchor='w', padx=12, pady=8,
        ).pack(fill='x')

        table_wrap = tk.Frame(window, bg='#f5f7fb')
        table_wrap.pack(fill='both', expand=True, padx=10, pady=4)

        # BUG-001: Separate Strategy ID, Chinese Name, and Version columns
        cols = ('id', 'name', 'version', 'status', 'position', 'amount', 'leverage', 'equity', 'realized', 'unrealized', 'fees', 'action', 'cycle')
        titles = ('策略 ID', '策略名称', '版本', '状态', '持仓', '持仓金额', '杠杆', '权益', '已实现盈亏', '未实现盈亏', '手续费', '最近行动', '最近周期（本地）')
        widths = (175, 195, 68, 108, 72, 92, 58, 92, 90, 90, 74, 110, 148)
        anchors = ('w', 'w', 'center', 'center', 'center', 'e', 'center', 'e', 'e', 'e', 'e', 'center', 'center')
        grid = ttk.Treeview(table_wrap, columns=cols, show='headings', height=14)
        for key, title, width, anchor in zip(cols, titles, widths, anchors):
            grid.heading(key, text=title)
            grid.column(key, width=width, minwidth=54, anchor=anchor)
        ys = ttk.Scrollbar(table_wrap, orient='vertical', command=grid.yview)
        xs = ttk.Scrollbar(table_wrap, orient='horizontal', command=grid.xview)
        grid.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        grid.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        xs.grid(row=1, column=0, sticky='ew')
        table_wrap.rowconfigure(0, weight=1)
        table_wrap.columnconfigure(0, weight=1)

        info = tk.StringVar(value='新策略上传后默认暂停；右键点击任意策略行可执行「更新策略」「查看详情」「历史回测」「成交明细」。')
        tk.Label(window, textvariable=info, bg='#f5f7fb', fg='#475569', anchor='w', wraplength=1280).pack(fill='x', padx=12, pady=(2, 4))

        cached_items: dict[str, dict] = {}

        def _display_name(item: dict) -> str:
            sid = item.get('id') or ''
            real_name = item.get('name')
            if real_name and str(real_name).strip():
                return str(real_name).strip()
            if fallback_name_fn:
                fb = fallback_name_fn(sid)
                if fb and fb != sid:
                    return fb
            return f'名称待完善（{sid}）'

        def selected():
            sel = grid.selection()
            sid = sel[0] if sel else grid.focus()
            if not sid:
                raise RuntimeError('请先选择一条策略')
            row = next((x for x in api_json('/api/v1/strategy-pool').get('items', []) if x['id'] == sid), None)
            if not row:
                raise RuntimeError('策略已变化，请刷新列表')
            return row

        def refresh():
            try:
                items = api_json('/api/v1/strategy-pool').get('items', [])
                cached_items.clear()
                for iid in grid.get_children():
                    grid.delete(iid)
                status_text = {
                    'REGISTERED': '已注册（未启用）',
                    'REGISTERED_PAUSED': '已暂停',
                    'PAUSED': '已暂停',
                    'ENABLED_NOT_RUNNING': '等待自然周期',
                    'RUNNING': '正常运行',
                    'MARKET_RECOVERING': '行情保护',
                    'FAULTED': '运行异常',
                    'ARCHIVED': '已归档',
                }
                for item in items:
                    sid = item['id']
                    cached_items[sid] = item
                    pos = json.loads(item['position']) if item.get('position') else None
                    if pos and pos.get('pending_entry'):
                        position = '待开仓'
                    elif pos:
                        d_cn = '做空' if str(pos.get('direction', 'LONG')).upper() == 'SHORT' else '做多'
                        position = f"{d_cn} {_fmt_qty(pos.get('qty'))}"
                    else:
                        position = '空仓'
                    version = item.get('active_version') or item.get('latest_version') or (pos or {}).get('version') or '—'
                    lev = leverage_label_fn(item.get('leverage'), item.get('position_direction')) if leverage_label_fn else (f"{item.get('leverage')}x" if item.get('leverage') else '—')
                    grid.insert(
                        '', 'end', iid=sid,
                        values=(
                            sid,
                            _display_name(item),
                            version,
                            status_text.get(item.get('status'), item.get('status') or '—'),
                            position,
                            _fmt_money(item.get('position_amount', '0')),
                            lev,
                            _fmt_money(item.get('equity')),
                            _fmt_money(item.get('realized', '0'), signed=True),
                            _fmt_money(item.get('unrealized', '0'), signed=True),
                            _fmt_money(item.get('fees', '0')),
                            item.get('action') or '—',
                            _fmt_local_time(item.get('last_cycle')),
                        ),
                    )
                info.set(f'共 {len(items)} 条策略。双击策略行打开账户与成交面板；右键点击策略行执行「更新策略」等操作。')
            except Exception as exc:
                info.set('读取策略池失败：' + str(exc))

        def download_template():
            from scripts.strategy_template_bundle import TEMPLATE_ZIP_NAME, export_strategy_template_zip
            dest = filedialog.asksaveasfilename(
                parent=window, title='保存 BTC V3 策略开发模板包', initialfile=TEMPLATE_ZIP_NAME,
                defaultextension='.zip', filetypes=[('ZIP 策略模板包', '*.zip')],
            )
            if not dest:
                return
            try:
                saved = export_strategy_template_zip(dest)
                messagebox.showinfo(
                    '模板包已导出',
                    f'已导出至：\n{saved}\n\n包含文件：\n'
                    '• strategy.template.json（空白策略模板，不可直接启用）\n'
                    '• strategy.schema.json（JSON Schema 格式校验规范）\n'
                    '• 策略开发与上传规范.md（完整开发与接入规范）\n'
                    '• AI策略开发指令.md（AI Agent 策略开发标准指令）',
                    parent=window,
                )
                info.set(f'已导出策略开发模板包：{saved.name}')
            except Exception as exc:
                messagebox.showerror('导出模板失败', str(exc), parent=window)

        def add_new_strategy():
            path = filedialog.askopenfilename(parent=window, title='选择新策略 JSON 文件', filetypes=[('JSON 策略', '*.json')])
            if not path:
                return
            try:
                raw = Path(path).read_bytes()
                result = api_json('/api/v1/strategy-pool/upload', method='POST', raw=raw)
                document = result['document']
                diff = result.get('version_diff') or {}
                changes = diff.get('changes') or []
                lines = [
                    f"校验：{'通过' if result.get('validation', {}).get('valid') else '失败'}",
                    f"策略 ID：{result['strategy_id']}",
                    f"中文名称：{document.get('name') or '—'}",
                    f"版本：{result['version']}",
                    f"市场/周期：{document.get('market')} / {document.get('timeframe')}",
                    f"SHA-256：{result['sha256']}",
                    '状态：已注册，默认暂停（可在列表中启用）',
                    '版本差异：' + ('；'.join(x['field'] for x in changes) if changes else '首个版本'),
                ]
                messagebox.showinfo('新增策略校验通过', '\n'.join(lines), parent=window)
                refresh()
            except Exception as exc:
                messagebox.showerror('添加策略失败', str(exc), parent=window)

        def _row_for_detail(item: dict) -> dict:
            return {
                'id': item['id'],
                'strategy_id': item['id'],
                'name': _display_name(item),
                'active_version': item.get('active_version') or item.get('latest_version'),
                'version': item.get('active_version') or item.get('latest_version'),
                'enabled': bool(item.get('enabled')),
                'status_label': item.get('status'),
                'raw': item,
                'is_pool_strategy': True,
            }

        def view_rule_details():
            try:
                item = selected()
                open_rule_details(root, item['id'], _row_for_detail(item))
            except Exception as exc:
                messagebox.showerror('无法查看策略详情', str(exc), parent=window)

        def view_account_panel():
            try:
                item = selected()
                open_account_details(root, item['id'], _row_for_detail(item))
            except Exception as exc:
                messagebox.showerror('无法查看账户面板', str(exc), parent=window)

        def update_selected():
            try:
                item = selected()
                open_update_strategy_dialog(window, item, api_json=api_json, on_updated=refresh)
            except Exception as exc:
                messagebox.showerror('无法更新策略', str(exc), parent=window)

        def backtest_selected():
            try:
                item = selected()
                open_backtest_dialog(window, item, api_json=api_json)
            except Exception as exc:
                messagebox.showerror('无法打开历史回测', str(exc), parent=window)

        def fills_selected():
            try:
                item = selected()
                raw_fills = api_json(f"/api/v1/strategy-pool/{item['id']}/fills?limit=2000").get('items', [])
                open_fills_table_window(window, strategy_id=item['id'], strategy_name=_display_name(item), raw_fills=raw_fills, source_kind='paper')
            except Exception as exc:
                messagebox.showerror('读取成交明细失败', str(exc), parent=window)

        def toggle_selected():
            try:
                row = selected()
                is_enabled = bool(row.get('enabled'))
                versions = api_json(f"/api/v1/strategy-pool/{row['id']}/versions").get('items', [])
                version = row.get('active_version') or (versions[-1]['version'] if versions else None)
                if not version:
                    raise RuntimeError('没有可启用版本')
                if is_enabled and not messagebox.askyesno('暂停策略', f"确定暂停 {_display_name(row)}（{row['id']}）？\n将停止新开仓，已有仓位仍按原开仓版本管理。", parent=window):
                    return
                result = api_json(f"/api/v1/strategy-pool/{row['id']}/enable", method='POST', payload={'version': version, 'enabled': not is_enabled})
                info.set(f"{_display_name(row)} 状态已更新为：{result.get('status')}。")
                refresh()
            except Exception as exc:
                messagebox.showerror('启停操作失败', str(exc), parent=window)

        def activate_version():
            try:
                row = selected()
                versions = api_json(f"/api/v1/strategy-pool/{row['id']}/versions").get('items', [])
                if not versions:
                    raise RuntimeError('没有可启用版本')
                choices = [x['version'] for x in versions]
                version = simpledialog.askstring('选择策略版本', f"策略：{_display_name(row)}（{row['id']}）\n可用版本：{', '.join(choices)}\n输入要启用的版本：", initialvalue=choices[-1], parent=window)
                if not version:
                    return
                if version not in choices:
                    raise RuntimeError('版本不在历史列表中')
                if not messagebox.askyesno('切换策略版本', f'将 {row["id"]} 的后续新开仓版本切换为 {version}？\n现有持仓仍按原开仓版本管理。', parent=window):
                    return
                result = api_json(f"/api/v1/strategy-pool/{row['id']}/enable", method='POST', payload={'version': version, 'enabled': True})
                info.set(f"版本已切换至 {version}，当前状态：{result.get('status')}。")
                refresh()
            except Exception as exc:
                messagebox.showerror('版本切换失败', str(exc), parent=window)

        def show_diff():
            try:
                row = selected()
                versions = api_json(f"/api/v1/strategy-pool/{row['id']}/versions").get('items', [])
                if len(versions) < 2:
                    raise RuntimeError('该策略目前只有一个版本，暂无历史版本差异')
                old, new = versions[-2]['version'], versions[-1]['version']
                diff = api_json(f"/api/v1/strategy-pool/{row['id']}/diff?old={old}&new={new}")
                diff_win = tk.Toplevel(window)
                diff_win.title(f"版本差异 · {_display_name(row)}（{old} -> {new}）")
                diff_win.geometry('760x500')
                txt = tk.Text(diff_win, wrap='word', font=('Consolas', 9), bg='white', fg='#0f172a', padx=10, pady=10)
                txt.pack(fill='both', expand=True)
                txt.insert('1.0', json.dumps(diff, ensure_ascii=False, indent=2))
                txt.configure(state='disabled')
            except Exception as exc:
                messagebox.showerror('无法查看版本差异', str(exc), parent=window)

        # BUG-002: Right-click context menu bound to clicked strategy row
        ctx_menu = tk.Menu(window, tearoff=0)
        ctx_menu.add_command(label='查看策略详情', command=view_rule_details)
        ctx_menu.add_command(label='查看账户与成交面板', command=view_account_panel)
        ctx_menu.add_separator()
        ctx_menu.add_command(label='更新策略（上传新版本）', command=update_selected)
        ctx_menu.add_command(label='查看版本差异', command=show_diff)
        ctx_menu.add_command(label='切换 / 回滚历史版本', command=activate_version)
        ctx_menu.add_separator()
        ctx_menu.add_command(label='历史回测', command=backtest_selected)
        ctx_menu.add_command(label='成交明细', command=fills_selected)
        ctx_menu.add_command(label='启用 / 暂停', command=toggle_selected)

        def on_right_click(event):
            iid = grid.identify_row(event.y)
            if not iid:
                return
            grid.selection_set(iid)
            grid.focus(iid)
            try:
                ctx_menu.tk_popup(event.x_root, event.y_root)
            finally:
                ctx_menu.grab_release()

        grid.bind('<Button-3>', on_right_click)
        grid.bind('<Double-1>', lambda _e: view_account_panel())

        # Bottom action bar (NO bottom "更新策略（上传新版本）" button per BUG-002!)
        buttons = tk.Frame(window, bg='#f5f7fb')
        buttons.pack(fill='x', padx=8, pady=(4, 10))
        tk.Button(
            buttons, text='下载模板', command=download_template,
            bg='#1d5ef0', fg='#ffffff', activebackground='#164bc4', activeforeground='#ffffff',
            relief='flat', padx=12, pady=4, font=('Microsoft YaHei UI', 9, 'bold'), cursor='hand2',
        ).pack(side='left', padx=(4, 6))
        ttk.Button(buttons, text='添加策略文件', command=add_new_strategy).pack(side='left', padx=4)
        ttk.Button(buttons, text='查看详情', command=view_rule_details).pack(side='left', padx=4)
        ttk.Button(buttons, text='账户面板', command=view_account_panel).pack(side='left', padx=4)
        ttk.Button(buttons, text='历史回测', command=backtest_selected).pack(side='left', padx=4)
        ttk.Button(buttons, text='成交明细', command=fills_selected).pack(side='left', padx=4)
        ttk.Button(buttons, text='启用 / 暂停', command=toggle_selected).pack(side='left', padx=4)
        ttk.Button(buttons, text='查看版本差异', command=show_diff).pack(side='left', padx=4)
        ttk.Button(buttons, text='切换版本', command=activate_version).pack(side='left', padx=4)
        ttk.Button(buttons, text='刷新', command=refresh).pack(side='right', padx=4)

        refresh()
        return window

    return _focus_or_track('strategy_pool_main', _build)
