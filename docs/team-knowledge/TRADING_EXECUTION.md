# TRADING_EXECUTION — 交易执行、账户及风险机制
generated_at: 2026-11-03 00:25 Asia/Shanghai
evidence_policy: 每条结论标注 [代码确认]/[运行验证]/[历史记录未复核]/[不确定/未实现]；运行状态引用前须重新核查实时数据；文档与代码冲突以本轮核实的代码与运行证据为准。本文件只读整理，未改任何代码/配置/策略。

## 1. 行情 [运行验证]
- 源 ALPACA_US_BTCUSD_SPOT（USD_RESEARCH）；WS CONNECTED，recovery_state HEALTHY，quote FRESH；REST PASS
- WS 恢复机制含 round/attempts/cooldown/protection_reason 字段（market.py）[代码确认]
- 运行态 BTC_LAB_OFFLINE=0, BTC_LAB_MARKET_MODE=LIVE_CREDENTIALS_FILE（进程环境）[运行验证]

## 2. K 线与周期
- 15 分钟 K 线；candle_archive OK，gap_count_288=0 [运行验证]
- 每根收盘触发一次决策，重复周期返回 CYCLE_ALREADY_SEEN [运行验证]

## 3. 执行与成本 [代码确认 + 运行验证:fills.cost_model_json]
- execution=MARKET_TAKER，fill_model=QUOTE_SIDE_FULL_FILL_V1（按 bid/ask 不利侧全量成交）
- taker_fee_rate 0.0005，maker_reference 0.0002，slippage_per_fill 0.0002，fee_rounding USD_CENT_UP，funding NOT_INCLUDED，参考 BINANCE_USDT_M_FUTURES
- 现金路径仅 OPEN_FEE + CLOSE_PNL；fills.slippage 为已嵌入成交价的标注，不二次扣款（backend/ledger.py:397 注释 "do not charge it twice"）
- 资金费未计入 → 杠杆/持仓成本被低估 [代码确认]

## 4. 风控
- 单笔风险 risk_fraction 0.02；A/B 1x，C 5x（profile.json）[代码确认]
- 止损监控 QUOTE_EVENT_INTERNAL_PAPER_STOP_MONITOR（报价事件触发）[运行验证]
- 启动持仓检查 startup_position_monitor_checked=true [运行验证]
- B 当前 available_cash 49.19，used_margin 9068.06（近满仓保证金占用）[运行验证]
- LIVE_TRADING_DISABLED=true，只做内部纸面 INTERNAL_PAPER_LEDGER [运行验证]

## 5. 账户隔离
- 正式与影子分库（data/paper.sqlite3 vs shadow-ledger.sqlite3）；A/B/C/E 各自 accounts 行，初始 10000 [运行验证]
- 账户有 version 乐观版本号（B v119）[运行验证]

## 6. 进化 / 晋级
- 正式/ABC 影子: EVOLUTION_BATCH_SIZE=50（backend/settings.py:17），evo_counts next_batch=50 [代码确认][运行验证]
  - 正式 closed: A1 B42 C18；影子 closed: A2 B40 C20 [运行验证]
- E: next_review_at=40 [运行验证]
- ⚠ 团队口径"40 笔触发"仅对 E 及影子 B 已达 40 成立；ABC 代码批量为 50。须所有者/总指挥统一口径 [不确定]
- 影子版本审批表 forward_version_approvals、forward_reviews [代码确认]；晋级须所有者授权 [历史记录未复核]

## 7. BOOK 模块
- book_pa_research.py：知识文件加载、prepare_research、validate_analysis(_v03)、ResearchVisionModel 可选模型适配、latest_research [代码确认]
- book_pa_prices_v02.py 价格解析；book_pa_chart_v03.py 编号图渲染；book_pa_sidecar.py 本地侧车 8813 [代码确认]
- 前端 BookPaPage.tsx；API /api/v1/research/book-pa/latest [代码确认]
- 限制：视觉模型调用禁用（max_requests_per_day 0、预算 0）；侧车未运行；不参与正式下单 [运行验证]
- 研究成果见 docs/reports/BOOK_PA_*（V0.1 集成、V02 质量、V03 收尾等）[历史记录未复核]
