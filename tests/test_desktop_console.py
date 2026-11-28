from scripts import local_dashboard


def test_recover_uses_shared_service_launcher(monkeypatch):
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        return type('Result', (), {'returncode': 0, 'stdout': 'ok', 'stderr': ''})()
    monkeypatch.setattr(local_dashboard.subprocess, 'run', fake_run)
    local_dashboard.recover()
    assert len(calls) == 1
    assert calls[0][-1].endswith('start_lab.ps1')


def test_recent_action_codes_are_shown_in_chinese():
    stop_distance = local_dashboard.action_label('WAIT_STOP_DIST')
    unknown = local_dashboard.action_label('UNRECOGNIZED_ACTION')

    assert stop_distance == '\u7b49\u5f85\u6b62\u635f\u8ddd\u79bb\u6761\u4ef6'
    assert 'WAIT_STOP_DIST' not in stop_distance
    assert unknown == '\u9700\u68c0\u67e5'
    assert local_dashboard.action_label('IDLE') == '等待信号'
    assert local_dashboard.action_label('NO_SETUP') == '等待信号'
    assert local_dashboard.action_label('NO_PENDING_SIGNAL') == '等待信号'
    assert local_dashboard.action_label('OTHER_STATE') == '等待信号'



def test_leverage_column_uses_consistent_open_flat_and_pending_states():
    assert local_dashboard.leverage_label(None, 'FLAT') == '\u7a7a\u4ed3'
    assert local_dashboard.leverage_label('5', 'LONG') == '5.00x'
    assert local_dashboard.leverage_label('0.60x', 'SHORT') == '0.60x'
    assert local_dashboard.leverage_label('5', 'PENDING_LONG') == '\u5f85\u5f00\u4ed3'
    assert local_dashboard.leverage_label('\u4e0a\u96505x', 'LONG') == '\u672a\u77e5'



def test_d_and_e_console_states_show_real_readiness_and_account_data():
    d = local_dashboard.console_rows({
        'D': {'state': 'FLAT', 'equity': '10000.00', 'realized': '0.00',
              'unrealized': '0.00', 'amount': '0.00', 'leverage': '\u7a7a\u4ed3',
              'action': 'INSUFFICIENT_15M_HISTORY', 'cycle': '2026-11-21 13:00',
              'control': 'D_OBSERVE_ONLY'},
        'E': {'state': 'FLAT', 'equity': '10000.00', 'realized': '0.00',
              'unrealized': '0.00', 'amount': '0.00', 'leverage': '\u7a7a\u4ed3',
              'action': 'WAIT_NEXT_BAR', 'control': 'E_OBSERVE_ONLY'},
    })
    assert d['D']['equity'] == '10000.00'
    assert d['D']['status_label'] == '\u6267\u884c\u672a\u63a5\u901a'
    assert local_dashboard.strategy_view('D', d['D'])['action'] == '\u7b49\u5f8515\u5206\u949fK\u7ebf\u9884\u70ed'
    assert d['E']['equity'] == '10000.00'
    assert d['E']['status_label'] == '\u89c2\u5bdf\u4e2d'
    assert local_dashboard.e_control_state({'status': 'READY'}, {'mode': 'OBSERVE_ONLY'}, 'RUNNING') == 'E_OBSERVE_ONLY'
    assert local_dashboard.e_control_state({'status': 'NOT_INITIALIZED'}, {'mode': 'OBSERVE_ONLY'}, 'RUNNING') == 'E_NOT_INITIALIZED'


def test_recover_reports_healthy_services_without_restarting(monkeypatch):
    monkeypatch.setattr(local_dashboard.subprocess, 'run', lambda *a, **k:
                        type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})())
    monkeypatch.setattr(local_dashboard, 'listeners', lambda: [
        {'port': 8012, 'pid': 1234, 'exe': 'python'},
        {'port': 5175, 'pid': 5678, 'exe': 'node'},
    ])
    monkeypatch.setattr(local_dashboard, '_tracked_pid', lambda name: {
        'backend': 1234, 'frontend': 5678,
    }[name])
    monkeypatch.setattr(local_dashboard, 'fetch', lambda path, port=8012: (
        {'status': 'ok', 'live_trading_disabled': True} if port == 8012 else 200
    ))

    result = local_dashboard.recover()

    assert '\u5747\u5df2\u8fd0\u884c' in result
    assert '\u672a\u91cd\u590d\u542f\u52a8' in result


def test_operations_panel_uses_live_and_backup_values_without_inventing_success():
    data = local_dashboard.operations_status_summary(
        {'scheduler': 'ENABLED_INTERNAL_PAPER', 'feed_health': 'LIVE', 'quote_health': 'FRESH',
         'websocket_recovery_state': 'HEALTHY', 'shadow_evolution': {'state': 'REBUILT_NEW_EPOCH',
         'agents': {'A': {'next_review_at': 40}, 'B': {'next_review_at': 40}, 'C': {'next_review_at': 40}}}},
        {'version': '1.0.4'},
        [{'id': 'pa-one', 'last_cycle': '2026-11-14T10:00:00+00:00', 'status': 'RUNNING'},
         {'id': 'pa-three-bar-hhhl', 'last_status': 'BLOCKED_GAP_RECONCILIATION_REQUIRED'}],
        {'generated_at': '2026-11-14T10:01:00+00:00', 'items': [{'backup_status': 'VERIFIED'}]},
    )
    assert data['8012\u7248\u672c'] == '1.0.4'
    assert data['ABC\u5f71\u5b50\u8d26\u672c'] == 'REBUILT_NEW_EPOCH'
    assert 'pa-one=' in data['PA\u5468\u671f']
    assert data['\u5907\u4efd\u5b8c\u6574\u6027'] == '1\u9879\u901a\u8fc7 / 0\u9879\u5931\u8d25'
    assert data['\u5f02\u5730\u526f\u672c'] == '\u672a\u914d\u7f6e\uff08\u4ec5\u672c\u673a\u5907\u4efd\uff09'
    assert 'pa-three-bar-hhhl' in data['\u6062\u590d\u6761\u4ef6']
def test_rule_detail_button_keeps_real_strategy_id_after_filters_and_search():
    visible = ['pa-trend-40-breakout', 'G', 'pa-three-bar-hhhl']
    assert [local_dashboard.rule_detail_target(row_id) for row_id in visible] == visible
    rows = {sid: {'name': sid, 'status_label': '运行中'} for sid in local_dashboard.RULE_DETAIL_IDS}
    filtered = local_dashboard.filter_console_rows(rows, 'PA', '全部状态', 'pa-three-bar-hhhl')
    assert filtered == ['pa-three-bar-hhhl']
    assert local_dashboard.detail_click_target(filtered[0], '#14') == 'pa-three-bar-hhhl'
    assert local_dashboard.detail_click_target(filtered[0], '#13') is None


def test_every_registered_strategy_id_is_valid_for_detail_action():
    assert tuple(local_dashboard.rule_detail_target(sid) for sid in local_dashboard.RULE_DETAIL_IDS) == local_dashboard.RULE_DETAIL_IDS


def test_trade_detail_fills_infer_holding_direction_for_all_open_and_close_sides():
    cases = [
        ({'side': 'BUY', 'reason': 'ENTRY_NEXT_OPEN'}, '做多'),
        ({'side': 'SELL', 'reason': 'ENTRY_NEXT_OPEN'}, '做空'),
        ({'side': 'SELL', 'reason': 'STOP_QUOTE'}, '做多'),
        ({'side': 'BUY', 'reason': 'TARGET_QUOTE'}, '做空'),
        ({'side': 'BUY', 'position_effect': 'OPEN', 'position_direction': 'SHORT'}, '做空'),
        ({'side': 'SELL', 'position_effect': 'CLOSE', 'position_direction': 'LONG'}, '做多'),
        ({'direction': 'SHORT', 'position_effect': 'OPEN'}, '做空'),
        ({'side': 'LONG', 'position_effect': 'CLOSE'}, '做多'),
        ({'direction': '做空', 'position_effect': 'OPEN'}, '做空'),
        ({'side': 'BUY', 'reason': 'UNCLASSIFIED'}, '—'),
    ]

    for fill, expected in cases:
        assert local_dashboard.detail_fill_values(fill)[3] == expected
    assert local_dashboard.fill_values({'side': 'SELL', 'rationale': 'ENTRY_NEXT_OPEN'})[2] == '空'
    assert local_dashboard.fill_values({'side': 'BUY', 'rationale': 'STOP_QUOTE'})[2] == '空'


def test_detail_popup_is_owned_raised_and_focused_over_console():
    calls = []

    class Window:
        def transient(self, owner): calls.append(('transient', owner))
        def lift(self): calls.append(('lift',))
        def focus_set(self): calls.append(('focus_set',))
        def bind(self, event, callback, add=None):
            calls.append(('bind', event, add))
            self.destroy_callback = callback

    class Owner: pass
    owner = Owner()
    window = Window()
    local_dashboard.configure_detail_popup(window, owner)

    assert calls == [('transient', owner), ('lift',), ('focus_set',), ('bind', '<Destroy>', '+')]
    assert list(owner._detail_popup_refs.values()) == [window]

    class Event: pass
    event = Event(); event.widget = window
    window.destroy_callback(event)
    assert owner._detail_popup_refs == {}


def test_v2_rule_expression_renderer_preserves_nested_logic_offsets_and_unknown_ops():
    expr = {'op': 'and', 'args': [
        {'op': 'gt', 'args': [{'op': 'close', 'args': [0]}, {'op': 'param', 'args': ['entry_level']}]},
        {'op': 'or', 'args': [
            {'op': 'sma', 'args': [20, 1]},
            {'op': 'future_operator', 'args': ['x', 7]},
        ]},
    ]}
    rendered = local_dashboard.explain_v2_expr(expr, {'entry_level': 100})
    assert '且' in rendered and '或' in rendered
    assert '收盘价（最新已收盘K线）' in rendered
    assert '参数 entry_level = 100' in rendered
    assert 'SMA(20, 向前1根K线)' in rendered
    assert '未识别表达式' in rendered and 'future_operator' in rendered and '7' in rendered


def test_rule_source_authority_never_promotes_unconfirmed_local_files():
    assert local_dashboard.rule_source_authority('D', {}, {'file': 'config/strategy_d/D_strategy_15m.json'})['confirmed'] is False
    assert local_dashboard.rule_source_authority('C', {'C': {'version': 'V3.1', 'sha256': 'abc'}},
                                                 {'version': 'V3.1', 'sha256': 'abc'})['confirmed'] is True
    assert local_dashboard.rule_source_authority('C', {'C': {'version': 'V3.1', 'sha256': 'abc'}},
                                                 {'version': 'V3.0', 'sha256': 'old'})['confirmed'] is False


def test_abc_trade_page_reads_latest_fills_with_read_only_bounded_query(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / 'paper.sqlite3'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE fills(fill_id TEXT, agent TEXT, occurred_at TEXT, trade_id TEXT)')
        db.executemany('INSERT INTO fills VALUES(?,?,?,?)', [
            ('f1', 'C', '2026-01-01T00:00:00+00:00', 't1'),
            ('f2', 'C', '2026-01-02T00:00:00+00:00', 't2'),
            ('f3', 'A', '2026-01-03T00:00:00+00:00', 't3'),
        ])
    monkeypatch.setenv('BTC_LAB_DB', str(db_path))

    page = local_dashboard.read_ledger_fills_page('C', limit=1, offset=1)

    assert [fill['fill_id'] for fill in page] == ['f1']
    with sqlite3.connect(db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM fills').fetchone()[0] == 3


def test_pa_rule_details_read_active_version_even_when_strategy_is_paused(monkeypatch):
    import json

    sid = 'pa-trend-40-breakout'
    doc = {
        'format': 'btc-lab-declarative-strategy/v2', 'strategy_id': sid, 'version': '1.0.0',
        'name': 'PA-01', 'market': 'BTC/USD', 'timeframe': '15m', 'description': 'test',
        'parameters': {}, 'risk': {'initial_equity': '10000', 'risk_fraction': '0.02', 'max_leverage': 5},
        'entry': {'long': {'op': 'eq', 'args': [{'op': 'close', 'args': [0]}, {'op': 'close', 'args': [1]}]},
                  'short': {'op': 'eq', 'args': [{'op': 'close', 'args': [0]}, {'op': 'close', 'args': [1]}]}},
        'position': {'direction': 'both', 'entry_timing': 'next_open', 'stop': {'kind': 'swing_atr'}, 'target_r': 2,
                     'max_hold_bars': 12, 'same_bar_priority': 'stop'},
    }
    item = {'id': sid, 'enabled': False, 'status': 'PAUSED', 'active_version': '1.0.0',
            'position_direction': 'FLAT', 'equity': '10000', 'last_action': 'WAIT'}
    def fake_api(path, **kwargs):
        if path == '/api/v1/system/status': return {'cost_model': {}}
        if path == '/api/v1/strategy-pool': return {'items': [item]}
        if path == f'/api/v1/strategy-pool/{sid}/versions':
            return {'items': [{'version': '1.0.0', 'sha256': 'abc', 'document': json.dumps(doc)}]}
        raise AssertionError(path)

    monkeypatch.setattr(local_dashboard, 'api_json', fake_api)
    monkeypatch.setattr(local_dashboard, 'pa_trade_fills', lambda strategy_id: [])

    result = local_dashboard._collect_rule_detail(sid, {'id': sid, 'name': 'PA-01', 'enabled': False})

    assert result['row']['enabled'] is False
    assert result['raw_json'] is not None
    assert '活动版本' in result['rules']


def test_pa_rows_use_registered_ids_and_show_distinct_states_and_flat_account():
    items = [
        {
            'id': 'pa-trend-40-breakout', 'enabled': True, 'status': 'RUNNING',
            'position_direction': 'FLAT', 'position_amount': '0', 'equity': '10000',
            'realized': '0', 'unrealized': '0', 'fees': '0',
            'last_cycle': '2026-11-12T13:30:00+00:00', 'last_action': 'WAIT', 'position': None,
        },
        {
            'id': 'pa-sma20-pullback-confirm', 'enabled': True, 'status': 'ENABLED_NOT_RUNNING',
            'position_direction': 'PENDING_SHORT', 'position_amount': '0', 'equity': '10000',
            'realized': '0', 'unrealized': '0', 'fees': '0', 'last_cycle': None,
            'last_action': 'SIGNAL_SHORT', 'position': {'pending_entry': True, 'direction': 'SHORT'},
        },
        {
            'id': 'pa-full-engulfing-reversal', 'enabled': False, 'status': 'PAUSED',
            'position_direction': 'FLAT', 'position_amount': '0', 'equity': '10000',
            'realized': '0', 'unrealized': '0', 'fees': '0', 'last_cycle': None,
            'last_action': 'WAIT', 'position': None,
        },
        {
            'id': 'pa-gapless-three-bar-reversal', 'enabled': True, 'status': 'FAULTED',
            'position_direction': 'FLAT', 'position_amount': '0', 'equity': None,
            'realized': '0', 'unrealized': None, 'fees': '0', 'last_cycle': None,
            'last_action': 'BLOCKED:INVALID_BARS', 'position': None,
        },
    ]

    rows = local_dashboard.console_rows({'A': {'state': 'FLAT'}}, items)

    assert list(rows) == ['A', 'B', 'C', 'D', 'E', 'G', *local_dashboard.PA_STRATEGY_IDS]
    assert rows['pa-trend-40-breakout']['name'] == 'PA-01 顺势突破'
    assert rows['pa-trend-40-breakout']['state'] == '空仓'
    assert rows['pa-trend-40-breakout']['amount'] == '0.00'
    assert rows['pa-trend-40-breakout']['leverage'] == '\u7a7a\u4ed3'
    assert rows['pa-trend-40-breakout']['cycle'] == '2026-11-12 21:30'
    assert rows['pa-sma20-pullback-confirm']['status_label'] == '已启用待周期'
    assert rows['pa-sma20-pullback-confirm']['state'] == '待开空'
    assert rows['pa-full-engulfing-reversal']['status_label'] == '已暂停'
    assert rows['pa-gapless-three-bar-reversal']['status_label'] == '故障'


def test_strategy_pool_failure_keeps_abcde_rows_and_marks_only_pa_rows_as_error(monkeypatch):
    def fake_fetch(path, port=8011):
        if path == '/api/v1/system/status':
            return {'feed_health': 'LIVE'}
        if path == '/api/v1/strategy-pool':
            raise RuntimeError('connection refused')
        if path == '/api/v1/strategy-control':
            return {key: {'state': 'RUNNING'} for key in 'ABCE'}
        if path == '/api/v1/statistics/abc':
            return {'agents': {key: {} for key in 'ABC'}}
        if path == '/api/v1/agents':
            return [{'agent': key, 'account': {'state': 'FLAT'}} for key in 'ABC']
        if path == '/api/v1/e/accounts':
            return {'formal': {'account': {'state': 'FLAT'}, 'status': 'READY', 'fills': []}}
        if path == '/api/v1/fills':
            return {'items': []}
        if path == '/api/v1/d/status':
            return {'status': 'AUTO_PAPER', 'account': {'state': 'FLAT'}, 'fills': []}
        raise AssertionError(path)

    monkeypatch.setattr(local_dashboard, 'fetch', fake_fetch)

    _, rows = local_dashboard.snapshot()

    assert all(agent in rows for agent in 'ABCDE')
    assert all(rows[sid]['status_label'] == '读取失败' for sid in local_dashboard.PA_STRATEGY_IDS)
    assert all('connection refused' in rows[sid]['action'] for sid in local_dashboard.PA_STRATEGY_IDS)


def test_pa_fill_history_uses_strategy_id_endpoint(monkeypatch):
    requested = []

    def fake_api_json(path):
        requested.append(path)
        return {'items': [{'cycle': '2026-11-12T13:30:00+00:00', 'side': 'SELL'}]}

    monkeypatch.setattr(local_dashboard, 'api_json', fake_api_json)

    fills = local_dashboard.pa_trade_fills('pa-trend-40-breakout')

    assert requested == ['/api/v1/strategy-pool/pa-trend-40-breakout/fills?limit=2000']
    assert fills[0]['side'] == 'SELL'


def test_pa_control_targets_the_real_id_and_selected_version(monkeypatch):
    calls = []
    monkeypatch.setattr(local_dashboard, 'api_json', lambda path, **kwargs: calls.append((path, kwargs)) or {'ok': True})

    result = local_dashboard.set_pa_enabled('pa-trend-40-breakout', '1.0.0', False)

    assert result == {'ok': True}
    assert calls == [('/api/v1/strategy-pool/pa-trend-40-breakout/enable', {
        'method': 'POST', 'payload': {'version': '1.0.0', 'enabled': False},
    })]


def test_switch_state_tracks_real_pa_and_legacy_controls_and_disables_stale_rows():
    sid = 'pa-trend-40-breakout'
    assert local_dashboard.console_control_state(sid, {'enabled': True, 'status_label': '运行中'}) == 'on'
    assert local_dashboard.console_control_state(sid, {'enabled': False, 'status_label': '已暂停'}) == 'off'
    assert local_dashboard.console_control_state(sid, {'enabled': False, 'status_label': '数据过期'}) == 'disabled'
    assert local_dashboard.console_control_state('A', {'control': 'RUNNING'}) == 'on'
    assert local_dashboard.console_control_state('A', {'control': 'PAUSED'}) == 'off'
    assert local_dashboard.console_control_state('D', {'control': 'RUNNING', 'enabled': True}) == 'on'
    assert local_dashboard.console_control_state('D', {'control': 'PAUSED', 'enabled': False}) == 'off'
    assert local_dashboard.console_control_state('D', {'control': 'UNKNOWN'}) == 'disabled'
    assert local_dashboard.console_control_state('G', {'enabled': True, 'control': 'G_MIRROR'}) == 'disabled'


def test_status_badges_simplify_scheduler_but_keep_real_values_for_tooltips():
    badges = local_dashboard.system_status_badges({
        'scheduler': 'ENABLED_INTERNAL_PAPER', 'feed_health': 'LIVE',
        'quote_health': 'FRESH', 'websocket_recovery_state': 'HEALTHY',
    }, 200, 2)

    assert [badge['label'] for badge in badges] == [
        '节点 2', 'HTTP 200', '模拟交易正常', '行情 LIVE', '报价 FRESH', 'WS HEALTHY',
    ]
    assert badges[2]['raw'] == 'ENABLED_INTERNAL_PAPER'
    assert all(badge['tone'] in ('primary', 'success') for badge in badges)


def test_status_badges_show_error_and_unknown_as_danger_and_neutral():
    badges = local_dashboard.system_status_badges({
        'scheduler': 'FAULTED', 'feed_health': 'UNKNOWN',
        'quote_health': 'STALE', 'websocket_recovery_state': None,
    }, '不可用', None)

    assert badges[0]['tone'] == 'neutral'
    assert badges[1]['tone'] == 'danger'
    assert badges[2]['tone'] == 'danger'
    assert badges[3]['tone'] == 'neutral'
    assert badges[4]['tone'] == 'warning'
    assert badges[5]['tone'] == 'neutral'


def test_strategy_status_appearance_uses_distinct_semantic_tones():
    assert local_dashboard.strategy_status_appearance('运行中') == {'icon': '▶', 'tone': 'success'}
    assert local_dashboard.strategy_status_appearance('已暂停') == {'icon': 'Ⅱ', 'tone': 'neutral'}
    assert local_dashboard.strategy_status_appearance('故障') == {'icon': '!', 'tone': 'danger'}
    assert local_dashboard.strategy_status_appearance('已启用待周期') == {'icon': '◷', 'tone': 'warning'}


def test_strategy_table_fields_split_id_name_and_runtime_status():
    rows = local_dashboard.console_rows({}, [{
        'id': 'pa-trend-40-breakout', 'enabled': True, 'status': 'RUNNING',
        'position_direction': 'SHORT', 'position_amount': '12000', 'equity': '10001.25',
        'realized': '-1.25', 'unrealized': '2.50', 'fees': '0.75',
        'last_cycle': '2026-11-12T14:30:00+00:00', 'last_action': 'HOLD',
    }])

    view = local_dashboard.strategy_view('pa-trend-40-breakout', rows['pa-trend-40-breakout'])

    assert view['strategy_id'] == 'PA-01'
    assert view['name'] == '顺势突破'
    assert view['status'] == '运行中'
    assert view['position'] == '做空'
    assert view['action'] == '持有'


def test_filter_and_summary_use_dynamic_real_rows_and_all_account_equity():
    rows = {
        'A': {'name': 'A', 'status_label': '已暂停', 'state': 'FLAT', 'equity': '9000.00'},
        'pa-trend-40-breakout': {'name': '顺势突破', 'status_label': '运行中', 'state': '做多', 'equity': '10000.50'},
        'pa-full-engulfing-reversal': {'name': '双重吞噬反转', 'status_label': '故障', 'state': '空仓', 'equity': '—'},
    }

    assert local_dashboard.filter_console_rows(rows, 'PA', '全部状态', 'engulf') == ['pa-full-engulfing-reversal']
    assert local_dashboard.filter_console_rows(rows, '全部', '运行中', '') == ['pa-trend-40-breakout']
    assert local_dashboard.console_summary(rows) == {
        'total': 3, 'running': 1, 'positions': 1, 'equity': '19,000.50',
    }


def test_g_mirror_row_uses_independent_read_only_account_and_category():
    status = {'enabled': True, 'follow_state': 'WAIT_FOR_C_NEXT_OPEN', 'status': 'WAIT_FOR_C_NEXT_OPEN',
              'position_direction': None, 'position_amount': '0', 'equity': '10000', 'realized': '0',
              'unrealized': '0', 'fees': '0', 'last_cycle': '2026-11-13T12:00:00+00:00',
              'last_action': 'WAIT_FOR_C_NEXT_OPEN'}
    rows = local_dashboard.console_rows({}, [], g_status=status)
    assert list(rows) == ['A', 'B', 'C', 'D', 'E', 'G', *local_dashboard.PA_STRATEGY_IDS]
    assert rows['G']['status_label'] == '等待C开仓'
    assert rows['G']['state'] == '空仓'
    assert rows['G']['leverage'] == '\u7a7a\u4ed3'
    assert rows['G']['equity'] == '10,000.00'
    assert local_dashboard.filter_console_rows(rows, '镜像', '全部状态') == ['G']
    assert len(local_dashboard.filter_console_rows(rows, '原有', '全部状态')) == 5
    assert len(local_dashboard.filter_console_rows(rows, 'PA', '全部状态')) == 8
    assert len(local_dashboard.filter_console_rows(rows, '全部', '全部状态')) == 14


def test_pool_api_error_keeps_last_account_values_but_marks_state_stale():
    previous = {'pa-trend-40-breakout': {
        'name': 'PA-01 顺势突破', 'status_label': '运行中', 'state': '做空',
        'equity': '10,010.00', 'realized': '12.00', 'unrealized': '-2.00',
        'fees': '1.00', 'amount': '15,000.00', 'cycle': '2026-11-12 22:30',
        'action': 'HOLD', 'enabled': True,
    }}
    current = {'pa-trend-40-breakout': {
        'name': 'PA-01 顺势突破', 'status_label': '读取失败', 'state': '—',
        'equity': None, 'action': '策略池读取失败：offline', 'enabled': False,
    }}

    row = local_dashboard.keep_cached_rows(previous, current)['pa-trend-40-breakout']

    assert row['equity'] == '10,010.00'
    assert row['state'] == '做空'
    assert row['status_label'] == '数据过期'
    assert 'offline' in row['action']
    assert row['enabled'] is False


def test_calendar_assigns_entry_and_exit_fees_to_realized_close_date_only():
    fills = [
        {'trade_id': 't1', 'position_effect': 'OPEN', 'position_direction': 'LONG', 'side': 'BUY',
         'quantity': '1', 'price': '100', 'fee': '1', 'occurred_at': '2026-11-12T15:50:00+00:00'},
        {'trade_id': 't1', 'position_effect': 'CLOSE', 'position_direction': 'LONG', 'side': 'SELL',
         'quantity': '1', 'price': '110', 'fee': '1', 'occurred_at': '2026-11-13T15:00:00+00:00'},
    ]

    result = local_dashboard.calculate_pnl_calendar(fills, 'A', 2026, 11)

    assert result['daily']['2026-11-13']['net'] == local_dashboard.Decimal('8')
    assert result['daily']['2026-11-13']['fees'] == local_dashboard.Decimal('2')
    assert '2026-11-12' not in result['daily']
    assert result['trading_days'] == 2
    assert result['closed_trade_results'] == [local_dashboard.Decimal('8')]


def test_calendar_separates_zero_result_from_no_trade_day_and_counts_complete_trades():
    fills = [
        {'trade_id': 't0', 'position_effect': 'OPEN', 'position_direction': 'SHORT', 'side': 'SELL',
         'quantity': '2', 'price': '100', 'fee': '0', 'occurred_at': '2026-11-02T00:00:00+00:00'},
        {'trade_id': 't0', 'position_effect': 'CLOSE', 'position_direction': 'SHORT', 'side': 'BUY',
         'quantity': '2', 'price': '100', 'fee': '0', 'occurred_at': '2026-11-02T00:15:00+00:00'},
    ]

    result = local_dashboard.calculate_pnl_calendar(fills, 'C', 2026, 11)

    assert result['daily']['2026-11-02']['net'] == local_dashboard.Decimal('0')
    assert '2026-11-03' not in result['daily']
    assert result['closed_trade_results'] == [local_dashboard.Decimal('0')]
    assert result['win_rate'] == 0


def test_calendar_uses_pa_realized_fill_prices_and_does_not_double_subtract_slippage():
    fills = [
        {'idempotency_key': 's:open:x', 'side': 'SELL', 'qty': '1', 'price': '101', 'fee': '1', 'slippage': '1',
         'reason': 'ENTRY_NEXT_OPEN', 'cycle': '2026-11-01T00:00:00+00:00', 'created_at': '2026-11-01T00:00:01+00:00'},
        {'idempotency_key': 's:close:x', 'side': 'BUY', 'qty': '1', 'price': '91', 'fee': '1', 'slippage': '1',
         'reason': 'STOP_QUOTE', 'cycle': '2026-11-02T00:00:00+00:00', 'created_at': '2026-11-02T00:00:01+00:00'},
    ]

    result = local_dashboard.calculate_pnl_calendar(fills, 'pa-inside-bar-break', 2026, 11)

    assert result['daily']['2026-11-02']['net'] == local_dashboard.Decimal('8')
    assert result['daily']['2026-11-02']['fees'] == local_dashboard.Decimal('2')
    assert result['closed_trade_results'] == [local_dashboard.Decimal('8')]
    assert result['incomplete'] == []


def test_calendar_marks_unpairable_closes_incomplete_instead_of_estimating():
    fills = [{'trade_id': 'orphan', 'position_effect': 'CLOSE', 'side': 'BUY', 'quantity': '1', 'price': '10',
              'fee': '1', 'occurred_at': '2026-11-03T00:00:00+00:00'}]

    result = local_dashboard.calculate_pnl_calendar(fills, 'B', 2026, 11)

    assert result['daily'] == {}
    assert result['incomplete']
    assert result['monthly_net'] == local_dashboard.Decimal('0')


def test_calendar_partials_realize_on_each_close_date_and_count_full_trade_on_final_exit():
    fills = [
        {'trade_id': 'tp', 'position_effect': 'OPEN', 'position_direction': 'LONG', 'side': 'BUY',
         'quantity': '2', 'price': '100', 'fee': '4', 'occurred_at': '2026-11-01T00:00:00+00:00'},
        {'trade_id': 'tp', 'position_effect': 'CLOSE', 'position_direction': 'LONG', 'side': 'SELL',
         'quantity': '1', 'price': '110', 'fee': '2', 'occurred_at': '2026-11-02T00:00:00+00:00'},
        {'trade_id': 'tp', 'position_effect': 'CLOSE', 'position_direction': 'LONG', 'side': 'SELL',
         'quantity': '1', 'price': '90', 'fee': '2', 'occurred_at': '2026-12-01T00:00:00+00:00'},
    ]

    november = local_dashboard.calculate_pnl_calendar(fills, 'C', 2026, 11)
    december = local_dashboard.calculate_pnl_calendar(fills, 'C', 2026, 12)

    assert november['daily']['2026-11-02']['net'] == local_dashboard.Decimal('6')
    assert november['closed_trade_results'] == []
    assert december['daily']['2026-12-01']['net'] == local_dashboard.Decimal('-14')
    assert december['closed_trade_results'] == [local_dashboard.Decimal('-8')]


def test_calendar_trusts_g_closed_fill_net_and_allocates_fees_without_second_slippage_cost():
    fills = [
        {'g_trade_id': 'g1', 'effect': 'OPEN', 'direction': 'SHORT', 'quantity': '2', 'price': '100',
         'fee': '2', 'slippage': '0.4', 'pnl': '0', 'occurred_at': '2026-11-01T00:00:00+00:00'},
        {'g_trade_id': 'g1', 'effect': 'PARTIAL_CLOSE', 'direction': 'SHORT', 'quantity': '1', 'price': '90',
         'fee': '1', 'slippage': '0.2', 'pnl': '5', 'occurred_at': '2026-11-02T00:00:00+00:00'},
    ]

    result = local_dashboard.calculate_pnl_calendar(fills, 'G', 2026, 11)

    assert result['daily']['2026-11-02']['net'] == local_dashboard.Decimal('5')
    assert result['daily']['2026-11-02']['fees'] == local_dashboard.Decimal('2')
    assert result['closed_trade_results'] == []


def test_calendar_does_not_fifo_pair_pa_exit_across_an_unclosed_prior_entry():
    fills = [
        {'side': 'SELL', 'qty': '2', 'price': '100', 'fee': '1', 'reason': 'ENTRY_NEXT_OPEN',
         'cycle': '2026-11-01T00:00:00+00:00', 'created_at': '2026-11-01T00:00:01+00:00'},
        {'side': 'SELL', 'qty': '1', 'price': '100', 'fee': '1', 'reason': 'ENTRY_NEXT_OPEN',
         'cycle': '2026-11-02T00:00:00+00:00', 'created_at': '2026-11-02T00:00:01+00:00'},
        {'side': 'BUY', 'qty': '1', 'price': '90', 'fee': '1', 'reason': 'TARGET_QUOTE',
         'cycle': '2026-11-02T01:00:00+00:00', 'created_at': '2026-11-02T01:00:01+00:00'},
    ]

    result = local_dashboard.calculate_pnl_calendar(fills, 'pa-inside-bar-break', 2026, 11)

    assert result['daily']['2026-11-02']['net'] == local_dashboard.Decimal('8')
    assert result['closed_trade_results'] == [local_dashboard.Decimal('8')]
    assert any('前一笔PA持仓没有平仓成交' in error for error in result['incomplete'])


def test_account_detail_metrics_uses_realized_ledger_when_account_endpoint_omits_it():
    fills = [
        {'trade_id': 't1', 'position_effect': 'OPEN', 'position_direction': 'LONG', 'side': 'BUY',
         'quantity': '1', 'price': '100', 'fee': '1', 'occurred_at': '2026-11-12T00:00:00+00:00'},
        {'trade_id': 't1', 'position_effect': 'CLOSE', 'position_direction': 'LONG', 'side': 'SELL',
         'quantity': '1', 'price': '110', 'fee': '1', 'occurred_at': '2026-11-12T01:00:00+00:00'},
    ]

    metrics = local_dashboard.account_detail_metrics('A', {}, {'equity': '10008'}, fills)

    assert metrics['realized'] == '8.00'
    assert metrics['closed_count'] == 1
    assert metrics['equity'] == '10,008.00'


def test_detail_number_accepts_console_values_with_thousands_separators():
    assert local_dashboard._detail_number('9,952.66') == '9,952.66'


def test_account_detail_footer_is_reserved_before_expandable_trade_table():
    import inspect

    source = inspect.getsource(local_dashboard.open_strategy_details)

    assert source.index("controls.pack(side='bottom'") < source.index('head.pack(')
    assert source.index("controls.pack(side='bottom'") < source.index('area.pack(')
