# G · C反向镜像策略（G-MIRROR V1.0.0）

- **策略ID**：`G`
- **分组**：镜像策略
- **驱动方式**：严格由 C 策略已提交的 `OPEN` / `PARTIAL_CLOSE` / `CLOSE` 成交事件驱动，执行等量反向镜像开仓与同步平仓
- **核心实现**：`backend/g_mirror.py`
- **账户口径**：独立 `data/g/mirror.sqlite3`（`$10,000 USD_RESEARCH`，含独立风控紧急退出保护与事件幂等去重）
