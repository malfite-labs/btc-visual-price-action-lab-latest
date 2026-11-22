# PA-10 完整压力支撑互换

活动市场 BTCUSDT（Binance Demo USDⓈ-M），计价 USDT。研究初始权益以 8011 实测字段为准（10000）。

## 1. 策略定位

完整压力支撑互换

## 2. 适用行情

突破阻力或跌破支撑，经过延伸、回踩和确认后顺向入场。

## 3. 指标与参数



{
  "warmup_closed_bars": 41,
  "level_history_bars": 40,
  "atr_period": 14,
  "level_atr_tolerance": 0.2,
  "min_touches": 2,
  "touch_min_bar_spacing": 3,
  "breakout_atr": 0.2,
  "breakout_body_fraction": 0.5,
  "extension_atr": 1,
  "retest_atr": 0.2,
  "max_close_through_atr": 0.1,
  "stop_atr_buffer": 0.1,
  "window_bars_after_breakout": 16,
  "target_r": 2.5
}

## 4. 做多规则

冻结前40根阻力，阳线有效突破，向上延伸后回踩，再阳线收破回踩确认高点。多用信号前40根最高 High，空用最低 Low。ATR14 取信号前真实波幅简单均值；ATR>0。至少2次独立触及（间隔≥3根、距离≤0.2×ATR）。方向 d：多=+1、空=−1。d×(Close−关键位)≥0.2×ATR，d×(Close−Open)>0，实体≥全幅50%；进入 BREAKOUT_CANDIDATE，冻结关键位、方向与 ATR。下一根起，d×(顺向极值−关键位)≥1×冻结ATR，进入 EXTENSION_CONFIRMED。其后用多头 Low / 空头 High 检查 |反向极值−关键位|≤0.2×冻结ATR；进入 RETEST_OBSERVED，保存该根顺向极值为确认线。

## 5. 做空规则

冻结前40根支撑，阴线有效跌破，向下延伸后反抽，再阴线收破回踩确认低点。多头收盘突破回踩根 High、且为阳线；空头收盘跌破回踩根 Low、且为阴线。阶段逐根推进，不在同一根跨出多阶段。等待确认时持续更新回踩以来的最低 Low（多）或最高 High（空）。

## 6. 信号确认

使用连续、已收盘的 BTCUSDT 15 分钟 K 线。常规路径在最新收盘后的 5 秒窗口内检查 Binance Demo 有效盘口并发单，多头用 ask、空头用 bid，实际成交以交易所确认回报为准。 过期机会不回填。

## 7. 止损规则

多用回踩以来最低减 0.1×冻结ATR，空用回踩以来最高加 0.1×冻结ATR。

## 8. 止盈规则

确认成交均价 ±2.5R。

## 9. 退出与超时

有效报价多头用 bid、空头用 ask。共享退出检查先判断止损，再判断止盈，最后检查超时，使用确认仓位及真实退出成交。旧版 OHLC 同根优先级原样保留在原文中。 最长 192 根。

## 10. 特殊逻辑

突破后第1至16根（含第16根）内完成确认。d×(Close−关键位)<−0.1×冻结ATR 即失效；断档或市场恢复会清除未完成形态。阶段、水平位、冻结ATR、突破时间、确认线和回踩极值保存在对应账户快照中；已处理周期不重放。 Demo 的阶段、周期及信号见 demo_extension_signal。

## 11. 资金与仓位

现网 V2 研究初始权益以 8011 返回为准。适用的原风险定仓、杠杆上限、交易所约束及保护保留；估算手续费与实际成交费用分开。 2%，策略杠杆上限 5×。

## 12. 原始来源与版本

归档号 PA-10，运行 ID PA-10。8011 版本 1.0.0，启用 True，当前入场许可 True。

策略库或内置版本证据


```json
{
  "kind": "built_in_extension",
  "implementation": "backend/pa_extension.py:PaperLedger._bind",
  "version": "1.0.0",
  "legacy_extension_ledger": "data-binance-demo/pa-v1-extensions/PA-10.sqlite3",
  "observed_table_names": [
    "fills",
    "snapshot"
  ]
}
```

规则校验


```json
{
  "method": "historical_api_rule_hash",
  "sha256": "fadcfbfe05a80d8ef644d77fe526abb5a33a87d47b08ec2816d22379ded2f728",
  "verified_for_current_export": false,
  "observed_current_http": null
}
```

开仓入口 DemoPAExtensionRuntime.on_event → pa10 → _entry_signal → execute_next_open。

退出入口 DemoPARuntime._protect → DemoHedgeCoordinator.exit_on_quote → finish_position。

Python 策略以代码快照复核。

原始路径和逐文件 SHA-256 见本目录 strategy.json。

8011 状态接口提供策略 ID、版本和启用状态，但未发布整个进程的部署源码摘要。共享执行入口来自工作区源码静态追踪。

现网活动规则 GET 返回 HTTP None。保留此前 HTTP 200 的 SHA-256 验证记录及当前规则源码，未经当前接口确认的 JSON 原文未取得。

旧规则查询接口返回 HTTP 200。BTC/USD 规则原始字节保存在 source_snapshots/legacy-active-api/PA-10.json，SHA-256 为 ff66e1887c484e7a56c6741f1f4dda205d6b03492c754557294b1ba299c1f59d。当前仍无法读取 BTCUSDT Demo 的活动规则响应。

## 原始 Python 规则摘录

以下函数来自快照，保留原有计算与控制流。完整依赖见策略 JSON 中的源文件清单。

### scripts/pa09_pa10_candidates.py


```python
def atr(bars):
    return mean(max(b['high']-b['low'], abs(b['high']-a['close']), abs(b['low']-a['close']))
                for a,b in zip(bars[-15:-1], bars[-14:]))

def touches(window, field, level, tolerance):
    last, count = -3, 0
    for i, bar in enumerate(window):
        if i-last >= 3 and abs(bar[field]-level) <= tolerance:
            last, count = i, count+1
    return count

def pa10(bars, state=None):
    """Return JSON-serializable state and optional signal. Process one new close."""
    validate(bars)
    if len(bars) < 41:
        return state, None
    b, window = bars[-1], bars[-41:-1]
    t = time(b)
    a = atr(window)
    if state:
        last = datetime.fromisoformat(state['last'])
        if t <= last:
            return state, None
        if t-last != STEP:
            return None, None
        state = {**state, 'last':t.isoformat()}
        if state['phase'] == 'CONFIRMED':
            return None, None
        if t-datetime.fromisoformat(state['breakout']) > 16*STEP:
            return None, None
        p, u, d = state['level'], state['atr'], state['direction']
        if d*(b['close']-p) < -.1*u:
            return None, None
        extreme = b['high'] if d==1 else b['low']
        test = b['low'] if d==1 else b['high']
        if state['phase']=='BREAKOUT_CANDIDATE' and d*(extreme-p)>=u:
            state['phase']='EXTENSION_CONFIRMED'
        elif state['phase']=='EXTENSION_CONFIRMED' and abs(test-p)<=.2*u:
            state.update(phase='RETEST_OBSERVED', test=test, confirm=extreme)
        elif state['phase']=='RETEST_OBSERVED':
            state['test'] = min(state['test'],test) if d==1 else max(state['test'],test)
            if d*(b['close']-state['confirm'])>0 and d*(b['close']-b['open'])>0:
                state['phase']='CONFIRMED'
                return state, dict(side='LONG' if d==1 else 'SHORT', stop=state['test']-d*.1*u,
                                   target_r=2.5, max_hold=192, signal=t.isoformat())
        return state, None
    if a <= 0:
        return None,None
    for d, field, extremum in ((1,'high',max),(-1,'low',min)):
        p=extremum(x[field] for x in window)
        if (touches(window,field,p,.2*a)>=2 and d*(b['close']-p)>=.2*a
                and d*(b['close']-b['open'])>0 and abs(b['close']-b['open'])>=.5*(b['high']-b['low'])):
            return dict(phase='BREAKOUT_CANDIDATE', level=p, atr=a, direction=d,
                        breakout=t.isoformat(), last=t.isoformat()),None
    return None,None

def entry(signal, at, fill, *, validating=False):
    """Check one candidate against a supplied fill; no order or accounting side effects."""
    if validating or at != datetime.fromisoformat(signal['signal'])+STEP or not isfinite(fill) or fill<=0:
        return None
    d=1 if signal['side']=='LONG' else -1
    risk=d*(fill-signal['stop'])
    if risk<=0 or signal['stop']<=0:
        return None
    target=signal['target'] if 'target' in signal else fill+d*risk*signal['target_r']
    return target if target>0 and d*(target-fill)>=signal.get('min_r',0)*risk else None
```


## 2026-11-20 首轮只读状态核验

- 归档状态：`INACTIVE`（指持久规则/配置状态；不代表当前正在运行）。
- 来源完整性：`PARTIAL`；规则源 SHA-256：`fadcfbfe05a80d8ef644d77fe526abb5a33a87d47b08ec2816d22379ded2f728`；源哈希核验：`UNVERIFIED`。
- 当前运行状态：`UNVERIFIED`。本次检查未发现 8011 监听，无法读取进程内实际活动版本。
- 规则入口（只用于审计）：`backend/pa_active_rules.py`, `backend/pa_extension.py`, `scripts/pa09_pa10_candidates.py`。
- 冲突/限制：
  - 策略扩展源码与历史规则导出存在，但当前活动 GET 未成功核验；PA09/10 生产切换记录仍为待批准。Demo enabled 列表属于独立 BINANCE_USDM_DEMO 环境。
- 未核实字段：当前进程内版本、当前启停与入场许可、最近持久证据后的周期处理。
- V3 处理：本轮只归档，不注册、不启用、不执行该策略。
