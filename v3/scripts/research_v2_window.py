"""Read-only Tk window for V3 research; no trading controls or background jobs."""
from __future__ import annotations
import calendar, json, threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from scripts.research_v2 import load_snapshot, export_snapshot, equity_analysis


def open_research_center(parent):
    window=tk.Toplevel(parent); window.title('策略研究中心 V2 · 只读'); window.geometry('1250x760'); window.minsize(900,580)
    state={'snapshot':None,'rows':{},'selected':None,'sort':False,'observations':{}}; status=tk.StringVar(value='正在读取只读数据…')
    header=ttk.Frame(window,padding=8);header.pack(fill='x')
    ttk.Label(header,text='策略研究中心 V2',font=('Microsoft YaHei UI',15,'bold')).pack(side='left')
    ttk.Label(header,text='只读 · 本地模拟 · 不同资金模型/时间窗口不可排名').pack(side='left',padx=20)
    ttk.Label(window,textvariable=status,foreground='#425a74').pack(fill='x',padx=10)
    tabs=ttk.Notebook(window);tabs.pack(fill='both',expand=True,padx=10,pady=6)
    overview=ttk.Frame(tabs);detail=ttk.Frame(tabs);curve=ttk.Frame(tabs);calendar_tab=ttk.Frame(tabs);history=ttk.Frame(tabs)
    for frame,title in [(overview,'策略总览'),(detail,'策略详情'),(curve,'权益与回撤'),(calendar_tab,'盈亏日历'),(history,'历史交易')]:tabs.add(frame,text=title)
    cols=('id','name','version','state','initial','equity','return','realized','unrealized','closed','win','avg_win','avg_loss','pf','dd','fees','last')
    labels=('策略','名称','活动版本','状态','初始资金','当前权益','收益率%','已实现','未实现','闭合笔数','胜率%','平均盈利','平均亏损','PF','回撤%','手续费','最新成交')
    table=ttk.Treeview(overview,columns=cols,show='headings',height=17)
    def sort_by(c):
        def key(i):
            v=table.set(i,c)
            try:return (0,float(v))
            except (TypeError,ValueError):return (1,v)
        state['sort']=not state['sort']
        for n,i in enumerate(sorted(table.get_children(),key=key,reverse=state['sort'])):table.move(i,'',n)
    for c,label in zip(cols,labels):table.heading(c,text=label,command=lambda x=c:sort_by(x));table.column(c,width=100 if c not in ('version','last') else 180,stretch=True)
    scroll=ttk.Scrollbar(overview,orient='horizontal',command=table.xview); table.configure(xscrollcommand=scroll.set);scroll.pack(side='bottom',fill='x'); table.pack(fill='both',expand=True)
    ttk.Label(overview,text='缺少完整闭合证据的记录不计数；样本不足显示“—”。数据为分接口时点快照，非原子。').pack(anchor='w')
    text=tk.Text(detail,wrap='word');text.pack(fill='both',expand=True)
    graph=tk.Canvas(curve,bg='white',highlightthickness=0);graph.pack(fill='both',expand=True)
    cbar=ttk.Frame(calendar_tab);cbar.pack(fill='x'); month=tk.StringVar(value=datetime.now().strftime('%Y-%m'))
    ttk.Label(cbar,text='月份 YYYY-MM').pack(side='left');ttk.Entry(cbar,textvariable=month,width=12).pack(side='left',padx=5)
    caltext=tk.Text(calendar_tab,wrap='none');caltext.pack(fill='both',expand=True)
    hcols=('opened','closed','direction','qty','entry','exit','fees','net','stop','target','reason')
    tradegrid=ttk.Treeview(history,columns=hcols,show='headings')
    for c,label in zip(hcols,('开仓时间','平仓时间','方向','数量','开仓均价','平仓均价','手续费','净盈亏','止损','止盈','退出原因')):tradegrid.heading(c,text=label);tradegrid.column(c,width=125)
    tradegrid.pack(fill='both',expand=True)
    foot=ttk.Frame(window,padding=8);foot.pack(fill='x')
    def show_selected():
        item=state['rows'].get(state['selected']);text.delete('1.0','end');caltext.delete('1.0','end');graph.delete('all')
        for iid in tradegrid.get_children():tradegrid.delete(iid)
        if not item:return
        metrics=item['metrics']; details={k:item.get(k) for k in ('id','strategy_id','version','registered','description','state','initial_equity','equity','realized','unrealized','net_return','fees','funding','position','last_cycle','last_trade','source','as_of','completeness','issues')}
        text.insert('end',json.dumps(details,ensure_ascii=False,indent=2,default=str)+'\n\n已验证闭合样本统计\n'+json.dumps(metrics,ensure_ascii=False,indent=2)+'\n\n原始成交记录\n'+json.dumps(item['fills'],ensure_ascii=False,indent=2,default=str))
        ea=equity_analysis(item['equity_points']);graph.create_text(15,20,anchor='nw',text='权益观测：'+str(ea['points'])+' 点；最大回撤：'+str(ea['max_drawdown'] or '样本不足')+'；'+ea['basis'],font=('Microsoft YaHei UI',11))
        if not item['equity_points']: graph.create_text(15,65,anchor='nw',text='没有持久化权益点；不合成历史曲线。',fill='#9a3412')
        else:
            from scripts.research_v2 import curve_coordinates
            points=curve_coordinates(item['equity_points'],max(600,graph.winfo_width()),max(350,graph.winfo_height()))
            last=None
            for point in points:
                if last is not None:
                    elapsed=(datetime.fromisoformat(point['time'])-datetime.fromisoformat(last['time'])).total_seconds()
                    if 0 < elapsed <= 900:graph.create_line(last['x'],last['y'],point['x'],point['y'],fill='#2563eb',width=2)
                graph.create_oval(point['x']-3,point['y']-3,point['x']+3,point['y']+3,fill='#2563eb',outline='');last=point
        for t in item['trades']:
            tradegrid.insert('', 'end',values=tuple(t.get(k) or '—' for k in ('opened_at','closed_at','direction','quantity','entry_price','exit_price','fees','net_pnl','stop','target','exit_reason')))
        days={}
        for t in item['trades']:
            try:
                from datetime import timezone,timedelta
                day=datetime.fromisoformat(t['closed_at'].replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8))).date().isoformat()
                if day.startswith(month.get()):days.setdefault(day,[]).append(t)
            except (ValueError,TypeError):pass
        caltext.insert('end','北京时间；仅有证据的完整闭合交易。未平仓及无法配对不计入。\n\n')
        from decimal import Decimal
        for day,trades in sorted(days.items()):caltext.insert('end',f"{day}   {sum((Decimal(t['net_pnl']) for t in trades),Decimal(0))}   {len(trades)} 笔\n")
        if not days:caltext.insert('end','本月无可验证闭合交易，或样本不足。')
    table.bind('<<TreeviewSelect>>',lambda e:(state.update(selected=table.selection()[0]) if table.selection() else None,show_selected()))
    ttk.Button(cbar,text='查看月份',command=show_selected).pack(side='left')
    def display(s):
        if not window.winfo_exists():return
        state['snapshot']=s
        for item in s['strategies']:
            if item['equity'] is not None:
                points=state['observations'].setdefault(item['id'],[])
                points.append({'time':s['as_of'],'equity':item['equity']})
                item['equity_points']=list(points)
        state['rows']={x['id']:x for x in s['strategies']}
        for iid in table.get_children():table.delete(iid)
        for row in s['strategies']:
            m=row['metrics']; vals=(row['id'],row.get('name'),row['version'],row['state'],row['initial_equity'],row['equity'],row['net_return'],row['realized'],row['unrealized'],m['closed'] if not row['issues'] else '不完整',m['win_rate'],m['avg_win'],m['avg_loss'],m['profit_factor'],equity_analysis(row['equity_points'])['max_drawdown'],row['fees'],row['last_trade'])
            table.insert('', 'end',iid=row['id'],values=tuple('—' if x is None else x for x in vals))
        status.set('快照 '+s['as_of']+'  |  '+str(len(s['strategies']))+' 项  |  '+('；'.join(s['errors']) or s['kind']))
        if s['strategies']:table.selection_set(s['strategies'][0]['id']);state['selected']=s['strategies'][0]['id'];show_selected()
    def refresh():
        status.set('正在读取只读接口…')
        def task():
            try:s=load_snapshot();window.after(0,lambda:display(s))
            except Exception as exc:
                message=str(exc);window.after(0,lambda:status.set('读取失败：'+message))
        threading.Thread(target=task,daemon=True).start()
    def export():
        if state['snapshot'] is None:return
        target=filedialog.askdirectory(parent=window,title='选择独立研究导出目录')
        if not target:return
        try:
            from scripts.research_v2 import validate_export_dir
            validate_export_dir(Path(target));paths=export_snapshot(state['snapshot'],target)
            messagebox.showinfo('导出完成','\n'.join(map(str,paths)),parent=window)
        except Exception as exc:messagebox.showerror('导出失败',str(exc),parent=window)
    ttk.Button(foot,text='刷新只读快照',command=refresh).pack(side='left')
    ttk.Button(foot,text='导出 CSV / JSON / 报告',command=export).pack(side='right')
    refresh();return window
