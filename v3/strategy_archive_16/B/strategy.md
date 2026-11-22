# B 反转（趋势过滤）

活动市场 BTCUSDT（Binance Demo USDⓈ-M），计价 USDT。研究初始权益以 8011 实测字段为准（10000）。

## 1. 策略定位

边缘反转并由均线趋势确认。

## 2. 适用行情

有方向趋势中的局部反转。

## 3. 指标与参数

前 10 根低点/高点、边缘容差 0.1%，影线至少 2×max(实体,0.01)。当前 SMA12 与 SMA48 及结束于前第 4 根的 SMA12 比较。

## 4. 做多规则

检测器依次检查跌破前低后收回、近前低的阳线长下影、近前低的阳吞阴。先选首个候选，再要求 SMA12 高于 SMA48 且高于前移 SMA12。

## 5. 做空规则

镜像边缘假突破收回、阴线长上影、阴吞阳，再要求 SMA12 低于 SMA48 及前移 SMA12。条件顺序以源码摘录为准。

## 6. 信号确认

使用连续、已收盘的 BTCUSDT 15 分钟 K 线。常规路径在最新收盘后的 5 秒窗口内检查 Binance Demo 有效盘口并发单，多头用 ask、空头用 bid，实际成交以交易所确认回报为准。 A/B/C 使用 288 根。

## 7. 止损规则

含信号根最近 4 根的低点减 0.01 或高点加 0.01。

## 8. 止盈规则

确认入场后的 1.5R，预估须覆盖双边手续费。

## 9. 退出与超时

有效报价多头用 bid、空头用 ask。共享退出检查先判断止损，再判断止盈，最后检查超时，使用确认仓位及真实退出成交。旧版 OHLC 同根优先级原样保留在原文中。 最长 12 根。

## 10. 特殊逻辑

先按候选顺序选择，再进行趋势过滤。

## 11. 资金与仓位

现网 V2 研究初始权益以 8011 返回为准。适用的原风险定仓、杠杆上限、交易所约束及保护保留；估算手续费与实际成交费用分开。 2%，策略杠杆上限 1×。

## 12. 原始来源与版本

归档号 B，运行 ID B。8011 版本 V3.1，启用 True，当前入场许可 True。

策略库或内置版本证据


```json
{
  "kind": "frozen_active_profile_and_separate_visual_library",
  "active_profile_version": "V3.1",
  "visual_library_metadata": {
    "agent": "B",
    "version": "v1.0.0",
    "parent": null,
    "status": "SEED",
    "created_at_utc": "2026-10-27",
    "source": "V1.0 frozen principles",
    "evolvable_sections": [
      "Examples",
      "Evolvable sections"
    ],
    "immutable": true
  },
  "visual_library_metadata_path": "strategies/B/v1.0.0.metadata.json"
}
```

规则校验


```json
{
  "method": "frozen_profile_matches_file",
  "sha256": "b8506be4e67ba40ec9234966371d42cd85aa750f4bdf00ca48ae3864388bc7a6",
  "verified": true
}
```

开仓入口 binance_demo_pa.execute_next_open → rule_cycle.evaluate_rule_signal → b-v31-signals.signal_b_filtered。

退出入口 DemoPARuntime._protect → DemoHedgeCoordinator.exit_on_quote → finish_position。

Python 策略以代码快照复核。

原始路径和逐文件 SHA-256 见本目录 strategy.json。

8011 状态接口提供策略 ID、版本和启用状态，但未发布整个进程的部署源码摘要。共享执行入口来自工作区源码静态追踪。

视觉策略库接口返回活动版本 v1.0.0，SHA-256 为 7d8f411e8b664a4ef5ebefe8282225c80520288bde3c7dda4a51e7fe41bdde7b。归档文件原始字节校验结果为 通过。

## 原始 Python 规则摘录

以下函数来自快照，保留原有计算与控制流。完整依赖见策略 JSON 中的源文件清单。

### config/rule_v26/b-v31-signals.py


```python
def result(bars,i,direction,setup):
    recent=bars[i-3:i+1]
    stop=min(n(x,'low') for x in recent)-TICK if direction=='LONG' else max(n(x,'high') for x in recent)+TICK
    return {'direction':direction,'stop':stop,'target':None,'setup':setup}

def signal_b(bars,i):
    if i<10:return None
    b=bars[i];p=bars[i-1];prior=bars[i-10:i];lo=min(n(x,'low') for x in prior);hi=max(n(x,'high') for x in prior);o=n(b,'open');c=n(b,'close');h=n(b,'high');l=n(b,'low');body=abs(c-o)
    if l<lo and c>lo:return result(bars,i,'LONG','B_FALSE_BREAK')
    if h>hi and c<hi:return result(bars,i,'SHORT','B_FALSE_BREAK')
    if l<=lo*D('1.001') and c>o and min(o,c)-l>=2*max(body,TICK):return result(bars,i,'LONG','B_WICK')
    if h>=hi*D('.999') and c<o and h-max(o,c)>=2*max(body,TICK):return result(bars,i,'SHORT','B_WICK')
    if l<=lo*D('1.001') and n(p,'close')<n(p,'open') and c>o and o<=n(p,'close') and c>=n(p,'open'):return result(bars,i,'LONG','B_ENGULF')
    if h>=hi*D('.999') and n(p,'close')>n(p,'open') and c<o and o>=n(p,'close') and c<=n(p,'open'):return result(bars,i,'SHORT','B_ENGULF')
    return None

def signal_b_filtered(bars, i):
    if i < 51:
        return None
    candidate = signal_b(bars, i)
    if candidate is None:
        return None
    fast = sum(n(b, 'close') for b in bars[i-11:i+1]) / D(12)
    slow = sum(n(b, 'close') for b in bars[i-47:i+1]) / D(48)
    previous_fast = sum(n(b, 'close') for b in bars[i-15:i-3]) / D(12)
    direction = candidate['direction']
    if direction == 'LONG' and fast > slow and fast > previous_fast:
        return candidate
    if direction == 'SHORT' and fast < slow and fast < previous_fast:
        return candidate
    return None
```


## 2026-11-20 首轮只读状态核验

- 归档状态：`ACTIVE`（指持久规则/配置状态；不代表当前正在运行）。
- 来源完整性：`SOURCE_COMPLETE_RUNTIME_UNVERIFIED`；规则源 SHA-256：`b8506be4e67ba40ec9234966371d42cd85aa750f4bdf00ca48ae3864388bc7a6`；源哈希核验：`PASS`。
- 当前运行状态：`UNVERIFIED`。本次检查未发现 8011 监听，无法读取进程内实际活动版本。
- 规则入口（只用于审计）：`config/rule_v26/b-v31-signals.py`, `config/rule_v26/profile.json`, `backend/rule_cycle.py`。
- 冲突/限制：
  - 当前无 8011 监听，运行进程内版本无法读取；静态规则文件 SHA 与 profile 一致。
- 未核实字段：当前进程内版本、当前启停与入场许可、最近持久证据后的周期处理。
- V3 处理：本轮只归档，不注册、不启用、不执行该策略。
