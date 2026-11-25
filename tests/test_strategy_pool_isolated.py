import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from backend.strategy_pool import StrategyPool, StrategyValidationError, validate_strategy


ROOT = Path(__file__).resolve().parents[1]


def document(version="1.0.0", entry_threshold=105, exit_threshold=105):
    doc = json.loads((ROOT / "config/strategy_pool/example-rsi-mean-reversion.json").read_text(encoding="utf-8"))
    doc.update(strategy_id="isolated-test", version=version, name="isolated", description="test")
    doc["entry"] = {"op": "gt", "args": [{"op": "close", "args": [1]}, entry_threshold]}
    doc["exit"] = {"op": "lt", "args": [{"op": "close", "args": [1]}, exit_threshold]}
    return doc


def bars(close, low=None, *, start="2026-01-01T00:00:00+00:00"):
    low = close - 1 if low is None else low
    start_dt = datetime.fromisoformat(start)
    result = []
    for i in range(960):
        px = close if i == 959 else 100
        result.append({"timestamp": (start_dt + timedelta(minutes=15*i)).isoformat(),
                       "open": px, "high": px + 1, "low": low if i == 959 else 99,
                       "close": px, "volume": 1})
    return result


def fresh_quote(bid=109,ask=110):
    now=datetime.now(timezone.utc)
    return {"source":"BINANCE_USDM_PERP","symbol":"BTC/USD","currency":"USD_RESEARCH","timestamp_valid":True,
            "bid":str(bid),"ask":str(ask),"event_at":(now-timedelta(seconds=1)).isoformat(),"received_at":now.isoformat(),"age_ms":5}


def cycle_of(data):
    return datetime.fromisoformat(data[-1]['timestamp'])


def next_bars(previous,close,low=None):
    start=(datetime.fromisoformat(previous[0]['timestamp'])+timedelta(minutes=15)).isoformat()
    return bars(close,low,start=start)


def current_bars(close,low=None):
    now=datetime.now(timezone.utc)
    end=now.replace(minute=(now.minute//15)*15,second=0,microsecond=0)
    start=(end-timedelta(minutes=15*959)).isoformat()
    return bars(close,low,start=start)


def test_all_registry_connections_are_closed_and_database_is_deletable(tmp_path):
    db = tmp_path / "pool.sqlite3"
    pool = StrategyPool(db)
    pool.upload(document())
    pool.list()
    pool.versions("isolated-test")
    pool.enable("isolated-test", "1.0.0")
    initial=bars(110);pool.cycle(initial,cycle_of(initial),quote_age_ms=10,quote=fresh_quote())
    # On Windows a live SQLite handle prevents this operation (WinError 32).
    db.unlink()
    assert not db.exists()


def test_enabled_strategy_executes_once_and_restart_restores_position(tmp_path):
    db = tmp_path / "pool.sqlite3"
    pool = StrategyPool(db)
    pool.upload(document())
    pool.enable("isolated-test", "1.0.0")
    initial=bars(110);cycle=cycle_of(initial)
    first = pool.cycle(initial, cycle, quote_age_ms=20,quote=fresh_quote())
    assert first[0]["status"] == "OPEN_LONG"
    assert pool.cycle(initial, cycle, quote_age_ms=20,quote=fresh_quote())[0]["status"] == "DUPLICATE_OR_STALE"
    restored = StrategyPool(db)
    assert json.loads(restored.list()[0]["position"])["version"] == "1.0.0"


def test_old_version_position_is_managed_while_paused_and_exit_is_recorded(tmp_path):
    pool = StrategyPool(tmp_path / "pool.sqlite3")
    pool.upload(document(exit_threshold=105))
    pool.enable("isolated-test", "1.0.0")
    initial=bars(110);pool.cycle(initial,cycle_of(initial),quote_age_ms=20,quote=fresh_quote())
    pool.enable("isolated-test", "1.0.0", enabled=False)
    following=next_bars(initial,100,99);result=pool.cycle(following,cycle_of(following),quote_age_ms=20)
    assert result[0]["status"] in ("CLOSE_STOP", "CLOSE_RULE")
    assert pool.list()[0]["position"] is None
    with closing(sqlite3.connect(pool.path)) as conn, conn:
        assert conn.execute("select count(*) from fills where side='SELL' and version='1.0.0'").fetchone()[0] == 1


def test_archive_requires_flat_paused_strategy_and_preserves_history(tmp_path):
    pool = StrategyPool(tmp_path / "pool.sqlite3")
    pool.upload(document())
    pool.enable("isolated-test", "1.0.0")
    initial = bars(110)
    pool.cycle(initial, cycle_of(initial), quote_age_ms=20, quote=fresh_quote())
    with pytest.raises(StrategyValidationError):
        pool.archive("isolated-test")

    pool.enable("isolated-test", "1.0.0", enabled=False)
    following = next_bars(initial, 100, 99)
    pool.cycle(following, cycle_of(following), quote_age_ms=20)
    archived = pool.archive("isolated-test")
    assert archived["status"] == "ARCHIVED"
    assert pool.list()[0]["status"] == "ARCHIVED"
    assert len(pool.versions("isolated-test")) == 1
    assert len(pool.fills("isolated-test")) == 2
    with pytest.raises(StrategyValidationError):
        pool.enable("isolated-test", "1.0.0")
    archived_cycle = pool.list()[0]["last_cycle"]

    later = next_bars(following, 110)
    result = pool.cycle(later, cycle_of(later), quote_age_ms=20, quote=fresh_quote())
    assert result == [{"id": "isolated-test", "status": "ARCHIVED"}]
    assert len(pool.fills("isolated-test")) == 2
    assert pool.list()[0]["last_cycle"] == archived_cycle


def test_restore_archived_strategy_revalidates_version_and_leaves_it_disabled(tmp_path):
    pool = StrategyPool(tmp_path / "pool.sqlite3")
    pool.upload(document())
    pool.archive("isolated-test")

    restored = pool.restore("isolated-test", "1.0.0")
    assert restored["status"] == "REGISTERED_PAUSED"
    item = pool.list()[0]
    assert item["enabled"] is False
    assert item["status"] == "PAUSED"
    assert item["active_version"] == "1.0.0"
    assert [event["action"] for event in pool.lifecycle_events("isolated-test")] == ["ARCHIVE", "RESTORE"]


def test_archive_schema_migrates_existing_pool_without_touching_account_state(tmp_path):
    db = tmp_path / "legacy-pool.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE strategies(id TEXT PRIMARY KEY,active_version TEXT,enabled INTEGER NOT NULL DEFAULT 0,paused_at TEXT);
            CREATE TABLE versions(strategy_id TEXT,version TEXT,sha256 TEXT,document TEXT,uploaded_at TEXT,enabled_at TEXT,PRIMARY KEY(strategy_id,version));
            CREATE TABLE accounts(strategy_id TEXT PRIMARY KEY,cash TEXT NOT NULL,position TEXT,realized TEXT NOT NULL DEFAULT '0',last_cycle TEXT,last_status TEXT,last_action TEXT);
            CREATE TABLE fills(id INTEGER PRIMARY KEY,strategy_id TEXT,version TEXT,cycle TEXT,side TEXT,qty TEXT,price TEXT,fee TEXT,slippage TEXT,reason TEXT,idempotency_key TEXT,created_at TEXT);
            CREATE TABLE backtests(id TEXT PRIMARY KEY,strategy_id TEXT,version TEXT,result TEXT,created_at TEXT);
            INSERT INTO strategies VALUES('legacy','1.0',0,'2026-01-01T00:00:00+00:00');
            INSERT INTO accounts VALUES('legacy','9000',NULL,'-100','2026-01-01T00:00:00+00:00','OK','WAIT');
        """)
    pool = StrategyPool(db)
    item = next(row for row in pool.list() if row["id"] == "legacy")
    assert item["cash"] == "9000"
    assert item["realized"] == "-100"
    assert item["last_cycle"] == "2026-01-01T00:00:00+00:00"
    assert item["status"] == "PAUSED"


def test_backtest_has_separate_store_and_does_not_touch_live_account_or_cycle_watermark(tmp_path):
    pool = StrategyPool(tmp_path / "live.sqlite3")
    pool.upload(document(exit_threshold=105))
    before = pool.list()[0].copy()
    tail_start=(datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(minutes=960*15)).isoformat()
    report = pool.backtest("isolated-test", "1.0.0", bars(110) + bars(100, low=99, start=tail_start))
    after = pool.list()[0]
    assert report["trades"]
    assert report["strategy_version"] == "1.0.0"
    assert Path(pool.backtest_path).exists()
    assert after["cash"] == before["cash"] and after["last_cycle"] == before["last_cycle"]


def test_upload_rejects_overwrite_and_version_switch_does_not_retag_open_position(tmp_path):
    pool = StrategyPool(tmp_path / "pool.sqlite3")
    pool.upload(document())
    with pytest.raises(StrategyValidationError, match="版本已存在"):
        pool.upload(document())
    pool.enable("isolated-test", "1.0.0")
    initial=bars(110);pool.cycle(initial,cycle_of(initial),quote_age_ms=20,quote=fresh_quote())
    pool.upload(document(version="2.0.0", entry_threshold=90))
    pool.enable("isolated-test", "2.0.0")
    assert json.loads(pool.list()[0]["position"])["version"] == "1.0.0"
    following=next_bars(initial,100,99);pool.cycle(following,cycle_of(following),quote_age_ms=20)
    assert pool.fills("isolated-test")[0]["version"]=="1.0.0"


def test_stale_quote_blocks_new_open_but_does_not_stop_existing_stop_management(tmp_path):
    pool = StrategyPool(tmp_path / "pool.sqlite3")
    pool.upload(document())
    pool.enable("isolated-test", "1.0.0")
    initial=bars(110);assert pool.cycle(initial,cycle_of(initial),quote_age_ms=999999)[0]["status"] == "BLOCKED_STALE_QUOTE"
    assert pool.list()[0]["position"] is None


def test_quote_stop_monitor_is_idempotent_and_uses_fresh_validated_shared_quote(tmp_path):
    pool=StrategyPool(tmp_path/"pool.sqlite3")
    pool.upload(document());pool.enable("isolated-test","1.0.0")
    initial=current_bars(110);pool.cycle(initial,cycle_of(initial),quote_age_ms=20,quote=fresh_quote())
    now=datetime.now(timezone.utc)
    quote={"source":"BINANCE_USDM_PERP","symbol":"BTC/USD","currency":"USD_RESEARCH","timestamp_valid":True,"bid":"108","ask":"109","event_at":(now-timedelta(seconds=1)).isoformat(),"received_at":now.isoformat(),"age_ms":5}
    assert pool.monitor_quote(quote)==[{"id":"isolated-test","status":"CLOSE_STOP"}]
    assert pool.monitor_quote(quote)==[]
    assert pool.list()[0]["position"] is None
    assert pool.fills("isolated-test")[0]["reason"]=="STOP_QUOTE"


def test_backtest_database_is_releasable_and_concurrent_cycle_has_one_entry(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    db=tmp_path/"pool.sqlite3";pool=StrategyPool(db);pool.upload(document());pool.enable("isolated-test","1.0.0")
    initial=bars(110);cycle=cycle_of(initial)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results=list(executor.map(lambda _:pool.cycle(initial,cycle,quote_age_ms=20,quote=fresh_quote()),range(4)))
    assert sum(result[0]["status"]=="OPEN_LONG" for result in results)==1
    assert sum(len(pool.fills("isolated-test")) for _ in range(1))==1
    pool.backtest("isolated-test","1.0.0",bars(110))
    Path(pool.backtest_path).unlink()
    db.unlink()


def test_invalid_quote_and_noncontinuous_bars_fail_closed_and_release_connection(tmp_path):
    db=tmp_path/"pool.sqlite3";pool=StrategyPool(db);pool.upload(document());pool.enable("isolated-test","1.0.0")
    now=datetime.now(timezone.utc)
    quote={"source":"OTHER","symbol":"BTC/USD","currency":"USD_RESEARCH","timestamp_valid":True,"bid":"100","ask":"101","event_at":now.isoformat(),"received_at":now.isoformat(),"age_ms":1}
    initial=bars(110);assert pool.cycle(initial,cycle_of(initial),quote_age_ms=1,quote=quote)[0]["status"]=="BLOCKED_INVALID_QUOTE"
    invalid=bars(110);invalid[-1]["timestamp"]=(datetime.fromisoformat(invalid[-2]["timestamp"])+timedelta(minutes=30)).isoformat()
    assert pool.cycle(invalid,cycle_of(invalid),quote_age_ms=1)[0]["status"]=="BLOCKED_INVALID_BARS:K线不连续或乱序"
    db.unlink()


def test_enabled_not_running_is_distinct_until_real_cycle_is_consumed(tmp_path):
    pool=StrategyPool(tmp_path/"pool.sqlite3");pool.upload(document())
    assert pool.list()[0]["status"]=="REGISTERED"
    pool.enable("isolated-test","1.0.0")
    assert pool.list()[0]["status"]=="ENABLED_NOT_RUNNING"
    initial=current_bars(100);now=cycle_of(initial)
    pool.cycle(initial,now,quote_age_ms=20)
    assert pool.list()[0]["status"]=="RUNNING"
    pool.enable("isolated-test","1.0.0",False)
    assert pool.list()[0]["status"]=="PAUSED"


def test_pool_position_respects_risk_leverage_and_market_cost_limits(tmp_path):
    pool=StrategyPool(tmp_path/"pool.sqlite3");pool.upload(document());pool.enable("isolated-test","1.0.0")
    initial=current_bars(110);pool.cycle(initial,cycle_of(initial),quote_age_ms=20,quote=fresh_quote())
    position=json.loads(pool.list()[0]['position'])
    qty=Decimal(position['qty']);entry=Decimal(position['entry']);stop=Decimal(position['stop'])
    fee=Decimal(position['fee_rate']);slip=Decimal(position['slippage_rate'])
    planned_loss=qty*((entry-stop)+entry*(fee+slip)+stop*(fee+slip))
    notional=qty*entry
    assert planned_loss<=Decimal('200')
    assert Decimal(position['margin'])==notional/Decimal('5')
    assert position['fee_rate']=='0.0005' and position['slippage_rate']=='0.0002'


def test_normal_exit_rule_closes_without_waiting_for_stop(tmp_path):
    pool=StrategyPool(tmp_path/"pool.sqlite3");pool.upload(document(exit_threshold=111));pool.enable("isolated-test","1.0.0")
    initial=bars(110);pool.cycle(initial,cycle_of(initial),quote_age_ms=20,quote=fresh_quote())
    following=next_bars(initial,110,109.5);result=pool.cycle(following,cycle_of(following),quote_age_ms=999999)
    assert result[0]['status']=='CLOSE_RULE'
    assert pool.fills('isolated-test')[0]['reason']=='RULE'


def test_schema_validation_rejects_code_unknown_fields_and_nonfinite_values():
    good=document()
    assert validate_strategy(good)[0]['format']=='btc-lab-declarative-strategy/v1'
    for mutate in (
        lambda value:value.update({"python":"print('unsafe')"}),
        lambda value:value.update({"unknown_field":True}),
        lambda value:value.update({"parameters":{"threshold":float('nan')}}),
        lambda value:value.update({"entry":{"op":"exec","args":[]}}),
    ):
        candidate=json.loads(json.dumps(good,allow_nan=True));mutate(candidate)
        with pytest.raises(StrategyValidationError):validate_strategy(candidate)


def test_corrupt_strategy_state_isolated_from_peer_cycle(tmp_path):
    from contextlib import closing
    pool=StrategyPool(tmp_path/"pool.sqlite3")
    pool.upload(document())
    other=document();other['strategy_id']='peer-strategy'
    pool.upload(other)
    pool.enable("isolated-test","1.0.0");pool.enable("peer-strategy","1.0.0")
    with closing(sqlite3.connect(pool.path)) as conn,conn:
        conn.execute("update accounts set position='not-json' where strategy_id='isolated-test'")
    assert next(row for row in pool.list() if row['id']=='isolated-test')['position_direction']=='UNKNOWN'
    initial=current_bars(110);cycle=cycle_of(initial)
    result=pool.cycle(initial,cycle,quote_age_ms=10,quote=fresh_quote())
    assert {x['id']:x['status'] for x in result}['isolated-test'].startswith('BLOCKED:')
    assert {x['id']:x['status'] for x in result}['peer-strategy']=='OPEN_LONG'
    assert pool.fills('peer-strategy')[0]['reason']=='ENTRY'


def test_orphan_idempotency_key_blocks_state_creation(tmp_path):
    pool=StrategyPool(tmp_path/"pool.sqlite3");pool.upload(document());pool.enable("isolated-test","1.0.0")
    initial=bars(110);cycle=cycle_of(initial)
    with closing(sqlite3.connect(pool.path)) as conn,conn:
        conn.execute("insert into fills(strategy_id,version,cycle,side,qty,price,fee,slippage,reason,idempotency_key,created_at) values(?,?,?,?,?,?,?,?,?,?,?)",
                     ("isolated-test","1.0.0",cycle.isoformat(),"BUY","1","100","0","0","ENTRY","isolated-test:open:"+cycle.isoformat(),cycle.isoformat()))
    result=pool.cycle(initial,cycle,quote_age_ms=10,quote=fresh_quote())
    assert result[0]['status']=='BLOCKED:开仓订单幂等冲突'
    assert pool.list()[0]['position'] is None
    assert len(pool.fills('isolated-test'))==1


def test_live_cycle_rejects_replayed_or_mismatched_bar_close(tmp_path):
    pool=StrategyPool(tmp_path/"pool.sqlite3");pool.upload(document());pool.enable("isolated-test","1.0.0")
    result=pool.cycle(bars(110),datetime.now(timezone.utc),quote_age_ms=1,quote=fresh_quote())
    assert result[0]['status']=='BLOCKED_CYCLE_MISMATCH'
    assert pool.list()[0]['position'] is None
    assert pool.list()[0]['last_cycle'] is None
    assert pool.list()[0]['status']=='FAULTED'


def test_bad_position_does_not_block_peer_quote_stop(tmp_path):
    pool=StrategyPool(tmp_path/"pool.sqlite3");pool.upload(document())
    other=document();other['strategy_id']='peer-strategy';pool.upload(other)
    position={"version":"1.0.0","entry":"100","stop":"105","qty":"1","entry_fee":"0.05","margin":"20","fee_rate":"0.0005","slippage_rate":"0.0002","opened_cycle":"old"}
    with closing(sqlite3.connect(pool.path)) as conn,conn:
        conn.execute("update accounts set position='not-json' where strategy_id='isolated-test'")
        conn.execute("update accounts set position=? where strategy_id='peer-strategy'",(json.dumps(position),))
    now=datetime.now(timezone.utc)
    quote={"source":"BINANCE_USDM_PERP","symbol":"BTC/USD","currency":"USD_RESEARCH","timestamp_valid":True,"bid":"104","ask":"105","event_at":(now-timedelta(seconds=1)).isoformat(),"received_at":now.isoformat(),"age_ms":5}
    result=pool.monitor_quote(quote)
    assert {row['id']:row['status'] for row in result}=={'isolated-test':'FAULTED','peer-strategy':'CLOSE_STOP'}
    assert next(row for row in pool.list() if row['id']=='peer-strategy')['position'] is None


def test_cross_operator_uses_previous_and_current_closed_bars(tmp_path):
    pool=StrategyPool(tmp_path/"pool.sqlite3");doc=document()
    doc['entry']={'op':'crosses_above','args':[{'op':'close','args':[1]},{'op':'param','args':['oversold']}]}
    doc['parameters']={'oversold':105,'exit_rsi':50}
    pool.upload(doc);pool.enable('isolated-test','1.0.0')
    initial=current_bars(110)
    result=pool.cycle(initial,cycle_of(initial),quote_age_ms=10,quote=fresh_quote())
    assert result[0]['status']=='OPEN_LONG'


def test_concurrent_enable_pause_operations_leave_a_valid_serialized_state(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    pool=StrategyPool(tmp_path/"pool.sqlite3");pool.upload(document())
    operations=[True,False]*10
    with ThreadPoolExecutor(max_workers=8) as executor:
        results=list(executor.map(lambda enabled:pool.enable('isolated-test','1.0.0',enabled),operations))
    assert len(results)==len(operations)
    row=pool.list()[0]
    assert row['status'] in ('PAUSED','ENABLED_NOT_RUNNING')
    assert row['active_version']=='1.0.0'

