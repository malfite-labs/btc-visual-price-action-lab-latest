# D 15分钟急跌后底部反转

活动市场 BTCUSDT（Binance Demo USDⓈ-M），计价 USDT。研究初始权益以 8011 实测字段为准（10000）。

## 1. 策略定位

急跌后底部反转，只做多。

## 2. 适用行情

15 分钟形态与 1 小时空头和波动过滤。

## 3. 指标与参数

15m EMA9/20，1h EMA20/50，Wilder ATR14/RSI14，RVOL20 中位数，收盘位置 CLV。至少 960 根有效 15m 和 240 根完整 1h。

## 4. 做多规则

候选及 1–6 根内确认条件见下方完整求值源码和原始规格。WATCH 冻结 ATR、低点、颈线与止损；跌破低点−0.1ATR 失效。

## 5. 做空规则

无做空路径。

## 6. 信号确认

只在已收盘 K 线确认，下一窗口用合规 ask；检查价差、跳空、结构、数量及独立风控。

## 7. 止损规则

初始止损为候选低点−0.25×冻结 ATR。TP1 确认后，下一合格周期按实现提高至至少入场价。

## 8. 止盈规则

按确认均价计算 R。TP1 1R 约平一半并按数量步进取整，TP2 2R 平余量。

## 9. 退出与超时

报价先检查止损，然后检查未达 TP1 的 8 根和总 16 根超时，再检查目标。风险断路器或清算预警有优先平仓路径。

## 10. 特殊逻辑

UTC 日初采用 00:00 对账后可信空仓权益；无基准或数据失效堵新仓。日亏达 1% 锁存并要求清仓，连续三笔已完结亏损暂停 24 小时。现网 D 启用标志关闭，关闭不取消保护。候选取消冷却 4 根，Demo 运行实现从 LONG 恢复空仓时也设 4 根，研究规格另写退出后 8 根。

## 11. 资金与仓位

现网 V2 研究初始权益以 8011 返回为准。适用的原风险定仓、杠杆上限、交易所约束及保护保留；估算手续费与实际成交费用分开。 研究规格的 2×逐仓是独立原文，Demo 路径看 DemoDPolicy 和 d_risk。

## 12. 原始来源与版本

归档号 D，运行 ID D。8011 版本 D-REBOUND-EMA-RSI-V1，启用 False，当前入场许可 False。

策略库或内置版本证据


```json
{
  "kind": "standalone_research_specification",
  "version": "schema 1.0.0",
  "file": "config/strategy_d/D_strategy_15m.json",
  "runtime_diff": "研究规格还列 ETHUSDT、2×逐仓、退出冷却8根。当前 Demo 为 BTCUSDT，原执行接线的预检用5×模型，退出冷却见 DemoDPolicy.close。"
}
```

规则校验


```json
{
  "method": "sha256_of_workspace_sources",
  "verified_local_copy": true
}
```

开仓入口 DemoDRuntime.on_event → DemoDPolicy.close/quote → d_signal.evaluate / d_risk.preflight_entry。

退出入口 DemoDPolicy.protect → d_position_policy.plan_position_event。

Python 策略以代码快照复核。

原始路径和逐文件 SHA-256 见本目录 strategy.json。

8011 状态接口提供策略 ID、版本和启用状态，但未发布整个进程的部署源码摘要。共享执行入口来自工作区源码静态追踪。

## 原始 Python 规则摘录

以下函数来自快照，保留原有计算与控制流。完整依赖见策略 JSON 中的源文件清单。

### backend/d_signal.py


```python
def _ema(values, period):
    output = [None] * len(values)
    if len(values) < period:
        return output
    value = sum(values[:period]) / period
    output[period - 1] = value
    alpha = 2 / (period + 1)
    for i in range(period, len(values)):
        value = alpha * values[i] + (1 - alpha) * value
        output[i] = value
    return output

def _wilder(values, period=14):
    output = [None] * len(values)
    if len(values) <= period:
        return output
    value = sum(values[1:period + 1]) / period
    output[period] = value
    for i in range(period + 1, len(values)):
        value = (value * (period - 1) + values[i]) / period
        output[i] = value
    return output

def _indicators(bars):
    closes = [b['close'] for b in bars]
    ranges = [0.0] + [max(bars[i]['high'] - bars[i]['low'], abs(bars[i]['high'] - closes[i - 1]), abs(bars[i]['low'] - closes[i - 1])) for i in range(1, len(bars))]
    gain = [0.0] + [max(closes[i] - closes[i - 1], 0) for i in range(1, len(bars))]
    loss = [0.0] + [max(closes[i - 1] - closes[i], 0) for i in range(1, len(bars))]
    ag, al = _wilder(gain), _wilder(loss)
    rsi = [None if g is None else (50.0 if g == 0 and l == 0 else 100.0 if l == 0 else 100 - 100 / (1 + g / l)) for g, l in zip(ag, al)]
    return {'ema9': _ema(closes, 9), 'ema20': _ema(closes, 20), 'ema50': _ema(closes, 50), 'atr': _wilder(ranges), 'rsi': rsi}

def _hours(bars):
    hours = []
    for index in range(len(bars) - 3):
        chunk = bars[index:index + 4]
        if chunk[0]['open_at'].minute != 0:
            continue
        hours.append({'open': chunk[0]['open'], 'high': max(x['high'] for x in chunk), 'low': min(x['low'] for x in chunk), 'close': chunk[-1]['close'], 'volume': sum(x['volume'] for x in chunk), 'close_at': chunk[-1]['close_at']})
    if len(hours) < 240:
        raise DataNotReady('INSUFFICIENT_1H_HISTORY')
    return hours

def evaluate(bars, state=None, *, market_source="ALPACA_US_BTCUSD_SPOT"):
    """Process latest bar using caller-persisted state. Invalid data never signals."""
    state = dict(state or {'phase': 'IDLE'})
    try:
        b = _validate(bars, market_source=market_source)
        hours = _hours(b)
    except (DataNotReady, TypeError, ValueError) as exc:
        return _output('DATA_NOT_READY', str(exc), state)
    key = b[-1]['close_at'].isoformat()
    if state.get('last_close') == key:
        return _output('DUPLICATE_BAR', 'DUPLICATE_BAR', state)
    if state.get('last_close'):
        try:
            if _time(state['last_close']) + timedelta(minutes=15) != b[-1]['close_at']:
                return _output('DATA_NOT_READY', 'STATE_CYCLE_GAP', state)
        except (ValueError, DataNotReady):
            return _output('DATA_NOT_READY', 'INVALID_STATE_TIME', state)
    state['last_close'] = key
    phase = state.get('phase', 'IDLE')
    if phase == 'PENDING':
        return _output('PENDING', 'AWAIT_EXECUTION', state)
    if phase == 'LONG':
        return _output('LONG', 'POSITION_MANAGED_EXTERNALLY', state)
    if phase == 'COOLDOWN':
        remaining = int(state.get('remaining', 0)) - 1
        state['remaining'] = max(remaining, 0)
        if remaining <= 0:
            state['phase'] = 'IDLE'
        return _output('COOLDOWN', 'COOLDOWN', state)
    if phase not in ('IDLE', 'WATCH'):
        return _output('DATA_NOT_READY', 'INVALID_STATE', state)
    i = len(b) - 1
    ind, hour_ind = _indicators(b), _indicators(hours)
    bar = b[i]
    spread = bar['high'] - bar['low']
    clv = (bar['close'] - bar['low']) / spread if spread > 0 else None
    if phase == 'IDLE':
        if spread <= 0:
            return _output('IDLE', 'ZERO_RANGE', state)
        if any(x['volume'] <= 0 for x in b[i-20:i+1]):
            return _output('DATA_NOT_READY', 'SIGNAL_VOLUME_NOT_POSITIVE', state)
        rvol = bar['volume'] / median(x['volume'] for x in b[i-20:i])
        a = ind['atr'][i-1]
        if a <= 0:
            return _output('IDLE', 'INVALID_ATR', state)
        wick = (min(bar['open'], bar['close']) - bar['low']) / spread
        found = (b[i-1]['close'] < ind['ema20'][i-1] < ind['ema20'][i-5]
                 and (max(x['high'] for x in b[i-12:i]) - bar['low']) / a >= 2.5
                 and bar['low'] < min(x['low'] for x in b[i-8:i])
                 and (bar['close'] > bar['open'] or wick >= .35)
                 and clv >= .55 and rvol >= 1.3)
        if found:
            state.update(phase='WATCH', candidate_close=key, bars_waited=0, atr=a,
                         low=bar['low'], neckline=max(x['high'] for x in b[i-3:i]),
                         stop=bar['low'] - .25 * a)
            return _output('WATCH', 'CANDIDATE_DETECTED', state)
        return _output('IDLE', 'NO_SETUP', state)
    try:
        a, low, neck, stop = (float(state[k]) for k in ('atr', 'low', 'neckline', 'stop'))
        waited = int(state['bars_waited']) + 1
    except (KeyError, TypeError, ValueError):
        return _output('DATA_NOT_READY', 'INVALID_WATCH_STATE', state)
    state['bars_waited'] = waited
    if bar['low'] < low - .1 * a:
        state.update(phase='COOLDOWN', remaining=4)
        return _output('COOLDOWN', 'CANDIDATE_INVALIDATED', state)
    if spread <= 0:
        if waited >= 6:
            state.update(phase='COOLDOWN', remaining=4)
            return _output('COOLDOWN', 'CANDIDATE_EXPIRED_ZERO_RANGE', state)
        return _output('WATCH', 'ZERO_RANGE', state)
    if any(x['volume'] <= 0 for x in b[i-20:i+1]):
        if waited >= 6:
            state.update(phase='COOLDOWN', remaining=4)
            return _output('COOLDOWN', 'CANDIDATE_EXPIRED_VOLUME_UNAVAILABLE', state)
        return _output('WATCH', 'SIGNAL_VOLUME_NOT_POSITIVE', state)
    rvol = bar['volume'] / median(x['volume'] for x in b[i-20:i])
    h = len(hours) - 1
    hourly_bear = hours[h]['close'] < hour_ind['ema50'][h] and hour_ind['ema20'][h] < hour_ind['ema20'][h-3]
    confirmed = (1 <= waited <= 6 and bar['close'] > neck + .05*a and bar['close'] > ind['ema9'][i]
                 and bar['close'] > bar['open'] and clv >= .65 and rvol >= 1.2
                 and ind['rsi'][i] > ind['rsi'][i-1] and ind['rsi'][i] >= 35
                 and 0 < bar['close'] - low <= 3.5*a and not hourly_bear
                 and .6*a <= bar['close'] - stop <= 3*a
                 and hour_ind['atr'][h] / hours[h]['close'] <= .04)
    if confirmed:
        state['phase'] = 'PENDING'
        signal = {'direction': 'LONG', 'setup': 'D_CONFIRM', 'signal_close': key,
                  'close': bar['close'], 'atr': a, 'low': low, 'neckline': neck, 'stop': stop}
        state['signal'] = signal
        return _output('PENDING', 'D_CONFIRM', state, signal)
    if waited >= 6:
        state.update(phase='COOLDOWN', remaining=4)
        return _output('COOLDOWN', 'CANDIDATE_EXPIRED', state)
    return _output('WATCH', 'AWAIT_CONFIRMATION', state)
```

### backend/d_risk.py


```python
def preflight_entry(signal, quote, equity, available_cash, prior_bar_volume, now, *, cost=None,
                    market_source="ALPACA_US_BTCUSD_SPOT", capital_epoch=False):
    """Return proposed entry/stop/targets; reject invalid data and candidates.

    The first valid post-close quote within the following bar is the only
    candidate entry opportunity. Callers must persist that attempt before use.
    """
    cost = cost or CostModel()
    tick = cost.simulated_tick if market_source == "BINANCE_FUTURES_DEMO" else TICK
    step = cost.quantity_step if market_source == "BINANCE_FUTURES_DEMO" else STEP
    try:
        if not tick.is_finite() or not step.is_finite() or min(tick, step) <= 0:
            return {"accepted": False, "reason": "INVALID_MARKET_FILTER"}
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError('INVALID_NOW')
        now = now.astimezone(timezone.utc)
        if signal.get('direction') != 'LONG' or signal.get('setup') != 'D_CONFIRM':
            return {'accepted': False, 'reason': 'INVALID_SIGNAL'}
        close_at = _time(signal['signal_close'])
        event_at = _time(quote['event_at'])
        if not close_at <= event_at < close_at + timedelta(minutes=15):
            return {'accepted': False, 'reason': 'CANDIDATE_EXPIRED'}
        if not close_at <= now < close_at + timedelta(minutes=15):
            return {'accepted': False, 'reason': 'CANDIDATE_EXPIRED'}
        if not d_market_matches(quote, market_source):
            return {'accepted': False, 'reason': 'SOURCE_MISMATCH'}
        if quote.get('timestamp_valid') is not True:
            return {'accepted': False, 'reason': 'QUOTE_STALE'}
        age_ms = (now - event_at).total_seconds() * 1000
        if not 0 <= age_ms <= cost.max_quote_age_ms:
            return {'accepted': False, 'reason': 'QUOTE_STALE'}
        bid, ask = _decimal(quote['bid']), _decimal(quote['ask'])
        equity, cash, volume = map(_decimal, (equity, available_cash, prior_bar_volume))
        atr, low, neckline, stop, close = map(_decimal, (signal['atr'], signal['low'], signal['neckline'], signal['stop'], signal['close']))
        if min(bid, ask, equity, cash, volume, atr, low, neckline, stop, close) <= 0 or ask < bid:
            return {'accepted': False, 'reason': 'INVALID_INPUT'}
        mid = (bid + ask) / 2
        if (ask - bid) / mid > MAX_SPREAD:
            return {'accepted': False, 'reason': 'SPREAD_TOO_WIDE'}
        stop = _floor(stop, tick)
        slip = cost.adverse_slippage_bps / D('10000')
        fee_rate = cost.fee_rate
        entry = _ceil(ask * (1 + slip), tick)
        risk = entry - stop
        if abs(ask - close) > D('.30') * atr or ask <= neckline or ask <= stop:
            return {'accepted': False, 'reason': 'ENTRY_GAP_OR_LEVEL'}
        if not (0 < entry - low <= D('3.5') * atr):
            return {'accepted': False, 'reason': 'ENTRY_TOO_FAR_FROM_LOW'}
        if not (D('.6') * atr <= risk <= D('3') * atr) or risk / entry > D('.02'):
            return {'accepted': False, 'reason': 'STOP_DISTANCE'}
        stop_fill = stop * (1 - slip)
        unit_loss = entry - stop_fill + fee_rate * (entry + stop_fill)
        if unit_loss <= 0:
            return {'accepted': False, 'reason': 'INVALID_RISK'}
        # Simulated 5x margin using spot quotes; no exchange funding model.
        if capital_epoch:
            from backend.binance_demo_capital import full_margin_quantity
            quantity = full_margin_quantity(min(equity, cash), entry, step, cost.minimum_quantity,
                                            cost.minimum_notional, fee_rate)
            if quantity > volume * VOLUME_FRACTION:
                return {"accepted": False, "reason": "D_VOLUME_CAPACITY"}
        else:
            quantity = _floor(min(equity * RISK_FRACTION / unit_loss,
                              equity * NOTIONAL_FRACTION / entry,
                              volume * VOLUME_FRACTION,
                              cash / (entry / LEVERAGE + entry * fee_rate)), step)
        # Fees round up to cents, so the exact pre-rounding risk budget may
        # need a slightly smaller quantity. Find the largest valid lot count.
        low_steps, high_steps = (int(quantity / step) if capital_epoch else 0), int(quantity / step)
        while low_steps < high_steps:
            steps = (low_steps + high_steps + 1) // 2
            candidate = step * steps
            entry_fee = (candidate * entry * fee_rate).quantize(D('.01'), rounding=ROUND_UP)
            exit_fee = (candidate * stop_fill * fee_rate).quantize(D('.01'), rounding=ROUND_UP)
            loss = candidate * (entry - stop_fill) + entry_fee + exit_fee
            if loss <= equity * RISK_FRACTION and candidate * entry / LEVERAGE + entry_fee <= cash:
                low_steps = steps
            else:
                high_steps = steps - 1
        quantity = step * low_steps
        first = _floor(quantity / 2, step)
        second = quantity - first
        tp1 = _ceil(entry + risk, tick)
        tp2 = _ceil(entry + 2 * risk, tick)
        if min(first, second) < cost.minimum_quantity or min(first * tp1, second * tp2) < cost.minimum_notional:
            return {'accepted': False, 'reason': 'INVALID_SPLIT_QUANTITY'}
        fee = (quantity * entry * fee_rate).quantize(D('.01'), rounding=ROUND_UP)
        estimated_loss = quantity * (entry - stop_fill) + fee + (quantity * stop_fill * fee_rate).quantize(D('.01'), rounding=ROUND_UP)
        if (not capital_epoch and estimated_loss > equity * RISK_FRACTION) or quantity * entry / LEVERAGE + fee > cash:
            return {'accepted': False, 'reason': 'ROUNDED_RISK_OR_CASH_LIMIT'}
        return {'accepted': True, 'quantity': str(quantity), 'first_quantity': str(first),
                'second_quantity': str(second), 'price': str(entry), 'stop': str(stop),
                'tp1': str(tp1), 'tp2': str(tp2), 'estimated_loss': str(estimated_loss),
                'leverage': str(LEVERAGE), 'fee': str(fee), 'signal_close': close_at.isoformat()}
    except (KeyError, TypeError, ValueError, ArithmeticError, InvalidOperation):
        return {'accepted': False, 'reason': 'INVALID_INPUT'}
```

### backend/d_circuit.py


```python
    def record_closed_trade(self, *, trade_id, net_pnl, closed_at):
        """Record one final result per trade, never a partial-close result."""
        if not isinstance(trade_id, str) or not trade_id.strip():
            raise ValueError('INVALID_TRADE_ID')
        pnl = _decimal(net_pnl)
        timestamp = _timestamp(closed_at).isoformat()
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT net_pnl,closed_at FROM d_closed_trades WHERE trade_id=?', (trade_id,)).fetchone()
            if prior:
                if Decimal(prior['net_pnl']) != pnl or prior['closed_at'] != timestamp:
                    db.rollback()
                    return {'accepted': False, 'reason': 'TRADE_CONFLICT'}
                db.rollback()
                return {'accepted': True, 'duplicate': True}
            state = db.execute('SELECT * FROM d_circuit_state WHERE id=1').fetchone()
            if state['last_closed'] and timestamp <= state['last_closed']:
                db.rollback()
                return {'accepted': False, 'reason': 'OUT_OF_ORDER_TRADE'}
            streak = state['streak'] + 1 if pnl < 0 else 0
            until = _timestamp(closed_at) + PAUSE if streak >= LOSS_LIMIT else None
            old_until = datetime.fromisoformat(state['paused_until']) if state['paused_until'] else None
            if old_until and (until is None or old_until > until):
                until = old_until
            db.execute('INSERT INTO d_closed_trades VALUES(?,?,?)', (trade_id, str(pnl), timestamp))
            db.execute('UPDATE d_circuit_state SET streak=?,paused_until=?,last_closed=? WHERE id=1',
                       (streak, until.isoformat() if until else None, timestamp))
            db.commit()
            return {'accepted': True, 'duplicate': False, 'loss_streak': streak,
                    'paused_until': until.isoformat() if until else None}

    def assess(self, *, now, equity):
        """Assess current verified equity; returns gates and never prevents exits."""
        stamp = _timestamp(now)
        day = stamp.date().isoformat()
        try:
            amount = _decimal(equity)
        except ValueError:
            with closing(self._connect()) as db:
                row = db.execute('SELECT latched FROM d_days WHERE day=?', (day,)).fetchone()
            return {'block_entries': True, 'flatten_required': bool(row and row['latched']),
                    'allow_exits': True, 'reason': 'EQUITY_UNAVAILABLE'}
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM d_days WHERE day=?', (day,)).fetchone()
            state = db.execute('SELECT * FROM d_circuit_state WHERE id=1').fetchone()
            paused = bool(state['paused_until'] and stamp < datetime.fromisoformat(state['paused_until']))
            latest = db.execute('SELECT MAX(day) FROM d_days WHERE last_observed IS NOT NULL').fetchone()[0]
            if latest and day < latest:
                db.rollback()
                return {'block_entries': True, 'flatten_required': bool(row and row['latched']),
                        'allow_exits': True, 'reason': 'OUT_OF_ORDER_EQUITY'}
            if row is None:
                db.rollback()
                return {'block_entries': True, 'flatten_required': False, 'allow_exits': True,
                        'reason': 'DAY_START_EQUITY_UNAVAILABLE', 'paused_until': state['paused_until']}
            if row['last_observed'] and stamp.isoformat() < row['last_observed']:
                db.rollback()
                return {'block_entries': True, 'flatten_required': bool(row['latched']),
                        'allow_exits': True, 'reason': 'OUT_OF_ORDER_EQUITY'}
            latched = bool(row['latched']) or amount <= Decimal(row['opening_equity']) * (Decimal(1) - DAILY_LOSS)
            db.execute('UPDATE d_days SET latched=?,last_observed=? WHERE day=?', (int(latched), stamp.isoformat(), day))
            db.commit()
            return {'block_entries': latched or paused, 'flatten_required': latched,
                    'allow_exits': True, 'reason': 'DAILY_LOSS_LIMIT' if latched else 'LOSS_STREAK_PAUSE' if paused else 'READY',
                    'paused_until': state['paused_until'], 'loss_streak': state['streak']}
```

### backend/d_position_policy.py


```python
def plan_position_event(position, quote, now, *, cost=None, market_source="ALPACA_US_BTCUSD_SPOT"):
    """Plan the next single state transition for a fresh bid quote.

    The calling dispatcher must loop after each committed action. In particular,
    one quote above both targets requires TP1 then TP2, each idempotent.
    """
    cost = cost or CostModel()
    tick = cost.simulated_tick if market_source == "BINANCE_FUTURES_DEMO" else TICK
    step = cost.quantity_step if market_source == "BINANCE_FUTURES_DEMO" else STEP
    try:
        if not tick.is_finite() or not step.is_finite() or min(tick, step) <= 0:
            return {"action": "BLOCKED", "reason": "INVALID_MARKET_FILTER"}
        if now.tzinfo is None:
            raise ValueError('INVALID_NOW')
        now = now.astimezone(timezone.utc)
        if not d_market_matches(quote, market_source):
            return {'action': 'BLOCKED', 'reason': 'SOURCE_MISMATCH'}
        if quote.get('timestamp_valid') is not True:
            return {'action': 'BLOCKED', 'reason': 'QUOTE_STALE'}
        event_at = _dt(quote['event_at'])
        if not 0 <= (now - event_at).total_seconds() * 1000 <= cost.max_quote_age_ms:
            return {'action': 'BLOCKED', 'reason': 'QUOTE_STALE'}
        bid, ask = _dec(quote['bid']), _dec(quote['ask'])
        if bid > ask:
            return {'action': 'BLOCKED', 'reason': 'INVALID_QUOTE'}
        entry = _dec(position['entry_price'])
        stop = _dec(position['stop_price'])
        tp1, tp2 = _dec(position['tp1']), _dec(position['tp2'])
        initial, remaining = _dec(position['initial_quantity']), _dec(position['quantity'])
        if remaining > initial or position.get('direction') != 'LONG':
            return {'action': 'BLOCKED', 'reason': 'INVALID_POSITION'}
        entry_bar_close = _dt(position['entry_bar_close'])
        if event_at < entry_bar_close - timedelta(minutes=15):
            return {'action': 'BLOCKED', 'reason': 'QUOTE_BEFORE_ENTRY'}
        bar_open = event_at.replace(minute=event_at.minute // 15 * 15, second=0, microsecond=0)
        pending_stop = position.get('pending_stop')
        if pending_stop is not None:
            effective_after = _dt(position['pending_stop_after'])
            if bar_open >= effective_after:
                return {'action': 'UPDATE_STOP', 'stop': str(max(stop, _dec(pending_stop))),
                        'current_bar_open': bar_open.isoformat()}
        slip = cost.adverse_slippage_bps / D('10000')
        sell_price = ((bid * (1 - slip)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        if bid <= stop:
            return {'action': 'STOP', 'quantity': str(remaining), 'price': str(sell_price)}
        if not position.get('tp1_filled') and bar_open >= entry_bar_close + timedelta(minutes=15 * 7):
            return {'action': 'TIME_EXIT', 'reason': 'NO_TP1_8_BARS', 'quantity': str(remaining), 'price': str(sell_price)}
        if bar_open >= entry_bar_close + timedelta(minutes=15 * 15):
            return {'action': 'TIME_EXIT', 'reason': 'MAX_HOLD_16_BARS', 'quantity': str(remaining), 'price': str(sell_price)}
        if not position.get('tp1_filled') and bid >= tp1:
            quantity = (initial / 2 / step).to_integral_value(rounding=ROUND_DOWN) * step
            if quantity <= 0 or quantity >= remaining:
                return {'action': 'BLOCKED', 'reason': 'INVALID_TP1_QUANTITY'}
            return {'action': 'TP1', 'quantity': str(quantity), 'price': str(sell_price),
                    'bar_close': (bar_open + timedelta(minutes=15)).isoformat()}
        if position.get('tp1_filled') and bid >= tp2:
            return {'action': 'TP2', 'quantity': str(remaining), 'price': str(sell_price)}
        return {'action': 'HOLD'}
    except (KeyError, TypeError, ValueError, ArithmeticError, InvalidOperation):
        return {'action': 'BLOCKED', 'reason': 'INVALID_INPUT'}
```


## 2026-11-20 首轮只读状态核验

- 归档状态：`ACTIVE`（指持久规则/配置状态；不代表当前正在运行）。
- 来源完整性：`PARTIAL`；规则源 SHA-256：`UNVERIFIED`；源哈希核验：`UNVERIFIED`。
- 当前运行状态：`UNVERIFIED`。本次检查未发现 8011 监听，无法读取进程内实际活动版本。
- 规则入口（只用于审计）：`config/strategy_d/D_strategy_15m.json`, `backend/d_signal.py`, `backend/d_paper.py`, `backend/d_runtime.py`。
- 冲突/限制：
  - D_strategy_15m.json 标注 research_specification_unbacktested 且 live_enabled=false；auto-paper.json 为 enabled=true，但 D 持久状态为 UNCERTAIN / D_DISPATCH_OperationalError，执行资格冲突。
- 未核实字段：当前进程内版本、当前启停与入场许可、最近持久证据后的周期处理。
- V3 处理：本轮只归档，不注册、不启用、不执行该策略。
