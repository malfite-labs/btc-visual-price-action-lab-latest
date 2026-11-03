# 保活 / 心跳方案对比（只读草案）

状态：**只讨论，不落地 runtime**。本文不改策略、风控、账本、持仓、`.env`，不重启进程，不补跑已过期的 15 分钟周期。

日期：2026-11-03（Asia/Shanghai）

证据口径：

- **[代码]** 本仓库当前树，以文件行为为准。
- **[BRIEF]** 本次附件问题陈述。箱上进程是否仍是 nohup，本文没有复跑。
- **[附件]** `scheduler` 审计/成交摘录，以及 `sand-supervisor` cgroup 采样行。
- **[历史]** `codex.md` 里的 Windows 远程访问监督记录。`/remote-access/` 被 `.gitignore` 整目录忽略，本工作区没有这份源码，不能用它推翻或证实 Linux 箱上的现网命令行。

与 BRIEF 不一致的地方在下面标成 **[代码修正]**。

## 1. 问题摘要

要分开的是四件不同的事，而不是一个「服务不健康」：

| 类型 | 这次证据里像什么 | 进程还在吗 | 会不会写出 `scheduler_cycles` 行 |
|---|---|---|---|
| 宿主挂起（box suspend） | 上海时间约 04:22–06:01，约 101 分钟。cgroup 采样间隔约 6068s（常态约 62s），`stalled=0.0%`。窗口内文件 mtime 与流恢复日志一起停摆 **[BRIEF/附件]** | 挂起期间整箱时钟停走。恢复后 PID 仍可被 `kill -0` 看成活着 | 不会。循环根本没跑 |
| 进程退出 | 本次漏根窗口没有对应的崩溃证据被放进附件 | 不在。`supervise` 只认这一种 | 退出期间不会写。拉起之后只处理新的收盘 |
| 应用卡死 | 本次附件没有把「宿主仍在走、仅 uvicorn 无响应」证成根因 | PID 还在，但健康检查超时 | 不会写新周期 |
| 行情门控 / 过期跳过 | 18:31 UTC 有 **1** 条 `MARKET_RECOVERY_NOT_VALIDATED` **[附件]**。其余缺行多数连这条审计都没有 | 进程在，HTTP 可以是 200 | 门控写 `audit_events`，**不**插入周期行。`CYCLE_EXPIRED` 连审计也不写 |

### 1.1 已核对的时间线

时间用 UTC，上海时间 = UTC+8。

- 大挂起：**2026-11-02 20:22–22:01 UTC**（上海 11-03 04:22–06:01）**[BRIEF]**。
- 这段里缺的周期行是 **20:30、20:45、21:00、21:15、21:30、21:45**。两侧有行：20:00 DONE 于 20:13:01，20:15 DONE 于 20:15:07，22:00 DONE 于 22:02:44 **[附件]**。
- **18:30–19:45 的六根不在这次大挂起里。** 附件审计从 18:00 起只有一条门控：`MARKET_RECOVERY_NOT_VALIDATED` @ 18:31:18。门控每挡住一根就会写一条系统审计 **[代码]** `process_closed_bar` → `blocked()` → `ledger.record_system_event`。六根里只有一根对得上。其余五根更像「没进入 `process_closed_bar`」。cgroup 摘录里还有 2960s / 4114s / 5756s 的采样间隔，但**没有时间戳**，不能改写成更早的挂起证明。
- 恢复之后缺的是单根：22:30、22:45、23:00、23:30；相邻的 22:00、22:15、23:15、23:45、00:00 是 DONE **[BRIEF]**。这不是同一次连续冻结。20:00 那根用了约 13 分钟才 DONE，说明单根处理可以吃掉 15 分钟窗口的大部分；下一根若在 `now >= close+15min` 才被看到，会走 `CYCLE_EXPIRED`，同样没有周期行。这是代码允许的路径，**不是**已经证明的根因。
- B 止损：`monitor_stops` 报价路径，22:02:00 `STOP_TRIGGER`，不依赖周期行 **[附件 + 代码]**。
- C 时间退出：`due_at=20:30`，`observed_closed_bar=22:00`，22:02:42 才记 `RULE_TIME_EXIT_DUE`，22:02:44 平仓 **[附件]**。到期 K 线在挂起期间没有被观察到。
- BRIEF 的最新检查（本仓库未复跑）：上海 08:06，feed LIVE，scheduler ENABLED，最后 DONE 为 00:00 UTC，完成于上海 08:03:51。

过期根是 `CYCLE_EXPIRED`。水位注释写明 skipped bars never replay。**不补跑。**

### 1.2 代码上「没有行」的五条路

下面几条都会让 `scheduler_cycles` 里缺少 DONE/FAILED/RUNNING。只看缺行不能区分挂起和门控。

1. **宿主挂起。** `market_loop` 在 `await asyncio.wait_for(stop_event.wait(), timeout=60)` 里一起被冻住。恢复后只取**最新一根** `candles[-1].close_at`，向 `.run/market-decision-watermark.json` 跳水位。中间根不会逐根派发 **[代码]** `backend/app.py` 的 `market_loop`。
2. **`CYCLE_EXPIRED`。** `now >= close + 15min` 时，`PaperCycleScheduler.run_closed_cycle` 在 `INSERT` 之前返回，不写表 **[代码]** `backend/scheduler.py`。
3. **门控。** `market_open_gate_reason` 非空时，`blocked(gate_reason)` 写审计、返回，不调用 `run_closed_cycle`。原因包括 `MARKET_DATA_INCOMPLETE`、`MARKET_STREAM_DISCONNECTED`、`MARKET_RECOVERY_NOT_VALIDATED`、`MARKET_STREAM_QUOTE_STALE` **[代码]** `backend/market.py`。
4. **水位已见。** `claim_closed_bar_for_dispatch` 对 `close <= previous` 返回 `CYCLE_ALREADY_SEEN`，不再调用 `process_closed_bar`。
5. **调度未武装。** 返回 `PAPER_AUTOMATION_DISABLED` / `SCHEDULER_DISABLED`，也不插入周期行。

`/api/v1/health` 只返回 `status`、`version`、`live_trading_disabled`。**200 不等于周期新鲜，也不等于 WebSocket 已恢复。** 周期与恢复态在 `/api/v1/system/status`（`scheduler`、`websocket`、`websocket_recovery_state`、`websocket_quote_health`、`feed_health`）。

### 1.3 止损和时间退出不是同一条路

- B 的止损在 `quote_loop` / 行情回调里调用 `monitor_stops`，不读周期表 **[代码]**。宿主挂起时报价循环同样被冻住，所以止损会拖到恢复后的第一笔可用报价。它**不是**被 `supervise` 漏掉的子进程。
- C 的 `RULE_TIME_EXIT` 要等某根收盘进入 `mark_time_exits`。该调用在 `process_closed_bar` 里、**门控之前**。门控挡住的是随后的 `run_closed_cycle`（因此没有周期行、也没有新开仓）。若到期标记已经写上，下一次报价上的 `monitor_targets` 仍可能平仓。这次 20:30 的到期没有提前标记，是因为那根 K 线没被观察到，不是因为门控把已标记的退出扣住了。
- **[代码修正]** `market_open_gate_reason` 的注释写「exits do not use this gate」。报价止损和目标价退出确实不走这道门。时间退出的**登记**却依赖 `market_loop` 看到收盘。保活文档不能把这句注释理解成「挂起期间时间退出仍会按时成交」。

## 2. 现状能力

### 2.1 `scripts/linux/supervise.sh` 覆盖谁

组件枚举只有 `backend`、`frontend`、`shadow` **[代码]**。

| 行为 | 事实 |
|---|---|
| 单实例 | 每个组件 `flock` 一把 `run/<name>.lock` |
| 重启条件 | 子进程退出（`kill -0` 失败）。然后写 `logs/crash/`，退避 5s 到 300s，稳定超过 600s 后退避复位 |
| 稳态「健康探测」 | **[代码修正]** 注释写了「健康探测」，循环里实际只有 `kill -0` 和每 300s 轮转日志。启动时仅 backend 等待一次 `/api/v1/health` |
| 卡死的活进程 | 不重启 |
| 宿主挂起 | 守护进程的 `sleep` 一起停。恢复后 `kill -0` 仍为真，不会补拉、也不会补周期 |
| shadow | 启动前等 backend health。`HALTED_POSITION_GAP` 是策略态，守护不改 `status.json` |
| 开机 | `docs/LINUX_RUNTIME.md`：本机没有可用的 systemd init，重启云电脑后要手动 `supervise.sh start` |

`stop.sh` 只按 pidfile 杀子进程，**不**写 `run/<name>.stop`。因此在 supervise 仍持锁时，`stop.sh` 杀掉的 backend 会被守护循环再次拉起。`start.sh` 再抢同一把锁会失败。

### 2.2 cloudflared 与 JWT proxy 不在这把守护里

- **[代码]** `supervise.sh` 没有 proxy、没有 cloudflared。`.gitignore` 忽略 `/remote-access/`。本工作区无法审阅 `proxy.py` / `tunnel.yml`。
- **[BRIEF]** 现网二者是 nohup，不在 `supervise.sh` 循环内。代码不能推翻这句话，也没有第二份 Linux 启动脚本证明它们已被守护。
- **[历史]** Windows 曾用计划任务跑 `remote-access/run_remote_access.ps1`：监督 `127.0.0.1:18774` 与名为 `btc-visual-lab` 的隧道；无令牌时期望本机 **403** 才算代理健康，并有独立健康端口。那是 PowerShell 监督器，不是 `scripts/linux/supervise.sh`。搬到 Linux 之前必须在箱上只读核对命令行，避免和现有 nohup 双开出第二条隧道。

远程入口挂了，只影响箱外能不能看见页面。它不负责 15 分钟周期，也挡不住宿主挂起。

### 2.3 已有探活，以及它们为什么不能直接拿来用

| 机制 | 实际做什么 | 对这次问题 |
|---|---|---|
| `scripts/p0_watchdog.py` | 约 30s 探测 `127.0.0.1:8011/api/v1/system/status`、5174、正式库只读 `quick_check`、最近一条 DONE 的周期年龄。连续 3 次**完全连不上**才备份并调用恢复脚本。`cycle_age>1800` 或行情非 LIVE 只标 `DEGRADED`，不重启。恢复态 `RETRY/RECOVERING/COOLDOWN/VALIDATING/LONG_PROTECTION` 以及一批 websocket 非 CONNECTED 状态标成 `MARKET_RECOVERY_MANAGED`，**故意不重启** | 不在 `supervise.sh` 里。`docs/team-knowledge/PROJECT_STATUS.md` 把「恢复 Watchdog」列为暂缓。挂起期间它自己也不跑 |
| `scripts/linux/recover_backend.sh` | **强制** `PAPER_AUTOMATION_ENABLED=false`，`BTC_LAB_OFFLINE` 默认 1。然后 `stop.sh` + `start.sh`，注释写明不解除 `LONG_PROTECTION` | **[代码修正]** 不能把它当成「现成的保活恢复」。在已武装的正式盘上调用，意图就是卸掉调度。supervise 持锁时 `start.sh` 还会抢锁失败，净效果往往只是一次计划外的杀进程 + 守护再拉起 |
| `scripts/health_check_readonly.py` | 只读打 8011/5174，把结论追加到 `logs/health-monitor-*.jsonl`。阈值：决策 `candle_close` 年龄不在 0–2100s 则记 `FORMAL_CYCLE_STALE_*` | **[代码修正]** `main()` 里 `import msvcrt`。在 Linux 上尚未做检查就会 `ImportError`。仓库里也没有 cron / systemd timer 调用它 |
| `/api/v1/health` | 进程能响应即 `ok` | 不能发现漏根、门控、挂起 |
| `/api/v1/system/status` | 有恢复态和调度开关 | 只有进程在跑事件循环时才会更新。挂起期间外部看到的是超时，不是一份「stale JSON」 |
| `deploy/systemd/*.service` | 单元注释写明本宿主不是 systemd PID1。`Restart=on-failure`。环境默认 `BTC_LAB_OFFLINE=1`、`PAPER_AUTOMATION_ENABLED=false` | 拷上去直接 enable 会把现网打回离线且关闭调度。`on-failure` 同样不处理「进程还在但整箱被暂停」 |
| 行情恢复 | `STREAM_IDLE_TIMEOUT_SECONDS=30`。失败进入 `LONG_PROTECTION`，跨重启从恢复历史文件还原。解除条件是本连接新鲜报价 **且** 288 根连续，REST 新鲜不能单独顶替 **[代码]** `backend/market.py` | 这是保护，不是宕机。用「周期变旧」去重启 backend，会在保护期间再打一条 Alpaca 连接 |

箱内没有独立心跳文件，也没有箱外探针的配置。

## 3. 方案对比

优先级是给所有者拍板用的实施顺序，不是本文已经做的事。数字越小越先做。**「需再授权」都是「要改运行环境之前」。本 PR 没有实施其中任何一项。**

| 方案 | 覆盖面 | 改动面 | 风险 | 再授权 | 优先级 |
|---|---|---|---|---|---|
| A1 箱内只记心跳，不重启 | 恢复**之后**区分「墙钟跳了一大段、PID 一直在」和「进程曾经退出」 | 新的只写文件小进程，或在现有循环旁路写 mtime。不改账本 | 挂起期间写不出来，不能告警、不能唤醒。写进交易进程会把心跳和策略版本绑在一起 | 要。即使只写 mtime 也是运行中的新进程/新代码 | 4 |
| A2 把 cloudflared + JWT proxy 纳入**单独**的进程守护 | 远程入口进程退出、端口不再监听 | 新的 Linux 循环或等价脚本。先只读记录现网命令行。不碰 backend 锁 | 与现有 nohup 双开第二条隧道；健康判据若从 Windows 的「403 才健康」抄错，会反复打死正常代理（历史已发生过误判） | 要。含隧道进程的启停权 | 3 |
| A3 用 backend 周期活性去重启 | 表面想补「活着但不跑周期」 | `supervise.sh` 或 watchdog 的重启条件；实际会调用到 `recover_backend.sh` / `stop.sh` | 门控、慢周期、挂起恢复瞬间都会被误杀。可能重复 WebSocket、406、`LONG_PROTECTION`，并把 `PAPER_AUTOMATION` 打成 false | 要，且**不建议批准** | 不做 |
| B 箱外探活 | **挂起发生的当时**就能看见「整箱或隧道不可达」。宿主还在走、只有应用无响应时，探针仍能连上别的端口或看到 health 超时，从而和应用卡死分开 | 箱外 cron/工作流。只读 GET。告警收件人。不在本仓库里改 `.env` | 公网 Access 未登录 **302 只证明边缘**，不证明 8011，更不证明周期。探针不能在冻结期间成交。若平台把入站流量当成空闲保活，那是平台政策假设，代码证明不了 | 要。落点、频率、URL、谁接收告警 | 1（只检测） |
| C 换更稳宿主，或让平台不要暂停这台 sandbox | 唯一从根上减少「整箱冻结 → 漏根 → 止损/时间退出推迟」 | 平台工单或迁机。迁机时还要碰部署路径、systemd 是否真是 PID1、Alpaca 单连接 | 双实例会 406 并进入 `LONG_PROTECTION`。systemd 模板的默认环境是离线且调度关闭。迁数据不是原子一致 | 要。迁机本身是一次 cutover | 2（先问平台，先别迁） |
| D 只读四分类（推荐的检测形态，见下） | 把挂起、进程退出、应用卡死、门控、过期跳过分成不同告警 | 箱外探针记录 + 恢复后对照现有审计/周期表/水位/crash 日志。可以先是一份操作核对，不必先写新服务 | 误把门控告警成宕机，诱使有人重启。核对 SQL 必须只读 | 箱外探针要授权。只读核对可以沿用现有运维授权，仍不得重启 | 1 |

### 3.1 A. 运行时内心跳 / 扩展 supervise

分成三截，不要合成一个「把保活做进 supervise」。

**A1 只记不拉。** 独立进程每 30–60s 写一行墙钟，或只 `touch` 一个心跳文件。恢复后若墙钟缺口与文件 mtime 缺口对齐、且没有 `logs/crash/`，就是挂起取证。若心跳仍在跳而 `/api/v1/health` 超时，才是应用卡死。挂起的那一段里，这个进程同样不会醒。

**A2 只给远程入口补「进程退出才拉起」。** 与 backend/frontend/shadow 分开锁、分开目录。不要复用 `recover_backend.sh`。健康判据沿用历史结论时，必须在 Linux 上重做一次只读验证：无令牌打到代理应快速拒绝，不能把 200/5xx/连接失败都当成该重启。backend 的行情恢复仍交给 `backend/market.py`。

**A3 不要做。** watchdog 已经把「周期变旧」和「恢复中」排除在重启之外。`supervise` 的 `kill -0` 也故意不解释应用语义。把二者接上，等于拆掉这两道已经写明的保护。

### 3.2 B. 箱外探活 / 保活

箱外探针用**自己的墙钟**发只读请求。建议两条信号，分开记：

1. **整箱或隧道不可达：** 连续超时，恢复后箱内日志/文件 mtime 出现同一段空洞，PID 没有换。这是挂起（或网络/隧道一起死）。单靠这次还分不清「平台暂停」和「云到箱的网络断了」，要和箱内 mtime 空洞对照。
2. **应用无响应：** 探针还能到达宿主（或隧道还在，但源站 health 超时），uvicorn pidfile 里的 PID `kill -0` 仍真，且 `logs/crash/` 没有新文件。这是卡死。`supervise` 今天看不见它。

不要把下面任一结果叫做宕机：

- health 200，`scheduler=ENABLED_INTERNAL_PAPER`，`websocket_recovery_state` 不是 `HEALTHY`，审计里有门控种类，周期表没行。
- health 200，水位向前跳了，周期函数若被调用会返回 `CYCLE_EXPIRED`，审计里没有门控种类。

「保活」若是指用外网包把空闲机器叫醒：附件里的 `[sand-supervisor]` 采样间隔本身变成数千秒，且 `stalled=0.0%`，更像**采样者与客机一起停了**，不是客机里有进程在空转。客机内的心跳打不出去。外网包能否阻止平台暂停，要问宿主平台，不能写进应用配置就算数。

公网探测的最小只读目标：

- 未登录 Access 返回 302：只说明 Cloudflare 边缘还在。
- 要证明源站进程，必须走已经允许的只读路径（经现有 JWT 的 GET health，或箱外到箱内的运维通道）。不要为了探针放宽 Access，不要发 POST，不要带 `backfill`。

### 3.3 C. 换宿主

本 box 的运行手册写明没有 systemd init，重启后要人工 `supervise.sh start`。平台采样器出现数千秒的空洞，应用层无法自救。

建议顺序：

1. 向平台确认这台 sandbox 是否会被暂停、空闲策略是什么、入站流量算不算活动、暂停时客机时钟是否停止。
2. 只有确认「会冻客机且无法关闭」之后，才把迁机当成实施项。
3. 迁机时单独做 cutover：先停旧实例的 Alpaca 连接，再启新实例；不要启用 `deploy/systemd` 里的离线默认环境；不补历史周期；影子 `HALTED_POSITION_GAP` 保持原样。

在平台答复之前，C 的优先级高于「继续加箱内守护」，低于「先把检测做出来」。检测结果就是迁机理由。

### 3.4 D. 从代码里得出的混合做法

这是推荐的检测形态，不是新的交易功能。

恢复后对每一根缺失的 15 分钟 close，只读归类：

| 归类 | 对照 |
|---|---|
| `HOST_SUSPEND` | 箱外探针（或平台采样）有一段不可达；箱内该时段没有新的日志 mtime、没有 crash 文件、没有审计；恢复后 PID 仍在；水位从缺口前跳到缺口后的某一根 |
| `PROCESS_EXIT` | `logs/crash/` 或 supervise 日志有 `exit=`；pid 变过；HTTP 曾不可达，而宿主上其他时钟仍在走 |
| `APP_HANG` | PID 一直在，宿主/箱外通道还通，health 超时，没有 crash 行 |
| `MARKET_GATE` | health 200，`system/status` 里恢复态或行情不是 LIVE/FRESH/HEALTHY，`audit_events.kind` 是门控原因，**没有**周期行 |
| `EXPIRED_OR_SKIPPED` | 水位已越过该根，没有门控审计，也没有周期行。`run_closed_cycle` 在过期时不写表。**留下缺口，不补** |

20:30–21:45 用现有附件可以归到 `HOST_SUSPEND`（与 20:22–22:01 的挂起重叠，22:00 在恢复后的窗口内 DONE）。18:30 更像至少一根 `MARKET_GATE`。18:45–19:45 与恢复后的单根缺失，附件不够，标成未归类，不要一律写成门控或挂起。

实施时不要新写一张会改交易行为的表。缺行本身就是记录。若以后要加只读注释，那是另一项授权，且不能触发派发。

## 4. 分阶段落地

检测挂起和应用挂死使用**不同信号**。两阶段都只告警。

### 阶段 0 — 本 PR

对比文档。运行中的进程、配置、数据库都不动。

### 阶段 1 — 检测宿主挂起

- 箱外按固定间隔只读探测，记录自己的墙钟、超时还是成功、HTTP 状态。
- 告警条件：连续失败超过一个探测间隔，且随后箱内文件 mtime / 行情恢复日志出现对齐的空洞。
- 不做：重启、补周期、解除 `LONG_PROTECTION`、改水位。

通过标准：能指出一段「箱外不可达 + 箱内时间空洞 + PID 未换」，并把它和「health 200 但缺周期行」分成两条告警。

### 阶段 2 — 检测应用卡死

- 前提：阶段 1 的探针或另一条到宿主的通道仍然成功。
- 告警条件：`/api/v1/health` 超时，pidfile 中的 PID 仍存在，`logs/crash/` 没有新文件，supervise 因为 `kill -0` 成功而没有重启。
- 与门控的分界：若 health 是 200，改查 `system/status` 与审计，归入阶段 3，**不要**发「进程死了」。

通过标准：人为区分（文档演练即可，不必在现网杀进程）「超时且 PID 在」和「超时且整箱无 mtime」。

### 阶段 3 — 门控与过期不算保活失败

缺周期行时先查：

- `audit_events` 是否已有 `MARKET_*`、`CLOSED_WINDOW_*`、`ALPACA_EXECUTABLE_DATA_UNAVAILABLE`、`PROTECTIVE_EXIT_RECOVERY_PENDING`。
- `websocket_recovery_state` 是否为 `HEALTHY`，`websocket` 是否为 `CONNECTED`，报价是否 `FRESH`。
- 水位是否已经跳过该根。

有门控审计：保护在起作用。没有审计且水位跳过：过期或没派发。两者都**不**重启 backend，**不**补跑。

### 阶段 4 — 所有者点名之后才改运行

按优先级 2、3 实施，且一次只做一件：

- 平台确认会暂停客机：再决定关暂停或迁机。
- 阶段 1/2 证明远程入口自己退出过：再把 cloudflared 与 JWT proxy 放进单独守护。
- 阶段 2 反复出现应用卡死：再讨论「health 超时才重启 backend」。重启路径必须继承现网的 `BTC_LAB_OFFLINE=0` 与 `PAPER_AUTOMATION_ENABLED=true`，不得调用现成的 `recover_backend.sh`，且恢复态非 `HEALTHY` 时不杀进程。

不进入阶段 4 的项：A3，补跑过期 cycle，把 `RULE_TIME_EXIT` 改成不看收盘的独立时钟。最后一项会改变出场语义，不属于保活。

## 5. 不能解决什么

- 纯进程 `supervise` 挡不住宿主 suspend。冻结前后 `kill -0` 都为真，守护的 `sleep` 只是被推迟执行。
- 箱内 cron、箱内心跳、watchdog 的 30s 循环，在客机时钟停止时都不会运行，因此也不能把机器叫醒。
- 外网探测可以发现不可达，不能在冻结期间替 `monitor_stops` 成交，也不能替 `mark_time_exits` 观察 20:30 那根 K 线。B 的止损和 C 的时间退出仍会等到恢复之后。
- 把 cloudflared / proxy 收进守护，只能减少「箱外打不开页面」。它不产生 `scheduler_cycles` 行。
- `/api/v1/health` 的 200 和 Access 的 302 都不能证明下一根 15 分钟 K 线会被派发。
- 任何方案都不应该、也不能安全地补写已过期的周期。水位与 `CYCLE_EXPIRED` 是为了禁止重放。
- `health_check_readonly.py` 在修好 `msvcrt` 之前不能在这台 Linux 上当探活。即使修好，它也只是箱内检查，挂起时一样不跑。
- 启用仓库里的 systemd 模板不能当作保活上线：默认离线、调度关闭，且本宿主没有 systemd init。

## 6. 落地前的再授权清单

批准某一项才做某一项。

1. 箱外探针的位置、间隔、只读 URL、告警送到哪里。明确禁止 POST、禁止放宽 Cloudflare Access、禁止 `backfill=true`。
2. 是否在箱内增加只写心跳文件的进程（A1）。默认可以先不做，阶段 1 的 mtime 空洞已经够用。
3. 是否允许只读登录现网，核对 cloudflared 与 JWT proxy 的命令行、端口、是否已有监督器，以便以后做 A2。本项核对本身也不要杀进程。
4. 是否向平台开单询问 sandbox 暂停政策；是否另案批准迁机。
5. 明确**不**包含在上述授权里：修改 `config/rule_v26`、风控、撮合、账本；调用 `recover_backend.sh`；启动 `p0_watchdog.py`；因 `DEGRADED` 或门控重启 backend；补跑 18:30–19:45、20:30–21:45 以及恢复后缺的那些根；改写水位文件；解除影子 `HALTED_POSITION_GAP`。

## 7. 本文件没有做的事

- 没有改 `scripts/linux/*`、`backend/`、`remote-access`（工作区里也没有这份被忽略的目录）、部署单元、环境变量。
- 没有建议在过期窗口上补 `scheduler_cycles` 或重放决策。
- 18:45–19:45 与 22:30/22:45/23:00/23:30 的缺行仍未归类。要归类需要箱上那段时间的水位文件、`audit_events` 全量种类、以及 `sand-supervisor` 采样的时间戳。这些不在本仓库附件的完整时间轴里。
