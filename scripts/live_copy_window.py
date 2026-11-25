"""Isolated native three-page V2 configuration window."""
import json
import os
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
import tkinter as tk
from tkinter import ttk, messagebox
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import ctypes
import sqlite3
from contextlib import closing, contextmanager


BEIJING = timezone(timedelta(hours=8))


def money(value):
    if value is None:
        return '—'
    rounded = Decimal(value).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
    return f'{rounded if rounded else abs(rounded):,.2f} USDT'


def pnl_tag(value):
    return 'gain' if value is not None and value>0 else 'loss' if value is not None and value<0 else 'flat'


def pnl_color(value):
    return {'gain':'#15803d','loss':'#b91c1c','flat':'#182c50'}[pnl_tag(value)]


def color_rows(tree):
    for name in ('gain','loss','flat'):
        tree.tag_configure(name,foreground={'gain':'#15803d','loss':'#b91c1c','flat':'#182c50'}[name])

event_reasons = {
    'MANUAL_PAUSE':'手动暂停新开仓','PAUSED':'暂停来源未记录','STOP_PENDING':'正在安全停止',
    'STARTUP_RECONCILIATION':'启动对账','NETWORK_RECOVERY':'网络恢复对账','BUDGET_LIMIT':'超出预算',
    'FILL_OUTSIDE_60_SECOND_WINDOW':'成交超出 60 秒时限或时间超前',
    'PAPER_POSITION_ALREADY_CLOSED':'纸盘关联仓位已平仓','ACTIVE_FOLLOWER_POSITION':'已有活动跟单仓位',
    'MINIMUM_ORDER_REQUIREMENTS':'未满足最小委托要求','NO_ACTIVE_LINKED_POSITION':'没有活动关联仓位',
    'NO_ASSOCIATED_POSITION':'没有关联仓位','UNSUPPORTED_EXECUTION_ACTION:PARTIAL_CLOSE':'当前执行器未执行部分退出',
    'ORIGINAL_OPEN_NOT_FOLLOWED':'原开仓未跟单，无需平仓',
}

def paper_event_label(fill):
    action={'OPEN':'开','FULL_CLOSE':'平','PARTIAL_CLOSE':'部分平','ADD':'加'}
    return action.get(fill['action'],fill['action'])+{'LONG':'多','SHORT':'空'}.get(fill['direction'],'')


def demo_copy_data(directory, *, chart=False):
    """Read exact fills and paper links scoped to one configuration."""
    data = dict(active=[],trades=[],triggers=[],chart=[],today=None,total=None,action='等待成交')
    ledger = directory/'c-v31-demo-execution.sqlite3'
    links, events = {}, {}
    inbox = directory/'demo-execution-inbox.sqlite3'
    if inbox.is_file():
        with closing(sqlite3.connect(inbox.as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
            has_details = db.execute("SELECT 1 FROM sqlite_master WHERE name='skipped_event_details'").fetchone()
            detail_rows = dict((event,(reason,stamp)) for event,reason,stamp in
                               db.execute('SELECT event,reason,decided_at FROM skipped_event_details')) if has_details else {}
            links = {json.loads(raw)['client_order_id']:trade for trade,raw in
                     db.execute('SELECT trade,intent FROM linked_orders')}
            for event,raw,result,plan,status in db.execute("""SELECT i.event_id,i.payload,e.result,i.result,i.status FROM inbox i
                LEFT JOIN executed_events e ON e.event=i.event_id ORDER BY i.cursor"""):
                row=json.loads(raw)
                plan_result=json.loads(plan or '{}')
                reason,_=detail_rows.get(event,(plan_result.get('reason'),None))
                if (result=='SKIPPED' and reason=='NO_ASSOCIATED_POSITION'
                        and row['action'] in ('FULL_CLOSE','PARTIAL_CLOSE')
                        and row['trade_id'] not in links.values()):
                    reason='ORIGINAL_OPEN_NOT_FOLLOWED'
                if result=='SKIPPED_MIN_ORDER' and not reason:
                    reason='MINIMUM_ORDER_REQUIREMENTS'
                if result=='SKIPPED' and not reason:
                    reason='旧版本未记录原因'
                data['triggers'].append((event,row,status,result,reason))
                if row['action']=='OPEN' and result in ('PROTECTED','FOLLOW_PAPER','RECOVERED'):
                    events[row['trade_id'],'开仓']=event.split(':',1)[-1]
                if row['action']=='FULL_CLOSE' and result=='CLOSED':
                    events[row['trade_id'],'平仓']=event.split(':',1)[-1]
    if not ledger.is_file():
        return data
    with closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        for client_id,state,raw in db.execute('SELECT id,state,evidence FROM cycles ORDER BY rowid'):
            e=json.loads(raw)
            trade=links.get(client_id)
            direction=e.get('intent',{}).get('direction')
            qty=Decimal(e.get('filled','0'))
            if state in ('FOLLOW_PAPER','PROTECTED','PARTIAL_PROTECTED') and qty>0 and trade:
                data['active'].append((direction,qty,Decimal(e['average'])))
                data['action']=f"已开仓 #{e.get('order_id','—')}"
            elif state not in ('CLOSED','NO_FILL'):
                data['action']='执行状态待核对'
            for effect,receipt_key in [('开仓','entry_trades'),('平仓','exit_trades')]:
                groups={}
                for fill in e.get(receipt_key,[]):
                    if fill.get('orderId') is not None and fill.get('time') is not None:
                        key = (fill['orderId'], fill['time']) if chart else (fill['orderId'], None)
                        groups.setdefault(key,[]).append(fill)
                for index,((oid,_),fills) in enumerate(sorted(groups.items(),key=lambda pair:
                        max(int(fill['time']) for fill in pair[1]))):
                    q=sum(Decimal(x['qty']) for x in fills)
                    amount=sum(Decimal(x['qty'])*Decimal(x['price']) for x in fills)
                    fee=(sum(Decimal(x['commission']) for x in fills)
                         if all(x.get('commissionAsset')=='USDT' for x in fills) else None)
                    stamp=datetime.fromtimestamp(max(int(x['time']) for x in fills)/1000,BEIJING)
                    net=e.get('net_pnl_before_funding_usdt') if effect=='平仓' and index==len(groups)-1 else None
                    if chart and trade and direction in ('LONG','SHORT'):
                        opening = effect=='开仓'
                        side = 'BUY' if opening == (direction=='LONG') else 'SELL'
                        if all(f['side']==side and f.get('symbol')=='BTCUSDT'
                               and f.get('positionSide') in (direction,'BOTH') for f in fills):
                            data['chart'].append(dict(paper_trade_id=trade,order_id=str(oid),
                                fill_timestamp=stamp.isoformat(),action='OPEN' if opening else 'CLOSE',
                                position_side=direction,execution_side=side,filled_price=str(amount/q),
                                filled_quantity=str(q),filled_notional_usdt=str(amount),
                                fee_usdt=None if fee is None else str(fee),
                                realized_pnl_usdt=str(sum(Decimal(f['realizedPnl']) for f in fills))
                                if all(f.get('realizedPnl') is not None for f in fills) else None,
                                net_pnl_before_funding_usdt=net))
                    if net is not None:
                        net=Decimal(net)
                        data['total']=(data['total'] or Decimal(0))+net
                        if stamp.date()==datetime.now(BEIJING).date():
                            data['today']=(data['today'] or Decimal(0))+net
                    data['trades'].append((stamp,effect,fills[0]['side'],direction,q,amount/q,
                        amount,fee,net,events.get((trade,effect),'—'),oid))
                    if effect=='平仓':
                        data['action']=f'已平仓 #{oid}'
    data['trades'].sort(key=lambda row:row[0],reverse=True)
    return data

ROOT = Path(os.environ['LOCALAPPDATA']) / 'BTC Paper Lab V3 Demo Acceptance'
CONFIG = ROOT / 'copy-v2-draft.json'
RUNTIME = ROOT / 'dry-v2-service'


def main(owner=None, *, paper_rows=lambda: {}):
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
    root.geometry('1120x720')
    root.minsize(880, 640)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('TFrame', background='#f0f5ff')
    style.configure('TLabel', background='#f0f5ff', foreground='#182c50')
    style.configure('TLabelframe', background='#f0f5ff', bordercolor='#c8daf6')
    style.configure('TLabelframe.Label', background='#f0f5ff', foreground='#163666')
    style.configure('Treeview.Heading', background='#e0ebfc', foreground='#182c50')
    style.configure('TButton', padding=(15, 9))
    style.configure('Primary.TButton', background='#1558ef', foreground='white')
    style.configure('Treeview', rowheight=42, font=('Microsoft YaHei UI', 11))
    root.configure(bg='#edf3fd')
    draft = {}
    if CONFIG.exists():
        try:
            draft = json.loads(CONFIG.read_text(encoding='utf8'))
        except (OSError, ValueError):
            pass
    supported = {'A','B','C','G'}
    project = Path(__file__).resolve().parents[1]

    def adapter_status(sid):
        if sid in supported:
            return True, ''
        if sid.startswith('pa-'):
            from scripts.live_copy_v2 import paper_fills
            import sqlite3
            try:
                if paper_fills(project/'data/strategy-pool.sqlite3',strategy=sid):
                    return True, ''
                if sid=='pa-trend-false-break-reclaim':
                    return True, '统一流水适配已隔离验证，尚无自然成交'
                return False, '正式账本暂无可核实成交'
            except (ValueError, ArithmeticError, KeyError, OSError, sqlite3.Error) as exc:
                if str(exc).startswith('PA_FULL_CLOSE_MISMATCH:'):
                    number=str(exc).split(':',1)[1]
                    return False, f'第 {number} 条平仓的买卖方向、数量或版本与开仓不符'
                return False, f'成交关联未核实（{exc}）'
        if sid in ('PA-09','PA-10'):
            return False, '独立账本成交缺少方向及生命周期关联；平仓后持仓快照无法还原历史方向'
        return False, '尚无经过验证的开平仓及交易关联适配器'
    defaults = {'strategy':'', 'mode':'定额跟单','fixed':'20','budget':'100','leverage':'5',
                'stop':'','tp':'','emergency':'','maximum':'','slippage':'','account':'Binance USDⓈ-M Demo'}
    draft['account'] = defaults['account']
    draft['mode'] = defaults['mode']
    defaults.update(follow_tp='1', follow_sl='1', custom_tp='0', custom_sl='0', flatten_on_stop='0')
    demo_policy = dict(environment='BINANCE_DEMO', execution_mode='DEMO', copy_mode='FIXED',
                       production_order_enabled=False)
    fields = {k:tk.StringVar(value=draft.get(k,v)) for k,v in defaults.items()}
    library = ROOT/'copy-configs.json'
    configs = {}
    selected_id = None
    table = None
    pnl_cells = {}
    controls = {}
    demo_transition = False
    queue_check = None
    queue_panel = None
    queue_cancel = None
    chart_requested_strategy = None
    balance_mode = None
    active_states = ('MONITORING','PAUSED','DEMO_RUNNING','DEMO_PAUSED','DEMO_STOP_PENDING','EXCHANGE_UNKNOWN',
                     'DEMO_RECONNECTING','DEMO_NETWORK_RECOVERY','DEMO_RECONCILING')
    names = {'DEMO_RUNNING':'Demo跟单运行中','MONITORING':'只读监测中',
             'PAUSED':'已暂停新开仓','DEMO_PAUSED':'已暂停新开仓',
             'DEMO_STOP_PENDING':'等待安全停止','STOPPED':'已停止',
             'RECONCILE_REQUIRED':'对账异常·暂停开仓','EXCHANGE_UNKNOWN':'交易所状态待确认',
             'PROTECTION_INCOMPLETE':'止损保护异常','DEMO_RECONNECTING':'网络重连中',
             'DEMO_NETWORK_RECOVERY':'网络恢复中·暂停新开仓','DEMO_RECONCILING':'对账中'}

    def persist():
        ROOT.mkdir(parents=True,exist_ok=True)
        tmp = library.with_suffix('.tmp')
        tmp.write_text(json.dumps(configs,ensure_ascii=False),encoding='utf8')
        tmp.replace(library)

    def saved_config_matches(cid,config):
        return (configs.get(cid)==config and
                json.loads(library.read_text(encoding='utf8')).get(cid)==config)

    if library.exists():
        configs = json.loads(library.read_text(encoding='utf8'))
    else:
        legacy = {}
        for path in (ROOT/'copy-presets').glob('*.json'):
            row = json.loads(path.read_text(encoding='utf8'))
            legacy[row['strategy']] = row
        if draft.get('strategy'):
            legacy[draft['strategy']] = draft
        for sid,row in legacy.items():
            cid = uuid4().hex
            configs[cid] = {**defaults,**row,'copy_config_id':cid,'created_at':datetime.now().isoformat(),
                            **demo_policy,'archived_at':None}
        old = ROOT/'continuous-copy'/'demo-execution-inbox.sqlite3'
        if old.is_file():
            with closing(sqlite3.connect(old.as_uri()+'?mode=ro',uri=True)) as db:
                source,fixed,budget = db.execute('SELECT source,fixed,budget FROM subscription').fetchone()
            for cid,row in configs.items():
                sid = row['strategy']
                if ((source.endswith('#'+sid) or sid=='C' and source.endswith('paper.sqlite3'))
                        and Decimal(row['fixed'])==Decimal(fixed) and Decimal(row['budget'])==Decimal(budget)):
                    row['legacy_demo_ledger']=True
        if configs:
            persist()
    page = ttk.Frame(root, padding=18)
    page.pack(fill='both', expand=True)
    account_line = tk.StringVar(value='正在读取 Demo 余额…')
    service_line = tk.StringVar(value='STOPPED')
    status_note = tk.StringVar()
    control_note = tk.StringVar()
    queue_note = tk.StringVar()
    mode_note = tk.StringVar()
    pnl_label = tk.StringVar(value='今日实盘净盈亏')
    pnl_value = tk.StringVar(value='暂无记录')
    demo_positions = {}
    mark_received = 0
    last_mark_request = 0
    pnl_widget = None
    pool = ThreadPoolExecutor(max_workers=1)
    root.bind('<Destroy>', lambda e: pool.shutdown(wait=False) if e.widget is root else None)

    def refresh_account():
        nonlocal balance_mode
        if balance_mode == 'READING':
            return
        balance_mode = 'READING'
        def query():
            from scripts.live_center import dpapi
            from scripts.live_c_demo_exchange import DemoExchange
            try:
                key,secret = json.loads(dpapi((ROOT/'credentials.dpapi').read_bytes(),decrypt=True))
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError('本机 Demo 凭据不可用') from exc
            demo = DemoExchange(key,secret)
            demo.synchronize()
            asset = next((a for a in demo.account()['assets'] if a['asset']=='USDT'),None)
            if asset is None:
                raise ValueError('账户响应缺少 USDT')
            return money(asset['availableBalance'])
        account_line.set('正在读取 Demo 余额…')
        future = pool.submit(query)
        def finish():
            nonlocal balance_mode
            if not future.done():
                root.after(150, finish)
                return
            balance_mode = None
            try:
                account_line.set(f"Demo {future.result()} · {datetime.now():%H:%M:%S}")
            except Exception as exc:
                account_line.set(f'Demo 余额读取失败：{exc} · 点击刷新账户')
        root.after(150, finish)

    def demo_directory(cid):
        directory = ROOT/'continuous-copy'
        if configs[cid].get('legacy_demo_ledger'):
            if sum(bool(config.get('legacy_demo_ledger')) for config in configs.values())!=1:
                raise ValueError('多个配置关联同一旧账本，归属待核对。')
            return directory
        return directory/cid

    def refresh_marks():
        nonlocal last_mark_request
        if time.monotonic()-last_mark_request<15:
            return
        last_mark_request=time.monotonic()
        def query():
            from scripts.live_center import dpapi
            from scripts.live_c_demo_exchange import DemoExchange
            key,secret=json.loads(dpapi((ROOT/'credentials.dpapi').read_bytes(),decrypt=True))
            exchange=DemoExchange(key,secret)
            exchange.synchronize()
            rows=exchange._request('GET','/fapi/v3/positionRisk',{'symbol':'BTCUSDT'})
            return {('LONG' if Decimal(r['positionAmt'])>0 else 'SHORT'):
                    r for r in rows if r['symbol']=='BTCUSDT' and Decimal(r['positionAmt'])}
        future=pool.submit(query)
        def finish():
            nonlocal demo_positions,mark_received
            if not future.done():
                root.after(150,finish)
                return
            try:
                demo_positions=future.result()
                mark_received=time.monotonic()
            except Exception:
                demo_positions={}
                mark_received=0
        root.after(150,finish)

    def metrics(data, *, owned):
        if not data['active']:
            return '—','—',None,None,'—'
        if len(data['active'])!=1:
            return '待核对','价格待更新',None,None,'—'
        direction,qty,entry=data['active'][0]
        name={'LONG':'做多','SHORT':'做空'}.get(direction,'待核对')
        row=demo_positions.get(direction) if owned and time.monotonic()-mark_received<=30 else None
        if row and abs(Decimal(row['positionAmt']))==qty and Decimal(row['entryPrice'])==entry:
            mark=Decimal(row['markPrice'])
            if mark.is_finite() and mark>0:
                return name,money(qty*mark),Decimal(row['unRealizedProfit']),Decimal(row['initialMargin']),f'{qty:f} BTC'
        return name,'价格待更新',None,None,f'{qty:f} BTC'

    def chart_snapshot():
        current = running()
        items, positions, errors = [], [], []
        registered = set()
        for cid, config in list(configs.items()):
            strategy = config['strategy'].upper()
            registered.add(strategy)
            try:
                directory = demo_directory(cid)
                data = demo_copy_data(directory,chart=True)
                items.extend(dict(fill,strategy_id=strategy,copy_config_id=cid) for fill in data['chart'])
                if (current.get('mode')=='DEMO' and current.get('copy_config_id')==cid
                        and current.get('strategy','').upper()==strategy):
                    fresh_marks = time.monotonic()-mark_received<=30
                    fresh_reconciliation = (current.get('state') in ('DEMO_RUNNING','DEMO_PAUSED')
                        and 0 <= time.time()-current.get('last_reconciled_at',0)<=45)
                    for side, qty, price in data['active']:
                        row = demo_positions.get(side) if fresh_marks else None
                        matches = row and abs(Decimal(row['positionAmt']))==qty and Decimal(row['entryPrice'])==price
                        if matches or (not fresh_marks and fresh_reconciliation):
                            positions.append(dict(strategy_id=strategy,copy_config_id=cid,position_side=side,
                                quantity=str(qty),price=str(price),unrealized_pnl=row.get('unRealizedProfit') if matches else None))
            except (OSError, sqlite3.Error, ValueError, KeyError, ArithmeticError, TypeError):
                errors.append(strategy)
        return dict(environment='demo',strategies=sorted(registered),items=items,positions=positions,
                    errors=errors,selected_strategy=chart_requested_strategy)

    def open_selected_kline():
        nonlocal chart_requested_strategy
        from scripts.kline_window import open_live_kline
        config=configs.get(selected_config(),{})
        chart_requested_strategy=config.get('strategy','').upper() or None
        open_live_kline(root,demo_snapshot=chart_snapshot)

    def running():
        try:
            row = json.loads((RUNTIME/'status.json').read_text(encoding='utf8'))
            pid = row.get('pid')
            if row['state'] in active_states and pid:
                if not service_pid_alive(pid):
                    if row['state']=='EXCHANGE_UNKNOWN':
                        row['state']='RECONCILE_REQUIRED'
                    else:
                        row['state']='STOPPED'
            elif row['state'] in active_states and time.time()-row['time']>60:
                row['state']='STOPPED'
            return row
        except (OSError, ValueError, KeyError, TypeError):
            return {'state':'STOPPED'}

    def state():
        return running()['state']

    def update_state():
        row=running()
        queued = read_switch()
        queue_note.set((f"等待当前策略 {queued['from_strategy']} 平仓后切换至 {queued['target']['strategy']} "
                        f"· 配置 {queued.get('target_id','待核对')} · "
                        f"{queued.get('note','等待原仓退出及对账')}") if queued else '')
        if queue_panel is not None:
            if queued:
                queue_panel.pack(fill='x')
                queue_cancel.configure(state='disabled' if queued['state'] in ('STARTING','STOPPING')
                    or row.get('state')=='DEMO_STOP_PENDING' else 'normal')
            else:
                queue_panel.pack_forget()
        service_line.set(names.get(row['state'],'状态待确认') + (' · '+row['strategy'] if row.get('strategy') else ''))
        when = row.get('last_connected_at') or row.get('last_reconciled_at')
        status_note.set(' · '.join(filter(None,(row.get('reason'),
            f'最近成功连接及对账 {datetime.fromtimestamp(when):%H:%M:%S}' if when else None,
            f"重连 {row['reconnect_count']} 次" if row.get('reconnect_count') else None))))
        if demo_transition:
            status_note.set('正在核对 Demo 账户或等待跟单服务切换…')
        is_demo = row.get('mode')=='DEMO'
        mode_note.set('Binance Demo 模拟资金跟单。生产下单关闭。' if is_demo else
                      '只读监测按纸盘成交生成计划。生产下单关闭。')
        pnl_label.set('今日 Demo 已平仓净盈亏（资金费前）')
        if table is not None and table.winfo_exists():
            demo_total = Decimal(0)
            has_demo_total = False
            for cid in table.get_children():
                table.set(cid,'状态',row_status(cid,row))
                owned=is_demo and row.get('copy_config_id')==cid and row.get('strategy')==configs[cid]['strategy']
                try:
                    data=demo_copy_data(demo_directory(cid))
                    if owned and data['active']:
                        refresh_marks()
                    direction,notional,_,_,_=metrics(data,owned=owned)
                    total=data['today']
                    recent = data['triggers'][-1] if data['triggers'] else None
                    if recent:
                        _, fill, _, _, reason = recent
                        action=f"纸盘 {paper_event_label(fill)} · {event_reasons.get(reason,reason) or 'Demo '+data['action']}"
                    else:
                        action=data['action']
                except (OSError,sqlite3.Error,ValueError,ArithmeticError,TypeError):
                    direction,notional,action,total='待核对','—','执行账本读取失败',None
                table.set(cid,'持仓方向',direction)
                table.set(cid,'持仓金额（USDT）',notional)
                table.set(cid,'盈亏',money(total))
                table.set(cid,'最近动作',action)
                emphasis = ('demo-active' if owned and row['state']=='DEMO_RUNNING' else
                            'copy-warning' if queued and queued.get('target_id')==cid else
                            'copy-warning' if row.get('copy_config_id')==cid and row['state'] in
                            ('DEMO_PAUSED','PAUSED','RECONCILE_REQUIRED','PROTECTION_INCOMPLETE') else '')
                table.item(cid,tags=(emphasis,) if emphasis else ())
                if cid not in pnl_cells:
                    label=tk.Label(table,anchor='e',padx=3,bd=0,font=('Microsoft YaHei UI',9))
                    label.bind('<Button-1>',lambda _e,key=cid:table.selection_set(key))
                    label.bind('<Double-Button-1>',lambda _e,key=cid:trade_details(key))
                    label.bind('<Button-3>',lambda e,key=cid:context_menu(e,table,False,cid=key))
                    pnl_cells[cid]=label
                pnl_cells[cid].configure(text=money(total),foreground=pnl_color(total))
                if total is not None:
                    has_demo_total=True
                    demo_total+=total
            pnl_value.set(money(demo_total) if has_demo_total else '暂无记录')
            if pnl_widget is not None and pnl_widget.winfo_exists():
                pnl_widget.configure(foreground=pnl_color(demo_total if is_demo and has_demo_total else None))
            place_pnl_cells()
        status=row['state']
        cid = selected_config()
        mode={'DRY_RUN':'只读','DEMO':'Binance Demo'}.get(row.get('mode'),'未知模式')
        same = cid is not None and row.get('copy_config_id')==cid
        if demo_transition:
            hint='正在切换，稍候可继续操作。'
        elif queued:
            hint='原服务继续管理已有仓位。可等待对账或取消尚未开始的目标。'
        elif status in ('RECONCILE_REQUIRED','EXCHANGE_UNKNOWN','PROTECTION_INCOMPLETE','DEMO_RECONCILING',
                        'DEMO_RECONNECTING','DEMO_NETWORK_RECOVERY','DEMO_STOP_PENDING'):
            hint=f"{names.get(status,status)}。请等待恢复或核对账户与账本，暂不可启动。"
        elif status in active_states:
            hint=f"当前 {row.get('strategy','未知')} · {mode}。"
            hint+=('可确认切换到 Demo，或停止诊断。' if same and mode=='只读' else
                   '已暂停，可恢复原模式或安全停止。' if same and status in ('PAUSED','DEMO_PAUSED') else
                   '当前配置已运行，可暂停新开仓或安全停止。' if same else
                   '选定其他配置后可申请排队切换。' if mode=='Binance Demo' else '切换其他配置前请先安全停止。')
        else:
            hint='选择一条已适配配置后启用 Demo。' if cid is None else '启用 Demo 需要单独确认，保存配置不启动。'
        control_note.set(hint)
        for name,button in controls.items():
            if button.winfo_exists():
                enabled=(not demo_transition and (not (same and mode=='Binance Demo' and status in active_states)
                         and status in ('STOPPED','MONITORING','PAUSED','DEMO_RUNNING','DEMO_PAUSED','DEMO_STOP_PENDING') if name=='demo' else
                         status=='STOPPED' if name=='run' else
                         status in ('PAUSED','DEMO_PAUSED') if name=='resume' else
                         status in ('MONITORING','DEMO_RUNNING') if name=='pause' else
                         status in active_states and status!='DEMO_STOP_PENDING'))
                button.configure(state='normal' if enabled else 'disabled')
        root.after(1000, update_state)

    def selected_config():
        return table.selection()[0] if table is not None and table.selection() else selected_id

    def place_pnl_cells(_event=None):
        if table is None or not table.winfo_exists():
            return
        for cid,label in pnl_cells.items():
            box=table.bbox(cid,'盈亏')
            if box:
                x,y,width,height=box
                label.configure(background='#dceaff' if cid in table.selection() else
                                style.lookup('Copy.Treeview','background') or '#ffffff')
                label.place(x=x,y=y,width=width,height=height)
            else:
                label.place_forget()

    def row_status(cid,row):
        cfg=configs[cid]
        if row.get('copy_config_id')==cid and row.get('strategy')==cfg['strategy']:
            return {'DEMO_RUNNING':'Demo运行中','MONITORING':'只读监测中',
                    'DEMO_PAUSED':'已暂停新开仓','PAUSED':'已暂停新开仓',
                    'DEMO_STOP_PENDING':'等待安全停止','STOPPED':'已停止',
                    'RECONCILE_REQUIRED':'对账异常·暂停开仓',
                    'EXCHANGE_UNKNOWN':'交易所状态待确认',
                    'PROTECTION_INCOMPLETE':'止损保护异常',
                    'DEMO_RECONNECTING':names['DEMO_RECONNECTING'],
                    'DEMO_NETWORK_RECOVERY':names['DEMO_NETWORK_RECOVERY'],
                    'DEMO_RECONCILING':names['DEMO_RECONCILING']}.get(row['state'],'已保存')
        pending=read_switch()
        if pending and pending.get('target_id')==cid:
            return '等待切换' if pending['state']!='BLOCKED' else '切换待核对'
        return '已保存'

    def send_command(command):
        pending = RUNTIME/f'command.ui-{os.getpid()}.tmp'
        pending.write_text(command,encoding='utf8')
        pending.replace(RUNTIME/'command.txt')

    def preflight_demo(config):
        from hashlib import sha256
        from scripts.live_center import dpapi
        from scripts.live_c_demo_exchange import DemoExchange
        credentials = (ROOT/'credentials.dpapi').read_bytes()
        key,secret = json.loads(dpapi(credentials,decrypt=True))
        exchange = DemoExchange(key,secret)
        exchange.synchronize()
        mode = exchange._request('GET','/fapi/v1/positionSide/dual').get('dualSidePosition')
        if type(mode) is not bool:
            raise ValueError('Demo 持仓模式无法核实。')
        exchange.hedge = mode
        snapshot = exchange.risk_snapshot()
        if any(snapshot[name] for name in ('positions','orders','conditional')):
            raise ValueError('Demo 有持仓、未决普通委托或 Algo 条件单，请保留原仓位管理。')
        settings = snapshot['symbol'][0]
        if (settings['marginType']!='ISOLATED' or int(settings['leverage'])>int(config['leverage'])
                or not snapshot['permissions']['enableFutures']):
            raise ValueError('Demo 交易权限、逐仓模式或实际杠杆未通过检查。')
        aliases = [row.get('accountAlias') for row in exchange._request('GET','/fapi/v3/balance')
                   if row.get('asset')=='USDT']
        if len(aliases)!=1 or not isinstance(aliases[0],str) or not aliases[0].strip():
            raise ValueError('无法核实 Demo 账户身份。')
        return sha256(aliases[0].encode()).hexdigest()[:8],credentials

    def current_demo_identity():
        from hashlib import sha256
        from scripts.live_center import dpapi
        from scripts.live_c_demo_exchange import DemoExchange
        credentials=(ROOT/'credentials.dpapi').read_bytes()
        exchange=DemoExchange(*json.loads(dpapi(credentials,decrypt=True)))
        exchange.synchronize()
        aliases=[r.get('accountAlias') for r in exchange._request('GET','/fapi/v3/balance') if r.get('asset')=='USDT']
        if len(aliases)!=1 or not isinstance(aliases[0],str) or not aliases[0].strip():
            raise ValueError('无法核实 Demo 账户身份。')
        return sha256(aliases[0].encode()).hexdigest()[:8]

    switch_path=RUNTIME/'pending-demo-switch.json'

    def read_switch():
        try:
            value=json.loads(switch_path.read_text(encoding='utf8'))
            if not isinstance(value,dict) or not isinstance(value.get('target'),dict) or not {
                    'from_strategy','state','from_id','target_id','account','source','extra'
            }<=value.keys() or 'strategy' not in value['target']:
                raise ValueError('Invalid queue')
            return value
        except FileNotFoundError:
            return None
        except (OSError,ValueError,TypeError):
            return dict(state='BLOCKED',from_strategy='未知',target={'strategy':'未知'},
                        note='排队记录无法核对，请取消后重新确认')

    def save_switch(value):
        pending=switch_path.with_suffix(f'.{os.getpid()}.tmp')
        pending.write_text(json.dumps(value,ensure_ascii=False),encoding='utf8')
        pending.replace(switch_path)

    @contextmanager
    def switch_lock():
        import msvcrt
        with (RUNTIME/'switch.lock').open('a+b') as claim:
            claim.seek(0)
            msvcrt.locking(claim.fileno(),msvcrt.LK_NBLCK,1)
            yield

    def cancel_switch():
        pending=read_switch()
        if pending and messagebox.askyesno('取消等待切换',
                '仅移除待切换目标。原服务继续管理已有仓位，并保持当前暂停状态。',parent=root):
            try:
                with switch_lock():
                    if (read_switch()!=pending or pending['state'] in ('STARTING','STOPPING')
                            or running()['state']=='DEMO_STOP_PENDING'):
                        raise ValueError('切换已经被认领或状态已变化，请检查服务状态。')
                    switch_path.unlink(missing_ok=True)
            except (OSError,ValueError) as exc:
                messagebox.showerror('等待切换',str(exc),parent=root)

    def queue_confirmation(previous,cid,config,source,extra,details):
        future=pool.submit(current_demo_identity)
        def ready():
            if not future.done():
                root.after(150,ready)
                return
            try:
                identity=future.result()
                prior=read_switch()
                dialog=tk.Toplevel(root)
                dialog.title('切换Demo跟单策略')
                dialog.transient(root)
                decision=[]
                def accept():
                    decision.append(True)
                    dialog.destroy()
                replacement=(f"\n将替换已排队的 {prior['target']['strategy']} 配置。" if prior else '')
                ttk.Label(dialog,wraplength=525,justify='left',padding=15,text=
                    f"当前运行：{previous['strategy']} · {previous.get('copy_config_id')}\n"
                    f"准备启用：{config['strategy']} · {cid}\nBinance Demo 账户校验标识：{identity}\n"
                    f"{details}\n确认后暂停原策略新增开仓，继续管理已有关联仓位。"
                    f"旧仓退出、委托和账本对账成功后自动启用目标。{replacement}").pack(fill='x')
                controls_row=ttk.Frame(dialog,padding=12)
                controls_row.pack(fill='x')
                ttk.Button(controls_row,text='取消并保存',command=dialog.destroy).pack(side='right')
                ttk.Button(controls_row,text='确认切换',command=accept).pack(side='right',padx=6)
                dialog.grab_set()
                root.wait_window(dialog)
                if not decision:
                    return
                row=running()
                with switch_lock():
                    if (row.get('copy_config_id')!=previous.get('copy_config_id') or row.get('mode')!='DEMO'
                            or read_switch()!=prior or not saved_config_matches(cid,config)
                            or prior and prior['state'] in ('STARTING','STOPPING')):
                        raise ValueError('确认期间活动服务或配置已变化，请重新确认。')
                    save_switch(dict(from_id=previous['copy_config_id'],from_strategy=previous['strategy'],
                        target_id=cid,target=config,source=str(source),extra=extra,account=identity,state='WAITING'))
                    send_command('pause')
            except (OSError,ValueError,RuntimeError,KeyError) as exc:
                messagebox.showerror('等待切换',str(exc),parent=root)
        root.after(150,ready)

    def advance_switch():
        nonlocal queue_check
        try:
            pending=read_switch()
            if not pending:
                return
            row=running()
            if pending['state']=='STARTING':
                if row.get('copy_config_id')==pending['target_id'] and row.get('state')=='DEMO_RUNNING':
                    with switch_lock():
                        if read_switch()==pending:
                            switch_path.unlink(missing_ok=True)
                elif time.time()-pending.get('started_at',time.time())>60:
                    with switch_lock():
                        if read_switch()==pending:
                            save_switch({**pending,'state':'BLOCKED','note':'启动回执待核对，请检查后台，系统未重复启动'})
                return
            if pending['state'] not in ('WAITING','STOPPING') or queue_check is not None or demo_transition:
                return
            if row.get('copy_config_id')!=pending['from_id'] or row.get('mode')!='DEMO':
                return
            if row['state']=='DEMO_RUNNING' and pending['state']=='WAITING':
                with switch_lock():
                    if read_switch()==pending:
                        send_command('pause')
                return
            paused = row['state']=='DEMO_PAUSED' and pending['state']=='WAITING'
            stopped = row['state']=='STOPPED' and not service_pid_alive(row.get('pid'))
            if not paused and not stopped:
                if pending['state']=='STOPPING' and row['state']=='DEMO_PAUSED':
                    with switch_lock():
                        if read_switch()==pending:
                            send_command('stop')
                return
            new_id=pending['target_id']
            config=pending['target']
            if not saved_config_matches(new_id,config) or config.get('archived_at'):
                with switch_lock():
                    if read_switch()==pending:
                        save_switch({**pending,'state':'BLOCKED','note':'目标配置已改变，请取消并重新确认'})
                return
            if any(demo_ledger_busy(k) for k in configs):
                return
            queue_check=pool.submit(preflight_demo,config)
            def checked():
                nonlocal queue_check
                if not queue_check.done():
                    root.after(150,checked)
                    return
                candidate,queue_check=queue_check,None
                try:
                    if read_switch()!=pending:
                        return
                    identity,_=candidate.result()
                    if identity!=pending['account']:
                        raise ValueError('Demo 账户已更换，请取消并重新确认。')
                    live=running()
                    if (live.get('copy_config_id')!=pending['from_id'] or live['state']!=row['state']
                            or live.get('pid')!=row.get('pid') or
                            (stopped and service_pid_alive(live.get('pid'))) or not saved_config_matches(new_id,config)):
                        return
                    if any(demo_ledger_busy(k) for k in configs):
                        return
                    source=Path(pending['source'])
                    check_demo_cursor(new_id,config['strategy'],source)
                    with switch_lock():
                        if read_switch()!=pending:
                            return
                        if paused:
                            save_switch({**pending,'state':'STOPPING','note':'原仓已对账，等待服务安全退出'})
                            send_command('stop')
                            return
                        save_switch({**pending,'state':'STARTING','started_at':time.time(),'note':'已认领启动，等待后台确认'})
                        spawn_follower(new_id,config,source,pending['extra'])
                except Exception as exc:
                    try:
                        with switch_lock():
                            current=read_switch()
                            if current==pending:
                                save_switch({**pending,'note':str(exc)})
                            elif current and current.get('state')=='STARTING' and current.get('target_id')==new_id:
                                save_switch({**current,'state':'BLOCKED','note':str(exc)})
                    except OSError:
                        return
            root.after(150,checked)
        except (OSError, ValueError):
            pass
        finally:
            root.after(1500,advance_switch)

    def service_pid_alive(pid):
        if not pid:
            return False
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p,ctypes.c_ulong]
        handle = kernel.OpenProcess(0x00100000,False,int(pid))
        if not handle:
            return ctypes.get_last_error()!=87
        try:
            return kernel.WaitForSingleObject(handle,0)!=0
        finally:
            kernel.CloseHandle(handle)

    def spawn_follower(cid, config, source, extra):
        import msvcrt
        with (RUNTIME/'active.lock').open('a+b') as lock:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            except OSError as exc:
                raise ValueError('已有跟单进程持有运行锁，请等待安全停止。') from exc
            send_command('run')
        with (RUNTIME/'service.log').open('ab') as log:
            process = subprocess.Popen([sys.executable,'-m','scripts.live_copy_service',str(source),
                str(RUNTIME/f"plans-{config['strategy']}-{time.time_ns()}.sqlite3"),'--fixed',config['fixed'],
                '--strategy',config['strategy'],'--config-id',cid,'--budget',config['budget'],
                '--runtime-dir',str(RUNTIME),*extra],cwd=Path(__file__).resolve().parents[1],stdin=subprocess.DEVNULL,
                stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
        return process

    def switch_to_demo(cid, config, source, extra, previous, confirmation):
        nonlocal demo_transition
        demo_transition = True
        def finish(error=None):
            nonlocal demo_transition
            demo_transition = False
            if error:
                messagebox.showerror('跟单服务',str(error),parent=root)
        def ownership(row):
            return tuple(row.get(k) for k in ('pid','mode','strategy','copy_config_id'))
        original = ownership(previous)
        def launch(confirmed):
            try:
                row = running()
                if row['state']!='STOPPED' or service_pid_alive(row.get('pid')):
                    raise ValueError('已有跟单服务或待对账状态，请核对后再试。')
                if configs.get(cid)!=config:
                    raise ValueError('核对期间配置已更改，请重新确认。')
                if (ROOT/'credentials.dpapi').read_bytes()!=confirmed[1]:
                    raise ValueError('核对期间 Demo 凭据已更换，请重新确认。')
                if any(demo_ledger_busy(k) for k in configs):
                    raise ValueError('Demo 本地账本仍有未结束交易或未知委托。')
                check_demo_cursor(cid,config['strategy'],source)
                process = spawn_follower(cid,config,source,extra)
                root.after(150,lambda:wait_for_start(process,time.monotonic()+20))
            except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
                finish(exc)
        def wait_for_start(process, deadline):
            row = running()
            if (row.get('pid')!=previous.get('pid') and row.get('mode')=='DEMO'
                    and row.get('copy_config_id')==cid and row.get('strategy')==config['strategy']):
                if row['state']=='DEMO_RUNNING':
                    finish()
                elif row['state'] in ('RECONCILE_REQUIRED','EXCHANGE_UNKNOWN','PROTECTION_INCOMPLETE','STOPPED'):
                    finish(row.get('reason') or names.get(row['state'],row['state']))
                elif time.monotonic()>=deadline:
                    finish()
                else:
                    root.after(150,lambda:wait_for_start(process,deadline))
            elif process.poll() is not None or time.monotonic()>=deadline:
                finish('Demo 未发布启动状态，请查看服务日志。')
            else:
                root.after(150,lambda:wait_for_start(process,deadline))
        def wait_for_stop(deadline,confirmed):
            row = running()
            if ownership(row)!=original:
                finish('跟单服务已更换，取消切换。')
            elif row['state']=='STOPPED' and not service_pid_alive(row.get('pid')):
                recheck = pool.submit(preflight_demo,config)
                def checked_after_stop():
                    if not recheck.done():
                        root.after(150,checked_after_stop)
                        return
                    try:
                        if recheck.result()!=confirmed:
                            raise ValueError('Demo 账户或凭据在切换期间发生变化，请重新确认。')
                        launch(confirmed)
                    except Exception as exc:
                        finish(exc)
                root.after(150,checked_after_stop)
            elif time.monotonic()>=deadline:
                finish('只读跟单尚未安全退出，未启动 Demo。')
            else:
                root.after(150,lambda:wait_for_stop(deadline,confirmed))
        future = pool.submit(preflight_demo,config)
        def checked():
            if not future.done():
                root.after(150,checked)
                return
            try:
                confirmed = future.result()
                account = confirmed[0]
                row = running()
                if ownership(row)!=original:
                    raise ValueError('核对期间跟单服务已更换，请重新确认。')
                if not messagebox.askyesno('明确启用 Binance Demo',
                        f'账户：Binance USDⓈ-M Demo（校验标识 {account}）\n'+confirmation,parent=root):
                    finish()
                    return
                row = running()
                if ownership(row)!=original or configs.get(cid)!=config:
                    raise ValueError('确认期间服务或配置已更换，请重新核对。')
                if previous['state'] in ('MONITORING','PAUSED'):
                    if row['state'] not in ('MONITORING','PAUSED'):
                        raise ValueError('只读服务状态已经变化，请重新确认。')
                    send_command('stop')
                    wait_for_stop(time.monotonic()+12,confirmed)
                else:
                    launch(confirmed)
            except Exception as exc:
                finish(exc)
        root.after(150,checked)

    def control(command):
        try:
            RUNTIME.mkdir(parents=True, exist_ok=True)
            if demo_transition:
                return
            previous = running()
            current = previous['state']
            if command in ('resume','run') and read_switch():
                raise ValueError('已有等待切换，请先取消排队；原仓仍按安全停止流程管理。')
            if command in ('run','demo'):
                cid = selected_config()
                if cid not in configs or configs[cid].get('archived_at'):
                    raise ValueError('请选择一条未归档配置。')
                v = configs[cid]
                can_switch = (command=='demo' and current in ('MONITORING','PAUSED')
                    and previous.get('mode')=='DRY_RUN' and previous.get('copy_config_id')==cid
                    and previous.get('strategy')==v['strategy'])
                can_queue=(command=='demo' and current in ('DEMO_RUNNING','DEMO_PAUSED','DEMO_STOP_PENDING')
                    and previous.get('mode')=='DEMO' and previous.get('copy_config_id')
                    and previous.get('copy_config_id')!=cid)
                if current!='STOPPED' and not can_switch and not can_queue:
                    mode={'DRY_RUN':'只读','DEMO':'Binance Demo'}.get(previous.get('mode'),'未知模式')
                    raise ValueError(f"当前跟单策略 {previous.get('strategy') or '未知'} · {mode} · "
                        f"{names.get(current,current)}。请先安全停止或完成对账。")
                if v['mode'] != '定额跟单':
                    raise ValueError('定比跟单等待保证金数据适配。')
                project = Path(__file__).resolve().parents[1]
                sid = v['strategy']
                ready, reason = adapter_status(sid)
                if not ready:
                    raise ValueError(reason)
                source = project/'data'/('strategy-pool.sqlite3' if sid.startswith('pa-') else
                                         'g/mirror.sqlite3' if sid=='G' else 'paper.sqlite3')
                if not source.is_file():
                    raise ValueError('正式纸盘账本不可用。')
                extra = []
                if command == 'demo':
                    if not can_queue and any(demo_ledger_busy(k) for k in configs):
                        raise ValueError('Demo 本地账本仍有未结束交易或未知委托，请先完成对账。')
                    stop = optional_exit(v,'custom_sl','stop')
                    target = optional_exit(v,'custom_tp','tp')
                    risk = Decimal(v['budget'])*Decimal('.02')
                    warning = ('\n未设置独立止损，纸盘或网络中断时可能无法及时平仓。' if stop is None else '')
                    confirmation = (
                            f"策略：{sid}\n配置 ID：{cid}\n"
                            f"每笔保证金：{money(v['fixed'])}\n总预算：{money(v['budget'])}\n杠杆上限：{v['leverage']}×\n"
                            '退出方式：跟随纸盘止盈止损\n'
                            f"独立止损：{str(stop)+'%' if stop is not None else '关闭'}\n"
                            f"独立止盈：{str(target)+'%' if target is not None else '关闭'}\n"+
                            ('确认后安全停止当前只读监测。\n' if can_switch else '')+
                            '确认后只跟随新纸盘成交，在 Binance Demo 提交虚拟订单。'+warning)
                    extra = ['--authorized-demo-orders','--leverage',v['leverage'],'--risk-cap',str(risk)]
                    if stop is not None:
                        extra += ['--stop-fraction',str(stop/100)]
                    if target is not None:
                        extra += ['--target-fraction',str(target/100)]
                    if v.get('legacy_demo_ledger'):
                        extra += ['--legacy-demo-ledger']
                    if can_queue:
                        queue_confirmation(previous,cid,dict(v),source,extra,
                            f"每笔 {money(v['fixed'])} · 预算 {money(v['budget'])} · {v['leverage']}×")
                    elif read_switch():
                        raise ValueError('已有等待切换，请先取消或等待队列完成。')
                    else:
                        switch_to_demo(cid,dict(v),source,extra,previous,confirmation)
                else:
                    spawn_follower(cid,v,source,extra)
                return
            if command in ('run','demo') or current not in active_states:
                return
            if command=='resume':
                if current not in ('PAUSED','DEMO_PAUSED'):
                    return
                command='run'
            send_command(command)
        except (OSError, KeyError, ValueError, ArithmeticError) as exc:
            messagebox.showerror('跟单服务',str(exc),parent=root)

    def values():
        return {key:value.get() for key,value in fields.items()}

    def optional_exit(v, toggle, key):
        if v.get(toggle) != '1':
            return None
        value = Decimal(v.get(key) or '0')
        if not value.is_finite() or not 0 < value < 100:
            raise ValueError('开启独立止盈或止损后，请填写大于 0 且小于 100 的百分比。')
        return value

    def busy(cid):
        row=running()
        cfg=configs.get(cid,{})
        if ((row.get('copy_config_id')==cid or not row.get('copy_config_id') and
             row.get('strategy')==cfg.get('strategy') and bool(cfg))
                and row['state'] in (*active_states,'RECONCILE_REQUIRED','PROTECTION_INCOMPLETE')):
            return True
        return demo_ledger_busy(cid)

    def demo_ledger_busy(cid):
        cfg=configs.get(cid,{})
        root_dir=ROOT/'continuous-copy'
        directory=root_dir if cfg.get('legacy_demo_ledger') else root_dir/cid
        inbox=directory/'demo-execution-inbox.sqlite3'
        ledger=directory/'c-v31-demo-execution.sqlite3'
        try:
            linked=set()
            if inbox.exists():
                with closing(sqlite3.connect(inbox.as_uri()+'?mode=ro',uri=True)) as db:
                    linked={json.loads(raw)['client_order_id'] for raw, in db.execute('SELECT intent FROM linked_orders')}
            if not ledger.exists():
                return bool(linked)
            with closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True)) as db:
                cycles=dict(db.execute('SELECT id,state FROM cycles'))
                writes=db.execute('SELECT count(*) FROM writes').fetchone()[0]
            return set(cycles)!=linked or bool(writes and not cycles) or any(
                x not in ('CLOSED','NO_FILL') for x in cycles.values())
        except (OSError,sqlite3.Error,ValueError,KeyError):
            return True

    def check_demo_cursor(cid,sid,source):
        from scripts.live_copy_v2 import paper_fills
        inbox=demo_directory(cid)/'demo-execution-inbox.sqlite3'
        if not inbox.is_file():
            return
        with closing(sqlite3.connect(inbox.as_uri()+'?mode=ro',uri=True)) as db:
            expected = str(source.resolve()) if sid=='C' else f'{source.resolve()}#{sid}'
            row=db.execute('SELECT source,cursor FROM subscription WHERE id=1').fetchone()
            if row is None or row[0]!=expected or paper_fills(source,strategy=sid)<row[1]:
                raise ValueError('原 Demo 成交游标与所选策略或账本不一致。')
            pending=db.execute("""SELECT i.payload FROM inbox i LEFT JOIN executed_events e ON e.event=i.event_id
                                  WHERE e.event IS NULL""").fetchall()
        now=datetime.now(timezone.utc)
        def recent_open(action,at):
            return action=='OPEN' and (now-at).total_seconds()<=60
        if any(recent_open(fill['action'],datetime.fromisoformat(fill['occurred_at']))
               for raw, in pending for fill in (json.loads(raw),)):
            raise ValueError('原跟单账本还有近期未处理开仓，避免补单，请等该成交超过安全时限后再试。')
        cursor=row[1]
        while fills := paper_fills(source,cursor,strategy=sid):
            if any(recent_open(fill.action,fill.occurred_at) for fill in fills):
                raise ValueError('存在启用前的近期纸盘开仓，避免补单，请待其超过安全时限后再试。')
            cursor=fills[-1].cursor

    def new_config():
        nonlocal selected_id
        selected_id=uuid4().hex
        for key,value in defaults.items():
            fields[key].set(value)
        render(1)

    def details(cid):
        v=configs[cid]
        messagebox.showinfo('查看跟单配置',
            f"{v['strategy']} · {money(v['fixed'])} / 笔 · {v['leverage']}×\n"
            f"总预算 {money(v['budget'])} · 配置环境 Binance Demo\n"
            f"创建时间 {v['created_at']}\n配置 ID {cid}",parent=root)

    detail_windows = {}

    def trade_details(cid):
        v=configs[cid]
        key = (cid, v['strategy'])
        existing = detail_windows.get(key)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_set()
            return
        window=tk.Toplevel(root)
        detail_windows[key] = window
        window.title(f"{v['strategy']}策略 · Demo跟单交易明细")
        window.geometry('1260x570')
        summary=ttk.Label(window,padding=12,justify='left')
        summary.pack(fill='x')
        unreal_label=ttk.Label(window,padding=(12,0,12,8))
        unreal_label.pack(fill='x')
        tabs=ttk.Notebook(window)
        tabs.pack(fill='both',expand=True,padx=12)
        trigger_panel=ttk.Frame(tabs)
        panel=ttk.Frame(tabs)
        tabs.add(trigger_panel,text='纸盘触发记录')
        tabs.add(panel,text='Demo 实际成交')
        headings=('触发时间（北京）','动作','纸盘交易 ID','消费状态','执行结果','原因','纸盘成交 ID')
        trigger=ttk.Treeview(trigger_panel,columns=headings,show='headings')
        scroll=ttk.Scrollbar(trigger_panel,orient='vertical',command=trigger.yview)
        cross=ttk.Scrollbar(trigger_panel,orient='horizontal',command=trigger.xview)
        trigger.configure(yscrollcommand=scroll.set,xscrollcommand=cross.set)
        scroll.pack(side='right',fill='y')
        cross.pack(side='bottom',fill='x')
        trigger.pack(fill='both',expand=True)
        for column,width in zip(headings,(160,65,280,80,110,250,265)):
            trigger.heading(column,text=column)
            trigger.column(column,width=width,stretch=False)
        columns=('成交／判定时间（北京）','开仓／平仓','买卖','持仓方向','数量 BTC','均价 USDT','金额','手续费',
                 '已实现净盈亏','纸盘成交 ID','Binance Demo订单ID','执行结果')
        tree=ttk.Treeview(panel,columns=columns,show='headings')
        sy=ttk.Scrollbar(panel,orient='vertical',command=tree.yview)
        sx=ttk.Scrollbar(panel,orient='horizontal',command=tree.xview)
        tree.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
        sy.pack(side='right',fill='y')
        sx.pack(side='bottom',fill='x')
        tree.pack(fill='both',expand=True)
        color_rows(tree)
        for column,width in zip(columns,(205,86,55,82,100,130,115,115,120,325,160,300)):
            tree.heading(column,text=column)
            tree.column(column,width=width,stretch=False)
        raw_note=ttk.Label(window,padding=8)
        raw_note.pack(fill='x')
        exact={}
        def draw():
            try:
                data=demo_copy_data(demo_directory(cid))
                row=running()
                owned=row.get('mode')=='DEMO' and row.get('copy_config_id')==cid and row.get('strategy')==v['strategy']
                direction,notional,unreal,margin,qty=metrics(data,owned=owned)
                summary.configure(text=f"{v['strategy']} · 配置 {cid} · {v['mode']} · 每笔 {money(v['fixed'])} · 总预算 {money(v['budget'])} · {v['leverage']}× · {row_status(cid,row)}\n"
                    f"持仓 {direction} {qty} · 名义金额 {notional} · 初始保证金 {money(margin)}\n"
                    f"今日已平仓 {money(data['today'])} · 累计已实现 {money(data['total'])}（已扣交易费，资金费前）",
                    foreground=pnl_color(data['total']))
                unreal_label.configure(text=f'当前未实现 {money(unreal)}',foreground=pnl_color(unreal))
                tree.delete(*tree.get_children())
                exact.clear()
                for stamp,effect,side,direction,qty,price,amount,fee,net,event,oid in data['trades']:
                    iid=tree.insert('','end',values=(stamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],effect,
                        {'BUY':'买','SELL':'卖'}.get(side,side),{'LONG':'做多','SHORT':'做空'}.get(direction,'—'),
                        f'{qty:f}',f'{price:f}',money(amount),money(fee),money(net),event,oid,'已成交'),
                        tags=(pnl_tag(net),))
                    exact[iid]=(fee,net)
                trigger.delete(*trigger.get_children())
                outcomes={'SKIPPED':'已跳过','SKIPPED_MIN_ORDER':'已跳过','NO_FILL':'未成交',
                          'RECOVERED':'已恢复','CLOSED':'已平仓','FOLLOW_PAPER':'已跟单','PROTECTED':'已跟单'}
                for event,fill,status,result,reason in reversed(data['triggers']):
                    when=datetime.fromisoformat(fill['occurred_at']).astimezone(BEIJING).strftime('%Y-%m-%d %H:%M:%S')
                    trigger.insert('','end',values=(when,paper_event_label(fill),fill['trade_id'],
                        '已消费' if status=='PLANNED' else '待消费',outcomes.get(result,result or '尚无执行回执'),
                        event_reasons.get(reason,reason) or '—',event))
                raw_note.configure(text='选中成交可查看原始精度' if exact else '暂无Demo成交')
            except (OSError,sqlite3.Error,ValueError,ArithmeticError,TypeError):
                raw_note.configure(text='读取跟单回执失败，请刷新')
        def refresh():
            refresh_marks()
            draw()
        def on_select(_event):
            if tree.selection():
                fee,net=exact.get(tree.selection()[0],(None,None))
                raw_note.configure(text=f"原始精度 USDT  手续费 {fee if fee is not None else '—'}  已实现净盈亏 {net if net is not None else '—'}")
        tree.bind('<<TreeviewSelect>>',on_select)
        ttk.Button(window,text='刷新',command=refresh).pack(anchor='e',padx=12,pady=8)
        refresh()
        window.after(2500,lambda:draw() if window.winfo_exists() else None)

    def edit(cid):
        nonlocal selected_id
        if busy(cid):
            messagebox.showerror('编辑配置','请先安全停止并完成对账。',parent=root)
            return
        selected_id=cid
        for key,value in fields.items():
            value.set(configs[cid].get(key,defaults[key]))
        render(1)

    def archive(cid):
        if busy(cid):
            messagebox.showerror('归档','配置正在运行或仍有关联仓位、未决委托。',parent=root)
            return
        configs[cid]['archived_at']=datetime.now().isoformat(timespec='seconds')
        persist()
        render(0)

    def restore(cid, window):
        configs[cid]['archived_at']=None
        persist()
        window.destroy()
        render(0)

    def context_menu(event, tree, archived, window=None, *, cid=None):
        cid=cid or tree.identify_row(event.y)
        if not cid:
            return
        tree.selection_set(cid)
        menu=tk.Menu(tree,tearoff=False)
        menu.add_command(label='查看配置',command=lambda:details(cid))
        if archived:
            menu.add_command(label='查看交易明细',command=lambda:trade_details(cid))
            menu.add_command(label='恢复配置',command=lambda:restore(cid,window))
        else:
            menu.add_command(label='编辑配置',command=lambda:edit(cid))
            menu.add_command(label='归档',command=lambda:archive(cid))
        menu.tk_popup(event.x_root,event.y_root)

    def archives():
        window=tk.Toplevel(root)
        window.title('归档库')
        tree=ttk.Treeview(window,columns=('策略','每笔 USDT','杠杆','归档时间'),show='headings')
        for col in tree['columns']:
            tree.heading(col,text=col)
            tree.column(col,width=135)
        for cid,v in configs.items():
            if v.get('archived_at'):
                tree.insert('', 'end',iid=cid,values=(v['strategy'],money(v['fixed']),v['leverage']+'×',v['archived_at']))
        tree.pack(fill='both',expand=True)
        tree.bind('<Button-3>',lambda e:context_menu(e,tree,True,window))

    def show_records():
        cid=selected_config()
        if cid not in configs:
            messagebox.showinfo('跟单记录','请选择要查看的跟单配置。',parent=root)
            return
        trade_details(cid)

    def save(*, destination=0, enable_demo=False):
        nonlocal selected_id
        try:
            v = values()
            if v['strategy'] not in paper_rows() or v['mode'] != '定额跟单':
                raise ValueError()
            budget = Decimal(v['budget'])
            if not budget.is_finite() or budget <= 0 or not 1 <= int(v['leverage']) <= 5:
                raise ValueError()
            if v['mode']=='定额跟单' and not 0 < Decimal(v['fixed']) <= budget:
                raise ValueError()
            optional_exit(v,'custom_sl','stop')
            optional_exit(v,'custom_tp','tp')
            for k in ('maximum','slippage'):
                if v[k] and not 0 < Decimal(v[k]) <= 100:
                    raise ValueError()
            if selected_id is None:
                selected_id=uuid4().hex
            prior=configs.get(selected_id,{})
            v['account']=defaults['account']
            if busy(selected_id):
                raise ValueError('请先安全停止并完成对账，再编辑此配置。')
            if prior and v['strategy']!=prior['strategy'] and (prior.get('legacy_demo_ledger') or
                    (demo_directory(selected_id)/'demo-execution-inbox.sqlite3').is_file()):
                raise ValueError('已关联账本的配置不能更换策略，请新建配置。')
            configs[selected_id]={**prior,**v,**demo_policy,'copy_config_id':selected_id,
                'created_at':prior.get('created_at',datetime.now().isoformat()),
                'archived_at':None}
            persist()
            render(destination)
            if enable_demo:
                control('demo')
        except (ValueError, ArithmeticError, OSError) as exc:
            messagebox.showerror('保存失败',str(exc) or '请检查预算、单笔保证金、杠杆和百分比。',parent=root)

    def render(index):
        nonlocal table,pnl_widget,queue_panel,queue_cancel
        table=None
        queue_panel=queue_cancel=None
        pnl_cells.clear()
        for widget in page.winfo_children():
            widget.destroy()
        title = ('BTC 实盘跟单中心','跟单配置','高级确认')[index]
        header = tk.Frame(page,bg='#dceaff',padx=18,pady=10)
        header.pack(fill='x')
        tk.Label(header,text=title,bg='#dceaff',fg='#10285c',
                 font=('Microsoft YaHei UI',19,'bold')).pack(anchor='w')
        tk.Label(header,text='Binance USDⓈ-M · 跟随纸盘已成交单',
                 bg='#dceaff',fg='#405574').pack(anchor='w')
        footer = ttk.Frame(page,padding=(0,12))
        footer.pack(side='bottom',fill='x')
        body = ttk.Frame(page,padding=(0,8))
        body.pack(fill='both',expand=True)
        if index == 0:
            stats = ttk.Frame(body)
            stats.pack(fill='x',pady=(0,12))
            for label,value in [('Demo 可用 USDT',account_line),('跟单配置数',str(sum(not c.get('archived_at') for c in configs.values()))),
                                ('服务状态',service_line),(pnl_label,pnl_value)]:
                label_options = {'labelwidget':ttk.Label(stats,textvariable=label)} if isinstance(label,tk.StringVar) else {'text':label}
                card = ttk.LabelFrame(stats,padding=8,**label_options)
                card.pack(side='left',fill='x',expand=True,padx=4)
                options = {'textvariable':value} if isinstance(value,tk.StringVar) else {'text':value}
                value_widget=ttk.Label(card,**options,font=('Microsoft YaHei UI',15,'bold'))
                if value is account_line:
                    value_widget.configure(wraplength=230)
                value_widget.pack()
                if value is pnl_value:
                    pnl_widget=value_widget
            columns = ('编号','配置 ID','模式','资金','杠杆','状态','持仓方向','持仓金额（USDT）','盈亏','最近动作')
            style.configure('Copy.Treeview',font=('Microsoft YaHei UI',9),
                            rowheight=max(21,int(root.tk.call('tk','scaling')*15)))
            style.map('Copy.Treeview',background=[('selected','#dceaff')])
            table = ttk.Treeview(body,columns=columns,show='headings',height=5,style='Copy.Treeview')
            for col in columns:
                table.heading(col,text=col)
                table.column(col,width=205 if col=='最近动作' else 59 if col=='编号' else 80,
                             anchor='e' if col in ('资金','持仓金额（USDT）','盈亏') else 'center')
            table.pack(fill='both',expand=True)
            table.tag_configure('demo-active',foreground='#b42318')
            table.tag_configure('copy-warning',foreground='#a15c09')
            for cid,v in configs.items():
                if not v.get('archived_at'):
                    table.insert('', 'end',iid=cid,values=(v['strategy'],cid[:10],v['mode'],money(v['fixed']),
                                 v['leverage']+'×',row_status(cid,running()),'—','—','—','保存配置'))
            if selected_id in table.get_children():
                table.selection_set(selected_id)
            table.bind('<Button-3>',lambda e: context_menu(e,table,False))
            table.bind('<<TreeviewSelect>>',place_pnl_cells,add='+')
            table.bind('<Configure>',place_pnl_cells,add='+')
            def on_row_double_click(event):
                cid=table.identify_row(event.y)
                if cid in configs and table.identify_region(event.x,event.y) in ('cell','tree'):
                    table.selection_set(cid)
                    trade_details(cid)
                    return 'break'
            table.bind('<Double-Button-1>',on_row_double_click)
            ttk.Button(body,text='归档库',command=archives).pack(anchor='w')
            ttk.Label(body,textvariable=mode_note).pack(anchor='w',pady=(8,2))
            ttk.Label(body,textvariable=control_note,wraplength=900).pack(anchor='w')
            queue_panel=ttk.Frame(body)
            ttk.Label(queue_panel,textvariable=queue_note,wraplength=900,foreground='#a15c09').pack(anchor='w')
            queue_cancel=ttk.Button(queue_panel,text='取消等待切换',command=cancel_switch)
            queue_cancel.pack(anchor='w')
            ttk.Label(body,textvariable=status_note,wraplength=900).pack(anchor='w',pady=(0,8))
            ttk.Button(footer,text='＋ 新建跟单',style='Primary.TButton',command=new_config).pack(side='left')
            for title, command in [('诊断：启动只读','run'),('启用Demo','demo'),('暂停新开仓','pause'),
                                   ('恢复跟单','resume'),('停止跟单','stop')]:
                controls[command]=ttk.Button(footer,text=title,command=lambda c=command:control(c))
                controls[command].pack(side='left',padx=3)
            ttk.Button(footer,text='刷新账户',command=refresh_account).pack(side='left')
            ttk.Button(footer,text='查看记录',command=show_records).pack(side='left')
            ttk.Button(footer,text='打开实盘K线',command=open_selected_kline).pack(side='right')
            return
        ttk.Label(body,text='① 选择策略       ② 跟单设置       ③ 高级模式',
                  font=('Microsoft YaHei UI',13,'bold')).pack(pady=(0,14))
        left=ttk.LabelFrame(body,text='选择纸盘策略' if index==1 else '高级设置',padding=16)
        right=ttk.LabelFrame(body,text='跟单设置' if index==1 else '确认配置',padding=16)
        left.pack(side='left',fill='both',expand=True,padx=(0,8))
        right.pack(side='left',fill='both',expand=True)
        def field(panel,key,label,choices=None):
            line=ttk.Frame(panel)
            line.pack(fill='x',pady=5)
            ttk.Label(line,text=label,width=23).pack(side='left')
            widget=(ttk.Combobox(line,textvariable=fields[key],values=choices,state='readonly',width=19)
                    if choices is not None else ttk.Entry(line,textvariable=fields[key],width=22))
            widget.pack(side='left',fill='x',expand=True)
            return line, widget
        if index==1:
            rows = paper_rows()
            if not fields['strategy'].get() and rows:
                fields['strategy'].set('C' if 'C' in rows else next(iter(rows)))
            _, selector = field(left,'strategy','纸盘策略',tuple(rows))
            readiness = ttk.Label(left,wraplength=330)
            readiness.pack(anchor='w',pady=10)
            def show_readiness(_event=None):
                sid = fields['strategy'].get()
                ready, reason = adapter_status(sid)
                readiness.configure(text=str(rows.get(sid,sid))+
                                    ((' · 可启用定额 Demo 跟单'+(' · '+reason if reason else '')) if ready
                                     else ' · 待适配，可保存草稿：'+reason))
            selector.bind('<<ComboboxSelected>>',show_readiness)
            show_readiness()
            fields['mode'].set('定额跟单')
            fields['account'].set(defaults['account'])
            field(right,'mode','跟单模式',('定额跟单',))
            field(right,'fixed','每笔跟单保证金（USDT）')
            field(right,'budget','总资金上限（USDT）')
            field(right,'leverage','计划杠杆',('1','2','3','4','5'))
            field(right,'account','账户',('Binance USDⓈ-M Demo',))
        else:
            ttk.Label(left,text='退出方式：跟随纸盘止盈止损').pack(anchor='w',pady=5)
            for key,label in [('custom_tp','独立止盈（选填）'),('custom_sl','独立止损（选填）')]:
                def caption(key=key,label=label):
                    return ('☑ ' if fields[key].get()=='1' else '☐ ')+label
                checkbox=ttk.Button(left,text=caption())
                def toggle(key=key,checkbox=checkbox,caption=caption):
                    fields[key].set('0' if fields[key].get()=='1' else '1')
                    checkbox.configure(text=caption())
                checkbox.configure(command=toggle)
                checkbox.pack(anchor='w')
            ttk.Label(left,text='☐ 终止时全部平仓（暂不可用；停止时继续管理原仓）').pack(anchor='w')
            for key,label in [('tp','自定义止盈 %（可空）'),('stop','自定义止损 %（可空）')]:
                field(left,key,label)
            ttk.Label(left,text='自定义单笔预算上限与最大滑点暂不可用').pack(anchor='w')
            ttk.Label(left,text='高级选项保存后生效。启用 Demo 需单独确认，停止时保留必要的持仓管理。',wraplength=420).pack(anchor='w',pady=8)
            v=values()
            amount = money(v['fixed'])+' / 笔' if v['mode']=='定额跟单' else '等待保证金数据适配'
            ttk.Label(right,text=f"{v['strategy']}\n{v['mode']} · {amount}\n总预算 {money(v['budget'])}\n"
                     f"计划杠杆 {v['leverage']}×\n交易环境：Binance Demo\n启动方式：Demo自动跟单\n生产下单：关闭",justify='left').pack(anchor='w')
            ttk.Label(right,textvariable=account_line).pack(anchor='w',pady=8)
        ttk.Button(footer,text='← 上一步',command=lambda:render(index-1)).pack(side='left')
        ttk.Button(footer,text='保存配置',command=save).pack(side='right',padx=8)
        if index==1:
            def confirm_settings():
                try:
                    fixed, budget = (Decimal(fields[key].get()) for key in ('fixed','budget'))
                    if not fixed.is_finite() or not budget.is_finite() or not 0 < fixed <= budget:
                        raise ValueError()
                    if fields['strategy'].get() not in rows:
                        raise ValueError()
                except (ValueError,ArithmeticError):
                    messagebox.showerror('跟单配置','请选择策略，并填写有效的保证金及预算。',parent=root)
                    return
                render(2)
            ttk.Button(footer,text='下一步：高级确认',style='Primary.TButton',command=confirm_settings).pack(side='right')
        else:
            ttk.Button(footer,text='启用Demo跟单',command=lambda:save(enable_demo=True)).pack(side='right')

    render(0)
    update_state()
    refresh_account()
    advance_switch()
    root.protocol('WM_DELETE_WINDOW',lambda:root.withdraw() if read_switch() else root.destroy())
    if owner is None:
        root.mainloop()
    return root


if __name__=='__main__':
    main()
