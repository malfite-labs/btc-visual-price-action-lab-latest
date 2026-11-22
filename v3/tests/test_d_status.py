import json
import sqlite3
from datetime import datetime, timezone
from backend.d_ledger import DLedger
from backend.d_status import read_execution_account


def test_status_missing_ledger_does_not_create_it(tmp_path):
    path = tmp_path / 'missing.sqlite3'
    assert read_execution_account(path, None, datetime.now(timezone.utc))['status'] == 'UNAVAILABLE'
    assert not path.exists()


def test_status_live_position_is_valued_only_from_valid_quote(tmp_path):
    path = tmp_path / 'd.sqlite3'
    DLedger(path)
    position = {'direction': 'LONG', 'quantity': '0.5', 'entry_price': '100', 'leverage': '5'}
    with sqlite3.connect(path) as db:
        db.execute('UPDATE d_account SET cash=?,position_json=? WHERE id=1', ('9999.97', json.dumps(position)))
        db.execute('INSERT INTO d_fills VALUES (?,?,?,?,?,?,?,?,?)', ('f','t','OPEN','0.5','100','0.03','open','2026-11-05T12:00:00+00:00','key'))
    now = datetime(2026, 11, 5, 12, 0, 2, tzinfo=timezone.utc)
    quote = {'symbol':'BTC/USD','source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'bid':'110','ask':'110.01','event_id':'q','event_at':'2026-11-05T12:00:01+00:00','received_at':'2026-11-05T12:00:01+00:00'}
    good = read_execution_account(path, quote, now)
    assert good['equity'] == '10004.97' and good['margin'] == '10.00'
    assert good['fills'][0]['position_effect'] == 'OPEN' and good['fills'][0]['position_direction'] == 'LONG'
    bad = read_execution_account(path, {**quote, 'source':'UNKNOWN'}, now)
    assert bad['reason'] == 'VALUATION_UNAVAILABLE' and bad['equity'] is None

