# G C 成交确认反向镜像

活动市场 BTCUSDT（Binance Demo USDⓈ-M），计价 USDT。研究初始权益以 8011 实测字段为准（10000）。

## 1. 策略定位

以 C 真实成交为输入的反向镜像。

## 2. 适用行情

不独立从 K 线产生信号；C 原策略为 15 分钟。

## 3. 指标与参数

C 成交归属、已确认数量、终态、信号时点和实时盘口。紧急止损保守损失预算为研究权益 2%。

## 4. 做多规则

C 已确认有效空仓成交时，在满足快照与时效门禁后尝试 G 多。

## 5. 做空规则

C 已确认有效多仓成交时，在满足快照与时效门禁后尝试 G 空。

## 6. 信号确认

以有效报价回调检查 C 已终态且有实际成交的开仓记录；已确认数量与当前 C 方向仓位一致，且信号符合报价时效要求。

## 7. 止损规则

按入场价、数量、费用和滑点计算满足 2% 最大损失预算的最远有效价格步进，持仓用各自方向 bid/ask。

## 8. 止盈规则

无独立固定目标；跟随 C 已确认减仓及退出。

## 9. 退出与超时

C 原入场 ID 消失或对应仓位归零时退出。C 部分减仓时按归属协同减量，尊重交易所最小数量与金额；无独立固定 K 线超时。

## 10. 特殊逻辑

以 C 入场 ID 构成镜像意图去重，G 成交和费用按自身归属独立记录，已有仓位先行保护。

## 11. 资金与仓位

V2 按 C 已确认数量尝试反向开仓，再执行 G 自身资金、费用、精度、杠杆上限和风控检查。

## 12. 原始来源与版本

归档号 G，运行 ID G。8011 版本 1.0.0，启用 True，当前入场许可 True。

策略库或内置版本证据


```json
{
  "kind": "built_in_code",
  "version": "1.0.0",
  "independent_json": null
}
```

规则校验


```json
{
  "method": "sha256_of_workspace_sources",
  "verified_local_copy": true
}
```

开仓入口 DemoPAFleet._dispatch → DemoGMirror.on_quote → DemoHedgeCoordinator.execute。

退出入口 DemoGMirror.on_quote → mirror_emergency_stop / DemoHedgeCoordinator.finish_position。

Python 策略以代码快照复核。

原始路径和逐文件 SHA-256 见本目录 strategy.json。

8011 状态接口提供策略 ID、版本和启用状态，但未发布整个进程的部署源码摘要。共享执行入口来自工作区源码静态追踪。

## 原始 Python 规则摘录

以下函数来自快照，保留原有计算与控制流。完整依赖见策略 JSON 中的源文件清单。

### backend/binance_demo_g.py


```python
    def _entry(self, owner, side):
        with sqlite3.connect(self.coordinator.journal.path) as db:
            return db.execute("""
                SELECT i.client_id, p.payload, t.client_id IS NOT NULL
                FROM demo_order_intent i
                JOIN demo_entry_protection p USING(client_id)
                LEFT JOIN demo_terminal term USING(client_id)
                LEFT JOIN demo_trade t USING(client_id)
                WHERE (? = 0 OR term.client_id IS NOT NULL)
                  AND json_extract(i.intent_json, '$.strategy_id')=?
                  AND json_extract(i.intent_json, '$.position_side')=?
                  AND t.client_id IS NOT NULL
                ORDER BY i.rowid DESC LIMIT 1
            """, (int(owner == "C"), owner, side)).fetchone()

    def on_quote(self, quote, now, *, enabled, entry_ready):
        if (quote.get("source"), quote.get("symbol"), quote.get("currency"), quote.get("timestamp_valid")) != (
                SOURCE, SYMBOL, "USDT", True):
            return None
        try:
            event = datetime.fromisoformat(quote["event_at"])
            received = datetime.fromisoformat(quote["received_at"])
            bid, ask = Decimal(str(quote["bid"])), Decimal(str(quote["ask"]))
            if not event <= received <= now or not timedelta(0) <= now-event <= self.max_age:
                return None
            if not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask:
                return None
        except (ArithmeticError, KeyError, ValueError, TypeError):
            return None

        positions, _ = self.coordinator._accounting()
        for side, source in (("LONG", "SHORT"), ("SHORT", "LONG")):
            owned = positions.get(("G", side), Decimal(0))
            c_qty = positions.get(("C", source), Decimal(0))
            if owned:
                row = self._entry("G", side)
                if not row:
                    raise OrderUnconfirmed("Confirmed G position has no terminal protection record")
                cid, payload, _ = row
                policy = json.loads(payload)
                exit_order = DemoIntent("G", policy["version"], "g-exit:" + cid,
                                        "SELL" if side == "LONG" else "BUY", side, owned)
                with sqlite3.connect(self.coordinator.journal.path) as db:
                    pending = db.execute("""
                        SELECT client_id FROM demo_pending_exit
                        WHERE json_extract(intent_json, '$.strategy_id')='G'
                          AND json_extract(intent_json, '$.position_side')=?
                    """, (side,)).fetchone()
                if pending:
                    return {"status": "EXIT_PENDING_RECONCILIATION", "client_id": pending[0]}
                c_row = self._entry("C", source)
                if not c_qty or not c_row or c_row[0] != policy["c_entry_id"]:
                    return self.coordinator.finish_position(exit_order)
                confirmed = self.coordinator.journal.entry_protection(cid)
                stop = Decimal(policy["stop"])
                if confirmed["entry"] is not None:
                    entry_price = Decimal(confirmed["entry"])
                    fee = owned * entry_price * self.cost.fee_rate
                    try:
                        stop = mirror_emergency_stop(self.cost, side, entry_price, owned,
                                                     Decimal(policy.get("entry_equity", self.equity)), fee)
                    except ValueError:
                        return self.coordinator.finish_position(exit_order)
                emergency = self.coordinator.exit_on_quote(
                    exit_order, quote, stop=stop, target=None,
                    now=now, max_age=self.max_age)
                if emergency is not None:
                    return emergency
                desired = c_qty
                if "c_initial_quantity" in policy:
                    basis = Decimal(policy["c_initial_quantity"])
                    opened = Decimal(confirmed["confirmed_quantity"])
                    desired = ((opened * c_qty / basis) // self.step) * self.step
                if owned > desired:
                    quantity = owned - desired
                    reference = bid if side == "LONG" else ask
                    if quantity < self.minimum_quantity or quantity % self.step or quantity * reference < self.minimum_notional:
                        return {"status": "MIRROR_REDUCTION_BELOW_MARKET_MINIMUM", "remaining": str(quantity)}
                    partial = DemoIntent("G", policy["version"],
                                         f"g-reduce:{cid}:{format(c_qty.normalize(), 'f')}",
                                         exit_order.side, side, quantity)
                    return self.coordinator.execute(partial)
                return None
            if not c_qty or not enabled or not entry_ready:
                continue
            source_row = self._entry("C", source)
            if not source_row:
                continue
            c_id, c_payload, _ = source_row
            c_policy = json.loads(c_payload)
            c_protection = self.coordinator.journal.entry_protection(c_id)
            if Decimal(c_protection["confirmed_quantity"]) != c_qty:
                continue
            if now - datetime.fromisoformat(c_policy["signal_close"]) > self.max_age:
                continue
            reference = ask if side == "LONG" else bid
            equity = self.equity
            qty = c_qty
            if self.capital_epoch:
                try:
                    equity = flat_strategy_equity(self.coordinator.journal.path, "G")
                    qty = full_margin_quantity(equity, reference, self.step, self.minimum_quantity,
                                               self.minimum_notional, self.cost.fee_rate)
                except ValueError as error:
                    return {"status": str(error)}
            intent = DemoIntent("G", self.version, "g-mirror:" + c_id,
                                "BUY" if side == "LONG" else "SELL", side, qty)
            with sqlite3.connect(self.coordinator.journal.path) as db:
                if db.execute("SELECT 1 FROM demo_order_intent WHERE client_id=?", (intent.client_id,)).fetchone():
                    continue
            fee = qty * reference * self.cost.fee_rate
            if (qty < self.minimum_quantity or qty % self.step or qty * reference < self.minimum_notional
                    or qty * reference > equity * 5 or qty * reference / 5 + fee > equity):
                return None
            try:
                stop = mirror_emergency_stop(self.cost, side, reference, qty, equity, fee)
            except ValueError:
                return None
            options = {}
            policy = {"stop": str(stop), "target_r": "1", "direction": side,
                      "version": self.version, "c_entry_id": c_id}
            if self.capital_epoch:
                options["required_margin"] = qty * reference / FIXED_LEVERAGE + fee
                policy.update(c_initial_quantity=str(c_qty), entry_equity=str(equity))
            return self.coordinator.execute(
                intent, require_flat=True, entry_notional=qty * reference + fee,
                protection=policy, **options)
        return None
```

### backend/g_mirror.py


```python
def mirror_emergency_stop(cost, direction, price, qty, equity, entry_fee):
    # Find the furthest valid tick whose conservative loss reserve stays within 2%.
    step = cost.simulated_tick
    max_distance = price-step if direction == "LONG" else equity*D("0.02")/qty
    max_ticks = int(max_distance/step)
    lo, hi, best = 1, max_ticks, None
    while lo <= hi:
        mid = (lo+hi)//2
        raw = price-step*mid if direction == "LONG" else price+step*mid
        stop = floor_step(raw, step) if direction == "LONG" else ceil_step(raw, step)
        risk = planned_loss_for_fill(qty, price, stop, cost, entry_fee)
        if risk <= equity*D("0.02"):
            best = stop
            lo = mid+1
        else:
            hi = mid-1
    if best is None:
        raise ValueError("RISK_BUDGET_TOO_SMALL_FOR_EMERGENCY_STOP")
    return best
```


## 2026-11-20 首轮只读状态核验

- 归档状态：`ACTIVE`（指持久规则/配置状态；不代表当前正在运行）。
- 来源完整性：`PARTIAL`；规则源 SHA-256：`UNVERIFIED`；源哈希核验：`UNVERIFIED`。
- 当前运行状态：`UNVERIFIED`。本次检查未发现 8011 监听，无法读取进程内实际活动版本。
- 规则入口（只用于审计）：`backend/g_mirror.py`, `config/research/strategy-source-map-v1.json`。
- 冲突/限制：
  - 策略源映射为 C 成交事件镜像；当前 g 镜像账本状态未读到，本次无 8011 运行态。Demo 配置中的启用状态不能证明本地纸盘当前运行。
- 未核实字段：当前进程内版本、当前启停与入场许可、最近持久证据后的周期处理。
- V3 处理：本轮只归档，不注册、不启用、不执行该策略。
