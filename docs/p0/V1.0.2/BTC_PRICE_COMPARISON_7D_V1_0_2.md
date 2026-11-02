# BTC/USD Spot vs BTCUSDT USDⓈ-M Perpetual — Corrected Seven-Day Comparison

- Correction date: 2026-10-27.
- Window: 2026-10-19 00:00Z through 2026-10-25 23:45Z, 672 expected 15m UTC bucket opens.
- Alpaca source: authenticated historical crypto bars, location `us`, spot `BTC/USD`; all pages followed; 672/672 unique bars, no missing buckets, 13 zero-volume bars.
- Binance source: official `data.binance.vision` USDⓈ-M futures daily `BTCUSDT` 15m archives for 2026-10-19…25; seven files, 96 rows each, 672/672.
- Matched timestamps: 672. Data are separate market types: Alpaca spot vs Binance USDⓈ-M perpetual. Binance values are contract-traded klines, not mark-price klines.

## Paired OHLC results

Percent difference = `(Alpaca OHLC − Binance UM OHLC) / Binance UM OHLC × 100`. Absolute percentiles use the absolute per-bucket difference; signed mean/median retain direction.

| Field | Signed median % | Signed mean % | Absolute median % | Absolute P95 % | Absolute max % |
|---|---:|---:|---:|---:|---:|
| Open | -0.082591 | -0.078624 | 0.082591 | 0.105002 | 0.121044 |
| High | -0.081113 | -0.074536 | 0.081255 | 0.106584 | 0.696522 |
| Low | -0.085152 | -0.080932 | 0.085152 | 0.108424 | 0.757532 |
| Close | -0.083870 | -0.078878 | 0.083870 | 0.102224 | 0.118850 |

In this sample Alpaca spot OHLC was generally below Binance perpetual OHLC. This is an observed historical basis, not a stable conversion factor or a guarantee of future basis.

## Stop-trigger comparison

No strategy position/stop-price series or trigger event ids were supplied. Thus the count of actual stop-trigger disagreements remains not assessable from this paired OHLC data. A separate future replay requires fixed position direction, stop levels, position lifecycle and trigger semantics. The already frozen V1.0.2 distinction remains: Alpaca spot uses last-trade price; Binance mode uses mark price.

## Correction history

The first comparison report stated 641 matched bars/31 Alpaca gaps and reported materially larger differences. That result was invalid: PowerShell automatically converted JSON timestamps to date objects and the comparison did not retain the provider's UTC semantics consistently. The error was identified when a 3-day raw-JSON UTC check returned a complete grid. Both providers were re-read/parsed by exact UTC bucket keys; corrected results above are 672/672 matches. Do not use the earlier 641-bar figures for engineering decisions.

## Result

`OHLC_COMPARISON=COMPLETED`; `ACTUAL_STOP_TRIGGER_REPLAY=NOT_ASSESSABLE`. The price comparison is valid for this seven-day sampled window; it does not imply the spot and perpetual data are interchangeable.
