# C Tony 裸K研究近似

活动市场 BTCUSDT（Binance Demo USDⓈ-M），计价 USDT。研究初始权益以 8011 实测字段为准（10000）。

## 1. 策略定位

顺势回踩、局部突破及动量延续。

## 2. 适用行情

趋势及反弹结束。

## 3. 指标与参数

方向为上一收盘对第 12 根或第 96 根收盘的比较。局部突破看前8根，T3 三根实体各至少占全幅50%。

## 4. 做多规则

依次 T4 大方向向上、两阴后一阳突破上一高点；T1 近端上行、阳阴阳后突破上一高；T2 近端上行、收破前8根最高；T3 近端上行、连续三强阳收盘递增。

## 5. 做空规则

T4 大方向下行、两阳后一阴跌破上一低点；T1 阴阳阴后跌破上一低；T2 收破前8根最低；T3 三强阴收盘递减。各自要求相应趋势方向。

## 6. 信号确认

使用连续、已收盘的 BTCUSDT 15 分钟 K 线。常规路径在最新收盘后的 5 秒窗口内检查 Binance Demo 有效盘口并发单，多头用 ask、空头用 bid，实际成交以交易所确认回报为准。 A/B/C 使用 288 根。

## 7. 止损规则

最近 4 根的最低减 0.01（多）或最高加 0.01（空）。

## 8. 止盈规则

确认入场后的 1.3R。T1–T4 记录前20根障碍参考，现行配置不以障碍或目标成本规则否决。

## 9. 退出与超时

有效报价多头用 bid、空头用 ask。共享退出检查先判断止损，再判断止盈，最后检查超时，使用确认仓位及真实退出成交。旧版 OHLC 同根优先级原样保留在原文中。 最长 12 根。

## 10. 特殊逻辑

T4 优先；G 以本策略已确认成交为镜像输入。

## 11. 资金与仓位

现网 V2 研究初始权益以 8011 返回为准。适用的原风险定仓、杠杆上限、交易所约束及保护保留；估算手续费与实际成交费用分开。 2%，策略杠杆上限 5×。

## 12. 原始来源与版本

归档号 C，运行 ID C。8011 版本 V3.1，启用 True，当前入场许可 True。

策略库或内置版本证据


```json
{
  "kind": "frozen_active_profile_and_separate_visual_library",
  "active_profile_version": "V3.1",
  "visual_library_metadata": {
    "agent": "C",
    "version": "v1.0.0",
    "parent": null,
    "status": "ACTIVE",
    "md_sha256": "672451cfc52201fd4a216dd2578c52c14ec5a1e866d5cc5ff4d94bdf1b766d20",
    "core_lock_sha256": "ae0a9a5f072c553d2aca1c198330a29a3d2c5a2fc8696fe1cd82deac7e4f858e",
    "core_scope": "Section 1 and Section 2 protected; full boundaries require validation before activation",
    "evolvable_sections": "awaiting user-designated sections",
    "immutable": true,
    "research_leverage": "5",
    "maintenance_rate": "0.01",
    "margin_model": "C_SPOT_RESEARCH_MARGIN_V1_1PCT",
    "activation_source": "user_authorized_internal_paper"
  },
  "visual_library_metadata_path": "strategies/C/v1.0.0.metadata.json"
}
```

规则校验


```json
{
  "method": "frozen_profile_matches_file",
  "sha256": "c27eef69cc256444ed7ad0d501cc695adbdeb06867c8ed063db2c6c01a9a69e5",
  "verified": true
}
```

开仓入口 binance_demo_pa.execute_next_open → rule_cycle.evaluate_rule_signal → v3-signals.signal_c。

退出入口 DemoPARuntime._protect → DemoHedgeCoordinator.exit_on_quote → finish_position。

Python 策略以代码快照复核。

原始路径和逐文件 SHA-256 见本目录 strategy.json。

8011 状态接口提供策略 ID、版本和启用状态，但未发布整个进程的部署源码摘要。共享执行入口来自工作区源码静态追踪。

视觉策略库接口返回活动版本 v1.0.0，SHA-256 为 672451cfc52201fd4a216dd2578c52c14ec5a1e866d5cc5ff4d94bdf1b766d20。归档文件原始字节校验结果为 通过。

## 原始 Python 规则摘录

以下函数来自快照，保留原有计算与控制流。完整依赖见策略 JSON 中的源文件清单。

### config/rule_v26/v3-signals.py


```python
def trend(bars,i,length=12):
    if i<length:return 0
    last=n(bars[i-1],'close');first=n(bars[i-length],'close')
    return 1 if last>first else -1 if last<first else 0

def result(bars,i,direction,setup):
    recent=bars[i-3:i+1]
    stop=min(n(x,'low') for x in recent)-TICK if direction=='LONG' else max(n(x,'high') for x in recent)+TICK
    return {'direction':direction,'stop':stop,'target':None,'setup':setup}

def signal_c(bars,i):
    if i<12:return None
    d=trend(bars,i);b=bars[i];p=bars[i-1];prior=bars[i-8:i];c=n(b,'close');o=n(b,'open')
    def make(direction,setup):
        x=result(bars,i,direction,setup); nearby=bars[max(0,i-20):i]
        level=max(n(y,'high') for y in nearby) if direction=='LONG' else min(n(y,'low') for y in nearby)
        x['obstacle']=level if (direction=='LONG' and level>c) or (direction=='SHORT' and level<c) else None
        return x
    if i>=96:
        broad=trend(bars,i,96);q=bars[i-2]
        if broad==-1 and n(q,'close')>n(q,'open') and n(p,'close')>n(p,'open') and c<o and c<n(p,'low'):return make('SHORT','T4')
        if broad==1 and n(q,'close')<n(q,'open') and n(p,'close')<n(p,'open') and c>o and c>n(p,'high'):return make('LONG','T4')
    q=bars[i-2]
    if d==1 and n(q,'close')>n(q,'open') and n(p,'close')<n(p,'open') and c>o and c>n(p,'high'):return make('LONG','T1')
    if d==-1 and n(q,'close')<n(q,'open') and n(p,'close')>n(p,'open') and c<o and c<n(p,'low'):return make('SHORT','T1')
    if d==1 and c>max(n(x,'high') for x in prior):return make('LONG','T2')
    if d==-1 and c<min(n(x,'low') for x in prior):return make('SHORT','T2')
    recent=bars[i-2:i+1]
    strong=all((n(x,'high')-n(x,'low'))>0 and abs(n(x,'close')-n(x,'open'))>=D('.5')*(n(x,'high')-n(x,'low')) for x in recent)
    if strong and d==1 and all(n(x,'close')>n(x,'open') for x in recent) and n(recent[0],'close')<n(recent[1],'close')<c:return make('LONG','T3')
    if strong and d==-1 and all(n(x,'close')<n(x,'open') for x in recent) and n(recent[0],'close')>n(recent[1],'close')>c:return make('SHORT','T3')
    return None
```


## 2026-11-20 首轮只读状态核验

- 归档状态：`ACTIVE`（指持久规则/配置状态；不代表当前正在运行）。
- 来源完整性：`SOURCE_COMPLETE_RUNTIME_UNVERIFIED`；规则源 SHA-256：`c27eef69cc256444ed7ad0d501cc695adbdeb06867c8ed063db2c6c01a9a69e5`；源哈希核验：`PASS`。
- 当前运行状态：`UNVERIFIED`。本次检查未发现 8011 监听，无法读取进程内实际活动版本。
- 规则入口（只用于审计）：`config/rule_v26/v3-signals.py`, `config/rule_v26/profile.json`, `backend/rule_cycle.py`。
- 冲突/限制：
  - 当前无 8011 监听，运行进程内版本无法读取；静态规则文件 SHA 与 profile 一致。
- 未核实字段：当前进程内版本、当前启停与入场许可、最近持久证据后的周期处理。
- V3 处理：本轮只归档，不注册、不启用、不执行该策略。
