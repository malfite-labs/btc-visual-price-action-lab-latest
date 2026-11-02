# BTC Visual Price Action Lab V1.0 — API Contract

- 版本：API V1.0.1（修订）；对应 ENG-SPEC V1.0.1 和 PRODUCT_REQUIREMENTS_V1_FROZEN.md。
- 服务对象：本机工作台。所有交易能力均为内部虚拟模拟；不定义交易所下单、签名、账户或入金 API。
- 规范约定：路径前缀 /api/v1；时间 RFC3339 UTC；金额/价格/数量序列化为十进制字符串；所有响应含 schema_version、request_id；错误含 code、message、retryable、correlation_id。

## 1. 读 API

| 方法及路径 | 用途 | 必需输入/输出 |
|---|---|---|
| GET /health | 进程健康 | 服务版本、启动时间、readiness、错误状态 |
| GET /system/status | 系统状态 | market/model/ledger/protection/evolution 状态、最后 event sequence、各 feed 时间 |
| GET /market/candles | 图表 K 线 | symbol、interval、from、to、closed_only；返回 candle、完整性状态、缺口 |
| GET /market/quote | 最新报价 | bid/ask 及 qty、mark_price、exchange_event_at、received_at、quote_id、age_ms、health |
| GET /market/funding-events | 资金费事件 | from/to、symbol；rate、effective_at、observed_at、source、历史/实时标志 |
| GET /agents | A/B/C 元信息 | 角色、运行状态、现役版本、决策进度 |
| GET /agents/{id}/account | 当前账户 | 现金、权益、可用保证金、名义敞口、仓位、止损、账本序列、PnL |
| GET /agents/{id}/account/equity-curve | 权益曲线 | UTC 范围、游标；由已提交账本快照生成，包含浮动权益标记方式 |
| GET /agents/{id}/decisions | 决策列表 | UTC 范围、游标；动作、状态、版本、图像 hash、拒绝/过期原因 |
| GET /decisions/{id} | 决策证据 | 模型标识、任务时序、账户版本、行情引用、响应证据 hash、结构化结果 |
| GET /trades | 完整往返交易 | agent、时间、游标；开/平 fill、成本、资金费、net_pnl、R、闭合状态 |
| GET /trades/{id} | 交易详情 | 决策、图像、版本、风险预算、开平成交、成本、止损过程、账本引用 |
| GET /fills | 图表成交标记 | agent、范围、游标；仅已原子提交成交，带 fill_id、trade_id、实际模拟时间价量与费用 |
| GET /agents/{id}/journal | 交易日记 | 时间/游标；决策含观望、理由、结构记忆版本和结果 |
| GET /strategies | MD 策略状态 | 三代理母版、现役/候选版本、权限摘要、哈希、状态 |
| GET /strategies/{agent}/versions | MD 版本清单 | 不可变版本、父版本、创建/激活时间、章节哈希、审查状态 |
| GET /strategies/{agent}/versions/{version}/content | MD 正文 | 访问控制通过后返回只读正文及 hash；不支持覆盖写 |
| GET /strategies/{agent}/diff?from=&to= | MD 差异 | 章节级 diff、核心锁区是否变更；任一版本缺失返回 NOT_FOUND |
| POST /strategies/C/drafts | 导入 C 草稿 | multipart/JSON content、source_note、expected_parent；只创建待审草稿，不更改现役版本 |
| GET /evolutions | 进化进度 | agent；已闭合笔数、当前50笔批次、排队批次、候选状态、截止日期、检查点 |
| GET /evolutions/{id}/comparison | 对照比较 | 同期权益曲线引用、双方闭合样本数、同期权益净收益额/率、平均盈亏比、最大回撤、每检查点判定和拒绝原因 |
| GET /audit/events | 审计流 | UTC 范围、type、after_sequence；严格按 sequence_no 续传 |
| GET /events/stream | 增量事件 | after_sequence、事件类别；重连使用游标续传，重复消息由 event_id/sequence 去重 |

分页游标包含 last_sequence/last_id 且不可由客户端构造；非法范围、游标返回 RANGE_INVALID/CURSOR_INVALID。空数据返回空集合并携带 completeness，不得伪造成正常实时状态。

## 2. 内部命令契约（不可由普通网页直接调用）

- create_decision_task(snapshot_id, agent_id, account_state_version, strategy_version_id) → task_id
- accept_model_result(task_id, response, model_evidence) → validated decision 或拒绝原因
- validate_decision(decision_id, quote_id, account_state_version) → ACCEPT/REJECT/EXPIRED
- calculate_order_intent(decision_id, quote_id) → 风险/费用/数量分解及 READY/REJECTED
- execute_paper_intent(intent_id, quote_id, account_state_version) → 成交或拒绝；同一 intent 幂等
- on_mark_event(mark_event_id) → STOP_TRIGGERED/STOP_PENDING/NO_TRIGGER
- resolve_pending_stop(position_id, fresh_quote_id) → 单一退出成交或继续 PENDING
- enqueue_review_batch(agent_id, closed_trade_count) → batch_id（同批幂等）
- evaluate_shadow_checkpoint(experiment_id, checkpoint_day) → PASS/FAIL/INDETERMINATE

写命令仅由内部协调器以服务身份执行，持久层必须再次校验权限、唯一约束和状态版本。网页 API 不得接受数量、价格或 stop 以绕过服务端风险计算；网页只能导入 C 策略草稿。

## 3. 关键对象 schema

- MarketCandle：symbol, interval=15m, open_at_utc, close_at_utc, OHLC, is_closed, source, source_hash, sequence_no。
- MarketQuote：quote_id, symbol, bid, ask, bid_qty, ask_qty, mark_price, exchange_event_at_utc, received_at_utc, feed_health, age_ms, source。
- FundingRateEvent：event_id, symbol, rate, effective_at_utc, observed_at_utc, source, source_kind, sequence_no。
- VisualSnapshot：snapshot_id, bar_close_utc, full_range=288 bars, zoom_range=96 bars, image_sha256, candle_hashes, renderer_version。
- Decision：decision_id, agent_id, bar_close_utc, deadline_utc, snapshot_id, strategy_version_id, account_state_version, action, stop_loss, rationale, model_evidence_id, validation_status。
- OrderIntent：intent_id, decision_id, quote_id, account_state_version, raw_qty, rounded_qty, size_caps, planned_risk, risk_components, status, reject_reason。
- Fill：fill_id, intent_id, agent_id, side, qty, reference_quote_id, reference_bid_ask, execution_price, slippage, fee, filled_at_utc, idempotency_key。
- Position：position_id, agent_id, direction, qty, entry_price, stop_price, stop_state, opened_at, closed_at, entry_version, state_version。
- AccountLedgerEvent：ledger_event_id, agent_id, event_type, signed_amount, cash_after, equity_snapshot, related_object_id, sequence_no, idempotency_key。
- RoundTripTrade：trade_id, entry_fill_id, exit_fill_ids, gross_pnl, fees_total, funding_total, net_pnl, r_multiple, is_closed, closed_at。
- EvolutionExperiment：experiment_id, agent_id, batch_id, incumbent_version, candidate_version, start_sequence, starts_at, ends_at, checkpoint, independent_control_account_id, candidate_account_id, status, history。
- AuditEvent：event_id, sequence_no, event_type, occurred_at, observed_at, correlation_id, actor, payload_hash, outcome。

## 4. 错误码与状态语义

SERVICE_UNAVAILABLE、STATE_INCOMPLETE、DATA_NOT_READY、RANGE_INVALID、GAP_FOUND、QUOTE_STALE、MARK_STALE、DECISION_EXPIRED、VERSION_CONFLICT、INVALID_MD、CORE_LOCK_VIOLATION、RISK_LIMIT、INSUFFICIENT_LIQUIDITY、FILTER_REJECTED、ACCOUNT_VERSION_CONFLICT、IDEMPOTENT_DUPLICATE、LEDGER_LOCKED、PROTECTION_UNCERTAIN、INSUFFICIENT_SAMPLE、INDETERMINATE_METRIC、NOT_FOUND。

错误响应不隐去原因。ACCOUNT_VERSION_CONFLICT 和 IDEMPOTENT_DUPLICATE 不自动生成新成交；需要上层读取已提交状态。PROTECTION_UNCERTAIN 表示保护成交未确认，绝不可映射成已平仓。

## 5. 尚未给定的配置

接口数值 CFG-01…CFG-08 见冻结基线；API schema 已定但数值不虚构。未冻结行情年龄、手续费、滑点、tick/step、模型预算/超时、资金费源时，运行门槛保持 disabled/reject。C 正文和核心锁边界 CFG-09 未提供前不做 C 行为验收。

