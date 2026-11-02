from decimal import Decimal
from backend.e_structure import confirmed_pivots,structure_action

def bars(lows,highs=None):
    highs=highs or [x+10 for x in lows]
    return [{'low':str(l),'high':str(h),'close':str((l+h)/2),'is_closed':True} for l,h in zip(lows,highs)]

def test_strict_pivot_needs_three_closed_bars_on_each_side():
    rows=bars([5,4,3,1,3,4,5])
    assert confirmed_pivots(rows[:-1],'LOW')==[]
    assert confirmed_pivots(rows,'LOW')==[3]
    rows[4]['low']='1'
    assert confirmed_pivots(rows,'LOW')==[]

def test_higher_low_tightens_only_after_confirmation():
    rows=bars([8,7,6,2,6,7,8,7,6,4,6,7,8])
    result=structure_action(rows,direction='LONG',scale=Decimal('2'))
    assert result['action']=='TIGHTEN' and result['pivot_index']==9 and result['stop']==Decimal('3.70')
    assert structure_action(rows[:-1],direction='LONG',scale=Decimal('2'))['action']=='HOLD'

def test_break_of_confirmed_low_exits_but_equality_does_not():
    rows=bars([8,7,6,2,6,7,8]);rows[-1]['close']='2'
    assert structure_action(rows,direction='LONG',scale=Decimal('1'))['action']=='HOLD'
    rows[-1]['close']='1.99'
    assert structure_action(rows,direction='LONG',scale=Decimal('1'))['action']=='EXIT'

def test_short_uses_strict_lower_high_and_break():
    rows=bars([1]*13,[2,3,4,9,4,3,2,3,4,7,4,3,2])
    result=structure_action(rows,direction='SHORT',scale=Decimal('2'))
    assert result['action']=='TIGHTEN' and result['stop']==Decimal('7.30')
    rows[-1]['close']='7.01'
    assert structure_action(rows,direction='SHORT',scale=Decimal('2'))['action']=='EXIT'

def test_unclosed_bar_rejected():
    import pytest
    rows=bars([5,4,3,1,3,4,5]);rows[-1]['is_closed']=False
    with pytest.raises(ValueError,match='UNCLOSED_CANDLE'):confirmed_pivots(rows,'LOW')

def test_same_low_is_not_an_improvement():
    rows=bars([8,7,6,2,6,7,8,7,6,2,6,7,8])
    assert structure_action(rows,direction='LONG',scale=Decimal('2'))['action']=='HOLD'

def test_confirmed_structure_exit_persists_until_fresh_quote(tmp_path):
    from backend.e_structure import process_structure_close
    from backend.e_paper_store import EPaperStore
    from backend.e_protection import protect_e_accounts
    from backend.domain import CostModel
    from test_e_visual import bars as snapshot
    from datetime import datetime,timedelta
    rows=snapshot()
    for b in rows:b.update(symbol='BTC/USD',interval='15Min',source='ALPACA_US_BTCUSD_SPOT',currency='USD_RESEARCH')
    for i,low in enumerate([100,99,98,95,98,99,100],start=280):
        rows[i].update(open='105',high='110',low=str(low),close='105')
    rows[-1].update(open='96',high='100',low='93',close='94')
    store=EPaperStore(tmp_path/'e.sqlite')
    assert store.open_position(agent='E',direction='LONG',quantity=Decimal('.1'),price=Decimal('100'),stop=Decimal('90'),fee=Decimal('.01'),slippage=Decimal('0'),idempotency_key='open',expected_version=0)['accepted']
    now=datetime.fromisoformat(rows[-1]['close_at'])+timedelta(seconds=1)
    import sqlite3
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE fills SET occurred_at=? WHERE position_effect='OPEN'",((now-timedelta(minutes=120)).isoformat(),))
    result=process_structure_close(store=store,bars=rows,now=now)
    assert result['action']=='EXIT_PENDING' and store.account('E')['state']=='OPEN_LONG'
    assert process_structure_close(store=store,bars=rows,now=now)['action']=='DUPLICATE_BAR'
    quote={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':'93','ask':'93.1','event_id':'q1','event_at':now.isoformat()}
    old_quote={**quote,'event_at':(now-timedelta(minutes=1)).isoformat()}
    assert not protect_e_accounts({'formal':store.path},old_quote,CostModel())['formal']['accepted']
    assert store.account('E')['state']=='OPEN_LONG'
    assert protect_e_accounts({'formal':store.path},quote,CostModel())['formal']['accepted']
    assert EPaperStore(store.path).account('E')['state']=='FLAT'

def test_structure_bar_before_entry_must_not_manage_position(tmp_path):
    from backend.e_structure import process_structure_close
    from backend.e_paper_store import EPaperStore
    from test_e_visual import bars as snapshot
    from datetime import datetime,timedelta
    rows=snapshot()
    for b in rows:b.update(symbol='BTC/USD',interval='15Min',source='ALPACA_US_BTCUSD_SPOT',currency='USD_RESEARCH')
    store=EPaperStore(tmp_path/'e.sqlite')
    store.open_position(agent='E',direction='LONG',quantity=Decimal('.1'),price=Decimal('100'),stop=Decimal('90'),fee=Decimal('.01'),slippage=Decimal('0'),idempotency_key='open',expected_version=0)
    now=datetime.fromisoformat(rows[-1]['close_at'])+timedelta(seconds=1)
    assert process_structure_close(store=store,bars=rows,now=now)['reason']=='BAR_NOT_AFTER_ENTRY'

def test_newly_confirmed_higher_low_sets_persisted_tighter_stop(tmp_path):
    from backend.e_structure import process_structure_close
    from backend.e_paper_store import EPaperStore
    from test_e_visual import bars as snapshot
    from datetime import datetime,timedelta
    import sqlite3
    rows=snapshot()
    for b in rows:b.update(symbol='BTC/USD',interval='15Min',source='ALPACA_US_BTCUSD_SPOT',currency='USD_RESEARCH')
    for b,low in zip(rows[-13:],[100,99,98,92,98,99,100,99,98,94,98,99,100]):
        b.update(open='105',high='110',low=str(low),close='105')
    store=EPaperStore(tmp_path/'e.sqlite')
    store.open_position(agent='E',direction='LONG',quantity=Decimal('.1'),price=Decimal('100'),stop=Decimal('90'),fee=Decimal('.01'),slippage=Decimal('0'),idempotency_key='open',expected_version=0)
    now=datetime.fromisoformat(rows[-1]['close_at'])+timedelta(seconds=1)
    with sqlite3.connect(store.path) as db:db.execute("UPDATE fills SET occurred_at=? WHERE position_effect='OPEN'",((now-timedelta(minutes=15)).isoformat(),))
    result=process_structure_close(store=store,bars=rows,now=now)
    assert result['action']=='TIGHTEN' and result['protection']['accepted']
    with sqlite3.connect(store.path) as db:assert Decimal(db.execute('select stop from e_protective_stops').fetchone()[0])>Decimal('90')

def test_short_structure_exit_uses_confirmed_high_and_fresh_ask(tmp_path):
    from backend.e_structure import process_structure_close
    from backend.e_paper_store import EPaperStore
    from backend.e_protection import protect_e_accounts
    from backend.domain import CostModel
    from test_e_visual import bars as snapshot
    from datetime import datetime,timedelta
    import sqlite3
    rows=snapshot()
    for b in rows:b.update(symbol='BTC/USD',interval='15Min',source='ALPACA_US_BTCUSD_SPOT',currency='USD_RESEARCH')
    for b,high in zip(rows[-8:-1],[103,104,105,108,105,104,103]):b.update(open='100',high=str(high),low='90',close='101')
    rows[-1].update(open='104',high='110',low='100',close='109')
    store=EPaperStore(tmp_path/'e.sqlite')
    store.open_position(agent='E',direction='SHORT',quantity=Decimal('.1'),price=Decimal('100'),stop=Decimal('110'),fee=Decimal('.01'),slippage=Decimal('0'),idempotency_key='open',expected_version=0)
    now=datetime.fromisoformat(rows[-1]['close_at'])+timedelta(seconds=1)
    with sqlite3.connect(store.path) as db:db.execute("UPDATE fills SET occurred_at=? WHERE position_effect='OPEN'",((now-timedelta(minutes=120)).isoformat(),))
    assert process_structure_close(store=store,bars=rows,now=now)['action']=='EXIT_PENDING'
    quote={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':'108.9','ask':'109','event_at':now.isoformat(),'event_id':'q'}
    assert protect_e_accounts({'formal':store.path},quote,CostModel())['formal']['accepted']
    assert EPaperStore(store.path).account('E')['state']=='FLAT'
