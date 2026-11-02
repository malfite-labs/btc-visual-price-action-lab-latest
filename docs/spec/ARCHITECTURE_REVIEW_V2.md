# BTC Visual Price Action Lab V1.0 — 架构审查 V2

- 审查版本：V2 / 规格修订后静态复核
- 输入：上一轮审查报告；本对话明确确认的冻结规则；上一轮已成功读取的完整原工程任务包及只读交接说明。
- 附件读取说明：本轮消息没有提供可读取附件引用，且 Codex 当前会话 artifacts 列表为空；因此没有声称本轮重新读取附件。依据上一轮已成功读取并保留全文的两份文档继续：BTC_VISUAL_PRICE_ACTION_LAB_V1_ENGINEERING_PACK.md、CODEX_HANDOFF_READ_ONLY.md。其当时内容读取完整。未因工程目录是否存在推断需求缺失。
- 文档关系：原任务包 V1.0 作为源材料保持不变；本次 PRD V1.0.1、ENG-SPEC V1.0.1 和 API/状态/验收文件是派生修订件。

## 1. 结果

SPEC_REVISION = PASS

P0_RESOLVED = 3/3

P1_RESOLVED = 9/9

P2_REMAINING = 3

REQUIREMENTS_BASELINE = COMPLETE

RISK_FORMULA = FROZEN

STOP_RECOVERY_STATE_MACHINE = COMPLETE

API_CONTRACT = COMPLETE

ACCEPTANCE_MATRIX = COMPLETE

READY_FOR_P0_IMPLEMENTATION = YES

此处 YES 仅表示规格与文件层面已具备启动 ENG-P0 的条件；本次任务未执行 P0 或其他工程阶段，且仍须遵守用户授权门。未授权前不得开发、安装依赖、连接行情/账户、调用模型或运行模拟交易。

## 2. P0/P1 逐项复核

| 原问题 | 结果 | 证据文件 |
|---|---|---|
| P0-1 独立冻结需求与追溯 | 已解决：PR 编号与源版本关系明确；逐项映射模块、API、验收；未提供值列为 TBC | PRODUCT_REQUIREMENTS_V1_FROZEN.md；REQUIREMENTS_TRACEABILITY_MATRIX.csv |
| P0-2 仓位风险公式歧义 | 已解决：1x 是敞口上限；2% 当前权益预算；费用/滑点/保证金/L1/过滤器求最小量，Decimal 向下取整并重验 | STATE_MACHINE_AND_RISK_SPEC_V1.md |
| P0-3 止损报价故障恢复 | 已解决：触发先持久化，缺报价 pending，不造 fill；恢复后对账并执行唯一退出；并发和重启去重原子化 | STATE_MACHINE_AND_RISK_SPEC_V1.md；API_CONTRACT_V1.md |
| P1-1 幂等/原子/状态版本 | 已解决：唯一幂等键、sequence、事务、CAS、冲突响应及重放定义 | ENG-SPEC §2；STATE-MACHINE §1–2 |
| P1-2 行情/费用/滑点/精度/模型超时参数 | 已解决规格和门禁：CFG-01…CFG-08 未知数值标记 TBC，P0 核验前对应功能禁用/拒单；未捏造参数 | PRD §3；API §5；STATE §5 |
| P1-3 市价模拟与 L1 容量 | 已解决：只用可见对手 L1；无深度不假设；先缩量并复核，不满足则整单拒绝；费用/滑点独立证据 | STATE §3–4；ACCEPTANCE RSK-02 |
| P1-4 API 缺失 | 已解决：权益曲线、资金费事件、MD 内容/差异、完整图表成交证据等路径和 schema 补全 | API §1–4 |
| P1-5 Funding/event contract | 已解决：FundingRateEvent 字段、事件/观察时间、来源和账本唯一计费合同 | API §3；ACCEPTANCE MKT-01/LED-02 |
| P1-6 50笔批次/排队 | 已解决：自上批次后每累计50笔闭合交易生成 sequence 固定批次；候选占用时排队、幂等恢复 | ENG-SPEC §2；API evolution; ACCEPTANCE EVO-01/02 |
| P1-7 影子指标/特殊情况 | 已解决：固定检查点、闭合交易口径、净收益、平均盈亏比、权益曲线最大回撤及不可判定不升级 | PRD §1；API comparison; ACCEPTANCE EVO-04…07 |
| P1-8 MD 结构/版本/C保护 | 已解决边界：只增不改、核心 hash 锁定、C 无原稿时仅测导入隔离、不做行为验收 | PRD §1/§3；API strategies; ACCEPTANCE STR-01 |
| P1-9 模型证据/重复验收 | 已解决：规范请求、响应 hash、图像/MD/model/schema hash 和计量字段；固定逻辑 fixture 可重复，真实服务只验契约不要求输出文本恒定 | ENG-SPEC §3；ACCEPTANCE §1 |

## 3. 原 P2 分配及升级

- 图表拥挤、聚合和窄屏视觉回归：P4；保留为 1 个 P2。
- 手工与自动测试边界：P0 建立类型标记，P6 以自动断言为阻断、人工检查为补充；保留为 1 个 P2。
- 阶段 P0 与 UI-P0 命名重叠及 M04 归属：P0 修订阶段表和命名；保留为 1 个 P2，作为交付阶段的编辑检查项。
- 事件流缺少全序和游标：原 P2-4 会影响重启/账本/止损正确性，升级为 P1，已由 sequence_no、唯一约束和重放规则解决；不计入剩余 P2。

## 4. 尚未解决事项及原因

以下事项不阻止静态规格修订完成，但会阻止各自工程阶段的运行验收，必须在 P0 或指定阶段前补入有版本配置，不可自行猜数：

1. CFG-01 报价最大年龄、时钟偏差及健康窗口：需基于供应商实际推送和测量确定。
2. CFG-02 手续费档位及有效来源：源材料未给用户具体档位，必须核验并留存来源。
3. CFG-03 滑点模型具体参数与价格保护边界：当前冻结计算结构，未给参数来源。
4. CFG-04 tick/step/minQty/minNotional 等过滤器：需按 symbol 官方公开元数据核验。
5. CFG-05 模型预算、timeout、最大 retry 和安全余量：源材料只规定不得跨周期，未给数值；依截止公式执行，数值需 P0 冻结。
6. CFG-06 影子候选故障次数上限：不得延长 30 天的规则已冻结，次数未给依据。
7. CFG-07 Funding 数据源及真实字段时序：接口合同已定，外部可用性须在 P0 核验。
8. CFG-08 可用保证金及费用预留口径：风险公式已定，具体账户字段解释需冻结。
9. CFG-09 C 原始 MD、核心章节边界：用户原稿尚未在本轮可读输入中出现；不得构造。

因此 READY_FOR_P0_IMPLEMENTATION = YES；READY_FOR_P1/P2/P3 live behavior = NO，直到相关 CFG 门槛关闭并取得所需授权。

## 5. 复核限制

本次只做静态文档一致性检查。没有运行任何代码、测试、交易模拟、市场接口或模型调用；因此没有宣称运行时实现或真实市场行为已验证。所有验收编号是未来工程阶段的可重复测试合同。
