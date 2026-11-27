# BTC Paper Lab V3.1｜38 套策略完整文档目录

> 本文档库由本机正式部署目录 `windows-binance-deploy` 自动导出，覆盖全部 **38 套**运行中策略（原有 5 + 镜像 1 + PA 10 + Chan 1 + R 6 + S 12 + AY 3），不含任何数据库、账户余额、持仓或 API 密钥。

| # | 分组 | 策略 ID | 中文名称 | 主周期 | 版本 | 规则指纹 (SHA-256 前12位) | 策略文档 |
|---|---|---|---|---|---|---|---|
| 1 | 原有策略 | `A` | A 趋势价格行为核心策略 | `15m` | `core` | `0e2c833bdbb2` | [01_core/A.md](./01_core/A.md) |
| 2 | 原有策略 | `B` | B 反转价格行为核心策略 | `15m` | `core` | `dc2665773b56` | [01_core/B.md](./01_core/B.md) |
| 3 | 原有策略 | `C` | C 主观融合核心策略 | `15m` | `core` | `672451cfc522` | [01_core/C.md](./01_core/C.md) |
| 4 | 原有策略 | `E` | E 量化规则执行核心策略 | `15m` | `core` | `70e4c4b5b573` | [01_core/E.md](./01_core/E.md) |
| 5 | 原有策略 | `D` | D 核心趋势量化策略 | `15m` | `D-V1` | `139a12ada772` | [01_core/D.md](./01_core/D.md) |
| 6 | 镜像策略 | `G` | C反向镜像策略 | `事件驱动` | `1.0.0` | `0b1a86a62578` | [02_mirror/G.md](./02_mirror/G.md) |
| 7 | PA系列 | `PA-05 (pa-big-candle-continuation)` | PA 大实体K顺势推进 | `15m` | `1.0.0` | `4168d67c1547` | [03_pa/PA-05_pa-big-candle-continuation.json](./03_pa/PA-05_pa-big-candle-continuation.json) |
| 8 | Chan | `pa-chan-center-divergence` | 中枢震荡与背驰回抽（量化近似） | `15m` | `1.0.0` | `858fc02bc0fe` | [04_chan/pa-chan-center-divergence.json](./04_chan/pa-chan-center-divergence.json) |
| 9 | PA系列 | `PA-07 (pa-full-engulfing-reversal)` | PA 兼容无缺口吞噬反转 | `15m` | `1.0.0` | `ea9140d3162f` | [03_pa/PA-07_pa-full-engulfing-reversal.json](./03_pa/PA-07_pa-full-engulfing-reversal.json) |
| 10 | PA系列 | `PA-08 (pa-gapless-three-bar-reversal)` | PA 无缺口三K反转 | `15m` | `1.0.0` | `da1b870fbe7e` | [03_pa/PA-08_pa-gapless-three-bar-reversal.json](./03_pa/PA-08_pa-gapless-three-bar-reversal.json) |
| 11 | PA系列 | `PA-04 (pa-inside-bar-break)` | PA 内包K趋势扩张 | `15m` | `1.0.0` | `9cfa53a1e723` | [03_pa/PA-04_pa-inside-bar-break.json](./03_pa/PA-04_pa-inside-bar-break.json) |
| 12 | PA系列 | `PA-02 (pa-sma20-pullback-confirm)` | PA 均线回踩确认 | `15m` | `1.0.0` | `56872adae426` | [03_pa/PA-02_pa-sma20-pullback-confirm.json](./03_pa/PA-02_pa-sma20-pullback-confirm.json) |
| 13 | PA系列 | `PA-03 (pa-three-bar-hhhl)` | PA 三根持续K结构 | `15m` | `1.0.0` | `d4287384edfe` | [03_pa/PA-03_pa-three-bar-hhhl.json](./03_pa/PA-03_pa-three-bar-hhhl.json) |
| 14 | PA系列 | `PA-01 (pa-trend-40-breakout)` | PA 40根顺势突破 | `15m` | `1.0.0` | `19641ecb4332` | [03_pa/PA-01_pa-trend-40-breakout.json](./03_pa/PA-01_pa-trend-40-breakout.json) |
| 15 | PA系列 | `PA-06 (pa-trend-false-break-reclaim)` | PA 顺势扫低扫高收复 | `15m` | `1.0.0` | `61c5107e6fd4` | [03_pa/PA-06_pa-trend-false-break-reclaim.json](./03_pa/PA-06_pa-trend-false-break-reclaim.json) |
| 16 | PA系列 | `PA-09` | PA-09 关键结构反转扩展策略 | `15m` | `1.0.0` | `c2c0c517d1e3` | [03_pa/PA-09.md](./03_pa/PA-09.md) |
| 17 | PA系列 | `PA-10` | PA-10 通道回调顺势扩展策略 | `15m` | `1.0.0` | `499b6c99554a` | [03_pa/PA-10.md](./03_pa/PA-10.md) |
| 18 | R系列 | `R01` | R01 · TD9 基础反转策略 (V1.1) | `15m+HTF` | `1.1.0` | `24d1d4e569d5` | [05_r_series/R01_v1.1.0.json](./05_r_series/R01_v1.1.0.json) |
| 19 | R系列 | `R02` | R02 · 完善型 TD9 放量确认策略 (V1.1) | `15m+HTF` | `1.1.0` | `a7b5585a8c8c` | [05_r_series/R02_v1.1.0.json](./05_r_series/R02_v1.1.0.json) |
| 20 | R系列 | `R03` | R03 · TD13 衰竭反转策略 (V1.1) | `15m+HTF` | `1.1.0` | `b90c456d0269` | [05_r_series/R03_v1.1.0.json](./05_r_series/R03_v1.1.0.json) |
| 21 | R系列 | `R04` | R04 · 双周期 TD9 结构放量策略 (V1.1 重点候选) | `15m+HTF` | `1.1.0` | `27fe01dc0fe3` | [05_r_series/R04_v1.1.0.json](./05_r_series/R04_v1.1.0.json) |
| 22 | R系列 | `R05` | R05 · 双周期 TD13 结构背书策略 (V1.1) | `15m+HTF` | `1.1.0` | `a72b62385a51` | [05_r_series/R05_v1.1.0.json](./05_r_series/R05_v1.1.0.json) |
| 23 | R系列 | `R06` | R06 · 多周期顺势衰竭放量策略 (V1.1) | `15m+HTF` | `1.1.0` | `e19f200a732f` | [05_r_series/R06_v1.1.0.json](./05_r_series/R06_v1.1.0.json) |
| 24 | S系列 | `thc-s01-15m` | 熬鹰研究·区间高空低多 15M | `15m` | `1.0.0` | `e6eaefd907e8` | [06_s_series/thc-s01-15m.json](./06_s_series/thc-s01-15m.json) |
| 25 | S系列 | `thc-s01-1h` | 熬鹰研究·区间高空低多 1H | `1h` | `1.0.0` | `aeaedaa5cdb3` | [06_s_series/thc-s01-1h.json](./06_s_series/thc-s01-1h.json) |
| 26 | S系列 | `thc-s01-4h` | 熬鹰研究·区间高空低多 4H | `4h` | `1.0.0` | `33f6749513f7` | [06_s_series/thc-s01-4h.json](./06_s_series/thc-s01-4h.json) |
| 27 | S系列 | `thc-s01-1d` | 熬鹰研究·区间高空低多 1D | `1d` | `1.0.0` | `cde7989bb0c1` | [06_s_series/thc-s01-1d.json](./06_s_series/thc-s01-1d.json) |
| 28 | S系列 | `thc-s02-15m` | 熬鹰研究·右侧突破追击 15M | `15m` | `1.0.0` | `f2b3fa189718` | [06_s_series/thc-s02-15m.json](./06_s_series/thc-s02-15m.json) |
| 29 | S系列 | `thc-s02-1h` | 熬鹰研究·右侧突破追击 1H | `1h` | `1.0.0` | `4f2f2ae99c6c` | [06_s_series/thc-s02-1h.json](./06_s_series/thc-s02-1h.json) |
| 30 | S系列 | `thc-s02-4h` | 熬鹰研究·右侧突破追击 4H | `4h` | `1.0.0` | `41a56dee3ecd` | [06_s_series/thc-s02-4h.json](./06_s_series/thc-s02-4h.json) |
| 31 | S系列 | `thc-s02-1d` | 熬鹰研究·右侧突破追击 1D | `1d` | `1.0.0` | `a739dfe0362d` | [06_s_series/thc-s02-1d.json](./06_s_series/thc-s02-1d.json) |
| 32 | S系列 | `thc-s01-s02-combo-15m` | 熬鹰研究·区间／突破互斥组合 15M | `15m` | `1.0.0` | `e759783116ce` | [06_s_series/thc-s01-s02-combo-15m.json](./06_s_series/thc-s01-s02-combo-15m.json) |
| 33 | S系列 | `thc-s01-s02-combo-1h` | 熬鹰研究·区间／突破互斥组合 1H | `1h` | `1.0.0` | `fc8465cab6b4` | [06_s_series/thc-s01-s02-combo-1h.json](./06_s_series/thc-s01-s02-combo-1h.json) |
| 34 | S系列 | `thc-s01-s02-combo-4h` | 熬鹰研究·区间／突破互斥组合 4H | `4h` | `1.0.0` | `c31700b6047b` | [06_s_series/thc-s01-s02-combo-4h.json](./06_s_series/thc-s01-s02-combo-4h.json) |
| 35 | S系列 | `thc-s01-s02-combo-1d` | 熬鹰研究·区间／突破互斥组合 1D | `1d` | `1.0.0` | `51367b3fbb04` | [06_s_series/thc-s01-s02-combo-1d.json](./06_s_series/thc-s01-s02-combo-1d.json) |
| 36 | AY系列 | `thc-ay02-15m` | 熬鹰研究·日线过滤与收敛突破 15M | `15m` | `1.0.0` | `bdc673a3dc8a` | [07_ay_series/thc-ay02-15m.json](./07_ay_series/thc-ay02-15m.json) |
| 37 | AY系列 | `thc-ay03-15m` | 熬鹰研究·恐慌反转代理 15M | `15m` | `1.0.0` | `24e467b9bb28` | [07_ay_series/thc-ay03-15m.json](./07_ay_series/thc-ay03-15m.json) |
| 38 | AY系列 | `thc-ay04-15m` | 熬鹰研究·震荡区间反转 15M | `15m` | `1.0.0` | `1c483895f6fc` | [07_ay_series/thc-ay04-15m.json](./07_ay_series/thc-ay04-15m.json) |
