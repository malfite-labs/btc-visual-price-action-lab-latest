# PA-04 PA 无缺口三K反转

活动市场 BTCUSDT（Binance Demo USDⓈ-M），计价 USDT。研究初始权益以 8011 实测字段为准（10000）。

## 1. 策略定位

量化假设：首根强阴/阳实体比至少0.6，中根小实体不超过0.25，末根反向强K实体比至少0.55且收盘超首根开盘；不要求24小时市场罕见的缺口。 信号仅用已收盘K线；v2下一根开盘入场；固定信号时摆动+ATR止损、2R止盈、最长96根K线、同根止损优先。上传保持未启用；自然周期下一开盘执行待代码修复验收。

## 2. 适用行情

按原始布尔表达式中的趋势、突破或反转条件识别。

## 3. 指标与参数

K 线偏移 0 为最近已收盘根，SMA 取窗口收盘算术均值，ATR 为窗口 TR 算术均值，实体比例为 |C−O|/(H−L)。策略参数 {
  "first_body": 0.6,
  "middle_body": 0.25,
  "min_body": 0.55
}。窗口与比较阈值见下列条件。

## 4. 做多规则

((SMA(20, 0) < SMA(60, 0)) 且 (收盘(2) < 开盘(2)) 且 (实体比例(2) ≥ 参数(first_body)) 且 (实体比例(1) ≤ 参数(middle_body)) 且 (收盘(0) > 开盘(0)) 且 (实体比例(0) ≥ 参数(min_body)) 且 (收盘(0) > 开盘(2)))

## 5. 做空规则

((SMA(20, 0) > SMA(60, 0)) 且 (收盘(2) > 开盘(2)) 且 (实体比例(2) ≥ 参数(first_body)) 且 (实体比例(1) ≤ 参数(middle_body)) 且 (收盘(0) < 开盘(0)) 且 (实体比例(0) ≥ 参数(min_body)) 且 (收盘(0) < 开盘(2)))

## 6. 信号确认

使用连续、已收盘的 BTCUSDT 15 分钟 K 线。常规路径在最新收盘后的 5 秒窗口内检查 Binance Demo 有效盘口并发单，多头用 ask、空头用 bid，实际成交以交易所确认回报为准。 双向同时成立则不入场，数据缺口不可用历史成交补造。

## 7. 止损规则

最近 3 根极值，止损向外加减 0.1×ATR14。信号收盘价到止损的距离须在 [0.5,4]×ATR 内。

## 8. 止盈规则

确认成交后的 2R。

## 9. 退出与超时

有效报价多头用 bid、空头用 ask。共享退出检查先判断止损，再判断止盈，最后检查超时，使用确认仓位及真实退出成交。旧版 OHLC 同根优先级原样保留在原文中。 最长 96 根。原始 same_bar_priority=stop。

## 10. 特殊逻辑

只在授权启用、市场与对账门禁许可时开新仓。归属仓位仍接受保护。订单意图按策略及信号周期去重。

## 11. 资金与仓位

现网 V2 研究初始权益以 8011 返回为准。适用的原风险定仓、杠杆上限、交易所约束及保护保留；估算手续费与实际成交费用分开。 原始风险字段 {
  "currency": "USD_RESEARCH",
  "initial_equity": "10000",
  "max_leverage": 5,
  "risk_fraction": "0.02"
}。

## 12. 原始来源与版本

归档号 PA-04，运行 ID pa-gapless-three-bar-reversal。8011 版本 1.0.0，启用 True，当前入场许可 True。

策略库或内置版本证据


```json
{
  "id": "pa-gapless-three-bar-reversal",
  "active_version": "1.0.0",
  "enabled": 1,
  "archived_at": null,
  "sha256": "da1b870fbe7e378d56427451872bd0cbcb06a7e1063c09deb6a5e23e320b14cd",
  "uploaded_at": "2026-11-19T14:56:29.399851+00:00",
  "path": "data-binance-demo/strategy-pool.sqlite3",
  "source": "strategies.active_version → versions.document",
  "canonical_sha256_verified": true
}
```

规则校验


```json
{
  "method": "sqlite_canonical_json",
  "sha256": "da1b870fbe7e378d56427451872bd0cbcb06a7e1063c09deb6a5e23e320b14cd",
  "verified": true
}
```

开仓入口 DemoPARuntime.on_event → execute_next_open → strategy_pool.evaluate_entry_v2 / build_v2_stop。

退出入口 DemoPARuntime._protect → DemoHedgeCoordinator.exit_on_quote → finish_position。

结构化布尔条件见 strategy.json。

原始路径和逐文件 SHA-256 见本目录 strategy.json。

8011 状态接口提供策略 ID、版本和启用状态，但未发布整个进程的部署源码摘要。共享执行入口来自工作区源码静态追踪。

## 原始 JSON 规则


```json
{
  "description": "量化假设：首根强阴/阳实体比至少0.6，中根小实体不超过0.25，末根反向强K实体比至少0.55且收盘超首根开盘；不要求24小时市场罕见的缺口。 信号仅用已收盘K线；v2下一根开盘入场；固定信号时摆动+ATR止损、2R止盈、最长96根K线、同根止损优先。上传保持未启用；自然周期下一开盘执行待代码修复验收。",
  "entry": {
    "long": {
      "args": [
        {
          "args": [
            {
              "args": [
                20,
                0
              ],
              "op": "sma"
            },
            {
              "args": [
                60,
                0
              ],
              "op": "sma"
            }
          ],
          "op": "lt"
        },
        {
          "args": [
            {
              "args": [
                2
              ],
              "op": "close"
            },
            {
              "args": [
                2
              ],
              "op": "open"
            }
          ],
          "op": "lt"
        },
        {
          "args": [
            {
              "args": [
                2
              ],
              "op": "body_fraction"
            },
            {
              "args": [
                "first_body"
              ],
              "op": "param"
            }
          ],
          "op": "gte"
        },
        {
          "args": [
            {
              "args": [
                1
              ],
              "op": "body_fraction"
            },
            {
              "args": [
                "middle_body"
              ],
              "op": "param"
            }
          ],
          "op": "lte"
        },
        {
          "args": [
            {
              "args": [
                0
              ],
              "op": "close"
            },
            {
              "args": [
                0
              ],
              "op": "open"
            }
          ],
          "op": "gt"
        },
        {
          "args": [
            {
              "args": [
                0
              ],
              "op": "body_fraction"
            },
            {
              "args": [
                "min_body"
              ],
              "op": "param"
            }
          ],
          "op": "gte"
        },
        {
          "args": [
            {
              "args": [
                0
              ],
              "op": "close"
            },
            {
              "args": [
                2
              ],
              "op": "open"
            }
          ],
          "op": "gt"
        }
      ],
      "op": "and"
    },
    "short": {
      "args": [
        {
          "args": [
            {
              "args": [
                20,
                0
              ],
              "op": "sma"
            },
            {
              "args": [
                60,
                0
              ],
              "op": "sma"
            }
          ],
          "op": "gt"
        },
        {
          "args": [
            {
              "args": [
                2
              ],
              "op": "close"
            },
            {
              "args": [
                2
              ],
              "op": "open"
            }
          ],
          "op": "gt"
        },
        {
          "args": [
            {
              "args": [
                2
              ],
              "op": "body_fraction"
            },
            {
              "args": [
                "first_body"
              ],
              "op": "param"
            }
          ],
          "op": "gte"
        },
        {
          "args": [
            {
              "args": [
                1
              ],
              "op": "body_fraction"
            },
            {
              "args": [
                "middle_body"
              ],
              "op": "param"
            }
          ],
          "op": "lte"
        },
        {
          "args": [
            {
              "args": [
                0
              ],
              "op": "close"
            },
            {
              "args": [
                0
              ],
              "op": "open"
            }
          ],
          "op": "lt"
        },
        {
          "args": [
            {
              "args": [
                0
              ],
              "op": "body_fraction"
            },
            {
              "args": [
                "min_body"
              ],
              "op": "param"
            }
          ],
          "op": "gte"
        },
        {
          "args": [
            {
              "args": [
                0
              ],
              "op": "close"
            },
            {
              "args": [
                2
              ],
              "op": "open"
            }
          ],
          "op": "lt"
        }
      ],
      "op": "and"
    }
  },
  "format": "btc-lab-declarative-strategy/v2",
  "market": "BTC/USD",
  "name": "PA 无缺口三K反转",
  "parameters": {
    "first_body": 0.6,
    "middle_body": 0.25,
    "min_body": 0.55
  },
  "position": {
    "direction": "both",
    "entry_timing": "next_open",
    "max_hold_bars": 96,
    "same_bar_priority": "stop",
    "stop": {
      "atr_period": 14,
      "buffer_atr": 0.1,
      "kind": "swing_atr",
      "lookback": 3,
      "max_distance_atr": 4,
      "min_distance_atr": 0.5
    },
    "target_r": 2
  },
  "risk": {
    "currency": "USD_RESEARCH",
    "initial_equity": "10000",
    "max_leverage": 5,
    "risk_fraction": "0.02"
  },
  "strategy_id": "pa-gapless-three-bar-reversal",
  "timeframe": "15m",
  "version": "1.0.0"
}
```


## 2026-11-20 首轮只读状态核验

- 归档状态：`ACTIVE`（指持久规则/配置状态；不代表当前正在运行）。
- 来源完整性：`SOURCE_COMPLETE_RUNTIME_UNVERIFIED`；规则源 SHA-256：`da1b870fbe7e378d56427451872bd0cbcb06a7e1063c09deb6a5e23e320b14cd`；源哈希核验：`PASS`。
- 当前运行状态：`UNVERIFIED`。本次检查未发现 8011 监听，无法读取进程内实际活动版本。
- 规则入口（只用于审计）：`registry/pa-gapless-three-bar-reversal/1.0.0.json`, `backend/strategy_pool.py`, `backend/strategy.py`。
- 冲突/限制：
  - 策略库 enabled=1、版本 SHA 已核对；最近保存周期为 2026-11-19 15:15 UTC，当前无 8011，不能称正在运行。
- 未核实字段：当前进程内版本、当前启停与入场许可、最近持久证据后的周期处理。
- V3 处理：本轮只归档，不注册、不启用、不执行该策略。
