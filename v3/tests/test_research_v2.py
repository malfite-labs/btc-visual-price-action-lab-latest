from scripts.research_v2 import closed_trades, performance, equity_analysis, export_snapshot


def test_only_complete_matching_fills_count_and_fees_once():
    fills = [dict(trade_id='x', position_effect='OPEN', position_direction='LONG', quantity='1', price='100', fee='1', occurred_at='2026-01-01T00:00:00+00:00'), dict(trade_id='x', position_effect='CLOSE', position_direction='LONG', quantity='1', price='110', fee='1', occurred_at='2026-01-02T00:00:00+00:00'), dict(trade_id='y', position_effect='OPEN', position_direction='LONG', quantity='1', price='100', fee='1', occurred_at='2026-01-03T00:00:00+00:00')]
    trades, issues = closed_trades(fills, 'A')
    assert len(trades) == 1 and trades[0]['net_pnl'] == '8'
    assert performance(trades)['closed'] == 1 and issues


def test_g_mirror_partial_exit_and_no_trade_ids_are_not_guessed():
    rows = [dict(g_trade_id='g', effect='OPEN', direction='SHORT', quantity='2', price='100', fee='1'), dict(g_trade_id='g', effect='CLOSE', direction='SHORT', quantity='1', price='90', fee='1')]
    assert closed_trades(rows, 'G')[0] == []
    assert closed_trades([dict(effect='OPEN',quantity='1')], 'PA-01')[0] == []


def test_missing_history_not_turned_into_zero_drawdown_or_win_rate():
    assert performance([])['win_rate'] is None
    assert equity_analysis([{'time':'2026-01-01T00:00:00+00:00','equity':'100'}])['max_drawdown'] is None
    data=equity_analysis([{'time':'2026-01-01T00:00:00+00:00','equity':'100'}, {'time':'2026-01-01T00:15:00+00:00','equity':'90'}])
    assert float(data['max_drawdown']) == 10 and data['at']=='2026-01-01T00:15:00+00:00'


def test_export_writes_only_requested_directory(tmp_path):
    out=export_snapshot({'as_of':'2026-01-01T00:00:00+00:00','strategies':[{'id':'A','equity':'100'}]},tmp_path)
    assert {p.suffix for p in out} == {'.csv','.json','.md'}
    assert all(p.parent == tmp_path for p in out)


def test_export_rejects_formal_runtime_paths(tmp_path):
    from scripts.research_v2 import validate_export_dir
    import pytest
    root=tmp_path/'windows-binance-deploy'
    with pytest.raises(ValueError): validate_export_dir(root/'data', protected_roots=[root])


def test_complete_trade_requires_timestamps_and_funding_is_not_fabricated():
    rows=[dict(trade_id='x',position_effect='OPEN',position_direction='LONG',quantity='1',price='100',fee='1'),dict(trade_id='x',position_effect='CLOSE',position_direction='LONG',quantity='1',price='110',fee='1')]
    trades,issues=closed_trades(rows,'C')
    assert not trades and issues


def test_snapshot_registration_and_failure_are_explicit():
    from scripts.research_v2 import load_snapshot
    class Fake:
        def get(self,path):
            if path.endswith('/agents'):return []
            if path.endswith('/strategy-pool'):return {'items': []}
            return {}
    s=load_snapshot(Fake())
    assert len(s['strategies']) == 16
    assert all(x['equity'] is None for x in s['strategies'])
    assert next(x for x in s['strategies'] if x['id']=='PA-09')['state'] is None


def test_g_realized_close_is_not_double_charged():
    fills=[dict(g_trade_id='G1',effect='OPEN',direction='SHORT',quantity='1',price='100',fee='1',occurred_at='2026-01-01T00:00:00+00:00'),dict(g_trade_id='G1',effect='CLOSE',direction='SHORT',quantity='1',price='90',fee='1',pnl='8',occurred_at='2026-01-02T00:00:00+00:00')]
    trades,issues=closed_trades(fills,'G')
    assert not issues and trades[0]['net_pnl']=='8'


def test_tk_window_displays_sixteen_mock_strategies_without_trading(monkeypatch):
    import time
    import tkinter as tk
    from tkinter import ttk
    import scripts.research_v2_window as ui
    from scripts.research_v2 import IDS
    rows=[{'id':x,'strategy_id':y,'version':'v','state':'TEST','initial_equity':None,'equity':None,'realized':None,'unrealized':None,'net_return':None,'fees':None,'funding':None,'position':None,'last_cycle':None,'last_trade':None,'source':'mock','as_of':'now','completeness':'TEST','metrics':{'closed':0,'win_rate':None,'profit_factor':None,'avg_win':None,'avg_loss':None},'trades':[],'fills':[],'issues':[],'equity_points':[]} for x,y in IDS]
    monkeypatch.setattr(ui,'load_snapshot',lambda:{'strategies':rows,'as_of':'now','errors':[],'kind':'mock'})
    root=tk.Tk();root.withdraw()
    try:
        window=ui.open_research_center(root)
        def walk(node):
            return [node]+[child for item in node.winfo_children() for child in walk(item)]
        root.after(1200,root.quit)
        root.mainloop()
        trees=[w for w in walk(window) if isinstance(w,ttk.Treeview)]
        assert trees and len(trees[0].get_children())==16
        assert len([w for w in walk(window) if isinstance(w,ttk.Notebook)][0].tabs())==5
    finally:
        root.destroy()


def test_no_cross_market_or_fabricated_equity_comparisons():
    from scripts.research_v2 import load_snapshot
    class Offline:
        def get(self,path):
            if path.endswith('/agents'): return []
            return {}
    snapshot=load_snapshot(Offline())
    assert '不可排名' in snapshot['comparison']
    assert all(row['equity_points']==[] for row in snapshot['strategies'])


def test_equity_curve_gaps_do_not_form_false_lines():
    from scripts.research_v2 import equity_curve_segments
    points=[{'strategy_id':'A','cycle_close_at_utc':'2026-01-01T00:00:00+00:00','equity_usd':'100','equity_valid':True},{'strategy_id':'A','cycle_close_at_utc':'2026-01-01T00:30:00+00:00','equity_usd':'101','equity_valid':True}]
    result=equity_curve_segments(points,'A')
    assert len(result['segments'])==2 and result['gaps'][0]['missing_cycles']==1


def test_curve_projects_only_existing_points():
    from scripts.research_v2 import curve_coordinates
    pts=[{'time':'2026-01-01T00:00:00+00:00','equity':'100'},{'time':'2026-01-01T00:30:00+00:00','equity':'90'}]
    coords=curve_coordinates(pts,500,300)
    assert len(coords)==2 and [p['time'] for p in coords]==[p['time'] for p in pts]


def test_duplicate_fill_is_never_counted_as_verified_trade():
    opening=dict(fill_id='a',trade_id='x',position_effect='OPEN',position_direction='LONG',quantity='1',price='100',fee='1',occurred_at='2026-01-01T00:00:00+00:00')
    closing=dict(fill_id='b',trade_id='x',position_effect='CLOSE',position_direction='LONG',quantity='1',price='110',fee='1',occurred_at='2026-01-02T00:00:00+00:00')
    trades,issues=closed_trades([opening,opening,closing,closing],'A')
    assert not trades and issues


def test_only_get_endpoints_are_used_by_research_snapshot():
    from scripts.research_v2 import load_snapshot
    class Spy:
        def __init__(self): self.paths=[]
        def get(self,path):
            self.paths.append(path)
            if path.endswith('/agents'):return []
            return {}
    spy=Spy();load_snapshot(spy)
    assert spy.paths and all(p.startswith('/api/v1/') for p in spy.paths)
    assert not any(x in p for p in spy.paths for x in ('/enable','/pause','/backtest','/reset','/orders'))


def test_pa_single_position_lifecycle_is_paired_only_with_evidence():
    from scripts.research_v2 import pair_pool_fills
    rows=[{'reason':'ENTRY_NEXT_OPEN','side':'BUY','qty':'1','price':'100','fee':'1','version':'1','created_at':'2026-01-01T00:00:00+00:00','cycle':'2026-01-01T00:00:00+00:00','idempotency_key':'p:open:a'}, {'reason':'TARGET_QUOTE','side':'SELL','qty':'1','price':'110','fee':'1','version':'1','created_at':'2026-01-01T01:00:00+00:00','cycle':'2026-01-01T01:00:00+00:00','idempotency_key':'p:quote-target_quote:b'}]
    trades,issues=pair_pool_fills(rows,'p')
    assert len(trades)==1 and trades[0]['net_pnl']=='8' and not issues
    rows[1]['qty']='0.5'
    assert not pair_pool_fills(rows,'p')[0]


def test_partial_mirror_profit_is_attributed_to_actual_close_dates():
    from scripts.research_v2 import realized_calendar_events
    fills=[{'g_trade_id':'g','effect':'OPEN','direction':'LONG','quantity':'2','price':'100','fee':'2','occurred_at':'2026-01-01T00:00:00+00:00','fill_id':'1'}, {'g_trade_id':'g','effect':'PARTIAL_CLOSE','direction':'LONG','quantity':'1','price':'110','fee':'1','pnl':'8','occurred_at':'2026-01-02T00:00:00+00:00','fill_id':'2'}, {'g_trade_id':'g','effect':'CLOSE','direction':'LONG','quantity':'1','price':'120','fee':'1','pnl':'18','occurred_at':'2026-01-03T00:00:00+00:00','fill_id':'3'}]
    events,issues=realized_calendar_events(fills,'G')
    assert [e['net_pnl'] for e in events]==['8','18'] and not issues
    assert [e['date'] for e in events]==['2026-01-02','2026-01-03']


def test_flat_equity_requires_matching_audit_and_ledger_cash():
    from scripts.research_v2 import flat_equity_points
    trades=[{'trade_id':'t','closed_at':'2026-01-01T00:00:00+00:00'}]
    fills=[{'trade_id':'t','fill_id':'f','position_effect':'CLOSE','occurred_at':'2026-01-01T00:00:00+00:00'}]
    ledger=[{'reference':'f','kind':'CLOSE_PNL','cash_after':'101','occurred_at':'2026-01-01T00:00:00+00:00'}]
    audit=[{'kind':'POSITION_CLOSED','payload':'{"trade_id":"t","exit_fill_id":"f"}'}]
    assert flat_equity_points(trades,fills,ledger,audit)[0]['equity']=='101'
    assert flat_equity_points(trades,fills,ledger,[])==[]


def test_research_does_not_call_pool_get_that_acquires_write_lock():
    from scripts.research_v2 import load_snapshot
    class Fake:
        def __init__(self):self.paths=[]
        def get(self,path):
            self.paths.append(path)
            return [] if path.endswith('/agents') else {}
    f=Fake();load_snapshot(f)
    assert not any('/strategy-pool' in p for p in f.paths)


def test_research_does_not_call_mutating_d_status_endpoint():
    from scripts.research_v2 import load_snapshot
    class Fake:
        def __init__(self):self.paths=[]
        def get(self,path):
            self.paths.append(path)
            return [] if path.endswith('/agents') else {}
    f=Fake();load_snapshot(f)
    assert '/api/v1/d/status' not in f.paths


def test_d_partial_exit_uses_committed_pnl_on_correct_day():
    from scripts.research_v2 import realized_calendar_events
    fills=[{'trade_id':'d','effect':'OPEN','position_direction':'LONG','quantity':'2','occurred_at':'2026-01-01T00:00:00+00:00','fill_id':'1'}, {'trade_id':'d','effect':'CLOSE','position_direction':'LONG','quantity':'1','pnl':'8','occurred_at':'2026-01-02T00:00:00+00:00','fill_id':'2'}, {'trade_id':'d','effect':'CLOSE','position_direction':'LONG','quantity':'1','pnl':'18','occurred_at':'2026-01-03T00:00:00+00:00','fill_id':'3'}]
    events,issues=realized_calendar_events(fills,'D')
    assert [e['net_pnl'] for e in events]==['8','18'] and not issues


def test_g_risk_exit_can_close_full_mirror_position_with_partial_close_label():
    from scripts.research_v2 import realized_calendar_events
    rows=[{'g_trade_id':'g','effect':'OPEN','direction':'SHORT','quantity':'1','occurred_at':'2026-01-01T00:00:00+00:00','fill_id':'1'}, {'g_trade_id':'g','effect':'PARTIAL_CLOSE','direction':'SHORT','quantity':'1','pnl':'3','occurred_at':'2026-01-02T00:00:00+00:00','fill_id':'2'}]
    events,issues=realized_calendar_events(rows,'G')
    assert not issues and events[0]['net_pnl']=='3'


def test_pool_overlapping_opens_are_not_assumed_to_imply_exit():
    from scripts.research_v2 import pair_pool_fills
    def row(t,reason,side,key):return {'reason':reason,'side':side,'qty':'1','price':'100','fee':'1','version':'1','created_at':f'2026-01-01T0{t}:00:00+00:00','cycle':f'2026-01-01T0{t}:00:00+00:00','idempotency_key':'p:'+key}
    trades,issues=pair_pool_fills([row(0,'ENTRY_NEXT_OPEN','BUY','open:a'),row(1,'ENTRY_NEXT_OPEN','BUY','open:b'),row(2,'STOP_QUOTE','SELL','quote-stop_quote:c')],'p')
    assert not trades and any('UNVERIFIED' in x for x in issues)


def test_ledger_reader_enforces_readonly_and_does_not_change_database(tmp_path):
    import sqlite3, hashlib
    from scripts.research_v2 import ReadOnlyStore
    data=tmp_path/'data';data.mkdir();path=data/'strategy-pool.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('create table strategies(id text,active_version text,enabled integer,paused_at text,archived_at text); create table accounts(strategy_id text,cash text,position text,realized text,last_cycle text,last_status text,last_action text); create table versions(strategy_id text,version text,document text); create table fills(id integer primary key,strategy_id text,version text,cycle text,side text,qty text,price text,fee text,slippage text,reason text,idempotency_key text,created_at text);')
        db.execute("insert into strategies values ('p','1',1,NULL,NULL)");db.execute("insert into accounts values ('p','100',NULL,'0',NULL,'OK','WAIT')")
        db.execute("insert into versions values ('p','1','{}')")
    before=hashlib.sha256(path.read_bytes()).hexdigest();store=ReadOnlyStore(tmp_path)
    db=store._connect('strategy-pool.sqlite3')
    try:
        import pytest
        with pytest.raises(sqlite3.OperationalError):db.execute("update accounts set cash='1'")
    finally:db.close()
    assert store.pool()['p']['equity']=='100'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before


def test_console_research_button_wires_to_read_only_window():
    from pathlib import Path
    source=(Path(__file__).resolve().parents[1]/'scripts'/'local_dashboard.py').read_text(encoding='utf8')
    assert "'策略研究'" in source and "scripts.research_v2_window" in source and "open_research_center(root)" in source


def test_equity_coverage_is_not_fabricated():
    from scripts.research_v2 import equity_analysis
    r=equity_analysis([{'time':'2026-01-01T00:00:00+00:00','equity':'100'}, {'time':'2026-01-02T00:00:00+00:00','equity':'90'}])
    assert r['start'].startswith('2026-01-01') and r['end'].startswith('2026-01-02')
    assert r['coverage'] is None and r['max_drawdown'] is not None


def test_calendar_uses_beijing_close_date_and_ledger_net():
    from scripts.research_v2 import audited_calendar_events
    trades=[{'trade_id':'t'}]
    fills=[{'trade_id':'t','fill_id':'f','position_effect':'CLOSE','occurred_at':'2026-01-01T17:00:00+00:00'}]
    audit=[{'kind':'POSITION_CLOSED','payload':'{"trade_id":"t","exit_fill_id":"f","net_pnl":"3.25"}'}]
    result=audited_calendar_events(trades,fills,audit)
    assert result[0]['date']=='2026-01-02' and result[0]['net_pnl']=='3.25'


def test_pool_mismatched_version_and_duplicate_keys_are_unverified():
    from scripts.research_v2 import pair_pool_fills
    entry={'reason':'ENTRY_NEXT_OPEN','side':'BUY','qty':'1','price':'100','fee':'1','version':'1','created_at':'2026-01-01T00:00:00+00:00','cycle':'2026-01-01T00:00:00+00:00','idempotency_key':'p:open:a'}
    close={'reason':'STOP_QUOTE','side':'SELL','qty':'1','price':'90','fee':'1','version':'2','created_at':'2026-01-01T01:00:00+00:00','cycle':'2026-01-01T01:00:00+00:00','idempotency_key':'p:quote-stop_quote:b'}
    assert not pair_pool_fills([entry,close],'p')[0]
    assert not pair_pool_fills([entry,entry],'p')[0]


def test_duplicate_timestamp_equities_do_not_invent_order_or_drawdown():
    from scripts.research_v2 import equity_analysis
    points=[{'time':'2026-01-01T00:00:00+00:00','equity':'100'}, {'time':'2026-01-01T00:00:00+00:00','equity':'80'}]
    result=equity_analysis(points)
    assert result['max_drawdown'] is None
