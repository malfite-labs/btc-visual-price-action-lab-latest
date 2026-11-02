# Linux 恢复与正式切换

## 当前快照性质（重要）

本归档在 **Windows 进程仍运行** 时制作。正式库与影子库、K 线库之间**非跨库原子一致**。

已观测到的迁移态（只读，未改写）：

- 正式：`paper.sqlite3` — A/C FLAT；**B OPEN_SHORT**（含止损元数据）
- 影子：`shadow-ledger.sqlite3` — ABC FLAT；B `forward_versions` = **FORWARD_AI_BATCH_40**；`evo_counts` B=40/50；**未结案缺周期** `2026-11-02T04:45:00+00:00` / `MISSED_BAR_WHILE_POSITION_OPEN`
- 影子 `status.json`：`HALTED_POSITION_GAP`（保留，不自动解除）
- 冻结清单：策略+风控执行哈希 **20/20** 匹配
- 正式规则版本：A V2.5、B V3.0、C V3.1（`config/rule_v26/profile.json`）

**不得**用本快照直接当作 cutover 后的实时真相；**不得**自动补跑迁移窗口内缺失的 15 分钟周期。

## 离线恢复验证（已做）

1. SQLite `PRAGMA quick_check`  
2. 账户/持仓/进化进度只读查询  
3. 冻结哈希核对  
4. `BTC_LAB_OFFLINE=1` 启动 backend/frontend，确认无真实 Alpaca  
5. 进程 flock 互斥与重复 start 保护  
6. 单元测试：撮合、规则周期、自动保护、恢复守卫  

证据目录：`docs/reports/linux-offline-acceptance/`

## 正式切换顺序（须用户明确授权）

1. Linux 离线验收通过  
2. 核对 Windows 最新正式与影子持仓  
3. **受控停止** Windows 正式调度器与影子 Worker  
4. 确认旧 Alpaca 行情连接已退出  
5. 生成**最终一致性**数据库与状态备份（Windows）  
6. 同步最终数据到 Linux `runtime/app`（覆盖验证用快照）  
7. 核验账户、持仓、冻结哈希、进化进度、缺周期结案状态  
8. 启动唯一 8011 后端 → 唯一影子 Worker → 5174 前端  
9. 检查行情、LONG_PROTECTION、影子缺周期  
10. 验证**新的**自然 15 分钟周期（不补造历史）

## 仍需 Windows 端提供的最终状态文件

- 受控停机后的 `data/paper.sqlite3`（及 WAL/SHM 若存在，需 checkpoint 后拷贝）
- 同步时刻的 `docs/reports/abc-forward-v26/shadow-ledger.sqlite3`
- `data/market-candles.sqlite3`（或约定的 K 线快照）
- `docs/reports/abc-forward-v26/status.json`、冻结清单与任何未提交的 `forward_*` 附属状态
- 停机证明：Windows 侧无残留 Alpaca WS / 无第二调度器
- （可选）`snapshot-report.json` 更新版，标明停机一致时间戳

当前：`REAL_CLOUD_CUTOVER = NO`。

## 云端独立长稳：恢复
- 以归档快照为独立起点（启动前快照见 docs/reports/linux-cloud-independent/pre-start-snapshot.json）。
- Alpaca 406 CONNECTION_LIMIT_EXCEEDED：系统保持 LONG_PROTECTION、暂停正式新开仓，由既有串行恢复机制重试；不要手动解除。多半因同一密钥在其他地方（如 Windows 实例）仍持有连接；释放后自动恢复并记录 recovery_succeeded。
- 不补跑缺失 15m 周期；崩溃后状态从 SQLite 与 status.json 继续。
