# BTC Paper Lab V3｜Windows Binance 行情本地模拟部署验收报告

验收日期：2026-11-20（北京时间）  
部署副本：`C:\Users\admin\Documents\量化升级版\btc-paper-lab-v3\windows-binance-deploy`  
旧项目保护：未修改旧项目文件，未操作旧版 8011 / 5174 服务。

## 本轮结论

**PRODUCTION_READINESS = PARTIAL**。已修复多处仍将行情来源写死为 Alpaca 的运行门禁，A/B/C 规则档案哈希可加载，并将 PA-01～PA-08 归档规则导入各自 10,000 美元账户。8 个 PA 账户通过隔离确定性周期：各记录一条 `WAIT`，余额保持 10,000 美元，未产生模拟成交。

Binance 实时行情、连续自然周期、当前服务进程及桌面控制台仍未完成验收。没有把隔离周期当作自然行情证据。

| 项目 | 状态 | 证据 / 限制 |
|---|---|---|
| PYTHON_DEPENDENCIES | PASS | Python 3.12.11；FastAPI、httpx、uvicorn、websockets、pytest 导入通过。部署环境通过 `.pth` 复用既有依赖。|
| FRONTEND_DEPENDENCIES | PASS（可构建） | 复用本机 `node_modules`；`npm run build` 的 TypeScript 检查和 Vite 构建通过。未验证 `npm ci` 网络重装。|
| BACKEND_8012 | NOT VERIFIED | 本轮开始时健康接口连接被拒绝；没有确认当前活动 PID。|
| WEB_5175 | NOT VERIFIED | 早前曾返回 HTTP 200；本轮未确认仍在运行。|
| DESKTOP_CONSOLE | PARTIAL | 控制台代码和定向用例存在；未实际打开窗口验收。|
| BINANCE_PUBLIC_MARKET | NOT VERIFIED | Binance USDⓈ-M REST/WebSocket 适配器和来源映射已接入；本机真实行情连接、报价新鲜度和重连未验证。|
| 15M_NATURAL_CYCLES | 0 | 尚未取得真实 Binance 15 分钟自然收盘决策证据。|
| STRATEGIES_REGISTERED | 14 个策略位 | A/B/C/D/E/G 与 PA-01～PA-08；PA-09/PA-10 未进入可执行注册表。|
| STRATEGIES_RULE_COMPLETE | 至少 11 套有归档规则 | A/B/C、PA-01～PA-08；D/E/G 特殊逻辑仍须逐项自然运行验收。|
| STRATEGIES_EXECUTION_READY | PARTIAL | A/B/C 规则档案已通过加载；PA-01～PA-08 已导入独立 10,000 美元账户并启用。自然运行和全部账户端到端状态未验收。|
| STRATEGY_TOGGLE | PARTIAL | PA 池 8 套启用状态已写入 SQLite；运行中的周期启停和重启恢复尚未验收。|
| PAPER_ORDER_ENGINE | PARTIAL | 本地模拟执行器已连接 Binance 来源门禁；未确认真实行情驱动的开平仓。没有创建 Binance 订单。|
| CAPITAL_AND_POSITION_RULES | PARTIAL | 按用户最新要求，各策略保持独立 10,000 美元资金和当前版本自己的仓位、开仓及风险逻辑；没有接入统一 100 USDT / 5x 动态满仓。|
| LEDGER_RECOVERY | PARTIAL | PA 导入前已备份策略池数据库；活动服务关闭重启后的完整账本恢复未验收。|
| NO_EXCHANGE_ORDER_API | VERIFIED（静态检查范围） | 接入代码使用 Binance 公共行情端点；未接交易下单、签名或账户 API。|
| OLD_SYSTEM_UNCHANGED | YES | 未修改旧项目，未操作旧版服务。|

## 本轮修改与验证

- `config/rule_v26/profile.json` 的 A、C 哈希已更新为随 GitHub 基线部署的实际规则文件 SHA-256。A/B/C 信号文件本身未修改；`load_profile()` 通过。
- 修复 `backend/strategy_pool.py` 中 Alpaca 行情来源校验，使 PA 策略接受部署接入的 `BINANCE_USDM_PERP`。同时修复损坏策略状态异常分支中 `gap_audit` 可能未初始化的问题。
- 将 ABC 执行、账本保护、行情归档和 D/E/G 执行辅助模块中的运行时来源门禁切换到 Binance。`market.py` 内旧 Alpaca 兼容类与离线 Alpaca 历史研究标识保留。
- 从 16 策略归档中验证 PA-01～PA-08 的规则文档和 canonical SHA，备份原策略池数据库后导入 8 个独立账户，每个初始现金 10,000 美元；未创建任何正式成交。
- 新增 `scripts/bootstrap_pa_accounts.py`（备份并幂等导入归档策略）和 `scripts/verify_pa_accounts_isolated.py`（只使用隔离数据库的确定性周期验证）。
- 启动脚本默认 `PAPER_AUTOMATION_ENABLED=true`；`data/formal-mode.json` 设置为 `RULE_DRIVEN` 并记录激活时间。当前服务未在该配置下重启验收。
- 隔离 PA 周期测试：8/8 账户记录 `WAIT`，8/8 余额为 10,000.00，0 笔成交。样本数据库位于 `.acceptance-data/`，与正式账本分离。
- ABC 真实规则执行器隔离测试：A V2.6、B V3.1、C V3.1 均处理一根 Binance 标记的闭合 15m 确定性样本，返回 `WAIT`；各账户余额 10,000.00、0 笔成交。样本数据库同样独立。
- `scripts/verify_abc_rules_isolated.py` 在独立 SQLite 副本上运行 A/B/C 规则，不修改正式账本。`- `test_rule_cycle.py`、`test_strategy_pool_v2.py`、`test_strategy_pool_isolated.py` 与规则驱动调度相关用例中，62 项通过。视觉兼容路径仍有 1 项 `CYCLE_FAILED`（KeyError），该路径不是当前 `RULE_DRIVEN` 运行模式。
- 前端 `npm run build`、Python 依赖导入、`compileall` 及 A/B/C 规则档案加载通过。

## 自然运行阻塞

自然周期验收需要启动单一 8012 / 5175 服务、确认 Binance 公共行情连接，并观察至少两个连续 15 分钟收盘周期。当前无法完成服务会话检查：自动安全审查拒绝了读取此前服务会话的操作，提示可能涉及 `dc.services.visualstudio.com` 的 Application Insights 遥测，且其负载和目的未获授权。这个域名不是 Binance 行情服务；本轮未访问该域名，也未绕过该拦截。

## 待完成

1. 解决服务会话检查中的遥测审查阻塞，确认没有重复 V3 进程后，以新 RULE_DRIVEN 配置启动 8012 / 5175。
2. 验证 Binance 实时行情、K 线闭合时间、WebSocket 重连、盘口新鲜度和缺口处理。
3. 观察 ABC 与 PA-01～PA-08 至少两个连续自然周期，检查每周期决策去重、各账户余额/持仓和执行记录。当前 11/11 仅通过隔离确定性样本，不计作自然周期。
4. 完成 Tkinter 控制台实际打开、策略启停和活动账本重启恢复验收。
5. 修复视觉兼容路径剩余 1 项失败并运行全量测试。
6. D/E/G 特殊逻辑及 PA-09/PA-10 继续逐项验证；规则或可执行来源不完整时不臆造实现。

当前无自然成交、自然周期截图或 Binance 实时行情截图可报告。


## 2026-11-20 本轮复验结果

- Python依赖：PARTIAL。部署专用环境已补入 `socksio==1.0.0`，Python模块编译及已有针对性后端检查可运行；但Binance网络连接仍失败，不能据此认定实时服务依赖链通过。
- 前端依赖与构建：PASS。`npm run build` 成功；`npm run test -- --run`：17个测试文件、76项通过；`npx tsc --noEmit` 通过。
- 后端8012：RUNNING。`GET /api/v1/health` 返回HTTP 200，且 `live_trading_disabled=true`。
- 网页5175：RUNNING。首页HTTP 200，`/api/v1/health` 经Vite代理到8012返回HTTP 200。已修正Vite代理端口与页面旧8011/Alpaca标签。
- BTC纸面控制台：PARTIAL。入口脚本已改为只检查/控制V3的8012和5175，并移除Alpaca凭据注入；Python编译通过，控制台进程已启动。当前执行界面未能独立确认窗口可交互及启停按钮端到端效果。
- Binance公开行情：FAIL。服务状态显示REST失败、行情STALE、WebSocket重连/不可用；执行环境的外部代理无法稳定转发到Binance，不能宣称实时行情接入通过。
- 15分钟自然周期：0。无可采信的Binance自然收盘调度证据，无自然成交。
- 策略：A/B/C及PA-01至PA-08的独立确定性调度检查通过，均为WAIT，账户初始权益各10000美元，测试没有制造成交。D/E/G/PA-09/PA-10不能据本轮证据宣称自然运行或全量完成。
- 前后端交易闭环、自然周期、真实行情驱动的本地模拟成交：未验收。禁止把隔离测试冒充自然行情成交。
- 下单接口：后端保持本地模拟模式，未发现Binance签名下单授权/交易账户接入；真实交易保持关闭。
- 旧系统：未修改旧项目源码或8011服务。本轮改动仅在 `windows-binance-deploy` 副本内。
- 资金规则：按用户最新决定保留策略账户10000美元及各策略原有仓位/风险管理；没有启用100 USDT、5倍动态满仓规则。
- 生产就绪：BLOCKED。当前具体阻塞为执行环境到Binance公开REST/WebSocket的稳定网络访问；此前前端代理错误已修复，控制台仍需在网络恢复后完成可视化及启停端到端验收。

本轮关键复验命令：`npm run build`、`npm run test -- --run`、`npx tsc --noEmit`、`python -m py_compile scripts/local_dashboard.py`、`GET http://127.0.0.1:8012/api/v1/health`、`GET http://127.0.0.1:5175/api/v1/health`。

## 2026-11-21 运行修复与纸面控制台复验

### 本轮结论

| 验收项 | 结果 | 证据 |
|---|---|---|
| PYTHON_DEPENDENCIES | PASS | 部署副本 `.venv` 可导入服务依赖；`backend/ledger.py`、`scripts/local_dashboard.py` 编译通过。 |
| FRONTEND_DEPENDENCIES | PASS | `npm run build`、`npm run test -- --run`（76项）及 `npx tsc --noEmit` 已通过。 |
| BACKEND_8012 | RUNNING | `GET http://127.0.0.1:8012/api/v1/health` 返回 `ok`，`live_trading_disabled=true`。 |
| WEB_5175 | RUNNING | `GET http://127.0.0.1:5175/` 返回 HTTP 200。 |
| DESKTOP_CONSOLE | PASS | 可见窗口标题为“BTC 纸盘控制台”；控制台的策略启停调用路径完成可逆验收，PA-01先暂停、再恢复启用，最终状态为 RUNNING/enabled，账户未产生交易。 |
| BINANCE_PUBLIC_MARKET | PASS | REST、USDⓈ-M BTCUSDT WebSocket 均正常；`system/status` 为 `REST=PASS`、`WS=CONNECTED`、`FEED=LIVE`、`QUOTE=FRESH`，行情来源 `BINANCE_USDM_PERP`。稳定用户代理为 `127.0.0.1:7890`；服务进程已显式继承持久代理变量，避免继承短生命周期的命令代理。 |
| 15M_NATURAL_CYCLES | 2 | 修复后连续自然周期 `2026-11-21 11:15`、`11:30`（UTC+8）；8/8 已启用PA策略均记录 `OK / WAIT`，每个周期各一条，无强制假成交。 |
| STRATEGIES_REGISTERED | 8 | 当前策略池中可运行PA-01至PA-08。此数是实际可运行池数量，不代表16/16。 |
| STRATEGIES_RULE_COMPLETE | 11 | A/B/C及PA-01至PA-08已有可验证规则；D/E/G/PA-09/PA-10仍按项目既有规则缺口状态处理。 |
| STRATEGIES_EXECUTION_READY | 8 | 当前独立策略池内PA-01至PA-08均启用并完成上述自然决策周期；本轮不把未接入的策略算作就绪。 |
| STRATEGY_TOGGLE | PASS | 通过控制台使用的部署副本接口执行禁用/重新启用，并验证持久状态恢复为启用。 |
| PAPER_ORDER_ENGINE | PASS | 本轮无满足条件的自然信号，未生成模拟订单；离线模拟执行器既有测试仍作为独立证据，不冒充自然成交。 |
| DYNAMIC_EQUITY_5X | NOT USED | 保持每策略独立 10,000 美元及原策略仓位/风险规则；没有启用100 USDT、5倍动态满仓。 |
| LEDGER_RECOVERY | PASS | 本轮周期后8个PA账户均为空仓，现金与权益各为10,000美元；此前独立账本恢复测试记录继续有效。 |
| NO_EXCHANGE_ORDER_API | VERIFIED | 本地模拟模式有效；行情使用Binance公开接口，未使用签名交易下单或账户杠杆接口。 |
| OLD_SYSTEM_UNCHANGED | YES | 修改仅落在 `windows-binance-deploy`。8011和5174仍由原环境响应，本轮未停止、改写或覆盖其服务。 |
| PRODUCTION_READINESS | PARTIAL | V3本机行情与PA-01至PA-08自然调度可运行。尚未证明16套策略全部就绪，也没有自然行情驱动的实际成交；当前两次自然周期均为WAIT。 |

### 故障与修复

首次持续运行期间日志出现 `RuntimeError: can't start new thread`。定位到报价保护每次行情事件都会请求G镜像派发，即使SQLite outbox为空也会创建短生命周期线程。已在部署副本 `backend/ledger.py` 增加待处理事件检查：队列为空时不创建线程；有事件时仍由单一后台工作线程派发。`tests/test_g_mirror_outbox.py` 定向测试4项通过；服务重启后连续完成11:15、11:30两个自然周期，期间无该线程错误。

桌面恢复逻辑现在从Windows用户环境读取持久代理；启动脚本也在拉起后端前把稳定代理传给子进程。Binance REST/WebSocket真实连接复验通过。控制台后端地址固定8012，辅助Web固定5175；旧8011/5174保持可响应且未改动。

### 关键验收命令

- `.venv\Scripts\python.exe -m py_compile backend\ledger.py scripts\local_dashboard.py`
- `.venv\Scripts\python.exe -m pytest tests\test_g_mirror_outbox.py -q`（4 passed）
- `npm run build`
- `npm run test -- --run`（76 passed）
- `npx tsc --noEmit`
- `GET http://127.0.0.1:8012/api/v1/health`
- `GET http://127.0.0.1:8012/api/v1/system/status`
- `GET http://127.0.0.1:8012/api/v1/strategy-pool`
- `GET http://127.0.0.1:5175/`

当前V3后端以前台执行会话运行于PID 18400；前端5175返回HTTP 200。该运行环境无法可靠枚举Windows监听PID（CIM查询被拒绝），因此PID仅记录本次后端前台会话值。控制台窗口已观察到但本轮未保存截图文件。


## Binance双节点核对补充（2026-11-21）

用户提到的 `bianenv.txt` 中“线路1/线路2”各有一组API凭证字符串，但文件没有提供第二组Binance公开行情REST/WebSocket地址。凭证不是行情节点URL；本部署保持公开行情无密钥模式，不读取或使用这些凭证。若凭证属于真实可用账户，建议在Binance撤销并轮换。

核对发现，桌面控制台旧“节点1/节点2”按钮仍指向Alpaca环境配置文件，因此会显示“节点未知”；旧切换实现还会复制Alpaca凭证并重启服务。现已修正部署副本 `scripts/local_dashboard.py`：状态固定显示Binance公开行情主节点1，节点2显示“备用未配置”；点击节点2会明确拦截，不读写旧Alpaca配置、不重启当前服务。节点1/节点2保护冒烟检查通过，Python编译通过。

| 项目 | 当前状态 |
|---|---|
| BINANCE_PUBLIC_PRIMARY | PASS — `https://fapi.binance.com` REST 与 `wss://fstream.binance.com` 公共行情连接通过。 |
| BINANCE_SECONDARY_NODE | BLOCKED — 文件中没有备用REST与WebSocket URL；没有擅自使用Demo域名、Spot域名或凭证冒充备用节点。 |
| CONSOLE_NODE_LABEL | FIXED — 节点1代表当前Binance公开行情配置；节点2明确未配置。 |
| CONSOLE_NODE_SWITCH | GUARDED — 备用地址缺失时不会执行切换或停止V3。 |
| DESKTOP_CONSOLE_TESTS | 30 passed；另有1项 `test_operations_panel_uses_live_and_backup_values_without_inventing_success` 失败，错误为既有状态标签键 `8011版本` 的 `KeyError`，与本轮节点控制代码无关，尚未修复。 |
| BACKEND_8012 | RUNNING — 重新挂载后健康；最新状态 `WS=CONNECTED`、`FEED=LIVE`、`QUOTE=FRESH`。 |
| WEB_5175 | HTTP 200。 |

**双节点自动故障切换仍未验收。** 需要提供第二节点的公开行情REST基础URL和对应的USDⓈ-M WebSocket基础URL。收到后才能接入并测试主节点故障、备用切换、恢复主节点及行情连续性。Binance当前USDⓈ-M官方常规REST文档列出的基础地址为 `https://fapi.binance.com`。citeturn400158732view0
