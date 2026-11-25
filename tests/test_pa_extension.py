import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.pa_extension import PAExtension
from test_pa09_pa10_execution import candles
from test_pa09_pa10_candidates import bar

def test_pa_entry_gate_uses_market_recovery_status(monkeypatch):
    from backend import app
    quote = {'source': 'BINANCE_USDM_PERP', 'timestamp_valid': True, 'age_ms': 1}
    monkeypatch.setattr(app, 'market_status', {'feed_health': 'LIVE', 'gap_count_288': 0,
                                               'startup_position_monitor_checked': True, 'quote': quote})
    monkeypatch.setattr(app.market, 'status', {'websocket_recovery_state': 'HEALTHY'})
    monkeypatch.setattr(app, '_quote_with_live_age', lambda value: value)
    assert app._pa_quote_entries_allowed() is True
    app.market.status['websocket_recovery_state'] = 'RECOVERING'
    assert app._pa_quote_entries_allowed() is False

class Wiring(unittest.TestCase):
    def test_phase_and_protected_quotes(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)
            ext=PAExtension(root,register=True,enabled={'PA-09','PA-10'})
            for sid in ext.ledgers:
                seq,px=candles(sid,sid=='PA-10')
                for i,b in enumerate(seq):
                    ext.ledger_cycle(sid,seq[max(0,i-60):i+1],
                                     datetime.fromisoformat(bar(i+1)['open_at']),True)
                    ext=PAExtension(root,register=False,enabled={'PA-09','PA-10'})
                q=dict(event_at=bar(len(seq))['open_at'],received_at=bar(len(seq))['open_at'],
                       bid=px,ask=px,timestamp_valid=True,source='BINANCE_USDM_PERP',
                       symbol='BTC/USD',currency='USD_RESEARCH',age_ms=0)
                ledger=ext.ledgers[sid]
                self.assertEqual(ext.ledger_quote(sid,q,lambda:False),'ENTRY_BLOCKED')
                self.assertEqual(ext.ledger_quote(sid,q,lambda:True),'DUPLICATE_OR_STALE')
                self.assertEqual(ledger.fills(),[])
    def test_actual_backend_callbacks(self):
        import os, subprocess, sys
        with TemporaryDirectory() as temp:
            env=os.environ.copy()
            env.update({key:str(Path(temp)/name) for key,name in {
                'BTC_LAB_DB':'core.sqlite3','BTC_LAB_CANDLE_DB':'candles.sqlite3',
                'BTC_LAB_STRATEGY_POOL_DB':'pool.sqlite3'}.items()})
            env.update(BTC_LAB_TEST_MODE='true', BTC_LAB_DATA_DIR=temp,
                       BTC_PA_EXTENSION_REGISTER='true',
                       PAPER_AUTOMATION_ENABLED='false')
            code=r"""
import asyncio, os, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
assert Path(os.environ['BTC_LAB_DATA_DIR']).resolve()!=Path('data').resolve()
sys.path.insert(0,'tests')
from test_pa09_pa10_execution import candles
from test_pa09_pa10_candidates import bar
import backend.app as app
from backend.pa_extension import PAExtension
from backend.domain import Decimal

def quote(at,bid,ask):
    return dict(event_at=at,received_at=datetime.now(timezone.utc).isoformat(),
                bid=str(bid),ask=str(ask),timestamp_valid=True,source=app.market.source,
                symbol='BTC/USD',currency='USD_RESEARCH',age_ms=0)

async def run():
    # Exercise real callbacks without starting the ASGI lifespan or market client.
    app._pa_quote_entries_allowed=lambda: True
    app._quote_with_live_age=lambda q:q  # replay transport clock; quote validation is real
    root=Path(os.environ['BTC_LAB_DATA_DIR'])
    app.pa_extension=PAExtension(root/'pa-v1-extensions',register=True,enabled={'PA-09','PA-10'},cost=app.COST_MODEL)
    for i in range(8):
        sid=f'fixture-{i}'
        doc=dict(format='btc-lab-declarative-strategy/v2',strategy_id=sid,version='1.0.0',
                 name=sid,market='BTC/USD',timeframe='15m',description='isolated coexistence',
                 parameters={},risk=dict(initial_equity='10000',currency='USD_RESEARCH',max_leverage=5,risk_fraction='0.02'),
                 entry={x:dict(op='gt',args=[1,2]) for x in ('long','short')},
                 position=dict(direction='both',entry_timing='next_open',same_bar_priority='stop',
                     stop=dict(kind='swing_atr',lookback=3,atr_period=14,buffer_atr=.1,min_distance_atr=.5,max_distance_atr=3),
                     target_r=2,max_hold_bars=96))
        app.strategy_pool.upload(doc)
        app.strategy_pool.enable(sid,'1.0.0')
    legacy=app.strategy_pool.list()
    a=Path(app.strategy_pool.path).read_bytes()
    # New strategy state is never written into the shared PA database.
    assert len((await app.strategy_pool_list())['items'])==10
    assert Path(app.strategy_pool.path).read_bytes()==a
    for sid,short,shift in [('PA-09',False,0),('PA-10',True,200)]:
        app.pa_extension=PAExtension(root/'pa-v1-extensions',enabled={sid},cost=app.COST_MODEL)
        seq,px=candles(sid,short)
        seq=[dict(k,open_at=bar(i+shift)['open_at'],close_at=bar(i+shift+1)['open_at']) for i,k in enumerate(seq)]
        for i,k in enumerate(seq):
            await app._dispatch_strategy_pool_cycle(seq[max(0,i-60):i+1],datetime.fromisoformat(k['close_at']),None)
        s=app.pa_extension.ledgers[sid].snapshot()
        assert s['pending']['side']==('SHORT' if short else 'LONG'),s
        q=quote(seq[-1]['close_at'],Decimal(str(px))-Decimal('.01'),px)
        await app.process_stop_quote(q)
        assert len(app.pa_extension.ledgers[sid].fills())==1
        # Restart the adapter; exits must work with entry gate closed.
        app.pa_extension=PAExtension(root/'pa-v1-extensions',cost=app.COST_MODEL)
        app._pa_quote_entries_allowed=lambda: False
        db=app.pa_extension.ledgers[sid]
        stop=Decimal(db.snapshot()['position']['stop'])
        at=(datetime.fromisoformat(q['event_at'])+timedelta(seconds=1)).isoformat()
        exit_q=quote(at,stop if not short else stop-Decimal('.01'),stop+Decimal('.01') if not short else stop)
        await app.process_stop_quote(exit_q)
        await app.process_stop_quote(exit_q)
        assert [f['reason'] for f in db.fills()]==['ENTRY_NEXT_OPEN','STOP_QUOTE'],db.fills()
        app._pa_quote_entries_allowed=lambda: True
    assert not any(app.strategy_pool.fills(x['id']) for x in legacy)
asyncio.run(run())
"""
            out=subprocess.run([sys.executable,'-c',code],env=env,text=True,
                               encoding='utf-8',errors='replace',capture_output=True,timeout=45)
            self.assertEqual(out.returncode,0,out.stdout+out.stderr)



