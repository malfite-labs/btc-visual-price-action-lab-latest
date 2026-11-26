"""Targeted tests for G benign risk-exit status and 16-strategy one-click recovery."""
import sqlite3
from pathlib import Path

from scripts.local_dashboard import (
    PA_STRATEGIES,
    console_rows,
    summarize_and_recover_strategies,
)


def _make_g_db(path: Path, *, enabled=1, position_json=None, follow_state='WAIT_FOR_C_NEXT_OPEN', error='MIRROR_RISK_EXIT'):
    with sqlite3.connect(str(path)) as db:
        db.execute(
            """CREATE TABLE g_account (
                singleton INTEGER PRIMARY KEY, cash TEXT, state TEXT, position_json TEXT,
                trade_count INTEGER, enabled INTEGER, follow_state TEXT, last_action TEXT,
                last_cycle TEXT, error TEXT
            )"""
        )
        db.execute(
            "INSERT INTO g_account VALUES (1,'9758.78','FLAT',?,84,?,?, 'MIRROR_RISK_EXIT','2026-11-26T06:04:46+00:00',?)",
            (position_json, enabled, follow_state, error),
        )
        db.commit()


def test_g_benign_risk_exit_displays_waiting_and_recovers_cleanly(tmp_path):
    g_db = tmp_path / 'mirror.sqlite3'
    _make_g_db(g_db, enabled=1, position_json=None, follow_state='WAIT_FOR_C_NEXT_OPEN', error='MIRROR_RISK_EXIT')
    g_status = {
        'enabled': True, 'follow_state': 'WAIT_FOR_C_NEXT_OPEN', 'position': None,
        'position_direction': None, 'last_error': 'MIRROR_RISK_EXIT', 'last_action': 'MIRROR_RISK_EXIT',
    }
    decisions = {a: {'control': 'RUNNING', 'state': 'FLAT'} for a in ('A', 'B', 'C', 'D', 'E')}
    pa_items = [{'id': sid, 'status': 'RUNNING', 'enabled': True, 'position_direction': 'FLAT'} for sid, _ in PA_STRATEGIES]
    rows = console_rows(decisions, pa_items=pa_items, g_status=g_status)
    assert rows['G']['status_label'] == '等待C开仓'

    extra_pool = [
        {'id': 'PA-09', 'status': 'RUNNING', 'enabled': True, 'position_direction': 'FLAT', 'last_status': 'WAIT'},
        {'id': 'PA-10', 'status': 'RUNNING', 'enabled': True, 'position_direction': 'FLAT', 'last_status': 'WAIT'},
    ]
    report = summarize_and_recover_strategies({'feed_health': 'LIVE'}, rows, pa_items + extra_pool, g_db_path=g_db)
    assert report['total'] == 16
    assert report['healthy'] == 15
    assert len(report['recovered']) == 1
    assert report['protected'] == []
    assert report['paused'] == []

    with sqlite3.connect(str(g_db)) as db:
        assert db.execute('SELECT error FROM g_account WHERE singleton=1').fetchone()[0] is None


def test_real_g_error_and_paused_and_protected_strategies_are_never_cleared(tmp_path):
    g_db = tmp_path / 'mirror_real_err.sqlite3'
    _make_g_db(g_db, enabled=1, position_json=None, follow_state='WAIT_FOR_C_NEXT_OPEN', error='LEDGER_INTEGRITY_ERROR')
    g_status = {
        'enabled': True, 'follow_state': 'WAIT_FOR_C_NEXT_OPEN', 'position': None,
        'position_direction': None, 'last_error': 'LEDGER_INTEGRITY_ERROR', 'last_action': 'LEDGER_INTEGRITY_ERROR',
    }
    decisions = {
        'A': {'control': 'RUNNING', 'state': 'FLAT'},
        'B': {'control': 'PAUSED', 'state': 'FLAT'},
        'C': {'control': 'RUNNING', 'state': 'FLAT'},
        'D': {'control': 'PROTECTION_UNCERTAIN', 'state': 'FLAT', 'action': 'D_RAW_QUOTE_INVALID'},
        'E': {'control': 'RUNNING', 'state': 'FLAT'},
    }
    pa_items = [{'id': sid, 'status': 'RUNNING', 'enabled': True, 'position_direction': 'FLAT'} for sid, _ in PA_STRATEGIES]
    rows = console_rows(decisions, pa_items=pa_items, g_status=g_status)
    assert rows['G']['status_label'] == '异常'
    report = summarize_and_recover_strategies({}, rows, pa_items, g_db_path=g_db)
    assert report['recovered'] == []
    assert len(report['paused']) == 1  # B stays paused
    assert len(report['protected']) == 2  # D + G real error stay protected
    with sqlite3.connect(str(g_db)) as db:
        assert db.execute('SELECT error FROM g_account WHERE singleton=1').fetchone()[0] == 'LEDGER_INTEGRITY_ERROR'
