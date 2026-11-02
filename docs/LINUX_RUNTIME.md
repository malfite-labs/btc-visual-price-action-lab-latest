# Linux 运行手册

## 前提

```bash
cd /home/box/btc-visual-price-lab/runtime/app
./scripts/linux/install.sh    # 首次：venv + 依赖 + 前端 build
```

本机 **无可用 systemd 作为 init**。日常用脚本；`deploy/systemd/*.service` 仅供将来有 systemd 的主机。

## 环境变量

| 变量 | 默认 | 含义 |
|---|---|---|
| `BTC_LAB_OFFLINE` | `1` | 离线：指向空 Alpaca 凭证，禁止真实行情 |
| `PAPER_AUTOMATION_ENABLED` | `false` | 未武装正式自动交易调度 |
| `VISION_ENABLED` / `VISION_CALLS_AUTHORIZED` | `false` | 关闭视觉模型调用 |
| `ALPACA_CREDENTIALS_FILE` | 离线桩路径 | 切勿在日志打印其内容 |
| `BTC_LAB_API_HOST/PORT` | `127.0.0.1` / `8011` | 后端绑定 |
| `BTC_LAB_UI_HOST/PORT` | `127.0.0.1` / `5174` | 前端绑定 |

**授权真实行情（仅在 cutover 批准后）**：`BTC_LAB_OFFLINE=0` 且凭证文件指向真实 `data/alpaca-market-new.env`（仍勿打印密钥），并确保 Windows 侧旧连接已退出。

## 启停

```bash
./scripts/linux/start.sh all          # backend + frontend；不自动 shadow
./scripts/linux/start.sh backend
./scripts/linux/start.sh frontend
./scripts/linux/start.sh shadow       # 需后端健康；离线验收一般不启
./scripts/linux/stop.sh all
./scripts/linux/status.sh
./scripts/linux/verify.sh             # 离线验收套件
./scripts/linux/recover_backend.sh    # 供 watchdog 调用
```

## 健康检查

```bash
curl -fsS http://127.0.0.1:8011/api/v1/health
# 期望含 live_trading_disabled: true
curl -fsS http://127.0.0.1:8011/api/v1/system/status
# 离线期望 market_task_startup=BLOCKED_CREDENTIALS, paper_automation=DISABLED_NOT_ARMED
```

## 日志与锁

- 日志：`/home/box/btc-visual-price-lab/runtime/logs/`
- 锁/PID：`/home/box/btc-visual-price-lab/runtime/run/`

## systemd 模板（非本机生效）

路径：`deploy/systemd/btc-backend.service`、`btc-shadow.service`、`btc-frontend.service`。  
复制到目标机 `/etc/systemd/system/` 前请核验 `User=` 与路径，并默认保持 `BTC_LAB_OFFLINE=1` 直至 cutover。

## 云端独立长稳运行（2026-11-02）
- 启动：`BTC_LAB_OFFLINE=0 PAPER_AUTOMATION_ENABLED=true scripts/linux/supervise.sh start all`
- 停止：`scripts/linux/supervise.sh stop all`（写 `run/<c>.stop` 并终止守护，不再重启）
- 状态：`scripts/linux/supervise.sh status`；`scripts/linux/status.sh`
- 机制：每组件一个守护循环，flock(`run/<c>.lock`) 保证单实例（重复 start 会报 already supervised）；子进程退出→`logs/crash/` 落盘证据→退避 5s→300s 重启（稳定 10 分钟后复位）；日志 `logs/<c>.log` 超 50MB 轮转并 gzip，保留 14 天。
- shadow 等待 backend health 后启动；HALTED_POSITION_GAP 为策略态，守护只重启进程，不伪造 resume。
- 注意：重启云电脑后需手动再次执行 start（无 systemd）。
