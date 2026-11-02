# PROJECT_STATUS — 当前真实状态、已知问题及冻结事项
generated_at: 2026-11-03 00:25 Asia/Shanghai
evidence_policy: 每条结论标注 [代码确认]/[运行验证]/[历史记录未复核]/[不确定/未实现]；运行状态引用前须重新核查实时数据；文档与代码冲突以本轮核实的代码与运行证据为准。本文件只读整理，未改任何代码/配置/策略。

## 1. 服务（本轮 ss/pgrep/status.sh 复核）[运行验证]
| 服务 | PID | 端口 | supervise loop PID |
|---|---|---|---|
| backend uvicorn | 166897 | 127.0.0.1:8011 | 166876, 166892（两个 backend loop 进程，疑为父子 fork，待确认非重复）|
| frontend vite | 166945 | 127.0.0.1:5174 | 166906 |
| shadow run_forward_v26 | 166950 | — | 166908 |
- health ok v1.0.3；WS CONNECTED / HEALTHY（success_at 2026-11-03 00:17 上海，reconnect_count 1）；scheduler ENABLED_INTERNAL_PAPER；vision MODEL_CALLS_DISABLED
- 进程环境 BTC_LAB_OFFLINE=0, PAPER_AUTOMATION_ENABLED=true

## 2. 账户 [运行验证]
- 正式: A FLAT 9976.69 / B OPEN_SHORT 0.11665220@77735.84 stop 78115.79（trade a62feb96…，未实现 +45.50 @ mark 77291.69）/ C FLAT 9875.14 / E FLAT 10000
- 影子: A/B/C FLAT；state HALTED_POSITION_GAP

## 3. 已知缺口/问题
1. 影子未解 gap 2026-11-02T04:45:00Z MISSED_BAR_WHILE_POSITION_OPEN（gap_events 46 / resolutions 45）[运行验证]
2. Windows 生产端是否双跑未核（等待所有者授权）[不确定]
3. docs/reports/LINUX_AUDIT_PARTIAL.md 缺失 [运行验证]
4. data/paper_trading.db 空遗留库 [运行验证]
5. 进化批量口径：ABC 代码 50 vs 团队"40 笔"；E 为 40 [代码确认，口径待裁决]
6. status.sh 头部显示 OFFLINE=1 与实际进程 OFFLINE=0 不一致（显示误导，非运行错误）[运行验证]
7. funding 未计入成本 [代码确认]
8. /api/v1/strategies 显示 v1.0.0 种子版本，与实际规则档 V2.5/V3.0/V3.1 口径不同，易混淆 [运行验证]
9. 前端 2026-11-02 23:32 exit=143（SIGTERM）记录见 docs/reports/linux-cloud-independent/frontend-20261102T233248.txt [历史记录未复核]
10. B 保证金占用高（available 49.19）[运行验证]
11. 双 backend supervise loop 进程是否为正常 fork 未核 [不确定]

## 4. 冻结项（未经所有者授权不得改）
- config/rule_v26/*（profile.json、A-rule.json、v3-signals.py、baseline-md 哈希）
- strategies/*（C/E core_lock PROTECTED）
- 风控参数 risk_fraction 0.02、杠杆、止损/退出逻辑、LONG_PROTECTION
- 模拟撮合 QUOTE_SIDE_FULL_FILL_V1 与 ledger 现金路径
- 正式与影子账户持仓/历史；密钥文件；影子 halt 状态
[约束来源：所有者指令，历史记录未复核其文件级清单]

## 5. 暂缓项
- 恢复 Watchdog；Windows 只读核验；影子 gap resolution；视觉模型调用（预算 0）；BOOK 侧车上线；E-V1.1-QUANT 激活；任何晋级 [不确定/未实现]
