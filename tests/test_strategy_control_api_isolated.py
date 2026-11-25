import os
from decimal import Decimal
from datetime import datetime, timezone

os.environ['BTC_LAB_OFFLINE'] = '1'
os.environ['PAPER_AUTOMATION_ENABLED'] = 'false'

from fastapi.testclient import TestClient
from backend import app as service
from backend.e_paper_store import EPaperStore
from backend.strategy_control import StrategyControl


import pytest


@pytest.fixture(autouse=True)
def rule_mode(monkeypatch):
    monkeypatch.setattr(service, 'formal_mode', 'RULE_DRIVEN')


def test_local_pause_and_resume_with_isolated_ledgers(tmp_path, monkeypatch):
    control = StrategyControl(tmp_path / 'control.sqlite')
    formal, shadow = (EPaperStore(tmp_path / name) for name in ('formal.sqlite', 'shadow.sqlite'))
    monkeypatch.setattr(service, '_strategy_control', lambda: control)
    monkeypatch.setattr(service, '_strategy_formal_store', lambda agent: formal)
    client = TestClient(service.app, client=('127.0.0.1', 12345))
    assert client.get('/api/v1/strategy-control').json()['E']['state'] == 'RUNNING'
    paused = client.post('/api/v1/strategy-control/E/pause')
    assert paused.status_code == 200 and paused.json()['state'] == 'PAUSED'
    assert not control.is_entry_allowed('E')
    assert client.post('/api/v1/strategy-control/E/resume').json()['state'] == 'RUNNING'
    assert client.post('/api/v1/strategy-control/E/pause', headers={'Origin': 'https://untrusted.example'}).status_code == 403


def test_missing_formal_never_reports_paused_and_resume_fails(tmp_path, monkeypatch):
    control = StrategyControl(tmp_path / 'control.sqlite')
    formal = EPaperStore(tmp_path / 'formal.sqlite')
    monkeypatch.setattr(service, '_strategy_control', lambda: control)
    monkeypatch.setattr(service, '_strategy_formal_store', lambda agent: None)
    client = TestClient(service.app, client=('127.0.0.1', 12345))
    result = client.post('/api/v1/strategy-control/E/pause').json()
    assert result['state'] == 'PAUSING' and result['formal'] == 'ERROR'
    assert client.post('/api/v1/strategy-control/E/resume').status_code == 409


def test_store_failure_still_blocks_entries(tmp_path, monkeypatch):
    control = StrategyControl(tmp_path / 'control.sqlite')
    monkeypatch.setattr(service, '_strategy_control', lambda: control)
    def fail(agent): raise ValueError('invalid identity')
    monkeypatch.setattr(service, '_strategy_formal_store', fail)
    client = TestClient(service.app, client=('127.0.0.1', 12345))
    result = client.post('/api/v1/strategy-control/E/pause').json()
    assert result['state'] == 'PAUSING' and result['formal'] == 'ERROR'
    assert not control.is_entry_allowed('E')


def test_visual_mode_cannot_use_rule_only_control(tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'formal_mode', 'VISUAL')
    client = TestClient(service.app, client=('127.0.0.1', 12345))
    assert client.post('/api/v1/strategy-control/A/pause').status_code == 409


def test_endpoint_closes_formal_position_and_guards_reentry(tmp_path, monkeypatch):
    import backend.e_paper_store as module
    monkeypatch.setattr(module, '__file__', str(tmp_path / 'backend/e_paper_store.py'))
    control = StrategyControl(tmp_path / 'data/strategy-control.sqlite3')
    formal, shadow = (EPaperStore(tmp_path / 'data/e' / name) for name in ('formal.sqlite3', 'shadow.sqlite3'))
    args = dict(agent='E', direction='LONG', quantity=Decimal('0.1'), price=Decimal('100'), stop=Decimal('90'),
                fee=Decimal('0.01'), slippage=Decimal('0'), idempotency_key='one', expected_version=0)
    assert formal.open_position(**args)['accepted']
    assert shadow.open_position(**args)['accepted']
    now = datetime.now(timezone.utc)
    quote = {'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
             'event_at':now.isoformat(),'received_at':now.isoformat(),'bid':'101','ask':'102'}
    monkeypatch.setattr(service, '_strategy_control', lambda: control)
    monkeypatch.setattr(service, '_strategy_formal_store', lambda agent: formal)
    monkeypatch.setattr(service, '_quote_with_live_age', lambda value: quote)
    client = TestClient(service.app, client=('127.0.0.1', 12345))
    assert client.post('/api/v1/strategy-control/E/pause').json()['state'] == 'PAUSED'
    assert formal.account('E')['state'] == 'FLAT'
    assert shadow.account('E')['state'] == 'OPEN_LONG'
    args.update(idempotency_key='two', expected_version=formal.account('E')['version'])
    assert formal.open_position(**args)['reason'] == 'STRATEGY_PAUSED'
    assert client.post('/api/v1/strategy-control/E/resume').json()['state'] == 'RUNNING'
    assert formal.open_position(**args)['accepted']

