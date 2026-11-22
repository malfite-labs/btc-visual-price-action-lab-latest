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
