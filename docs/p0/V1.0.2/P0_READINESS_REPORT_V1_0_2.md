# P0 Readiness Report — Alpaca Source V1.0.2 (Updated 2026-10-27)

## Current evidence

| Gate | Current result | Reason |
|---|---|---|
| Alpaca Market Data | `PASS` for sampled REST and WebSocket access | User-authorized credentials authenticated the crypto stream; history/latest quote/latest trade REST returned HTTP 200; BTC/USD quote stream event received. |
| BTC 15m chart window | `AVAILABLE` for tested window | Raw JSON UTC verification: 288/288 bars over three days and nested local 96/96; seven bars have zero volume. A seven-day window returned 672/672 buckets with 13 zero-volume bars. |
| Source comparison | `COMPLETED` for OHLC | Corrected raw-UTC comparison has 672 paired bars. Actual stop-trigger disagreement remains unassessable without stop levels/position histories. |
| P1 implementation readiness | `NO` | USD account/instrument denomination, paper fee/slippage and internal quantity configuration, model budgets, shadow recovery threshold and C MD input are not all frozen. |

## Reproducible evidence summary

- Credential file: user-confirmed `C:\Users\admin\Documents\量化\.env`, opened read-only. Only the two named Alpaca variables were held in process memory; no values were emitted or persisted.
- Market-data endpoints only: historical 1m bars (10 rows, HTTP 200); latest quote (HTTP 200, event age 2734 ms); latest trade (HTTP 200, event age 73059 ms); 3-day 15m bars (288/288); 7-day 15m bars (672/672); WebSocket auth and BTC/USD channel subscription succeeded and a quote event arrived with 867 ms observed event age.
- Sampled REST RTT: 1m bars 1448 ms; three-day 15m 1637 ms; seven-day paginated 3718 ms total; latest quote 349 ms; latest trade 370 ms. These are point measurements, not latency guarantees.
- Provider scope: Alpaca location `us`, spot `BTC/USD`; no per-event venue code appears in payload. Do not label it Binance perpetual.
- Binance official USDⓈ-M public archive yielded 672 15m bars for the aligned comparison window. Corrected signed median close basis (Alpaca minus Binance / Binance) was -0.083870%, absolute median 0.083870%, P95 0.102224%, max 0.118850%. No actual stop-price replay was possible.

## Configuration still blocking P1/runtime readiness

1. CFG-01 sustained event-age, clock-skew and reconnect-health thresholds remain unapproved; point event ages do not define those limits.
2. CFG-02/03 internal paper fee and adverse slippage numerical values remain unapproved.
3. CFG-04 internal quantity precision/minimum constraints remain unapproved; no Alpaca order-filter or order API was called.
4. CFG-05 vision model timeout/budget and CFG-06 shadow failure recovery limit remain unapproved.
5. CFG-08 frozen USDT virtual account vs USD-quoted spot instrument valuation/conversion remains unresolved; no USD=USDT assumption.
6. CFG-09 user-authored C MD/core boundary is still not supplied.

These are still correctness gates. No implementation or simulation was started. No real or paper trading endpoint was called.

## Correction log

The initial V1.0.2 comparison reported 31 missing Alpaca buckets due to PowerShell date-object conversion. Raw JSON timestamps were re-parsed as strings with `System.Text.Json` and matched on UTC epoch buckets. The corrected results show complete 3-day and 7-day windows; see `P0_ALPACA_MARKET_DATA_VALIDATION.md` and the corrected seven-day comparison. The earlier gap count and dependent statistics must not be reused.
