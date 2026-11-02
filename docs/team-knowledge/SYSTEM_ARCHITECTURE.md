# SYSTEM_ARCHITECTURE — 系统架构与代码索引
generated_at: 2026-11-03 00:25 Asia/Shanghai
evidence_policy: 每条结论标注 [代码确认]/[运行验证]/[历史记录未复核]/[不确定/未实现]；运行状态引用前须重新核查实时数据；文档与代码冲突以本轮核实的代码与运行证据为准。本文件只读整理，未改任何代码/配置/策略。

## 1. 根路径
- 控制根 `/home/box/btc-visual-price-lab`（extract/incoming/logs/runtime/secrets）；项目根 `runtime/app`（下文相对此路径）。[运行验证]
- 本 box 为 2026-11-02 22:56（上海）Windows 快照迁移的 Linux 镜像，现以云端独立模式运行。[历史记录未复核]（见 docs/LINUX_MIGRATION.md）

## 2. 组件
| 组件 | 入口 | 端口/进程 | 证据 |
|---|---|---|---|
| 后端 FastAPI | `backend/app.py`（uvicorn backend.app:app） | 127.0.0.1:8011 | [运行验证] |
| 前端 React+Vite | `frontend/src/App.tsx`、`main.tsx` | 127.0.0.1:5174 | [运行验证] |
| 影子前向 worker | `scripts/run_forward_v26.py` + `forward_v26_core/engine/review/status.py` | 独立进程 | [运行验证] |
| 守护 | `scripts/linux/supervise.sh _loop {backend,frontend,shadow}` + flock | bash 循环 | [运行验证] |
| BOOK 侧车 | `backend/book_pa_sidecar.py`（默认 8813） | 未在运行 | [代码确认]/[运行验证:未监听] |

## 3. 后端模块索引 (backend/)
- 行情: market.py（Alpaca REST/WS、报价、WS 恢复态）、candle_archive.py（15m K 线归档）、charts.py [代码确认]
- 调度/编排: scheduler.py、orchestration.py、rule_cycle.py（规则驱动周期） [代码确认]
- 策略/信号: strategy.py、config/rule_v26/{profile.json,A-rule.json,v3-signals.py} [代码确认]
- 执行/账本: execution.py（模拟撮合）、ledger.py（现金事件）、margin.py、b_stop_depth.py [代码确认]
- 恢复保护: recovery_guard.py（recovery_blocked） [代码确认]
- 进化: evolution.py（EVOLUTION_BATCH_SIZE 见 settings.py=50） [代码确认]
- E 策略: e_*.py（e_auto_cycle、e_paper_entry/store、e_protection、e_quant_*、e_shadow_*、e_trailing 等） [代码确认]
- 视觉模型: vision.py / vision_provider.py / vision_prompt.py（当前 MODEL_CALLS_DISABLED） [运行验证]
- BOOK: book_pa_research.py / book_pa_prices_v02.py / book_pa_chart_v03.py / book_pa_sidecar.py [代码确认]
- 统计: stats_panel.py；配置: settings.py；领域对象: domain.py [代码确认]

## 4. 数据库
- 正式账本 `data/paper.sqlite3`：accounts, fills, ledger_events, audit_events, decisions, scheduler_cycles, rule_position_meta, evo_*, agent_memories [运行验证]
- 影子账本 `docs/reports/abc-forward-v26/shadow-ledger.sqlite3`：同上 + forward_cycles/decisions/positions/trades/reviews/versions/bars/halts/halt_resolutions/gap_events/gap_resolutions/version_approvals [运行验证]
- `data/paper_trading.db`：4KB 空遗留库，非运行库 [运行验证]
- BOOK 结果库: knowledge/research_results.sqlite3（侧车默认）[代码确认]

## 5. 主要 API（backend/app.py）[代码确认]
/api/v1/health, /system/status, /market/{candles,sources,quote,orderbook,snapshot,snapshot/image}, /agents, /agents/{agent}/account, /agents/{agent}/decisions, /fills, /trades, /trades/{id}, /journal, /strategies, /strategies/{agent}/{versions,diff}, /strategies/C/import, /evolutions, /evolutions/{id}/comparison, /audit/events, /e/{accounts,observations,quant-observations}, /research/book-pa/latest
- 注：/api/v1/status、/paper/status 不存在（404）[运行验证]

## 6. 前端模块 (frontend/src) [代码确认]
App.tsx、Chart.tsx、DecisionLog、StatisticsPanel、EPanel（E 正式/影子）、RecoveryStatus、CounterfactualView、BookPaPage+bookPaModel（BOOK）、b40Research.json、api.ts

## 7. 数据流 [代码确认]
Alpaca WS/REST → market.py 报价/15m 收盘 → candle_archive → rule_cycle/scheduler 生成 A/B/C 信号 → execution.py 按报价侧成交 → ledger.py 写 fills/ledger_events → evolution 计数 → 前端经 API 展示。影子 worker 独立读行情写 shadow-ledger。

## 8. 相关既有文档
docs/ARCHITECTURE.md, docs/LINUX_ARCHITECTURE.md, docs/STRATEGIES.md, docs/KNOWN_ISSUES.md（未逐条复核 → [历史记录未复核]）
