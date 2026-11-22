# PA-09 区间边界 Pinbar 反转

活动市场 BTCUSDT（Binance Demo USDⓈ-M），计价 USDT。研究初始权益以 8011 实测字段为准（10000）。

## 1. 策略定位

区间边界 Pinbar 反转

## 2. 适用行情

横盘区间，价格触及支撑或阻力后收回区间；不适用于明显均线分离的趋势。

## 3. 指标与参数



{
  "warmup_closed_bars": 61,
  "range_bars": 40,
  "atr_period": 14,
  "width_fraction_inclusive": [
    0.015,
    0.06
  ],
  "boundary_atr_tolerance": 0.2,
  "min_touches_each": 2,
  "touch_min_bar_spacing": 3,
  "sma_periods": [
    20,
    60
  ],
  "sma_relative_difference_max": 0.015,
  "long_wick_min_body_multiple": 2,
  "opposite_wick_max_body_multiple": 1,
  "stop_atr_buffer": 0.1,
  "target_atr_inset": 0.15,
  "minimum_reward_risk": 2
}

信号前40根：S=min(Low)，R=max(High)，区间宽度 (R−S)/((R+S)/2) 为1.5%–6%（含边界）。支撑用 Low、阻力用 High，距离 ≤0.2×ATR；两侧各至少2次，独立触及间隔至少3根。信号前 SMA20、SMA60 的差除以 SMA60，绝对值 ≤1.5%。ATR14 为信号前14根真实波幅简单均值，必须大于0。

## 4. 做多规则

S≤Close≤R；Close>Open；Open−Low≥2×实体；High−Close≤实体；|Low−S|≤0.2×ATR。

## 5. 做空规则

S≤Close≤R；Close<Open；High−Open≥2×实体；Close−Low≤实体；|High−R|≤0.2×ATR。

## 6. 信号确认

使用连续、已收盘的 BTCUSDT 15 分钟 K 线。常规路径在最新收盘后的 5 秒窗口内检查 Binance Demo 有效盘口并发单，多头用 ask、空头用 bid，实际成交以交易所确认回报为准。 过期机会不回填。

## 7. 止损规则

多：min(信号 Low,S)−0.1ATR。空：max(信号 High,R)+0.1ATR。

## 8. 止盈规则

固定对侧价，多 R−0.15ATR，空 S+0.15ATR。信号收盘与预估入场均检查最低 2R，入场前检查成本。

## 9. 退出与超时

有效报价多头用 bid、空头用 ask。共享退出检查先判断止损，再判断止盈，最后检查超时，使用确认仓位及真实退出成交。旧版 OHLC 同根优先级原样保留在原文中。 最长 96 根。

## 10. 特殊逻辑

不满61根、区间/趋势/触及/形态校验失败、零实体、收盘出界、错误方向的保护价、低于2R、成本或仓位校验失败，不开仓。 Demo 按持久化的信号周期去重。

## 11. 资金与仓位

现网 V2 研究初始权益以 8011 返回为准。适用的原风险定仓、杠杆上限、交易所约束及保护保留；估算手续费与实际成交费用分开。 2%，策略杠杆上限 5×。

## 12. 原始来源与版本

归档号 PA-09，运行 ID PA-09。8011 版本 1.0.0，启用 True，当前入场许可 True。

策略库或内置版本证据


```json
{
  "kind": "built_in_extension",
  "implementation": "backend/pa_extension.py:PaperLedger._bind",
  "version": "1.0.0",
  "legacy_extension_ledger": "data-binance-demo/pa-v1-extensions/PA-09.sqlite3",
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
  "sha256": "c3cd72d4dcb8a8a9865a3a206feccc8ec5fd2d4b78e4d846596386e338457be6",
  "verified_for_current_export": false,
  "observed_current_http": null
}
```

开仓入口 DemoPAExtensionRuntime.on_event → pa09 → _entry_signal → execute_next_open。

退出入口 DemoPARuntime._protect → DemoHedgeCoordinator.exit_on_quote → finish_position。

Python 策略以代码快照复核。

原始路径和逐文件 SHA-256 见本目录 strategy.json。

8011 状态接口提供策略 ID、版本和启用状态，但未发布整个进程的部署源码摘要。共享执行入口来自工作区源码静态追踪。

现网活动规则 GET 返回 HTTP None。保留此前 HTTP 200 的 SHA-256 验证记录及当前规则源码，未经当前接口确认的 JSON 原文未取得。

旧规则查询接口返回 HTTP 200。BTC/USD 规则原始字节保存在 source_snapshots/legacy-active-api/PA-09.json，SHA-256 为 f270836e0e02723ff221b328558b95d2689ea423b4bbb282d707a9b26d31841f。当前仍无法读取 BTCUSDT Demo 的活动规则响应。

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

def pa09(bars, *, audit=None):
    def reject(reason):
        if audit is not None:
            audit(reason)
        return None

    validate(bars)
    if len(bars) < 61:
        return reject('warmup')
    window, b, a = bars[-41:-1], bars[-1], atr(bars[:-1])
    s,r = min(x['low'] for x in window), max(x['high'] for x in window)
    sma60 = mean(x['close'] for x in bars[-61:-1])
    if a <= 0 or not .015 <= (r-s)/((r+s)/2) <= .06:
        return reject('atr_or_width')
    if abs(mean(x['close'] for x in bars[-21:-1])-sma60)/sma60 > .015:
        return reject('sma')
    if min(touches(window,'low',s,.2*a),touches(window,'high',r,.2*a)) < 2:
        return reject('touches')
    if not s <= b['close'] <= r:
        return reject('close_outside')
    o,c,h,low = (b[k] for k in ('open','close','high','low'))
    body = abs(c-o)
    if c > o and o-low >= 2*body and h-c <= body and abs(low-s) <= .2*a:
        result = dict(side='LONG', stop=min(low,s)-.1*a, target=r-.15*a)
    elif c < o and h-o >= 2*body and c-low <= body and abs(h-r) <= .2*a:
        result = dict(side='SHORT', stop=max(h,r)+.1*a, target=s+.15*a)
    else:
        return reject('boundary_or_pinbar')
    result.update(signal=time(b).isoformat(), max_hold=96, min_r=2)
    return result if entry(result, time(b)+STEP, c) else reject('planned_rr')

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
- 来源完整性：`PARTIAL`；规则源 SHA-256：`c3cd72d4dcb8a8a9865a3a206feccc8ec5fd2d4b78e4d846596386e338457be6`；源哈希核验：`UNVERIFIED`。
- 当前运行状态：`UNVERIFIED`。本次检查未发现 8011 监听，无法读取进程内实际活动版本。
- 规则入口（只用于审计）：`backend/pa_active_rules.py`, `backend/pa_extension.py`, `scripts/pa09_pa10_candidates.py`。
- 冲突/限制：
  - 策略扩展源码与历史规则导出存在，但当前活动 GET 未成功核验；PA09/10 生产切换记录仍为待批准。Demo enabled 列表属于独立 BINANCE_USDM_DEMO 环境。
- 未核实字段：当前进程内版本、当前启停与入场许可、最近持久证据后的周期处理。
- V3 处理：本轮只归档，不注册、不启用、不执行该策略。
