import asyncio
from backend.kline_observation import CandleReader, INTERVALS, normalize_event


def test_public_candle_intervals_and_unclosed_flag():
    class Response:
        def raise_for_status(self):pass
        def json(self):return [[1000,'100','110','90','105','2',1999,'200',3]]
    class Client:
        async def get(self,url,params):
            assert url=='https://fapi.binance.com/fapi/v1/klines'
            assert params['symbol']=='BTCUSDT' and params['interval']=='3m'
            return Response()
    async def run():
        reader=CandleReader(Client(),clock=lambda:1.5)
        row=(await reader.get('3m',limit=2))['items'][0]
        assert row['is_closed'] is False and row['open_at_ms']==1000
    asyncio.run(run())
    assert '1M' in INTERVALS and '1m' in INTERVALS


def test_event_normalization_never_guesses_effect_from_side():
    row={'fill_id':'x','side':'BUY','quantity':'1','price':'10','occurred_at':'2026-01-01T00:00:00+00:00'}
    event=normalize_event('A',row)
    assert event['verification_status']=='UNVERIFIED'
    row.update(position_effect='OPEN',position_direction='LONG',trade_id='t')
    assert normalize_event('A',row)['verification_status']=='VERIFIED'


def test_partial_exit_requires_actual_remaining_quantity():
    from backend.kline_observation import mark_position_lifecycle
    events=[{'event_id':'o','strategy_id':'D','trade_id':'t','occurred_at':'2026-01-01T00:00:00+00:00','position_effect':'OPEN','position_direction':'LONG','quantity':'2','verification_status':'VERIFIED'}, {'event_id':'c','strategy_id':'D','trade_id':'t','occurred_at':'2026-01-01T00:05:00+00:00','position_effect':'CLOSE','position_direction':'LONG','quantity':'1','verification_status':'VERIFIED'}]
    result=mark_position_lifecycle(events)
    assert result[1]['position_effect']=='PARTIAL_CLOSE'
    assert result[1]['remaining_quantity']=='1'


def test_unmatched_close_stays_unverified():
    from backend.kline_observation import mark_position_lifecycle
    close={'event_id':'c','strategy_id':'G','trade_id':'t','occurred_at':'2026-01-01T00:00:00+00:00','position_effect':'CLOSE','position_direction':'SHORT','quantity':'1','verification_status':'VERIFIED'}
    assert mark_position_lifecycle([close])[0]['verification_status']=='UNVERIFIED'


def test_event_reader_queries_sqlite_in_read_only_mode(tmp_path):
    import sqlite3,hashlib
    from backend.kline_observation import EventReader
    data=tmp_path/'data';data.mkdir();path=data/'paper.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('create table fills (fill_id text,agent text,trade_id text,occurred_at text,position_effect text,position_direction text,quantity text,price text,side text,rationale text,strategy_version text)')
        db.execute("insert into fills values ('f','A','t','2026-01-01T00:00:00+00:00','OPEN','LONG','1','10','BUY','rule','1')")
    before=hashlib.sha256(path.read_bytes()).digest()
    events=EventReader(tmp_path).read()
    assert len(events)==1 and events[0]['strategy_id']=='A' and events[0]['verification_status']=='VERIFIED'
    assert hashlib.sha256(path.read_bytes()).digest()==before


def test_routes_use_observation_readers_without_trading_actions(monkeypatch):
    from backend import app
    class Candles:
        async def get(self,interval,limit,end_time):return {'interval':interval,'items':[]}
    class Events:
        errors=[]
        def read(self):return [{'event_id':'1'}]
    monkeypatch.setattr(app,'observation_candles',Candles())
    monkeypatch.setattr(app,'observation_events',Events())
    assert asyncio.run(app.observation_candle_route('15m',20,None))['interval']=='15m'
    assert asyncio.run(app.observation_events_route())['items'][0]['event_id']=='1'
