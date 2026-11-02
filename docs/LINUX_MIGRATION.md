# Linux 迁移说明（BTC Visual Price Action Lab）

## 目标

将 Windows 版完整项目以**最小 OS/部署层改动**适配到 Grok Bot Linux 云电脑，保留：

- 正式策略 A V2.5 / B V3.0 / C V3.1
- 影子 ABC + AI 候选进化（含影子 B `FORWARD_AI_BATCH_40`）
- 撮合 `QUOTE_SIDE_FULL_FILL_V1`、手续费/滑点、止盈止损、时间退出、强平
- SQLite 账本与历史成交
- 前端监控界面

**不做**：重写量化系统、改策略逻辑、重置账户、清空持仓、自动补跑缺失 15 分钟周期、未经授权的正式切换或真实行情连接。

## 归档与接收

| 项 | 值 |
|---|---|
| 归档 | `BTC-Visual-Price-Lab-Linux.tar.gz` |
| 大小 | 120275301 |
| SHA256 | `26f62affca1114b890195f9430e95049548bc1079c10949baccb2fb61de5faf1` |
| 受控根 | `/home/box/btc-visual-price-lab`（chmod 700） |
| 只读参考树 | `extract/BTC-Visual-Price-Lab` |
| **运行根** | `runtime/app` |
| 审计附档 | **`LINUX_AUDIT_PARTIAL.md` 未随附且树内未找到**；改用 `codex.md`、`docs/*`、`MIGRATION_MANIFEST.json`、`incoming/snapshot-report.json` |

归档为 **Windows 仍在运行时** 的快照，多库非跨库原子一致 → 仅用于部署/恢复验证，**不能**当作最终实盘状态。

## 环境差异与适配

| 项 | 实际 | 处理 |
|---|---|---|
| 发行版 | Debian 13 (trixie) x86_64 | 按 Debian/Ubuntu 系脚本编写 |
| Python | **仅** 3.13.5 | venv + 直接依赖安装（`pip install -e .` 已知不可靠） |
| Node | v24.22.1 / npm 11.21.1 | frontend `npm ci/install` + `npm run build` |
| systemd | **PID1 非 systemd**，无 user bus | 提供 `deploy/systemd/*.service` **模板**；本机用 `scripts/linux/*` + `flock`/pidfile |
| 端口 | 8011/5174 可用 | 默认绑定 `127.0.0.1` |

## 离线默认（强制）

- `BTC_LAB_OFFLINE=1`（默认）→ `ALPACA_CREDENTIALS_FILE` 指向空凭证桩 → `market_task_startup=BLOCKED_CREDENTIALS`
- `PAPER_AUTOMATION_ENABLED=false` → 调度器 `DISABLED_NOT_ARMED`
- **禁止**额外真实 Alpaca 连接；`start all` **不**自动拉起影子 Worker
- 正式 cutover：`REAL_CLOUD_CUTOVER` 须用户明确授权，并换用 Windows **受控停机后**最终一致性快照

## 已完成步骤摘要

1. 校验归档大小与 SHA256  
2. 解压至隔离目录并同步到 `runtime/app`  
3. 收紧 `.env` / Alpaca / remote-access 凭证权限（chmod 600；不在日志打印密钥）  
4. 创建 `.venv`，安装 Python 依赖；前端构建  
5. SQLite `quick_check`；冻结清单 20/20；正式版本从 `config/rule_v26/profile.json` 读取  
6. Linux 脚本 + systemd 模板 + 离线验收证据  
7. 最小代码适配：`scripts/p0_watchdog.py` 在非 Windows 上调用 `scripts/linux/recover_backend.sh`

## 正式切换前置（尚未执行）

见 `LINUX_RECOVERY.md`。当前 `REAL_CLOUD_CUTOVER = NO`。
