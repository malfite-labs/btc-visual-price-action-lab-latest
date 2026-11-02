# P0 Alpaca Market Interface Audit — V1.0.3

The one-shot probe `tools/verify_alpaca_readonly.py` used the authorized two Alpaca variables in memory and fixed official crypto market-data URLs under `data.alpaca.markets/v1beta3/crypto/us`. No credentials or response prices/depth values are stored in this report.

## Actual read-only test evidence

Observed at 2026-10-26 18:43:49–18:44:38 UTC:

| Check | Result | Evidence |
|---|---|---|
| Authenticated REST 15m bars | PASS | 289 provider bars returned in 3,102 ms; 288 closed and one incomplete excluded; last closed candle ended 18:30 UTC |
| 3-day closed window | PASS for this sample | 288 bars; 0 duplicate open timestamps; 0 non-900-second intervals; 7 zero-volume provider bars retained |
| Latest REST quote | PASS | bid/ask and both sizes present; provider event time and local receive time recorded; 870 ms observed age; 2,267 ms request round trip |
| REST order book | PASS | 84 bid levels and 99 ask levels; event timestamp present; 1,036 ms request round trip |
| WebSocket auth/subscription/live data | PASS | Auth success and subscription ack received; first actual quote event received. This is a point connection test, not a continuous uptime/reconnect SLA test. |
| Market identity | Alpaca US BTC/USD spot feed | This is not Binance BTCUSDT perpetual, and no mark price/funding is inferred. |

Official endpoint contracts: [historical crypto bars](https://docs.alpaca.markets/us/reference/cryptobars-1), [latest crypto orderbook](https://docs.alpaca.markets/us/reference/cryptolatestorderbooks-1), [real-time crypto stream](https://docs.alpaca.markets/us/docs/real-time-crypto-pricing-data). A zero-volume Alpaca bar may use quote-derived OHLC; the adapter preserves provider values and does not synthesize trades.

## Unverified items

- Continuous WebSocket stability, multi-hour latency distribution, and forced network recovery have not been live-measured.
- No Binance comparison is repeated or needed for V1.0.3 Alpaca-first execution; this implementation does not connect Binance.
- Bid/ask observed age is variable; the current 2,000ms stale threshold is a fail-closed research setting, not a statistically calibrated guarantee.
- A verified 288-bar sample does not guarantee future complete windows. The runtime marks any gap/duplicate/incomplete history and keeps model/trading scheduling disabled.
