# BTC Paper Lab V3.1 统一策略研究数据接口规范

> 适用模块：`scripts/research_v3_engine.py`（`ResearchV3Engine`）
> 计算引擎版本：`RESEARCH-METRICS-V3.0.0`
> 属性：**严格只读（GET-only + SQLite `mode=ro` + `PRAGMA query_only=ON`）**

---

## 1. 五层逻辑架构

1. **第一层（动态策略发现）**：自动扫描 `/api/v1/agents`、`data/d/trading.sqlite3`、`/api/v1/e/accounts`、`/api/v1/g-mirror/status`、`data/strategy-pool.sqlite3`（全部未归档策略）、`data/pa-v1-extensions/*.sqlite3`、`data/r-series/*.sqlite3` 与 `data/extensions/*.sqlite3`。**严禁**使用硬编码策略 ID 列表限制研究范围；任何通过策略池（Mode A）或标准扩展契约（Mode B）新增的合规策略均自动进入研究体系，分组与主控制台保持一致（`原有策略`/`镜像策略`/`PA系列`/`Chan`/`R系列`/`S系列`/`AY系列` 及未来动态分组）。
2. **第二层（并发容错只读适配）**：通过 `ThreadPoolExecutor(max_workers=6)` 并发拉取只读 HTTP 接口（单请求 4s 超时、绕过代理直连 `127.0.0.1:8012`）并批量查询本地只读 SQLite（策略池仅 2 次批量 SQL，100 策略规模加载耗时 `< 100ms`）；单接口异常或超时仅记录至 `errors` / `data_quality.missing_data`，不影响其他策略展示。
3. **第三层（统一绩效与归因计算）**：详见《BTC Paper Lab V3.1 量化绩效指标计算规范.md》。
4. **第四层（TTL 缓存与回测隔离）**：内置 15 秒内存快照缓存（支持强制刷新），单独只读读取 `data/strategy-pool-backtests.sqlite3` 中的已保存回测报告作为独立数据源（`BACKTEST_SAVED`），严禁与纸盘自然运行数据（`PAPER_LIVE`）混合统计。
5. **第五层（桌面研究界面）**：`scripts/research_v3_window.py` 仅负责异步展示、筛选、多策略对比与 CSV/Markdown 导出，不包含底层账本解析逻辑。

---

## 2. 统一研究数据契约（`StrategyResearchRecord`）

### 2.1 策略基础信息（`base`）
- `strategy_id`：全局唯一稳定标识
- `strategy_name`：真实中文名称（来自文档或注册表；缺失时标记 `名称待完善（<id>）`）
- `strategy_group`：策略分组（`原有策略` / `镜像策略` / `策略池` 及未来动态分组）
- `strategy_version`：当前活动版本
- `strategy_type`：`CORE_RULE` / `CORE_D` / `CORE_E` / `MIRROR_EVENT` / `POOL_V2` / `PA_EXTENSION`
- `symbol` / `timeframe`：`BTC/USD` / `15m`
- `registered_at` / `running_since` / `running_days` / `state` / `versions_history`

### 2.2 账户及资金信息（`account`）
- `account_id` / `account_mode`（`PAPER_SIMULATION`）/ `currency`（`USD_RESEARCH`）
- `capital_model`（如 `PAPER_10000_RISK_2PCT_5X`、`MIRROR_C_EVENT_10000`；不同资金模型对比时强制触发不可排名告警）
- `initial_capital` / `current_equity` / `available_balance` / `realized_pnl` / `unrealized_pnl` / `total_fees` / `current_position` / `active_open_count`

### 2.3 完整闭合交易记录（`closed_trades[]`）
- `trade_id` / `strategy_id` / `account_id`
- `opening_strategy_version` / `execution_strategy_version`（严格保留跨版本真实归属）
- `entry_time` / `exit_time`（UTC ISO-8601）
- `side`（`LONG` / `SHORT`）/ `entry_price` / `exit_price` / `quantity`
- `gross_pnl` / `open_fee` / `close_fee` / `fees` / `slippage` / `net_pnl`
- `exit_reason` / `exit_reason_category` / `holding_duration_sec` / `close_evidence_status`

### 2.4 权益与回撤序列（`equity_series[]` & `drawdown_summary`）
- 每点包含：`timestamp`, `equity`, `realized_pnl`, `unrealized_pnl`, `equity_peak`, `drawdown_amount`, `drawdown_pct`, `basis`（`INITIAL_CAPITAL` / `REALIZED_RECONSTRUCTED` / `CURRENT_SNAPSHOT`）
- 汇总包含：`peak_equity`, `max_drawdown_amount`, `max_drawdown_pct`, `dd_start`, `dd_trough`, `dd_recovery`, `in_drawdown`, `basis`

### 2.5 研究数据质量信息（`data_quality`）
- `data_source` / `coverage_start` / `coverage_end` / `snapshot_time`
- `completeness`（`完整` / `样本不足（N笔）` / `暂无成交` / `部分缺失/待核查`）
- `missing_data[]` / `metric_availability` / `calculation_version`

---

## 3. 隔离与只读安全红线

- 严禁通过研究模块修改策略规则、账户资金、持仓、止盈止损、启停状态或实盘跟单配置。
- 严禁将历史回测盈利合并计入纸盘实际收益；严禁为补齐曲线凭空生成历史权益点。
- 多源分接口快照必须在界面与报告中明确标示「非原子时点快照」。


---

## 4. 生命周期同步与事件失效快照（V3.1 轻量化升级）

- **只读快照元数据**：`/api/v1/strategy-pool` 与 `/api/v1/system/status` 复用后端 `ReadOnlySnapshotCache`（15s TTL + 状态变更主动失效），并暴露 `snapshot_meta`（`version`、`cached_at`、`age_ms`），供研究中心与未来 AI 只读查询接口（如 GrokBot）直接复用，禁止重复搭建行情与绩效计算链路。
- **生命周期自动同步**：研究中心与实时 K 线只读查询 `data/strategy-lifecycle.sqlite3`，自动剔除 `DELETED` 策略，为 `ARCHIVED` 策略标注 `lifecycle_state='ARCHIVED'` 及冷归档路径，确保归档策略不混入活跃运行与实时跟单候选。
