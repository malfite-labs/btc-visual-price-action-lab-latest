# BTC Paper Lab V3.1 新策略接入规范

> 适用版本：BTC Paper Lab V3.1（`btc-lab-declarative-strategy/v2`）
> 部署目录：`C:\Users\admin\Documents\量化升级版\btc-paper-lab-v3\windows-binance-deploy`

---

## 1. 接入架构总览

自 V3.1 策略池标准化升级后，所有通过策略池上传的合规策略均自动进入统一闭环，**无需修改任何前端、桌面控制台、调度器或实盘跟单的底层代码**：

1. **上传与校验**：`POST /api/v1/strategy-pool/upload`（新增）或 `POST /api/v1/strategy-pool/upload/validate` + `upload?expected_strategy_id=<id>`（目标策略版本更新）。
2. **元数据分离存储**：`strategies` 表维护唯一 `id` 与启用状态，`versions` 表按 `(strategy_id, version)` 与 `sha256` 不可变归档完整策略 JSON 文档；`StrategyPool.list()` 自动解析文档中的 `name`（中文展示名称）、`description` 与 `version`。
3. **独立纸盘账户初始化**：上传时自动在 `accounts` 表初始化 `$10,000.00 USD_RESEARCH` 独立纸盘账户，不与其他策略共享资金或仓位。
4. **公共行情门禁与自然周期调度**：启用后自动接入 `COMMON-MARKET-GUARD-V1` 公共行情门禁与 15 分钟自然周期调度器（`StrategyPool.cycle()`）及实时报价止盈止损监控（`StrategyPool.monitor_quote()`）。
5. **统一详情与账户面板**：桌面控制台通过 `is_pool_strategy_id()` 动态识别所有非核心策略 ID，点击策略行或「查看详情 / 账户面板」即可打开四标签规则详情窗与完整账户/成交/盈亏日历面板。
6. **统一 Demo 与正式实盘跟单接入**：`build_live_paper_rows()` 自动从 `strategy-pool.sqlite3` 发现全部未归档策略并呈现中文名称；`scripts/live_copy_v2.py` 的 `pa_pool_fills()` 统一解析开多/开空/平多/平空及部分平仓事件，自动接入 Demo 与正式实盘的可选策略列表。
   - **安全隔离**：新策略上传或启用纸盘 **绝不会** 自动授权正式实盘下单；纸盘、Demo、正式实盘的账户、API 凭据、跟单游标与订单去重均完全独立。

---

## 2. 策略文件格式与必要字段

策略文件必须为 UTF-8 编码的 JSON 文件（单文件不超过 64 KB），格式声明固定为 `btc-lab-declarative-strategy/v2`：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `format` | string | 是 | 固定为 `"btc-lab-declarative-strategy/v2"` |
| `strategy_id` | string | 是 | 全局唯一策略 ID（小写字母/数字/连字符，如 `pa-chan-center-divergence`）；**严禁通过修改 ID 实现改名** |
| `name` | string | 是 | 面向用户的中文策略名称（如 `"中枢震荡与背驰回抽（量化近似）"`）；缺失时系统回退为 `名称待完善（<id>）` |
| `version` | string | 是 | 语义化版本号（如 `"1.0.0"`、`"1.1.0"`）；同一策略版本号不可覆盖 |
| `description` | string | 建议 | 策略核心逻辑与适用行情说明 |
| `market` | string | 是 | 固定 `"BTC/USD"`（映射 Binance USDⓈ-M BTCUSDT 公开行情） |
| `timeframe` | string | 是 | 固定 `"15m"` |
| `parameters` | object | 是 | 策略可调参数字典（规则中引用的参数必须全部显式声明） |
| `entry` | object | 是 | 包含 `long` 与 `short` 两个声明式 AST 表达式节点 |
| `position` | object | 是 | 包含 `direction`（`long`/`short`/`both`）、`entry_timing`（`next_open`）、`max_hold_bars`、`same_bar_priority`（`stop`）、`stop`（含 `kind`/`lookback`/`atr_period`/`buffer_atr`/`min_distance_atr`/`max_distance_atr`）与 `target_r` |
| `risk` | object | 是 | 固定 `currency: "USD_RESEARCH"`、`initial_equity: "10000"`、`max_leverage: 5`、`risk_fraction: "0.02"` |

---

## 3. 版本更新与持仓保护规则

1. **绑定式更新**：在策略池列表中**右键点击目标策略行 →「更新策略（上传新版本）」**，系统自动锁定 `expected_strategy_id`。
2. **强校验阻断**：上传文件的 `strategy_id` 与目标策略不一致、版本号重复、内容 SHA-256 重复或规则校验失败时，直接拒绝更新，原可运行版本不受任何影响。
3. **持仓与账本保护**：
   - 更新版本绝不重置原策略账户、历史权益、历史成交或跟单映射。
   - 若策略当前持有未平仓位，该仓位继续严格沿用其开仓时记录的 `position.version` 与原有止盈止损规则；新版本仅对平仓后的后续新开仓生效。

---

## 4. 历史回测接入规范

- 所有合规注册策略均自动支持「历史回测」（日历日期选择，北京时间 UTC+8，内部对齐 15 分钟 UTC 边界，最大跨度 **3 个自然月**，超限明确提示不静默截断）。
- 回测自动预加载指标预热 K 线（不产生起始日期前的成交），自动检测数据缺口与重复 K 线，使用独立内存账本执行并归档至 `strategy-pool-backtests.sqlite3`，零污染纸盘、Demo 与正式实盘账本。
