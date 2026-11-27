# D · D 核心趋势量化策略（D-COMMON-GUARD-V1）

- **策略ID**：`D`
- **分组**：原有策略
- **主周期**：15m（多周期日线/1H结构过滤 + 实时报价止损/分批止盈保护）
- **核心实现**：`backend/d_signal.py`、`backend/d_executor.py`、`backend/d_ledger.py`、`backend/d_bridge.py`
- **账户口径**：独立 `data/d/trading.sqlite3`（`$10,000 USD_RESEARCH`，接入 `COMMON-MARKET-GUARD-V1` 公共行情门禁）
