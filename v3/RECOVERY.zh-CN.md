# BTC Paper Lab V3：Windows 恢复说明

这是 2026-11-22 从正式 V3 提取的隔离源码快照；仓库根目录的旧项目与 v3 目录分开。只使用 Binance 公共行情和本地模拟账本。归档中的旧路径、旧运行环境和版本是历史元数据，不是当前部署设置。

## 新电脑部署

1. 安装 Python 3.12、Node.js（含 npm）、Git 和 PowerShell。将 v3 内容放到当前 Windows 用户的 `Documents\量化升级版\btc-paper-lab-v3\windows-binance-deploy`。现有单实例保护硬编码该路径；不能并行启用其他副本。
2. 在该目录运行 `py -3.12 -m venv .venv`，然后 `.\.venv\Scripts\python.exe -m pip install -e ".[test]"`。在 frontend 执行 `npm ci`。独立安装 Python，不复制旧机器的 .venv。
3. `.env.example` 仅包含示例，按需要在本机建立 `.env`；不要提交真实配置。Binance 行情无需凭据。检查代理和网络能访问公共行情。
4. 历史资金、持仓、启停、水位和成交只能通过可信的 SQLite 在线一致性备份恢复；本仓库不包含这些私有数据。停止目标机器上的 V3 后，将对应备份还原至 `data` 原相对路径，确保没有遗留 WAL/SHM，启动前使用 SQLite `PRAGMA integrity_check` 检查。不同数据库的在线备份分别取得，并非跨库原子快照；恢复时必须人工核对账户、持仓及成交的一致性。
5. D/E 的活动配置不在账本内。要复现本次授权设置，将 `config\recovery-templates\d` 和 `e` 下 JSON 分别复制到 `data\d` 和 `data\e`，核对 E 当前量化版本、风控及启停。不要把模板中的 AUTO_PAPER 误当成只读模式。E formal.sqlite3 必须存在；仅空账本需单独通过 EPaperStore 初始化，不能复制或伪造历史交易。策略池账户及注册状态也必须核对，不能假定空库自动完整恢复16项。
6. 运行 `powershell -File scripts\start_lab.ps1`，核对 8012/5175 唯一实例、账户、持仓、价格保护、启动状态、周期水位和行情。控制台入口为 `scripts\launch_v3.ps1`。退出入口为 `scripts\stop_lab.ps1`；归属不明时脚本应拒绝结束进程。此处只描述目标机操作，不要求更改安全策略。

## 隔离测试和限制

测试必须在隔离副本执行，并确保所有数据路径为临时目录；tests/conftest.py 在收集前设置临时数据库。不要以测试为由启动正式交易引擎。已收盘数据缺口不得伪造交易；过期报价仍按原规则拒绝开仓。正式状态需要另行验证，源码包本身不保证新机器完整恢复通过。原始策略归档是历史快照，实际规则以源码、活动配置及可信备份为准。
