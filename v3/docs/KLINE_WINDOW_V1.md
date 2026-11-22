# BTC · K线交易观察 V1

此功能使用 Tk 拥有的 Windows 窗口和 WebView2。它在本地打开专用的 5175 路径 /kline。两个工具栏、指标设置、策略筛选和成交动态共用该窗口。首次默认 15m。指标偏好保存在 WebView2 用户数据中，窗口位置和大小保存在 .run 下。

行情接口 /api/v1/observation/candles 使用 Binance BTCUSDT USDⓈ-M 公共 REST K线。每次最多 500 根，前端每 12 秒更新最近数据。历史分页和成交定位都使用交易所返回的实际开收时间。形成中的 K 线会注明状态。成交接口 /api/v1/observation/events 以 SQLite mode=ro 和 query_only 读取正式账本，不调用研究中心曾发现存在副作用的 D 和 PA 状态接口。成交显示使用持久化方向与事件类型。缺少历史开仓、方向、时间或价格时，无法核验的记录不会变成图上买卖点。

## Windows 受控部署

1. 保留当前部署副本，准备已测试的分支文件。正式 8012 需要在获准的维护窗口统一部署新接口。现有 5175 的 Vite 代理复用原地址，新页面源码与路由入口同时更新。保留研究中心 V2 文件和所有数据库。
2. 在部署副本确认 Microsoft Edge WebView2 Evergreen Runtime 和 Windows .NET Framework x64 编译工具可用。从 Microsoft NuGet 下载 Microsoft.Web.WebView2 1.0.4370.26 的包，保存为 .zip，执行 tools/kline-host/build.ps1 -PackageZip <文件路径>。构建脚本校验包摘要并在 .run/kline-host 下生成宿主。
3. 用本地只读请求确认 /api/v1/observation/candles?interval=15m 和 /api/v1/observation/events 正常，并确认 5175 的 /kline 路径加载专用页面。正常关闭并重新打开桌面控制台，点击「打开K线」。检查实际成交、窗口缩放和关闭后的前后端健康。窗口如果仍然显示旧按钮，核对桌面进程是否已加载更新。

## 隔离测试

在 v3 目录运行 Python 定向测试。前端运行 npm test 和 npm run build。交互测试需要在前端目录先启动隔离 Vite，端口 5186，再运行 node src/kline-ui-smoke.mjs。测试拦截所有 /api/v1 请求并提供明确标注的演示数据。设置 KLINE_SMOKE_SCREENSHOTS 可指定本地截图目录。仓库不包含正式账本或截图。
