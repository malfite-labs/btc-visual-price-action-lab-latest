# Linux 架构说明

## 部署布局

```
/home/box/btc-visual-price-lab/          # chmod 700
  incoming/                              # 归档 + SHA256SUMS + snapshot-report
  extract/BTC-Visual-Price-Lab/          # 解压只读参考
  runtime/
    app/                                 # ★ 运行根（可写部署树）
    run/                                 # pid/lock
    logs/                                # 进程日志
  secrets/
    alpaca-offline-empty.env             # 离线空凭证桩
  logs/                                  # 迁移/验收日志
```

## 进程与端口

| 组件 | 端口 | 职责 | 单实例机制 |
|---|---|---|---|
| backend (uvicorn) | 127.0.0.1:8011 | 正式 ABC 模拟盘、行情管理、只读/控制 API | `runtime/run/backend.lock` + pidfile；Alpaca WS 另有 `_StreamLease`（fcntl） |
| shadow worker | （无独立端口） | `scripts/run_forward_v26.py`，经本地 HTTP 消费行情 | `runtime/run/shadow.lock` + 引擎内 `forward-worker.lock` |
| frontend (vite) | 127.0.0.1:5174 | 监控 UI | `runtime/run/frontend.lock` + pidfile |

约束：唯一 8011、唯一 Alpaca WS 所有者、唯一影子 Worker、不重复自然 15 分钟周期、不重复模拟成交。

## 数据

- 正式账本：`data/paper.sqlite3`
- 影子账本：`docs/reports/abc-forward-v26/shadow-ledger.sqlite3`
- K 线：`data/market-candles.sqlite3`
- 规则配置：`config/rule_v26/profile.json` + 策略 py/md
- 冻结清单：`docs/reports/abc-forward-v26/freeze.json`（20 项哈希）

## LONG_PROTECTION / WebSocket 恢复（保留语义）

代码路径：`backend/market.py`、恢复/保护相关测试 `tests/test_auto_protection.py`、`tests/test_recovery_guard.py`。

行为要点（不得因 Linux 启动成功而强制解除）：

1. WebSocket 异常 → 进入 LONG_PROTECTION，暂停正式 ABC **新开仓**
2. 串行重试连接 → 认证/订阅 → 收到**当前连接**新鲜合法报价
3. 通过既有连续 K 线验证 → 自动解除 → 审计 `recovery_succeeded`
4. **REST 报价新鲜不能单独替代 WebSocket 验证**

离线模式：空凭证 → 不建真实 WS；状态中可见 `market_task_startup=BLOCKED_CREDENTIALS`，`websocket_quote_health=STREAM_UNAVAILABLE`。

## 技术栈（未更换）

Python + FastAPI/uvicorn、React/Vite、SQLite、Alpaca 行情接口、既有 AI 调用接口。不引入新库/MQ/复杂容器。
