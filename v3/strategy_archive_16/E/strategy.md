# E E V1.1 量化结构策略

活动市场 BTCUSDT（Binance Demo USDⓈ-M），计价 USDT。研究初始权益以 8011 实测字段为准（10000）。

## 1. 策略定位

区间及结构反转，量化四模式。

## 2. 适用行情

区间下沿、防守转强、突破回踩及跌破反抽。

## 3. 指标与参数

前 32 根真实波幅中位数 u；ε=max(0.0005C,0.15u)，容差=max(0.0005C,0.20u)。区间按 64/48/96/144 根顺序，宽 0.5%–6%，两侧触及间隔至少 6 根且 ≥70% 收盘在区间。

## 4. 做多规则

E1 下沿两根确认，E2 急跌后反转，E3 上沿突破、回踩及续强。逐项公式见 detect_setups 摘录。

## 5. 做空规则

E4 下沿跌破、回踩及续弱仍在检测器中，现行 Demo allow_short 默认关闭。E4 不提交空单。

## 6. 信号确认

使用连续、已收盘的 BTCUSDT 15 分钟 K 线。常规路径在最新收盘后的 5 秒窗口内检查 Binance Demo 有效盘口并发单，多头用 ask、空头用 bid，实际成交以交易所确认回报为准。 使用 288 根。双向候选冲突跳过，同向优先级 E3、E1、E2、E4。扣成本后的收益/风险要求至少 2。

## 7. 止损规则

各模式结构极值加减 0.15u。持仓使用确认的七根结构枢轴，只有入场后确认的改善结构才能收紧。

## 8. 止盈规则

研究收益参考分别用区间对侧、跌前价格、箱体投影与局部障碍。Demo 以结构退出，2R 是预检参考。

## 9. 退出与超时

有效 bid/ask 止损或收盘突破反向结构后由下一有效报价执行。Demo 文档设置 max_hold_bars=0，结构分支不以该值计固定超时。

## 10. 特殊逻辑

E1 同区间键避免重复开仓；仅确认的已收盘结构可推进。E V1.0 与 E V1.1 的原始文档分开保留。

## 11. 资金与仓位

现网 V2 研究初始权益以 8011 返回为准。适用的原风险定仓、杠杆上限、交易所约束及保护保留；估算手续费与实际成交费用分开。 2%，策略杠杆上限 1×。

## 12. 原始来源与版本

归档号 E，运行 ID E。8011 版本 E-V1.1-QUANT，启用 True，当前入场许可 True。

策略库或内置版本证据


```json
{
  "kind": "versioned_research_documents",
  "version": "E-V1.1-QUANT",
  "previous_version": "E-V1.0",
  "file": "strategies/E/E-V1.1-QUANT.metadata.json"
}
```

规则校验


```json
{
  "method": "sha256_of_workspace_sources",
  "verified_local_copy": true
}
```

开仓入口 binance_demo_pa.execute_next_open → e_quant_v11.detect_setups → e_quant_gate.quant_reward_risk。

退出入口 DemoPARuntime._e_structure_close / _protect → structure_action / DemoHedgeCoordinator.exit_on_quote。

Python 策略以代码快照复核。

原始路径和逐文件 SHA-256 见本目录 strategy.json。

8011 状态接口提供策略 ID、版本和启用状态，但未发布整个进程的部署源码摘要。共享执行入口来自工作区源码静态追踪。

## 原始 Python 规则摘录

以下函数来自快照，保留原有计算与控制流。完整依赖见策略 JSON 中的源文件清单。

### backend/e_quant_v11.py


```python
def _window(bars,end):
    """Fixed range order; the end is exclusive and may not include a breakout bar."""
    eps,tol,_ = _scale(bars,end-1)
    for length in (64,48,96,144):
        start=end-length
        if start<1: continue
        chunk=bars[start:end]
        lo=min(b['low'] for b in chunk);hi=max(b['high'] for b in chunk);mid=(hi+lo)/2
        if mid<=0 or not D('.005') <= (hi-lo)/mid <= D('.06'):continue
        touch=max(D('.15')*(hi-lo),tol)
        lows=[i for i,b in enumerate(chunk) if b['low']-lo<=touch]
        highs=[i for i,b in enumerate(chunk) if hi-b['high']<=touch]
        if not lows or not highs or lows[-1]-lows[0]<6 or highs[-1]-highs[0]<6:continue
        if sum(lo<=b['close']<=hi for b in chunk)*10 < len(chunk)*7:continue
        return {'start':start,'end':end-1,'lo':lo,'hi':hi}
    return None

def _scale(bars,t):
    if t<33: raise ValueError('INSUFFICIENT_SCALE_HISTORY')
    tr=[max(bars[i]['high']-bars[i]['low'],abs(bars[i]['high']-bars[i-1]['close']),abs(bars[i]['low']-bars[i-1]['close'])) for i in range(t-32,t)]
    u=median(tr)
    if u<=0:raise ValueError('INVALID_RANGE_SCALE')
    return max(D('.0005')*bars[t]['close'],D('.15')*u),max(D('.0005')*bars[t]['close'],D('.20')*u),u

def detect_setups(rows,*,_shadow_change=None):
    """Validate a contiguous snapshot, then detect all four E setups deterministically."""
    resolve_e_decision({'action':'SKIP'},rows,mapping_verified=False)
    try:
        change=_shadow_change or {}
        if not isinstance(change,dict) or len(change)>1:raise ValueError('INVALID_SHADOW_CHANGE')
        allowed={'E2_drop_window':set(range(6,13,2)),'E3_breakout_window':set(range(3,11)),'E4_breakdown_window':set(range(3,11))}
        if any(k not in allowed or type(v) is not int or v not in allowed[k] for k,v in change.items()):raise ValueError('INVALID_SHADOW_CHANGE')
        bars=[{**b,**{k:D(str(b[k])) for k in ('open','high','low','close')}} for b in rows]
        t=len(bars)-1;eps,tol,u=_scale(bars,t);cur=bars[t];out=[];diagnostics=[]
        region=_window(bars,t-1)
        if region:
            lo,hi=region['lo'],region['hi'];prev=bars[t-1]
            if prev['low']<=lo+tol and prev['close']<=lo+tol and cur['close']>=lo+eps and cur['close']>cur['open'] and cur['close']>prev['close']:
                j=min((t-1,t),key=lambda i:(bars[i]['low'],i))
                out.append({'setup':'E1','side':'LONG','stop':str(bars[j]['low']-D('.15')*u),'reward':str(hi),'anchor':_ref(bars,j),'range':region})
        else:diagnostics.append('RANGE_INVALID')
        k=change.get('E2_drop_window',8)
        j=min(range(t-5,t),key=lambda i:(bars[i]['low'],i));low=bars[j]['low'];base=bars[t-k]['close'];minlow=min(bars[i]['low'] for i in range(t-k+1,t+1))
        if ((base-minlow)/base>=max(D('.008'),D('2.5')*u/cur['close']) and cur['low']>low and cur['close']>cur['open'] and cur['close']>max(bars[t-2]['high'],bars[t-1]['high'])+eps and cur['close']>=low+D('.35')*(base-low)):
            out.append({'setup':'E2','side':'LONG','stop':str(low-D('.15')*u),'reward':str(base),'anchor':_ref(bars,j)})
        for direction,setup in (('LONG','E3'),('SHORT','E4')):
            lookback=change.get('E3_breakout_window' if direction=='LONG' else 'E4_breakdown_window',8)
            for b in range(t-lookback,t):
                box=_window(bars,b)
                if not box:continue
                e,_,_=_scale(bars,b);edge=box['hi'] if direction=='LONG' else box['lo']
                broken=(bars[b]['close']>edge+e and bars[b-1]['close']<=edge+e) if direction=='LONG' else (bars[b]['close']<edge-e and bars[b-1]['close']>=edge-e)
                if not broken:continue
                retests=[]
                for r in range(b+1,t):
                    er,tr,_=_scale(bars,r)
                    if (bars[r]['low']<=edge+tr and bars[r]['close']>=edge-er) if direction=='LONG' else (bars[r]['high']>=edge-tr and bars[r]['close']<=edge+er):retests.append(r)
                confirmed=(cur['close']>edge+eps and cur['close']>cur['open'] and cur['low']>=box['lo']) if direction=='LONG' else (cur['close']<edge-eps and cur['close']<cur['open'])
                if retests and confirmed:
                    group=range(b+1,t+1);j=min(group,key=lambda i:(bars[i]['low'],i)) if direction=='LONG' else max(group,key=lambda i:(bars[i]['high'],-i))
                    stop=(bars[j]['low']-D('.15')*u) if direction=='LONG' else (bars[j]['high']+D('.15')*u)
                    target=edge+(box['hi']-box['lo'])*(1 if direction=='LONG' else -1)
                    # Provisional conservative visual obstacle proxy: a pre-breakout local swing.
                    # It is tagged as engineering inference and never authorizes an order.
                    if direction=='LONG':
                        obstacles=[bars[i]['high'] for i in range(1,b-1) if bars[i]['high']>bars[i-1]['high'] and bars[i]['high']>=bars[i+1]['high'] and cur['close']<bars[i]['high']<target]
                        if obstacles:target=min(obstacles)
                    else:
                        obstacles=[bars[i]['low'] for i in range(1,b-1) if bars[i]['low']<bars[i-1]['low'] and bars[i]['low']<=bars[i+1]['low'] and target<bars[i]['low']<cur['close']]
                        if obstacles:target=max(obstacles)
                    out.append({'setup':setup,'side':direction,'stop':str(stop),'reward':str(target),'anchor':_ref(bars,j),'breakout':_ref(bars,b),'retest':_ref(bars,retests[0]),'range':box,'reward_note':'ENGINEERING_LOCAL_SWING_PROXY_OR_BOX_PROJECTION'})
                elif not retests:diagnostics.append('NOT_RETESTED')
                break
        directions={x['side'] for x in out}
        for x in out:
            if 'range' in x:x['range']={k:str(v) if isinstance(v,Decimal) else v for k,v in x['range'].items()}
        selected=None if len(directions)>1 else next((x for name in ('E3','E1','E2','E4') for x in out if x['setup']==name),None)
        snapshot_sha256=hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str).encode('utf8')).hexdigest()
        return {'strategy_version':'E-V1.1-QUANT','snapshot_sha256':snapshot_sha256,'closed_bar_time':rows[-1]['close_at'],'status':'CONFLICT_SKIP' if len(directions)>1 else 'SETUP_DETECTED' if out else 'NO_SETUP','candidates':out,'selected':selected,'diagnostics':diagnostics,'order_eligible':False,'order_reason':'VISUAL_AND_EXECUTION_NOT_VALIDATED'}
    except (KeyError,InvalidOperation,ArithmeticError) as exc:
        raise ValueError('INVALID_SNAPSHOT') from exc
```

### backend/e_quant_gate.py


```python
def quant_reward_risk(candidate, pre, cost):
    side = 1 if candidate["side"] == "LONG" else -1
    entry = Decimal(pre["fill_price"])
    slip = cost.adverse_slippage_bps / Decimal(10000)
    reward_fill = Decimal(candidate["reward"]) * (1 - slip * side)
    net_reward = (reward_fill - entry) * side - (entry + reward_fill) * cost.fee_rate
    risk = Decimal(pre["planned_loss"]) / Decimal(pre["quantity"])
    return risk, net_reward
```

### backend/e_structure.py


```python
def confirmed_pivots(bars,kind):
    if kind not in {'LOW','HIGH'}:raise ValueError('INVALID_STRUCTURE_KIND')
    if any(row.get('is_closed') is not True for row in bars):raise ValueError('UNCLOSED_CANDLE')
    field='low' if kind=='LOW' else 'high'
    values=[Decimal(str(row[field])) for row in bars]
    if any(not value.is_finite() or value<=0 for value in values):raise ValueError('INVALID_PRICE')
    return [i for i in range(3,len(values)-3)
            if all((values[i]<values[j] if kind=='LOW' else values[i]>values[j])
                   for j in (*range(i-3,i),*range(i+1,i+4)))]

def structure_action(bars,*,direction,scale):
    if direction not in {'LONG','SHORT'}:raise ValueError('INVALID_DIRECTION')
    scale=Decimal(str(scale))
    if not scale.is_finite() or scale<=0:raise ValueError('INVALID_SCALE')
    low=direction=='LONG';kind='LOW' if low else 'HIGH';field='low' if low else 'high'
    pivots=confirmed_pivots(bars,kind)
    if not pivots:return {'action':'HOLD','reason':'NO_CONFIRMED_STRUCTURE'}
    current=pivots[-1];level=Decimal(str(bars[current][field]));close=Decimal(str(bars[-1]['close']))
    if not close.is_finite() or close<=0:raise ValueError('INVALID_CLOSE')
    if (close<level if low else close>level):return {'action':'EXIT','pivot_index':current,'level':level}
    if len(pivots)<2:return {'action':'HOLD','reason':'NO_PRIOR_STRUCTURE'}
    previous=Decimal(str(bars[pivots[-2]][field]))
    if not (level>previous if low else level<previous):return {'action':'HOLD','reason':'NO_IMPROVEMENT'}
    return {'action':'TIGHTEN','pivot_index':current,'level':level,'stop':level+Decimal('0.15')*scale*(-1 if low else 1)}
```


## 2026-11-20 首轮只读状态核验

- 归档状态：`ACTIVE`（指持久规则/配置状态；不代表当前正在运行）。
- 来源完整性：`PARTIAL`；规则源 SHA-256：`UNVERIFIED`；源哈希核验：`UNVERIFIED`。
- 当前运行状态：`UNVERIFIED`。本次检查未发现 8011 监听，无法读取进程内实际活动版本。
- 规则入口（只用于审计）：`strategies/E/E-V1.1-QUANT.md`, `backend/e_quant_v11.py`, `backend/e_quant_execution.py`。
- 冲突/限制：
  - observe-version.json 为 OBSERVE_ONLY / E-V1.1-QUANT；auto-paper.json 的 long_enabled 与 short_enabled 均为 true，strategy_control 显示 RUNNING。执行模式存在冲突，不能据此认定自动开仓。
- 未核实字段：当前进程内版本、当前启停与入场许可、最近持久证据后的周期处理。
- V3 处理：本轮只归档，不注册、不启用、不执行该策略。
