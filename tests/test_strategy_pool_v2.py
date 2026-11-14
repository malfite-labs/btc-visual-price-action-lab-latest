import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import backend.strategy_pool as pool_module
from backend.strategy_pool import StrategyPool, StrategyValidationError, validate_strategy

ROOT = Path(__file__).resolve().parents[1]


def v2_document():
    return {
        "format": "btc-lab-declarative-strategy/v2",
        "strategy_id": "f-v2-test",
        "version": "1.0.0",
        "name": "F v2 test",
        "market": "BTC/USD",
        "timeframe": "15m",
        "description": "test fixture",
        "parameters": {"fast": 20, "slow": 60},
        "risk": {"initial_equity": "10000", "currency": "USD_RESEARCH", "max_leverage": 5, "risk_fraction": "0.02"},
        "entry": {
            "long": {"op": "gt", "args": [{"op": "close", "args": [0]}, {"op": "sma", "args": [20, 0]}]},
            "short": {"op": "lt", "args": [{"op": "close", "args": [0]}, {"op": "sma", "args": [20, 0]}]},
        },
        "position": {
            "direction": "both",
            "entry_timing": "next_open",
            "stop": {"kind": "swing_atr", "lookback": 3, "atr_period": 14, "buffer_atr": 0.1, "min_distance_atr": 0.6, "max_distance_atr": 2.5},
            "target_r": 2,
            "max_hold_bars": 192,
            "same_bar_priority": "stop",
        },
    }


def rising_bars(count=80):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = []
    for i in range(count):
        close = Decimal(100 + i)
        bars.append({"timestamp": (start + timedelta(minutes=15*i)).isoformat(), "open": str(close-1), "high": str(close+2), "low": str(close-2), "close": str(close), "volume": "1"})
    return bars


def falling_bars(count=80):
    result = rising_bars(count)
    for i, bar in enumerate(result):
        close = Decimal(200 - i)
        bar.update(open=str(close+1), high=str(close+2), low=str(close-2), close=str(close))
    return result


def test_v2_schema_and_causal_expression_support_are_available():
    doc = v2_document()
    assert validate_strategy(doc)[0]["format"] == "btc-lab-declarative-strategy/v2"
    bars = rising_bars()
    bars[-1].update(open="181", high="184", low="180", close="183")
    expr = {"op": "and", "args": [
        {"op": "gt", "args": [{"op": "close", "args": [0]}, {"op": "highest_high", "args": [5, 1]}]},
        {"op": "gte", "args": [{"op": "body_fraction", "args": [0]}, 0.2]},
        {"op": "gt", "args": [{"op": "atr", "args": [14, 0]}, 0]},
        {"op": "gt", "args": [{"op": "sma", "args": [20, 0]}, {"op": "sma", "args": [20, 5]}]},
    ]}
    assert pool_module.evaluate_v2(expr, bars, doc["parameters"]) is True


def test_v2_direction_evaluation_returns_one_side_or_wait():
    doc = v2_document()
    bars = rising_bars()
    assert pool_module.evaluate_entry_v2(doc, bars) == "LONG"
    doc["entry"]["long"] = {"op": "gt", "args": [1, 2]}
    doc["entry"]["short"] = {"op": "gt", "args": [1, 2]}
    assert pool_module.evaluate_entry_v2(doc, bars) is None


def test_v2_stop_uses_signal_and_prior_two_extremes_with_atr_gate():
    doc = v2_document()
    bars = rising_bars()
    short_bars=falling_bars()
    long_stop, reason = pool_module.build_v2_stop(doc, "LONG", bars)
    short_stop, short_reason = pool_module.build_v2_stop(doc, "SHORT", short_bars)
    assert reason is None and short_reason is None
    assert long_stop < Decimal(bars[-1]["close"])
    assert short_stop > Decimal(short_bars[-1]["close"])


def test_v2_validation_bounds_offsets_and_rejects_unknown_position_fields():
    doc = v2_document()
    doc["entry"]["long"]["args"][0]["args"] = [201]
    with pytest.raises(StrategyValidationError):
        validate_strategy(doc)
    doc = v2_document()
    doc["position"]["unbounded"] = True
    with pytest.raises(StrategyValidationError):
        validate_strategy(doc)


def test_v2_warmup_includes_indicator_periods_and_bar_offsets():
    doc=v2_document();doc["entry"]["long"]={"op":"gt","args":[{"op":"highest_high","args":[40,1]},{"op":"close","args":[3]}]}
    assert StrategyPool._required_bars(doc)==41


def test_v1_schema_and_indicator_semantics_remain_unchanged():
    raw = json.loads((ROOT / "config/strategy_pool/example-rsi-mean-reversion.json").read_text(encoding="utf-8"))
    assert validate_strategy(raw)[0]["format"] == "btc-lab-declarative-strategy/v1"
    bars = rising_bars()
    assert pool_module.evaluate({"op": "gt", "args": [{"op": "close", "args": [1]}, 100]}, bars, {}) is True


def test_v2_position_sizing_accepts_both_sides_with_signal_stop(tmp_path):
    doc=v2_document();bars=rising_bars();bars[-1].update(open="181",high="184",low="180",close="183")
    long_stop,_=pool_module.build_v2_stop(doc,"LONG",bars)
    short_bars=falling_bars();short_stop,_=pool_module.build_v2_stop(doc,"SHORT",short_bars)
    long,_,_=StrategyPool._new_position("f-v2-test",doc,bars,"cycle",Decimal("0.0005"),Decimal("0.0002"),Decimal("0.00000001"),Decimal("0.00000001"),Decimal("1"),Decimal("10000"),entry_reference="184",direction="LONG",stop_price=long_stop)
    short,_,_=StrategyPool._new_position("f-v2-test",doc,short_bars,"cycle",Decimal("0.0005"),Decimal("0.0002"),Decimal("0.00000001"),Decimal("0.00000001"),Decimal("1"),Decimal("10000"),entry_reference="120",direction="SHORT",stop_price=short_stop)
    assert long["direction"]=="LONG" and Decimal(long["stop"])<Decimal(long["entry"])<Decimal(long["target"])
    assert short["direction"]=="SHORT" and Decimal(short["target"])<Decimal(short["entry"])<Decimal(short["stop"])


def test_v2_rejects_target_that_cannot_cover_round_trip_costs():
    doc=v2_document();doc["position"]["target_r"]=0.0001;bars=rising_bars();stop=Decimal("90")
    with pytest.raises(StrategyValidationError,match="TARGET_CANNOT_COVER_COSTS"):
        StrategyPool._new_position("f-v2-test",doc,bars,"cycle",Decimal("0.0005"),Decimal("0.0002"),Decimal("0.00000001"),Decimal("0.00000001"),Decimal("1"),Decimal("10000"),entry_reference="180",direction="LONG",stop_price=stop)


def test_v2_stop_wins_same_bar_target_and_time_exit_is_bar_counted():
    doc=v2_document()
    pos={"version":"1.0.0","direction":"LONG","entry":"100","stop":"95","target":"110","qty":"1","entry_fee":"0.05","fee_rate":"0.0005","slippage_rate":"0.0002","bars_held":5}
    collision=[{"timestamp":"2026-01-01T00:00:00+00:00","open":"100","high":"112","low":"94","close":"101","volume":"1"}]
    assert StrategyPool._exit_reason_v2(pos,doc,collision,6)[0]=="STOP"
    clear=[{"timestamp":"2026-01-01T00:00:00+00:00","open":"100","high":"104","low":"96","close":"102","volume":"1"}]
    assert StrategyPool._exit_reason_v2(pos,doc,clear,192)[0]=="TIME"


def test_v2_backtest_fills_next_bar_open_and_censors_across_gaps(tmp_path):
    doc=v2_document();doc["entry"]["long"]={"op":"gt","args":[{"op":"close","args":[0]},138]};doc["entry"]["short"]={"op":"gt","args":[1,2]}
    pool=StrategyPool(tmp_path/"pool.sqlite3");pool.upload(doc)
    bars=rising_bars(40)
    bars[-1].update(open="138",high="140",low="137",close="139")
    bars.append({"timestamp":(datetime.fromisoformat(bars[-1]["timestamp"])+timedelta(minutes=15)).isoformat(),"open":"139","high":"140","low":"138","close":"139","volume":"1"})
    # A later gap starts a new segment and must not preserve an open position.
    bars.append({"timestamp":(datetime.fromisoformat(bars[-1]["timestamp"])+timedelta(minutes=30)).isoformat(),"open":"139","high":"140","low":"138","close":"139","volume":"1"})
    report=pool.backtest("f-v2-test","1.0.0",bars)
    entries=[fill for fill in report["trades"] if fill["reason"]=="ENTRY_NEXT_OPEN"]
    assert entries and entries[0]["cycle"]==bars[40]["timestamp"]
    assert Decimal(entries[0]["price"])==Decimal(bars[40]["open"])*(1+Decimal("0.0002"))
    assert report["segment_count"]==2
    assert report["censored_positions_at_gaps_or_end"]>=1


def test_v2_cycle_registers_signal_then_opens_on_next_cycle(tmp_path):
    doc=v2_document();doc["entry"]["long"]={"op":"gt","args":[{"op":"close","args":[0]},100]};doc["entry"]["short"]={"op":"gt","args":[1,2]}
    pool=StrategyPool(tmp_path/"pool.sqlite3");pool.upload(doc);pool.enable("f-v2-test","1.0.0")
    bars=rising_bars(40);bars[-1].update(open="138",high="140",low="137",close="139")
    quote={"source":"ALPACA_US_BTCUSD_SPOT","symbol":"BTC/USD","currency":"USD_RESEARCH","timestamp_valid":True,"bid":"138.9","ask":"139.1","event_at":datetime.now(timezone.utc).isoformat(),"received_at":datetime.now(timezone.utc).isoformat(),"age_ms":5}
    assert pool.cycle(bars,datetime.fromisoformat(bars[-1]["timestamp"]),quote_age_ms=10,quote=quote)[0]["status"]=="SIGNAL_LONG"
    following=dict(bars[-1]);following.update(timestamp=(datetime.fromisoformat(bars[-1]["timestamp"])+timedelta(minutes=15)).isoformat(),open="140",high="141",low="139",close="140")
    quote.update(bid="139.9",ask="140.1",event_at=(datetime.fromisoformat(bars[-1]["timestamp"])+timedelta(minutes=15,seconds=1)).isoformat(),received_at=datetime.now(timezone.utc).isoformat())
    assert pool.monitor_quote(quote)==[{"id":"f-v2-test","status":"OPEN_LONG"}]
    assert pool.list()[0]["position_direction"]=="LONG"
    bars.append(following)
    assert pool.cycle(bars,datetime.fromisoformat(following["timestamp"]),quote_age_ms=10,quote=quote)[0]["status"]=="HOLD"
    assert pool.fills("f-v2-test")[0]["reason"]=="ENTRY_NEXT_OPEN"


def test_v2_cycle_blocks_entry_on_stale_or_invalid_quote(tmp_path):
    doc = v2_document()
    doc["entry"]["long"] = {"op": "gt", "args": [{"op": "close", "args": [0]}, {"op": "sma", "args": [20, 0]}]}
    doc["entry"]["short"] = {"op": "gt", "args": [1, 2]}
    pool = StrategyPool(tmp_path / "pool.sqlite3")
    pool.upload(doc)
    pool.enable("f-v2-test", "1.0.0")
    bars = rising_bars(40)
    close = datetime.fromisoformat(bars[-1]["timestamp"])
    quote = {
        "source": "ALPACA_US_BTCUSD_SPOT", "symbol": "BTC/USD", "currency": "USD_RESEARCH",
        "timestamp_valid": True, "bid": "138.9", "ask": "139.1", "age_ms": 1,
        "event_at": datetime.now(timezone.utc).isoformat(), "received_at": datetime.now(timezone.utc).isoformat(),
    }
    result = pool.cycle(bars, close, quote=quote, quote_age_ms=60_001)
    assert result[0]["status"] == "BLOCKED:QUOTE_UNAVAILABLE_OR_STALE"
    account = pool.list()[0]
    assert account["last_cycle"] == close.isoformat()
    assert account["last_status"] == account["last_action"] == "BLOCKED:QUOTE_UNAVAILABLE_OR_STALE"
    assert account["position_direction"] == "FLAT" and pool.fills("f-v2-test") == []

    invalid_pool = StrategyPool(tmp_path / "invalid-quote.sqlite3")
    invalid_pool.upload(doc)
    invalid_pool.enable("f-v2-test", "1.0.0")
    quote["timestamp_valid"] = False
    blocked = invalid_pool.cycle(bars, close, quote=quote, quote_age_ms=1)
    assert blocked[0]["status"] == "BLOCKED:QUOTE_UNAVAILABLE_OR_STALE"
    assert invalid_pool.list()[0]["position_direction"] == "FLAT"
    assert invalid_pool.fills("f-v2-test") == []


def test_v2_cycle_records_insufficient_warmup_without_entry(tmp_path):
    doc = v2_document()
    pool = StrategyPool(tmp_path / "pool.sqlite3")
    pool.upload(doc)
    pool.enable("f-v2-test", "1.0.0")
    bars = rising_bars(19)
    close = datetime.fromisoformat(bars[-1]["timestamp"])
    quote = {
        "source": "ALPACA_US_BTCUSD_SPOT", "symbol": "BTC/USD", "currency": "USD_RESEARCH",
        "timestamp_valid": True, "bid": "118.9", "ask": "119.1", "age_ms": 1,
        "event_at": datetime.now(timezone.utc).isoformat(), "received_at": datetime.now(timezone.utc).isoformat(),
    }
    result = pool.cycle(bars, close, quote=quote, quote_age_ms=1)
    assert result[0]["status"].startswith("BLOCKED:")
    assert pool.list()[0]["last_cycle"] == close.isoformat()
    assert pool.list()[0]["position_direction"] == "FLAT" and pool.fills("f-v2-test") == []


def test_v2_cycle_records_signal_stop_rejection_as_wait(tmp_path):
    doc = v2_document()
    doc["position"]["stop"]["min_distance_atr"] = 10
    doc["position"]["stop"]["max_distance_atr"] = 11
    pool = StrategyPool(tmp_path / "pool.sqlite3")
    pool.upload(doc)
    pool.enable("f-v2-test", "1.0.0")
    bars = falling_bars(80)
    close = datetime.fromisoformat(bars[-1]["timestamp"])
    now = datetime.now(timezone.utc)
    quote = {
        "source": "ALPACA_US_BTCUSD_SPOT", "symbol": "BTC/USD", "currency": "USD_RESEARCH",
        "timestamp_valid": True, "bid": "120", "ask": "120.1", "age_ms": 1,
        "event_at": now.isoformat(), "received_at": now.isoformat(),
    }
    result = pool.cycle(bars, close, quote=quote, quote_age_ms=1)
    assert result[0]["status"] == "WAIT_STOP_DISTANCE_OUT_OF_RANGE"
    account = pool.list()[0]
    assert account["last_cycle"] == close.isoformat()
    assert account["last_status"] == "OK"
    assert account["last_action"] == "WAIT_STOP_DISTANCE_OUT_OF_RANGE"
    assert account["position_direction"] == "FLAT" and pool.fills("f-v2-test") == []


def test_v2_short_quote_target_uses_ask_and_records_buy_to_close(tmp_path):
    pool=StrategyPool(tmp_path/"pool.sqlite3");pool.upload(v2_document())
    pos={"version":"1.0.0","direction":"SHORT","entry":"100","stop":"110","target":"90","qty":"1","entry_fee":"0.05","fee_rate":"0.0005","slippage_rate":"0.0002"}
    with pool_module._database(pool.path) as conn:conn.execute("update accounts set position=? where strategy_id=?",(json.dumps(pos),"f-v2-test"))
    now=datetime.now(timezone.utc);quote={"source":"ALPACA_US_BTCUSD_SPOT","symbol":"BTC/USD","currency":"USD_RESEARCH","timestamp_valid":True,"bid":"89.9","ask":"90","event_at":now.isoformat(),"received_at":now.isoformat(),"age_ms":1}
    assert pool.monitor_quote(quote)==[{"id":"f-v2-test","status":"CLOSE_TARGET"}]
    assert pool.fills("f-v2-test")[0]["side"]=="BUY"


@pytest.mark.parametrize("filename,expected_body",[("f-trend-breakout-v1.json",0.4),("f-trend-pullback-v1.json",0.6)])
def test_f_candidate_documents_validate_and_preserve_frozen_parameters(filename,expected_body):
    doc=json.loads((ROOT/"config/strategy_pool"/filename).read_text(encoding="utf-8"))
    assert validate_strategy(doc)[0]["format"]=="btc-lab-declarative-strategy/v2"
    assert doc["position"]["max_hold_bars"]==192 and doc["position"]["target_r"]==2
    assert doc["parameters"]["minimum_body_fraction"]==expected_body
    assert doc["risk"]["risk_fraction"]=="0.02" and doc["risk"]["max_leverage"]==5


def _gap_bar(close_at, *, high="67200", low="67000"):
    opened = close_at - timedelta(minutes=15)
    return {
        "symbol": "BTC/USD", "interval": "15Min", "source": "ALPACA_US_BTCUSD_SPOT",
        "currency": "USD_RESEARCH", "is_closed": True,
        "open_at": opened.isoformat(), "close_at": close_at.isoformat(),
        "open": "67100", "high": high, "low": low, "close": "67150", "volume": "0.001",
    }


def _pool_with_gap_position(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    doc = v2_document()
    doc["strategy_id"] = "gap-audit-test"
    doc["position"]["max_hold_bars"] = 128
    pool = StrategyPool(tmp_path / "gap.sqlite3")
    pool.upload(doc)
    pool.enable("gap-audit-test", "1.0.0")
    last = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)
    through = last + timedelta(minutes=18 * 15)
    position = {
        "version": "1.0.0", "entry": "66726.85196", "stop": "67727.68854",
        "target": "64224.76050", "qty": "0.18229189", "entry_fee": "6.08",
        "fee_rate": "0.0005", "slippage_rate": "0.0002", "direction": "SHORT",
        "opened_cycle": "2026-11-14T03:45:20.738807+00:00", "mark": "67127.67",
        "bars_held": 17, "max_hold_bars": 128,
    }
    with pool_module._database(pool.path) as conn:
        conn.execute("update accounts set position=?,last_cycle=?,last_status=?,last_action=? where strategy_id=?",
                     (json.dumps(position), last.isoformat(), "BLOCKED_GAP_RECONCILIATION_REQUIRED", "BLOCKED_GAP_RECONCILIATION_REQUIRED", "gap-audit-test"))
        conn.execute("insert into fills(strategy_id,version,cycle,side,qty,price,fee,slippage,reason,idempotency_key,created_at) values(?,?,?,?,?,?,?,?,?,?,?)",
                     ("gap-audit-test", "1.0.0", (last - timedelta(days=1)).isoformat(), "SELL", "0.2", "70000", "7", "2", "LEGACY_CENSORED_ENTRY", "legacy-unmatched-entry", (last - timedelta(days=1)).isoformat()))
        conn.execute("insert into fills(strategy_id,version,cycle,side,qty,price,fee,slippage,reason,idempotency_key,created_at) values(?,?,?,?,?,?,?,?,?,?,?)",
                     ("gap-audit-test", "1.0.0", position["opened_cycle"], "SELL", position["qty"], position["entry"], "6.08", "2.43", "ENTRY_NEXT_OPEN", "entry-1", position["opened_cycle"]))
    gap = [_gap_bar(last + timedelta(minutes=15*i)) for i in range(1, 19)]
    return pool, position, last, through, gap


def test_candle_gap_reconciliation_preserves_trade_and_waits_for_next_real_cycle(tmp_path):
    pool, position, last, through, gap = _pool_with_gap_position(tmp_path)
    before_fills = pool.fills("gap-audit-test")
    audit = pool.reconcile_candle_gap(
        "gap-audit-test", expected_last_cycle=last, through_cycle=through, bars=gap,
        expected_position=position, accepted_standard="CANDLE_OHLC_PROXY_V1",
    )
    account = pool.list()[0]
    assert audit["status"] == "AWAITING_NEXT_NATURAL_CYCLE"
    assert account["last_cycle"] == last.isoformat()
    assert json.loads(account["position"])["bars_held"] == 35
    assert json.loads(account["position"])["entry"] == position["entry"]
    assert json.loads(account["position"])["stop"] == position["stop"]
    assert pool.fills("gap-audit-test") == before_fills

    history = []
    first_close = last - timedelta(minutes=15*79)
    for index in range(80):
        history.append(_gap_bar(first_close + timedelta(minutes=15*index)))
    next_bar = _gap_bar(through + timedelta(minutes=15))
    outcomes = pool.cycle(history + gap + [next_bar], through + timedelta(minutes=15))
    assert outcomes == [{"id": "gap-audit-test", "status": "HOLD"}]
    account = pool.list()[0]
    assert account["last_cycle"] == (through + timedelta(minutes=15)).isoformat()
    assert json.loads(account["position"])["bars_held"] == 36
    assert pool.gap_reconciliations("gap-audit-test")[-1]["state"] == "COMPLETED"
    assert pool.fills("gap-audit-test") == before_fills


def test_candle_gap_reconciliation_rejects_triggered_exit_and_time_limit(tmp_path):
    pool, position, last, through, gap = _pool_with_gap_position(tmp_path / "stop")
    gap[4] = _gap_bar(last + timedelta(minutes=15*5), high="67728")
    with pytest.raises(StrategyValidationError):
        pool.reconcile_candle_gap(
            "gap-audit-test", expected_last_cycle=last, through_cycle=through, bars=gap,
            expected_position=position, accepted_standard="CANDLE_OHLC_PROXY_V1",
        )
    account = pool.list()[0]
    assert account["last_cycle"] == last.isoformat()
    assert json.loads(account["position"])["qty"] == position["qty"]
    assert pool.gap_reconciliations("gap-audit-test") == []

    pool, position, last, through, gap = _pool_with_gap_position(tmp_path / "time")
    position["bars_held"] = 120
    with pool_module._database(pool.path) as conn:
        conn.execute("update accounts set position=? where strategy_id=?", (json.dumps(position), "gap-audit-test"))
    with pytest.raises(StrategyValidationError):
        pool.reconcile_candle_gap(
            "gap-audit-test", expected_last_cycle=last, through_cycle=through, bars=gap,
            expected_position=position, accepted_standard="CANDLE_OHLC_PROXY_V1",
        )
    assert pool.gap_reconciliations("gap-audit-test") == []
