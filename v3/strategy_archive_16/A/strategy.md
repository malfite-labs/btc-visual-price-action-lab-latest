# A 突破回踩

活动市场 BTCUSDT（Binance Demo USDⓈ-M），计价 USDT。研究初始权益以 8011 实测字段为准（10000）。

## 1. 策略定位

突破既有极值后回踩确认。

## 2. 适用行情

趋势突破与回踩。

## 3. 指标与参数

8 根滚动最高/最低截至偏移 2。K 线实体占比为 |C−O|/(H−L)，阈值严格大于 0.2。

## 4. 做多规则

上一收盘突破该高点，本根最低回踩，本根收盘高于该高点且高于上一收盘，实体条件通过。

## 5. 做空规则

上一收盘跌破该低点，本根最高反抽，本根收盘低于该低点且低于上一收盘，实体条件通过。

## 6. 信号确认

使用连续、已收盘的 BTCUSDT 15 分钟 K 线。常规路径在最新收盘后的 5 秒窗口内检查 Binance Demo 有效盘口并发单，多头用 ask、空头用 bid，实际成交以交易所确认回报为准。 A/B/C 使用 288 根，双向同时满足则放弃。

## 7. 止损规则

含信号根最近 6 根的最低减 0.01（多）或最高加 0.01（空）。

## 8. 止盈规则

以确认入场价和初始止损距离设 1.8R。预检目标须覆盖预计双边手续费。

## 9. 退出与超时

有效报价多头用 bid、空头用 ask。共享退出检查先判断止损，再判断止盈，最后检查超时，使用确认仓位及真实退出成交。旧版 OHLC 同根优先级原样保留在原文中。 最长 8 根。

## 10. 特殊逻辑

规则档由工作配置 SHA-256 锁定。

## 11. 资金与仓位

现网 V2 研究初始权益以 8011 返回为准。适用的原风险定仓、杠杆上限、交易所约束及保护保留；估算手续费与实际成交费用分开。 风险比例 2%，策略杠杆上限 1×。

## 12. 原始来源与版本

归档号 A，运行 ID A。8011 版本 V2.6，启用 True，当前入场许可 True。

策略库或内置版本证据


```json
{
  "kind": "frozen_active_profile_and_separate_visual_library",
  "active_profile_version": "V2.6",
  "visual_library_metadata": {
    "agent": "A",
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
  "visual_library_metadata_path": "strategies/A/v1.0.0.metadata.json"
}
```

规则校验


```json
{
  "method": "frozen_profile_matches_file",
  "sha256": "af9ca1ffc97aefc021b2e8f4d041596fca4fa963b6a76dc605dfd2aa644af691",
  "verified": true
}
```

开仓入口 binance_demo_pa.execute_next_open → rule_cycle.evaluate_rule_signal → rule_evolution_v2.signal。

退出入口 DemoPARuntime._protect → DemoHedgeCoordinator.exit_on_quote → finish_position。

原始 JSON 已原样保留，解释器以代码快照复核。

原始路径和逐文件 SHA-256 见本目录 strategy.json。

8011 状态接口提供策略 ID、版本和启用状态，但未发布整个进程的部署源码摘要。共享执行入口来自工作区源码静态追踪。

视觉策略库接口返回活动版本 v1.0.0，SHA-256 为 9d5272f3eac221566a769287f81bd452fcf6d324d61774934f059efbfac1e0b1。归档文件原始字节校验结果为 通过。

## 原始 Python 规则摘录

以下函数来自快照，保留原有计算与控制流。完整依赖见策略 JSON 中的源文件清单。

### scripts/rule_evolution_v2.py


```python
def value(spec,bars,i):
 if type(spec) in (int,float):return D(str(spec))
 j=i-spec.get('offset',0);kind=spec['kind'];period=spec.get('period',2)
 if j<0:raise IndexError()
 b=bars[j];n=lambda x,k:D(str(x[k]))
 if kind in ('open','high','low','close'):return n(b,kind)
 if kind in ('rolling_high','rolling_low','trend_pct','range_pct'):
  if j-period+1<0:raise IndexError()
  w=bars[j-period+1:j+1]
  if kind=='rolling_high':return max(n(x,'high') for x in w)
  if kind=='rolling_low':return min(n(x,'low') for x in w)
  if kind=='trend_pct':return (n(w[-1],'close')/n(w[0],'close')-1)*100
  return (max(n(x,'high') for x in w)-min(n(x,'low') for x in w))/n(w[-1],'close')*100
 h=n(b,'high');l=n(b,'low');o=n(b,'open');c=n(b,'close');r=h-l
 if r<=0:raise IndexError()
 if kind=='body_ratio':return abs(c-o)/r
 if kind=='upper_wick_ratio':return (h-max(o,c))/r
 return (min(o,c)-l)/r

def signal(rule,bars,i):
 try:
  matches=[side for side in ('long','short') if all(OPS[c['op']](value(c['left'],bars,i),value(c['right'],bars,i)) for c in rule[side])]
  if len(matches)!=1 or i+1<rule['stop_bars']:return None
  side=matches[0];w=bars[i-rule['stop_bars']+1:i+1];stop=(min(D(str(x['low'])) for x in w)-D('.01')) if side=='long' else (max(D(str(x['high'])) for x in w)+D('.01'))
  return {'direction':side.upper(),'stop':stop,'target':None,'setup':'AI_RULE_V2'}
 except (IndexError,ZeroDivisionError):return None
```

## 原始 JSON 规则


```json
{
  "long": [
    {
      "left": {
        "kind": "close",
        "offset": 1
      },
      "op": ">",
      "right": {
        "kind": "rolling_high",
        "offset": 2,
        "period": 8
      }
    },
    {
      "left": {
        "kind": "low",
        "offset": 0
      },
      "op": "<=",
      "right": {
        "kind": "rolling_high",
        "offset": 2,
        "period": 8
      }
    },
    {
      "left": {
        "kind": "close",
        "offset": 0
      },
      "op": ">",
      "right": {
        "kind": "rolling_high",
        "offset": 2,
        "period": 8
      }
    },
    {
      "left": {
        "kind": "close",
        "offset": 0
      },
      "op": ">",
      "right": {
        "kind": "close",
        "offset": 1
      }
    },
    {
      "left": {
        "kind": "body_ratio",
        "offset": 0
      },
      "op": ">",
      "right": 0.2
    }
  ],
  "short": [
    {
      "left": {
        "kind": "close",
        "offset": 1
      },
      "op": "<",
      "right": {
        "kind": "rolling_low",
        "offset": 2,
        "period": 8
      }
    },
    {
      "left": {
        "kind": "high",
        "offset": 0
      },
      "op": ">=",
      "right": {
        "kind": "rolling_low",
        "offset": 2,
        "period": 8
      }
    },
    {
      "left": {
        "kind": "close",
        "offset": 0
      },
      "op": "<",
      "right": {
        "kind": "rolling_low",
        "offset": 2,
        "period": 8
      }
    },
    {
      "left": {
        "kind": "close",
        "offset": 0
      },
      "op": "<",
      "right": {
        "kind": "close",
        "offset": 1
      }
    },
    {
      "left": {
        "kind": "body_ratio",
        "offset": 0
      },
      "op": ">",
      "right": 0.2
    }
  ],
  "stop_bars": 6,
  "target_r": 1.8,
  "max_hold": 8
}
```


## 2026-11-20 首轮只读状态核验

- 归档状态：`ACTIVE`（指持久规则/配置状态；不代表当前正在运行）。
- 来源完整性：`SOURCE_COMPLETE_RUNTIME_UNVERIFIED`；规则源 SHA-256：`af9ca1ffc97aefc021b2e8f4d041596fca4fa963b6a76dc605dfd2aa644af691`；源哈希核验：`PASS`。
- 当前运行状态：`UNVERIFIED`。本次检查未发现 8011 监听，无法读取进程内实际活动版本。
- 规则入口（只用于审计）：`config/rule_v26/A-rule.json`, `config/rule_v26/profile.json`, `backend/rule_cycle.py`, `scripts/rule_evolution_v2.py`。
- 冲突/限制：
  - 当前无 8011 监听，运行进程内版本无法读取；静态规则文件 SHA 与 profile 一致。
- 未核实字段：当前进程内版本、当前启停与入场许可、最近持久证据后的周期处理。
- V3 处理：本轮只归档，不注册、不启用、不执行该策略。
