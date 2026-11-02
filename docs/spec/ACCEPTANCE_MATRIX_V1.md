# BTC Visual Price Action Lab V1.0 — 验收矩阵

- 版本：ACCEPTANCE V1.0.1；关联 PRODUCT_REQUIREMENTS_V1_FROZEN.md / ENG-SPEC V1.0.1。
- 原工程包的阻断测试编号保留；证据必须标注 SYNTHETIC、FIXTURE、PUBLIC_MARKET 或 REAL_VISION。禁止用合成事件冒充真实市场结果。
- 自动化检查是阻断主判据；人工视觉检查是补充证据。实时外部验收采用受控、单独授权的准入批次，不能作为本轮执行行为。

| ID | 自动化/证据 | 通过标准 | 阶段 |
|---|---|---|---|
| ENV-01 | 快照/路径 allowlist 检查 | 与旧量化目录文件哈希零差异；规格与项目隔离 | P0 |
| ENV-02 | API 路由/依赖静态扫描 | 只有公开行情读连接定义；无签名、下单、入金或真实账户接口 | P0 |
| MKT-01 | 固定源行情夹具和 event replay | 15m 收盘唯一、无漏/重；FundingRateEvent 来源/生效/观察时序可复算 | P1 |
| MKT-02 | 参数化缺口、乱序、重复、延迟夹具 | health、sequence、告警和恢复状态均符合合同 | P1 |
| MKT-03 | 断线/报价过期/重连事件流 | 过期时不新开仓；不造 fill；恢复先对账，止损 pending 可恢复 | P1/P3 |
| VIS-01 | 固定 288+96 candle fixture | 全局/局部边界、闭合时间、重用 candle 集合精确 | P1 |
| VIS-02 | A/B/C snapshot hash 对比 | 同周期图像 bytes 与 SHA-256 完全一致 | P1 |
| VIS-03 | Renderer manifest + 图像断言 | 无技术指标、交易标记、未来 candle；边界通过固定数据校验 | P1/P6 |
| AG-01 | 经授权的真实多模态小批次 + 自动证据检查 | 供应商确实收到图片；版本/请求/响应证据完整；100 次正式计数可审计 | P2/P6 |
| AG-02 | 并发代理隔离测试 | 三个代理图像 hash 相同，MD/记忆/账户上下文互不可读 | P2 |
| AG-03 | schema fuzz / invalid result fixtures | 过期、坏格式、无效止损全拒绝并保存原因 | P2 |
| AG-04 | timeout、迟到响应、重启事件测试 | 周期截止后回复不可交易；预算/attempt/deadline 一致 | P2/P6 |
| ORD-01 | 状态转换 model-based tests | 多空开平合法；反手先平；并发 close 仅一笔成交 | P3 |
| ORD-02 | 加仓、双持、过期账户版本尝试 | 全部拒绝，无额外 ledger/fill | P3 |
| RSK-01 | 4% stop distance fixture | 缩量满足 2% 计划损失且不移动 stop；舍入后复算 | P3 |
| RSK-02 | fee/slippage/filter/liquidity fixtures | 费用不是零默认；L1 不足向下限量；小于 minQty/minNotional 拒单 | P3 |
| RSK-03 | 账户权益和可用保证金变更序列 | 1x notional 与保证金约束总成立；配置缺失拒单 | P3 |
| STP-01 | mark trigger 与盘口成交夹具 | mark 只触发；成交用对手 L1 价+滑点；trigger/fill 时间分开 | P3 |
| STP-02 | 止损/AI close 并发排列测试 | 唯一 position version 只消费一次；单次 close、费用和 trade | P3 |
| STP-03 | 报价不可用、断线、重启、无历史 mark | STOP_EXIT_PENDING 持久；不虚构成交；恢复安全且状态不假报受保护 | P3/P6 |
| LED-01 | 重复投递同事件 N 次 | unique idempotency 后仅一个账户影响，响应可重放 | P3 |
| LED-02 | 独立账本重算样本 | 毛/净 PnL、手续费、资金费、精度逐项一致 | P3 |
| LED-03 | 每个事务提交点故障注入 | 重启余额/仓位/版本/sequence/计数一致，无副作用重播 | P3/P6 |
| UI-01 | 固定 fills fixture 的浏览器自动截图/DOM | 同图 A 红/B 绿/C 橙；fill 数、方向、时间、价格锚点逐条匹配 | P4 |
| UI-02 | 点击 fill 端到端读查询 | 原图、版本、决策、成交、成本和退出证据可追溯 | P4 |
| UI-03 | 注入 stale/model degraded/ledger locked | UI 状态与 API 一致；无 LIVE/PROTECTED 假显示 | P4 |
| UI-04 | 桌面/窄屏视觉回归 + 人工补充 | 锚点/遮挡阈值自动判；人工只记录剩余可读性问题 | P4 |
| STR-01 | MD parser/hash mutation tests | 版本不可覆盖；C 核心变更拒绝；C 原稿缺失时不通过行为验收 | P2 |
| EVO-01 | 49→50 完整往返交易 event fixture | 正好触发一个批次；未闭合及拒单不计数 | P5 |
| EVO-02 | 候选占用期间多个50批次 | 仅一候选运行，后续批次按序排队且不覆盖 | P5 |
| EVO-03 | 双账户同起点 future event replay | 账户/记忆/仓位独立，输入行情和模型版本相同 | P5 |
| EVO-04 | 天数和双方样本边界矩阵 | 只在第7/14/21/30天计算；双方各20笔前不升级 | P5 |
| EVO-05 | 净收益、盈亏比和回撤边界 fixtures | 全部严格同时满足方可 PASS→PROMOTION_PENDING | P5 |
| EVO-06 | 无亏损交易、零回撤、指标 NaN、模型失败 | 指标不可比则 INDETERMINATE，不跳过门槛或自动升级 | P5 |
| EVO-07 | 第30天失败、有旧仓待平 | 候选归档；通过时等待现役自然平仓，下一合格决策切换 | P5 |
| E2E-01 | REAL_VISION evidence counter | 每代理100次真实图片调用统计完整；仅记录结果，不以此承诺盈利 | P6 |
| E2E-02 | 全 WAIT / 无交易期 fixture | 不强制下单；决策闭环依旧完整 | P6 |
| E2E-03 | 长时事件 replay + 随机事务中断 | 无非法成交；状态和账本可恢复，证据可按 sequence 重放 | P6 |

## 1. 可重复性规则

- 逻辑和恢复测试使用固定事件 JSONL、时钟注入和固定十进制配置 hash；同一 fixture 必须得到同一状态、账本及判定。
- 外部行情连通、真实模型多模态调用和 100 次观察不可保证供应商输出字节重复。对它们自动验证请求确实发送、响应结构、模型标识、图像 hash、用量/延迟、错误与计数；不得用模型理由文本完全一致作为通过条件。
- UI 固定成交样本使用稳定渲染版本、固定容器尺寸/字体和明确坐标/数据锚点；像素比较只允许文档定义的容差，不得人工目测替代数据匹配。
- 每次报告保存需求版本、代码版本（未来工程阶段）、配置 hash、fixture hash、环境、开始/结束 UTC、pass/fail/skip 与数据类别。

## 2. 上线准入仍需确认

CFG-01…CFG-08 数值由 P0 的来源核验/用户确认填入配置 manifest。真实视觉服务和公开市场观察需另行授权；本规范本身不授权运行这些测试。

## 3. 影子绩效计算的黄金断言

固定 fixture 至少覆盖：双方 20 笔边界、候选净收益相等、盈亏比相等、回撤相对限制刚好相等、绝对差 0.02 刚好相等、对照回撤为 0、候选无亏损、无盈利、未平仓浮盈/浮亏、资金费和费用、mark event 缺口。严格大于的收益/盈亏比在相等时不通过；两个回撤上限相等时通过；不可判定不升级。净收益使用同起点至检查点权益差，回撤使用共同 mark event sequence 的权益路径。
