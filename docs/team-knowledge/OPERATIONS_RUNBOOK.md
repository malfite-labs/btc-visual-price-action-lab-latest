# OPERATIONS_RUNBOOK — 运维、故障处理与恢复
generated_at: 2026-11-03 00:25 Asia/Shanghai
evidence_policy: 每条结论标注 [代码确认]/[运行验证]/[历史记录未复核]/[不确定/未实现]；运行状态引用前须重新核查实时数据；文档与代码冲突以本轮核实的代码与运行证据为准。本文件只读整理，未改任何代码/配置/策略。

## 1. 脚本 (scripts/linux/) [代码确认]
common.sh（环境；默认 BTC_LAB_OFFLINE=1）、install.sh、start.sh、stop.sh、status.sh、supervise.sh、verify.sh、recover_backend.sh
- 本 Debian box 无 systemd 启用，依赖 supervise.sh + flock [运行验证]
- RUN_DIR=/home/box/btc-visual-price-lab/runtime/run；日志 runtime/logs/{backend,frontend,shadow}[-error].log 及 crash/ [运行验证]
- Windows 脚本 scripts/*.ps1（start_lab/stop_lab/recover_backend/rollback_rule_v26/switch_formal_paper_mode）仅 Windows 生产适用 [代码确认]

## 2. 日常检查（只读）
1. `bash scripts/linux/status.sh` — 进程/端口/health/影子 status.json
   - ⚠ status.sh 头部 OFFLINE/PAPER_AUTOMATION 显示的是**调用者 shell 的 common.sh 默认值**，不是运行进程实际环境；实际以 `/proc/<backend pid>/environ` 或 /system/status 为准 [运行验证]
2. `curl -s 127.0.0.1:8011/api/v1/system/status` — websocket、websocket_recovery_state、scheduler、paper_gate_reason
3. `ss -ltnp | grep -E '8011|5174'`，`pgrep -af 'uvicorn|vite|run_forward|supervise'`
4. 影子：docs/reports/abc-forward-v26/ 下 status.json 与 shadow-ledger 的 forward_gap_events/resolutions
5. scripts/health_check_readonly.py [代码确认，未运行]

## 3. 故障流程
检测 → 判影响 → 保护状态（不重启、不解 halt）→ 定根因 → 最小修复方案 → 授权后执行 → 验证 → 六段报告
- 避免重复后端/重复 WS：supervise 用 flock；启动前先 status.sh 确认无同名进程 [代码确认]
- 后端恢复：scripts/linux/recover_backend.sh（需授权）[代码确认]
- 影子 POSITION_GAP：只追加 forward_gap_resolutions 证据（现金链、数量匹配、持仓 FLAT、跳过周期不改），历史 45 次均如此 [运行验证]；需所有者专项授权
- Watchdog（scripts/p0_watchdog.py）禁止擅自恢复 [历史记录未复核/约束]

## 4. 参考文档
docs/LINUX_RUNTIME.md, LINUX_RECOVERY.md, LINUX_MIGRATION.md, LINUX_ARCHITECTURE.md, RECOVERY.md, QUICKSTART.md；云端报告 docs/reports/linux-cloud-independent/（SUMMARY.txt、supervise.log、frontend-20261102T233248.txt 等）
- 宿主挂起与漏周期的保活方案对比（docs only，未实施）：docs/ops/keepalive-heartbeat-options.md
- docs/reports/LINUX_AUDIT_PARTIAL.md 缺失 [运行验证]

## 5. 密钥
云端独立 Alpaca 凭证存于控制根 secrets/（文件模式，不在文档展开）；密钥曾在聊天出现，建议所有者在 Alpaca 控制台轮换 [运行验证/建议]
