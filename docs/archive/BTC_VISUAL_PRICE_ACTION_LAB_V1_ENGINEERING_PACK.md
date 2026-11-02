# BTC Visual Price Action Lab V1.0 — 完整工程任务包（设计冻结版）

> 版本：V1.0 / 2026-10-26  
> 状态：需求和技术行为规范已冻结；**未授权开发、部署、真实交易**。  
> 用途：供项目经理、架构师、Codex/Harness 后续逐阶段执行与验收。  
> 约束：不读取、不修改、不恢复此前暂停的量化项目；创建独立目录；无真实下单功能。  
> 规范语言：**必须**=强制验收；**建议**=可在工程准备阶段评估；**待核验**=不得自行宣称已经确定。

## 0. 执行摘要与禁止事项

系统为三位自主视觉交易员（A：趋势结构；B：关键位反转；C：用户自定义），共同观察同一时刻 BTCUSDT U 本位永续合约的**15分钟裸K线组合图**：最近72小时288根＋最近24小时96根（局部是同一批历史数据的放大视图），每15分钟收盘后各自以独立MD与记忆请求同一视觉模型版本做决策。程序只负责账户风险校验、市价模拟、实时标记价止损、成本计提和审计。前端在同一张K线图用A红、B绿、C橙标注**实际模拟成交**。三名交易员独立账户，总起始权益10,000 USDT，1倍杠杆，目标名义敞口为当前独立账户权益100%，计划单笔损失不得超过权益2%，必要时缩仓，每人最多一笔仓位，不加仓。

每个交易员独立累计50笔完整交易后，复盘并历史诊断，生成MD候选，在未来行情中开立对照和候选影子模拟账户（每人最多一名候选），第7/14/21/30天评价；双方各至少20笔完整交易且同时满足净收益更高、平均盈亏比更高、最大回撤不超过旧版1.1倍且不多于2个百分点，才可升级。若现役仍持仓，等待自然平仓后再切换。30天不通过则淘汰，队列不覆盖当前实验。

**禁止：**真实下单API、私钥、自动入金、跨策略共享记忆、未收盘K线作为视觉判断、未来函数、补造行情/成交、默默忽略费用、以图像像素作成交价格、将进化改写当作盈利改善的证据、把伪造测试数据称为真实市场成绩。

**第一阶段成功标准：**三名AI各100次真实视觉判断（不强制交易），稳定完成行情→图像→决策→模拟交易→账本→前端标注闭环；盈利不是验收前提。长周期影子进化可先用合成事件验证逻辑，不能以测试事件替代真实未来验证。

## 1. 范围及核心不变量

### 1.1 包含
1. 公开市场实时K线、买一卖一、标记价格、资金费率/事件及相关历史缺口核验。
2. 决策图像生成、模型适配、多套MD、长期记忆、决策解析与有效期控制。
3. 三份独立虚拟账户、模拟市价撮合、2%风险、1倍杠杆、持续止损监控、账本和审计。
4. 单页主交易工作台、策略管理、交易日记、进化与影子比较、异常提示。
5. 50笔复盘、历史诊断、候选MD、固定评价节点、自动晋级与版本追溯。
6. 模拟事件与真实模型端到端验证、异常和恢复演练。

### 1.2 排除
- 实盘交易/币安测试网下单、任何可签名交易订单的权限；本阶段只读取公开行情。
- 1小时/4小时作为AI视觉输入；任何MA/MACD/RSI/成交量等指标参与信号。
- 人为强制下单以凑满100次判断或50笔交易。
- 利润保证、机器学习权重训练、跨账户资金调度、多仓加仓、多个影子候选并行。

### 1.3 不变量
- `bar_close_utc`、`received_at_utc`、`decision_completed_at_utc`、`fill_at_utc` 分别记录，禁止混写。
- 每个 `(agent_id, candle_close_time, active_version_id)` 最多产生一个可执行决策；每条订单、成交、资金流水有幂等键。
- 三个AI同一时刻图像字节一致（SHA-256一致），策略文本、私有日记和持仓不同。
- 交易必须在模型返回且风控核对之后的市场数据上成交；不得倒填至信号K线收盘价。
- 止损执行链路不依赖视觉模型；行情/报价不可用时明确报告保护不可确认，不得制造已成交记录。
- 所有策略版本只增不改、核心MD母版保留；C不可进化核心交易理念；成交与审计只追加。

## 2. 技术分层、部署与依赖

### 2.1 推荐的逻辑组件
| 编号 | 组件 | 职责 | 依赖 |
|---|---|---|---|
| M01 | Market Gateway | 公开行情接入、时间校准、健康检查、断线恢复 | 币安公开接口 |
| M02 | Candle Store | 已收盘15M聚合、回补、审计和唯一性检查 | M01 |
| M03 | Chart Renderer | 288+96组合图及内容哈希 | M02 |
| M04 | Strategy Registry | MD母版、版本、权限、文件哈希 | 独立存储 |
| M05 | Agent Orchestrator | 15M任务、各AI独立上下文、超时去重 | M02-M04 |
| M06 | Vision Adapter | 模型能力握手、图像请求、解析与成本记录 | M05 |
| M07 | Decision Validator | 动作合法性、策略版本、仓位版本、止损结构校验 | M05-M06 |
| M08 | Risk & Execution | 额度/止损/报价校验、虚拟市价成交、费率 | M01,M07 |
| M09 | Ledger & Journal | 现金/权益/资金费/仓位/交易日记、原子写入 | M08 |
| M10 | Evolution Coordinator | 50笔触发、候选、影子比较、切换 | M04,M06,M09 |
| M11 | Read API & Stream | 只读查询和增量推送，MD导入经验证 | M02-M10 |
| M12 | Web Workbench | 交易主图、账户、日志、进化UI | M11 |
| M13 | Supervisor | 进程健康、恢复、错误隔离、备份、监控 | 全部 |

首选工程栈（建议，不是冻结要求）：Windows 本地；Python 3.12、FastAPI、SQLite（账本事务）、Parquet（行情归档）、React/TypeScript + 可视化K线组件；模型适配器支持更换服务商。无需Docker/Redis。具体第三方SDK应在实施时核验。

### 2.2 外部市场数据核验清单
币安公开文档存在15M K线、标记价、最优买卖报价、历史资金费数据接口；实际市场接入仍应先进行响应字段、频率、服务时段及可达性验证，不能从文档推断线上一定可用。官方参考：
- USD-M REST Market Data: https://developers.binance.info/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
- USD-M WS Market Streams: https://developers.binance.info/docs/derivatives/usds-margined-futures/websocket-market-streams/All-Market-Mini-Tickers-Stream
- USD-M WS Book Ticker: https://developers.binance.me/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-api/market-data

核验顺序：15M收盘事件标志→REST历史补齐→实时买一/卖一数量及时间→标记价触发→资金费结算时间→symbol过滤、精度及最小模拟订单规则→断线重连/限流处理。若公开行情不可达，保留适配器，不伪造正常运行。

## 3. 前端信息架构与页面规格

### 3.1 页面路由（建议）
| 页面 | 路由 | P级 | 主要展示 | 必须交互 |
|---|---|---|---|---|
| 主工作台 | `/` | P0 | BTC15M主图、三策略买卖点、实时账户卡、系统状态 | 选择A/B/C可见性、成交详情、时间跨度缩放、暂停**展示**不影响引擎 |
| 交易记录 | `/trades` | P0 | 开平时间价格、方向、数量、费率、资金费、净PnL、策略版本 | 筛选A/B/C、按交易ID打开详情 |
| 策略与MD | `/strategies` | P0 | 三份母版、现役MD版本、C导入状态、版本摘要 | 安全导入/预览MD、查看差异，不直接热改现役版本 |
| 交易日记 | `/journal` | P1 | 15分钟AI决策、观望、解释、当前结构记忆 | 按时间/AI/结果过滤，查看原始输入证据 |
| 进化中心 | `/evolution` | P1 | 50笔计数、任务排队、影子阶段、7/14/21/30节点、晋级记录 | 展开候选vs对照曲线/证据，查看失败原因 |
| 系统健康 | `/system` | P0 | 行情连接、标记价、买卖报价、模型延迟、保护状态、恢复记录 | 查看告警与恢复审计；V1不允许UI直接强制跳过风险门槛 |

### 3.2 主工作台布局
- 顶栏：BTCUSDT PERP / 15M / UTC，最近完整K线收盘时间、实时行情状态、`PAPER ONLY`、当前模型版本。
- 中心（主要空间）：用户可缩放/拖动的单一K线图，A红 `#E45454`，B绿 `#16A777`，C橙 `#ED992F`；颜色表示**策略**而非涨跌；标记标签 `A开多`、`A平多`、`A开空`、`A平空`等，繁忙时可聚合并展开，同价同时间自动错位。
- 标记仅来自 `fills/trades`，绑定 `fill_id`、成交时间/价及`decision_id`；未成交AI建议不能画成实际买卖点。
- 侧栏/下方：A/B/C 三卡（权益、当日/累计净PnL、当前方向/数量、止损、MD版本、最新决策及时间）。
- 底部：最近交易事件、异常告警、进化摘要。
- 点击交易标记：打开详情抽屉：行情快照/原始组合图、AI理由、计划止损、风险缩仓、实际成交、费用、版本哈希及后续退出信息。
- 响应式：桌面布局优先；窄屏把账户卡折叠到图下、保持图表可读；暗/亮两种主题对比度达标。
- **AI输入图与用户交易图解耦**：AI始终看到固定288+96的纯图；用户缩放、策略标注、主题切换不影响AI输入字节。

### 3.3 可视化状态规范
`LIVE`：行情时间戳和数据健康达标；`STALE`：报价超时，不允许新交易；`RECOVERING`：正在对账；`MODEL_DEGRADED`：禁止新开仓但保留保护机制；`PROTECTION_UNCERTAIN`：止损触发/执行链路无可靠行情；`LEDGER_LOCKED`：账本无法对账。各状态真实映射后端，只能在有事实基础时显示“已保护”。

### 3.4 体验与验收细节
- 图表默认只显示现役A/B/C标记；影子候选在进化详情页展示比较，不污染主图。
- 时间统一UTC存储、浏览器可以本地化显示但必须显示时区。
- 更新策略文件、系统告警、网络恢复等操作必须显示明确成功/失败反馈；不可显示虚假的“已经升级”。
- 权益图取真实模拟账本的时间序列，不从截图或前端估算。
- P0前端验收必须使用模拟成交账本的**已知固定样本**，逐条匹配UI标记位置、方向、颜色和成交金额。

## 4. 后端接口目录与权限约束

所有接口V1仅面向本地工作台。`GET`只读；MD导入端点需要内容与版本检验，不允许通过HTTP直接覆盖当前生效版本；无真实交易订单API。接口前缀建议 `/api/v1`，示意的路径可在详细OpenAPI冻结时统一命名。

| 方法 | 路径 | 输入 | 输出/用途 | 关键错误 |
|---|---|---|---|---|
| GET | `/health` | 无 | 进程健康、版本 | `SERVICE_UNAVAILABLE` |
| GET | `/system/status` | 无 | 行情/模型/账本/止损状态+最后事件时间 | `STATE_INCOMPLETE` |
| GET | `/market/candles?symbol=BTCUSDT&interval=15m&from=&to=` | UTC范围 | 已确认K线、完整性标识 | `RANGE_INVALID`, `GAP_FOUND` |
| GET | `/market/quote` | 无 | bid/ask/mark、行情时间戳、新鲜度 | `QUOTE_STALE` |
| GET | `/agents` | 无 | A/B/C角色、策略版本、状态 | `DATA_NOT_READY` |
| GET | `/agents/{id}/account` | 代理ID | 现金、权益、保证金、仓位、止损、PnL | `NOT_FOUND` |
| GET | `/agents/{id}/decisions?from=&to=` | 时间分页 | 决策、动作、有效/拒绝原因 | `RANGE_INVALID` |
| GET | `/decisions/{id}` | 决策ID | 完整证据引用、时序、模型输出 | `NOT_FOUND` |
| GET | `/trades?agent=&from=&to=&cursor=` | 筛选分页 | 完整交易及成本 | `CURSOR_INVALID` |
| GET | `/trades/{id}` | 交易ID | 开平仓、费用、信号与成交关联 | `NOT_FOUND` |
| GET | `/fills?agent=&from=&to=` | 筛选 | 图表标记专用成交列表 | `RANGE_INVALID` |
| GET | `/agents/{id}/journal` | 分页 | 当前及历史交易日记 | `NOT_FOUND` |
| GET | `/strategies` | 无 | A/B/C母版、现役/候选、哈希 | `NOT_READY` |
| GET | `/strategies/{id}/versions` | 策略ID | 版本列表与生效时间 | `NOT_FOUND` |
| POST | `/strategies/C/import` | MD文件、来源说明、预期版本 | 上传为**待审查母版草稿**，不热改现役 | `INVALID_MD`, `VERSION_CONFLICT` |
| GET | `/evolutions?agent=` | 策略ID | 50笔进度、任务队列、候选与状态 | `NOT_FOUND` |
| GET | `/evolutions/{id}/comparison` | 实验ID | 同期权益、交易数、净收益、盈亏比、回撤、评估节点 | `INSUFFICIENT_SAMPLE` |
| GET | `/audit/events?from=&to=&type=` | 时间分页 | 只读审计事件 | `RANGE_INVALID` |
| WS/SSE | `/events/stream` | 事件类别和游标 | K线完成、报价状态、AI决策、成交、账户、演化和告警 | 掉线重连+事件游标续传 |

**模型内部命令接口（不可暴露给普通网页）：** `create_decision_task(snapshot, agent_state, md_version)` → `decision_result`；`validate_decision(result, account_version, quote)` → `accept/reject`；`simulate_market_fill(order_intent, quote)` → `fill/failed`；`on_mark_price(mark_event)` → `stop_event`；`evolution.review_50(agent_id)` → `candidate_version`。全部必须绑定审计/幂等ID。

### 4.1 必需数据契约（字段级）
- `MarketCandle`：`symbol`,`interval`,`open_at_utc`,`close_at_utc`,`open`,`high`,`low`,`close`,`is_closed`,`source`,`hash`。
- `MarketQuote`：`bid`,`ask`,`bid_qty`,`ask_qty`,`mark_price`,`event_at_utc`,`received_at_utc`,`feed_health`,`source`。
- `VisualSnapshot`：`snapshot_id`,`bar_close_utc`,`full_range`,`zoom_range`,`image_sha256`,`source_candle_hashes`,`generated_at_utc`。
- `StrategyVersion`：`agent_id`,`version_id`,`parent_id`,`md_sha256`,`core_lock_hash`,`created_at`,`activated_at`,`status`。
- `Decision`：`decision_id`,`agent_id`,`bar_close_utc`,`deadline_utc`,`snapshot_id`,`strategy_version`,`account_state_version`,`action`,`stop_loss`,`rationale`,`model_id`,`requested_at`,`completed_at`,`validation_status`。
- `OrderIntent`：`intent_id`,`decision_id`,`action`,`size_target`,`size_approved`,`risk_budget`,`reject_reason`,`account_state_version`,`quote_id`。
- `Fill`：`fill_id`,`intent_id`,`agent_id`,`side`,`qty`,`price`,`spread_cost`,`slippage`,`fee`,`quote_id`,`filled_at_utc`,`idempotency_key`。
- `Position`：`position_id`,`agent_id`,`direction`,`qty`,`entry_price`,`stop_price`,`opened_at`,`closed_at`,`entry_strategy_version`,`status`。
- `AccountLedgerEvent`：`ledger_event_id`,`agent_id`,`event_type`,`amount`,`balance_after`,`equity_snapshot`,`related_fill_id`,`recorded_at`,`idempotency_key`。
- `RoundTripTrade`：`trade_id`,`entry_fill_id`,`exit_fill_ids`,`net_pnl`,`fees_total`,`funding_total`,`r_multiple`,`is_closed`,`closed_at`。
- `EvolutionExperiment`：`experiment_id`,`agent_id`,`incumbent_version`,`candidate_version`,`starts_at`,`ends_at`,`checkpoint`,`control_equity_start`,`candidate_equity_start`,`control_trade_count`,`candidate_trade_count`,`evaluation_result`,`status`,`history`。
- `AuditEvent`：`event_id`,`event_type`,`event_at`,`observed_at`,`correlation_id`,`actor`,`payload_hash`,`outcome`。

所有USDT金额、价格、数量采用十进制定点精度或同等无二进制浮点漂移的方案；每个对象带schema版本；时间全部UTC并分清事件时刻与接收时刻。

## 5. 状态机及事件顺序

### 5.1 决策生命周期
`BAR_WAIT → BAR_CONFIRMED → SNAPSHOT_READY → AI_RUNNING → DECISION_RECEIVED → VALIDATED → (WAIT | REJECTED | ORDER_INTENT) → FILLED/FAILED`。
超时或行情缺失进 `EXPIRED`/`PAUSED`；当模型回复跨越下一根K线周期边界，旧结果不可交易。每名AI每根收盘K线最多一个执行机会；拒单不算完整交易。

### 5.2 持仓与订单生命周期
- 账户：`FLAT → OPEN_LONG/OPEN_SHORT → FLAT`；先平后反手；永不加仓；止损优先处理。
- 订单意图：`CREATED → RISK_VALIDATED → READY → FILLED/REJECTED/EXPIRED`。
- 同时存在止损和模型主动平仓：基于严格事件序列先核销唯一持仓，后来事件不得再次平仓或重复计费。

### 5.3 行情故障生命周期
`HEALTHY → STALE → RECOVERING → HEALTHY`，严重数据或账本不一致进入`LOCKED`。STALE期间不得开新仓；保留持仓，不补造中断期间成交；恢复需核对已收盘K线缺口、最新标记价与可交易报价、止损状态及账本。

### 5.4 进化生命周期
`ACTIVE → REVIEW_QUEUED → CANDIDATE_DRAFT → SHADOW_RUNNING → (PROMOTION_PENDING | REJECTED | INSUFFICIENT_SAMPLE) → (PROMOTED | ARCHIVED)`。
每人每50笔完整交易一组触发，候选占用唯一槽时新任务排队；候选故障有限恢复，不重置30天倒计时，故障严重则淘汰；C核心理念校验失败直接拒绝候选；待升级时现役持仓自然平仓后下一次空仓决策生效。

### 5.5 恢复与重放
重启只允许根据持久化事件构建当前状态，不能重做已执行的成交；所有有副作用的处理都要求幂等键、账户版本乐观校验、同事务写入交易事件和账本变化。真实模型运行中的任务重启后不得把旧回复当作新周期指令。

## 6. 风险、成交、资金与进化计算契约

### 6.1 市价模拟
- 开多按可得`ask`模拟买入，开空按`bid`模拟卖出；平多按`bid`，平空按`ask`，再应用滑点、手续费与适用资金费。
- AI决策完成后的**可得实时行情**才能支持成交；在无可靠报价时拒绝新开仓；若已触发止损但报价缺失，标记`PROTECTION_UNCERTAIN`，不得虚构已成交。
- 标记价格触发保护性止损；触发后用可得交易报价模拟退出。标记价不能替代成交价。
- 资金费发生时按已核验的适用时间和头寸计算；费用等级、滑点、精度和报价老化阈值为预实施验证配置，不擅自设为零。

### 6.2 风险计算
目标名义仓位=`min(账户当前权益, 可用保证金可支持名义仓位)`（1倍杠杆）；根据入场价与AI止损价的距离以及预估进出成本计算计划单笔风险，上限账户权益2%。超出则只缩小数量，**不改止损价**；不足最小有效数量或保证金时拒单。实际极端行情亏损可能超过2%，应真实记账。

### 6.3 影子比较
- 新旧对照采用共同开始时刻、相同基准权益、相同行情图像与模型版本、费用、风险及执行规则，保持独立交易与上下文；不直接用现役账户旧历史累计盈亏作对照。
- 正式评价节点为影子开始后的第7/14/21/30天；每节点必须双方分别累计至少20笔完整交易才计算胜负；**第一次全部通过即可进入待升级**。
- 候选同期净收益 > 对照同期净收益；候选平均盈利/平均亏损 > 对照；候选最大回撤 <= 对照1.1倍，且差值 <= 2个百分点（同时满足）。回撤基于完整权益曲线含浮盈浮亏。
- 盈亏比无可比定义（例如没有亏损交易）→不可判定，不自动升级；对照回撤为零时以严格相对限制处理，不跳过限制；30天不达标淘汰。
- 每个节点完整保存指标、样本量及原因。多次检查会提高偶然晋级概率，报告需明确该限制。

## 7. MD 策略文件契约

文件目录逻辑：`strategies/A_TREND.md`、`strategies/B_REVERSAL.md`、`strategies/C_MY_STYLE.md`，母版与进化版本分开保存。

固定章节（人类自然语言描述，不要求用户写公式）：身份和适用市场、核心操盘理念、15M三天全局与一天局部看盘顺序、判断趋势/震荡的方法、做多条件、做空条件、主动平仓及止损原则、禁止交易情形、持仓与复盘习惯、至少可补充的多/空/观望图例、不可改变的系统硬约束、可进化章节与版本元数据。

A/B可进化风格原则；C必须保持原始核心理念（单独只读校验哈希），只能进化可变章节。无论哪套MD均不得修改1倍杠杆、2%风险、唯一持仓、无实盘订单等系统边界。

**C策略当前未提交**：允许构建导入契约及模拟测试，但不可将不存在的用户理念编造为正式C策略，也不能宣称其行为已经验收通过。

## 8. 工程实施阶段、依赖与停止门槛

| 阶段 | 任务包 | 依赖 | 最少交付物 | 通过条件 / 停止条件 |
|---|---|---|---|---|
| P0 | 独立环境、风险封锁、规格核验 | 无 | 项目骨架、依赖/密钥方案、外部接口验证记录、测试计划 | 检测到旧项目改动或真实交易权限 → 立即停止 |
| P1 | 市场行情及图像 | P0 | 15M完整性、恢复、288+96视觉图、统一快照ID | 数据缺口/未收盘泄漏/图哈希分叉 → 停止 |
| P2 | MD与视觉模型 | P1 | 三独立模型会话、结构化决策、超时/成本记录 | 不能真识图或跨策略上下文污染 → 停止 |
| P3 | 风控、模拟执行与账本 | P1,P2 | 原子账本、单仓约束、行情止损、费用/资金费 | 重复成交、超过风险预算、账本不一致 → 停止 |
| P4 | 前端主工作台P0 | P1-P3 | 主图、三账户卡、交易详情、健康状态、MD导入 | 标记不等于成交账本、异常状态伪正常 → 停止 |
| P5 | 进化与影子研究＋P1前端 | P2-P4 | 50笔任务、版本不可变、独立对照、评价与升级、日记 | 影子资金串账、回写历史、违规升级 → 停止 |
| P6 | 综合验收、恢复、真实模型观察 | P0-P5 | 测试报告、A/B/C各100次判断统计、运行手册、回滚方案 | 任一P0安全红线失败 → 不可宣布V1通过 |

阶段允许代码实现前先输出设计/合同和测试用例；跨阶段合并需要证据。先对每阶段完成离线合成样本的自动化测试，再经授权接入公开实时行情和视觉模型；**本任务包本身不授权执行上述步骤**。

### 8.1 每阶段验收证据结构
`deliverables/phase_N/{README.md, contract.md, test_report.json, audit_samples.jsonl, known_issues.md, sha256_manifest.txt}`；必须注明测试数据属于合成还是实际行情、模型调用是否真实、运行时间范围、通过/失败/跳过数量及不可复现的环境条件。

## 9. 完整验收矩阵

| ID | 分类 | 测试/证据 | 通过标准 | 等级 |
|---|---|---|---|---|
| ENV-01 | 隔离 | 与旧项目文件快照比对 | 旧量化目录零改动 | 阻断 |
| ENV-02 | 安全 | 网络调用白名单及代码审计 | 仅公开行情；无真实下单/签名接口 | 阻断 |
| MKT-01 | 行情 | 实际15M收盘事件与REST补齐 | 无重复、无漏记收盘事件 | 阻断 |
| MKT-02 | 行情 | 缺口、乱序、重复、延迟模拟 | 状态及异常记录准确 | 阻断 |
| MKT-03 | 行情 | 标记价及买卖报价断线恢复 | STALE期无新交易且不假造成交 | 阻断 |
| VIS-01 | 视觉 | 固定有已知历史片段 | 288全局/96局部，时间边界准确 | 阻断 |
| VIS-02 | 视觉 | 三AI同一时刻快照比对 | 图像字节哈希一致 | 阻断 |
| VIS-03 | 视觉 | 截图内容人工+自动核验 | 无技术指标、交易答案、未来数据 | 阻断 |
| AG-01 | AI | 真实视觉模型多模态握手 | 确实接收图片并返回结构化结果 | 阻断 |
| AG-02 | AI | 三独立MD/记忆同时调用 | 无跨代理泄漏，版本正确 | 阻断 |
| AG-03 | AI | 过期/格式错误/无止损输出 | 拒单并保存原因 | 阻断 |
| AG-04 | AI | 15M超时与重启回调 | 迟到指令不得成交 | 阻断 |
| ORD-01 | 执行 | 开多、开空、平仓、反手 | 无跨状态非法动作、反手先平 | 阻断 |
| ORD-02 | 执行 | 加仓/双向并持尝试 | 统一拒绝 | 阻断 |
| RSK-01 | 风控 | 4%止损满仓与2%阈值 | 自动缩仓且不移动止损 | 阻断 |
| RSK-02 | 风控 | 手续费/滑点、数量精度、资金不足 | 正确减仓或拒单 | 阻断 |
| RSK-03 | 风控 | 1倍杠杆+当前权益变化 | 始终满足账户可用风险预算 | 阻断 |
| STP-01 | 止损 | 标记价触发，盘口价结算 | 触发与成交报价分离、有审计 | 阻断 |
| STP-02 | 止损 | 止损与AI平仓竞态 | 单次平仓、单次计费 | 阻断 |
| STP-03 | 止损 | 故障/中断/重连 | 不假造成交、不隐藏保护不确定性 | 阻断 |
| LED-01 | 账本 | 全部资金事件重复投递 | 幂等且双重入账为0 | 阻断 |
| LED-02 | 账本 | 手续费/资金费/盈亏重算 | 精度和借贷一致、可复算 | 阻断 |
| LED-03 | 账本 | 重启恢复 | 余额、持仓、版本、计数一致 | 阻断 |
| UI-01 | 前端 | 固定交易账本样本→主图 | A红B绿C橙与成交点完全匹配 | 阻断 |
| UI-02 | 前端 | 点击成交点 | 原图、MD版本、成交价格/费率/原因全可追溯 | 高 |
| UI-03 | 前端 | 断线与账本故障 | 有明确异常状态、不冒充实时或受保护 | 阻断 |
| UI-04 | 前端 | 桌面/窄屏手工巡检 | 图表可用、标记不遮挡关键内容 | 高 |
| STR-01 | MD | A/B/C母版+进化权限测试 | C核心只读；母版不覆盖 | 阻断 |
| EVO-01 | 进化 | 49→50笔闭合交易 | 仅完整交易计数、独立触发 | 阻断 |
| EVO-02 | 进化 | 候选运行中再达50笔 | 只排队不覆盖、不多开候选 | 阻断 |
| EVO-03 | 进化 | 相同起始资金/未来行情对照 | 记忆/仓位/账本隔离 | 阻断 |
| EVO-04 | 进化 | 7/14/21/30节点 | 未双方各20笔时不升级 | 阻断 |
| EVO-05 | 进化 | 收益、盈亏比、回撤边界 | 所有条件同时成立才能升 | 阻断 |
| EVO-06 | 进化 | 零亏损、零回撤、模型故障 | 正确不可判定/严格门槛/有限恢复 | 阻断 |
| EVO-07 | 进化 | 30天失败、已有持仓 | 淘汰或待空仓切换；不迁移旧仓 | 阻断 |
| E2E-01 | 集成 | 三AI并行100次真实视觉决策各 | 原始调用、状态、成本、异常记录完整 | 阻断 |
| E2E-02 | 集成 | 非交易时段/全观望样本 | 不人为制造交易、仍可过基础验收 | 高 |
| E2E-03 | 集成 | 连续运行/随机重启/网络故障 | 未发生非法成交、状态可恢复 | 阻断 |

**测试分类：**合成数据测试用于逻辑状态机；真实行情验证用于接口和交易图；真实模型调用用于视觉决策。测试报告必须严格区分三者。

## 10. 关键尚待核验参数（不改变冻结产品需求）

| 条目 | 状态 | 实施前处理及准入条件 |
|---|---|---|
| 实时行情新鲜度阈值 | 待核验 | 基于官方推送频率和真实延迟测量写入配置，超过即STALE |
| 模型调用预算与超时重试 | 待核验 | 预算封顶，重试不得超出当次15M决策周期 |
| 影子候选故障次数阈值 | 待核验 | 有限次数、固定总计时、全部审计；不能延长30天 |
| 成交滑点、交易手续费 | 待核验 | 真实公开参数或保守透明假设，禁用“默认零成本” |
| 资金费结算与风险报价 | 待核验 | 记录实际获取来源、时刻及异常 |
| 影子开始时旧版有仓 | 技术边界待收敛 | 对照/候选必须从一致、可审计的基准状态启用；不可复制难以公平比较的未平仓盈亏而不披露 |
| 账户零回撤、盈亏比不可算 | 已定原则 | 不跳过比较，无法比较时不升级 |
| 策略C操盘MD | 用户待提供 | 不构造假个人理念；仅验证导入及隔离能力 |
| 币安行情地区可用性 | 待实测 | 无法访问公开接口则记录阻断，不规避平台限制 |

## 11. 任务包交付格式与最终报告

工程执行期每阶段应提供：`SCOPE`、`FILES_CHANGED`、`TESTS_PASS_FAIL_SKIP`、`REAL_MARKET_DATA_USED`、`REAL_VISION_CALLS`、`PAPER_FILLS`、`SECURITY_BLOCKERS`、`KNOWN_LIMITATIONS`、`NEXT_STAGE_GATE`。仅当所有阻断测试通过且真实模型证据完整，才允许宣称阶段通过；未授权不得启用真实市场运行或安装破坏性依赖。

**明确：本文件仅是设计与规划文件。尚未生成源代码、未启动程序、未进行交易。**
