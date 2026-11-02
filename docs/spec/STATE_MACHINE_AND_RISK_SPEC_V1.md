# BTC Visual Price Action Lab V1.0 — 状态机、幂等与风险规范

- 版本：STATE-RISK V1.0.1；对应 PRD V1.0.1、ENG-SPEC V1.0.1。
- 所有状态转换均记录 append-only event；成功写入由唯一键、账户状态版本和单事务共同保证。

## 1. 决策和订单状态

Decision：BAR_WAIT → BAR_CONFIRMED → SNAPSHOT_READY → AI_RUNNING → DECISION_RECEIVED → VALIDATING → WAIT / REJECTED / EXPIRED / ORDER_INTENT。

OrderIntent：CREATED → RISK_VALIDATED → READY → FILLED；可转为 REJECTED、EXPIRED、FAILED。内部模拟成交为原子单次成交；无部分成交状态。若报价可见数量不足，先将数量缩至可执行上限并重新通过风险、最小数量和 step 检查；达不到最小有效量则整笔拒绝，不伪造部分 fill。

Position：FLAT → OPEN_LONG 或 OPEN_SHORT → STOP_PENDING / CLOSE_PENDING → FLAT。禁止同一账户多仓、对冲双持或加仓。反向信号先以唯一 close intent 平仓，平仓成功后下一次有效决策才可反向开仓。

状态转换的 compare-and-set 条件：期望 account_state_version 等于持久层当前版本；每次状态变更 version + 1。版本不符则事务回滚并返回冲突，决策须基于新状态重新判断，不复用旧 intent。

## 2. 标记价止损与故障恢复状态机

正常路径：PROTECTION_ARMED → MARK_TRIGGERED → STOP_EXIT_PENDING（将唯一 stop_trigger_event_id、position_id、trigger_mark、mark event sequence、stop price 原子持久化）→ FRESH_QUOTE_VALIDATED → EXIT_INTENT_COMMITTED → EXIT_FILLED → POSITION_FLAT。

行情故障路径：

1. 每个标记事件以 exchange event time + monotonic sequence 进入持久事件流。多仓触发条件 mark_price <= stop_price；空仓触发条件 mark_price >= stop_price；等于止损价视为触发。触发由标记价决定，不由盘口价决定。
2. 触发事件先持久化 STOP_EXIT_PENDING。报价缺失、过期、交叉盘口、无效价格或报价健康不是 LIVE 时，不生成 fill/fee/close ledger；系统进入 PROTECTION_UNCERTAIN + MARKET_STALE，禁止新开仓。
3. 报价恢复后先检查 pending trigger、当前仓位版本、账本序列和最新 quote。若唯一仓位仍在且 quote 新鲜有效，则生成唯一 stop-exit intent，按持仓方向取对手价（多仓卖出 bid，空仓买入 ask），应用配置滑点，原子关闭仓位并记费用。触发时间与实际模拟成交时间分别保存，不把成交回填到 mark 时间。
4. 若恢复扫描发现历史标记事件已越过止损，使用已持久事件触发；如果断线期没有可审计的 mark 事件，不能臆测触发与成交，继续保护不确定并锁定该账户的新开仓，审计缺失窗口。
5. 如果同时收到 AI 平仓和止损，持仓/版本 compare-and-set 只有一个 close intent 可成功。止损优先；先提交者消费 position_state_version，另一个得到 ALREADY_CLOSED/IDEMPOTENT_DUPLICATE，不再成交、不重复计费。
6. 重启后依据事件流先恢复 STOP_EXIT_PENDING 和 position/account version，再消费未完成触发；stop intent id 为 stop:{position_id}:{trigger_event_id}。成交、费用、position close、trade close 在一个事务内写入。重放重复 trigger 或 fill 返回原结果。

异常转移：账本和仓位不一致 → LEDGER_LOCKED；序列缺口无法证明 → RECOVERING；不得显示 LIVE 或“已保护”。人工不能在 V1 UI 绕过锁或生成成交。

## 3. 统一仓位与计划风险公式

输入必须属于同一决策快照：决策时账户权益 E、可用保证金 M、入场对手报价 Q、有效买卖量 L、仓位方向 d、AI 止损价 S、已冻结费用/滑点/数量过滤器版本。金额/价格/数量用 Decimal。

定义：

- 风险预算：R = 0.02 × E。
- 最大 1x 名义量：q_notional = E / P_entry_ref；其中 P_entry_ref 为模型风险核算使用的最坏入场价格（包含不利滑点），该数值不允许预测成比当前报价更有利。
- 保证金量：q_margin = M / IM_per_unit；1x 下 IM_per_unit 按 1 个标的单位所需保证金计算并计入已冻结余额。CFG-08 冻结前拒绝开仓。
- L1 流动性量：多头开仓 q_liquidity = ask_qty；空头开仓 q_liquidity = bid_qty。V1 不假设未读取的深度可用，不按盘口外数量填成交。
- 价格与费用成本/单位 C_unit：不利入场滑点 + 入场手续费 + 从 P_entry_ref 到止损触发后的可执行对手价的损失 + 止损退出滑点 + 退出手续费。多仓以入场买价减退出卖价，空仓以退出买价减入场卖价；止损价必须在入场价的亏损侧。费用率、滑点和盘口报价老化值取同一已版本化配置，不得默认零。资金费独立按已发生事件真实记账；未来资金费只有能按固定规则估算时才纳入计划预算，未能估算不得声称预算含其成本。
- 风险量：q_risk = R / C_unit。若 C_unit <= 0、非有限、止损不在亏损侧、费率/滑点配置缺失，拒绝。
- 合规原始量：q_raw = min(q_risk, q_notional, q_margin, q_liquidity, q_symbol_max)。q_symbol_max 来自有效交易过滤器。
- 精度：q = floor(q_raw / stepSize) × stepSize，采用十进制定点向下舍入。

必须以舍入后的 q 和同一报价/配置重新计算名义值、初始保证金、预计成本及计划损失。仅当同时满足：q >= minQty、名义金额 >= minNotional、q <= 所有上限、名义值 <= E、计划损失 <= R、所需保证金 <= M、报价新鲜且 L1 可成交，才准许。任一不满足即拒单；不改 AI 止损价、不向上取整、不分拆补造流动性。

市场报价自模型完成后重新取样；若 quote_id 已过 CFG-01 年龄、bid > ask、数量为零或数据健康非 LIVE，拒绝新开仓。实际模拟 fill 使用同一已验真的 quote_id 及其对手方价加不利滑点，记录参考报价、填价、滑点、费用分解。若实际执行配置下重算风险超限，则拒绝整笔，不做超预算 fill。

## 4. 成交与现金账本

- 开多：ask + 不利滑点；开空：bid - 不利滑点。平多：bid - 不利滑点；平空：ask + 不利滑点。
- Market fill 使用 taker fee schedule。Fill 价是模拟执行价格，不是 K 线 close 或 mark price。
- 已实现毛 PnL 按方向、数量及实际模拟 fill 价计算；净 PnL = 毛 PnL − 开/平手续费 − 已发生资金费。资金费每个 event_id 对每个持仓只能计入一次，按事件适用时刻和当时仓位量计算。
- 权益 = 现金 + 按定义时点标记的未实现 PnL；权益曲线 API 返回现金、浮盈浮亏、估值报价 id 和更新时间，避免把估值和已实现收益混淆。
- 手续费、资金费与 fill/close 状态在同事务追加；小数精度和舍入规则在 schema/config 中显式版本化。

## 5. 参数冻结及未知值的处理

CFG-01 报价年龄/时钟偏差，CFG-02 taker fee schedule，CFG-03 滑点模型，CFG-04 tick/step/minQty/minNotional，CFG-08 可用保证金口径必须在 P0 核验并作为运行配置哈希保存。CFG-05 模型 timeout/retry 采用 ENG-SPEC 的截止公式，数值 P0 冻结。数值未确认不影响公式规格冻结，但会阻断相应运行功能。

## 6. 影子比较绩效公式（冻结）

统一使用影子开始时刻的共同起始权益 E0。每个检查点的净收益金额为 checkpoint_equity - E0；净收益率为 (checkpoint_equity - E0) / E0。checkpoint_equity = 现金 + 同一 mark event sequence 上按 mark price 计算的未实现 PnL；成交费用和已发生资金费已进入现金/仓位账本。候选与对照必须按相同事件时刻、报价、账户估值规则计算。不得以账户历史累计收益代替同期结果。

完整往返交易的 net_pnl = gross_realized_pnl - entry_fees - exit_fees - funding_paid。平均盈亏比定义为：所有 net_pnl > 0 的闭合交易净收益算术平均值，除以所有 net_pnl < 0 的闭合交易净亏损绝对值算术平均值。未闭合交易不计入交易样本或盈亏比，但其浮动 PnL 计入 checkpoint equity 和回撤曲线。没有盈利样本、没有亏损样本、亏损平均值为零、数据缺失或非有限数时，盈亏比不可判定，检查点 INDETERMINATE，禁止自动升级。

最大回撤基于实验开始至检查点的完整权益路径，按每个共同 mark event sequence 更新：peak_t = max(E0, equity_1…equity_t)，DD_t = (peak_t - equity_t) / peak_t；MDD = max(DD_t)。缺少任一共同估值窗口的有效 mark 时，该检查点不可判定，不插值、不使用未来数据。

第 d 天（仅 d ∈ {7,14,21,30}）通过必须同时满足双方完整闭合交易数 >= 20、candidate_net_gain > control_net_gain、candidate_avg_win_loss > control_avg_win_loss、candidate_MDD <= control_MDD × 1.10 且 candidate_MDD <= control_MDD + 0.02。0.02 表示绝对回撤比例差 2 个百分点。control_MDD=0 时两个限制仍同时生效，等价要求 candidate_MDD=0。任一条件失败则该节点不晋级；在30天以前后续固定节点仍可重新评估；第30天未通过或不可判定即淘汰。首次全部通过转为 PROMOTION_PENDING，活动仓位不迁移，等待现役自然平仓后按 PR-012 切换。
