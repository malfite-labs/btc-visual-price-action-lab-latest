# P0 Alpaca BTC/USD Market Data Validation

- Validation date: 2026-10-26 UTC (local date 2026-10-27, Asia/Shanghai).
- Scope: authenticated Alpaca Market Data REST/WebSocket reads only. No account, asset, order, position, trade-execution, or write endpoint was called.
- Credential source: `C:\Users\admin\Documents\量化\.env` (read-only); only `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` were extracted in process memory. Both were non-empty and passed basic whitespace/placeholder checks. Values were not printed, logged, copied, hashed, or written to a file. The WebSocket authentication acknowledgement confirms the credentials were accepted by the market-data stream. This does not assert that the same key lacks permissions on other API products; those endpoints were not inspected or called.
- The old `.env` was not modified. No credential file was created in the new project. No code or dependency was added.

## Actual tests

| Test | Result | Evidence |
|---|---|---|
| Credential authentication | `VALID` for Alpaca Market Data stream | WebSocket returned the authenticated success message; market data subscription was acknowledged. No broader account/trading permission check was made. |
| Historical minute bars | `PASS` | `GET /v1beta3/crypto/us/bars`, symbol `BTC/USD`, timeframe `1Min`; HTTP 200; 10 bars for 2026-10-26 18:01:00Z–18:10:00Z; fields `o,h,l,c,v,n,t,vw`; RTT 1448 ms on the repeated raw-JSON check (initial call 1497 ms). |
| Recent trade | `PASS` | `GET /v1beta3/crypto/us/latest/trades`; HTTP 200; trade id present, price 80229.1, size 0.0003; event time `2026-10-26T18:04:20.911734534Z`; observed event age 73059 ms; RTT 370 ms. |
| Latest bid/ask | `PASS` | `GET /v1beta3/crypto/us/latest/quotes`; HTTP 200; bid 80221.15, bid size 0.0009933; ask 80245.27, ask size 0.0010094; event time `2026-10-26T18:05:30.845359412Z`; observed event age 2734 ms; RTT 349 ms. |
| Three-day 15m chart | `AVAILABLE` | HTTP 200; requested UTC opens `2026-10-23T18:00:00Z` through `2026-10-26T17:45:00Z`; 288 unique bars/288 expected, no missing or duplicate timestamps; last 96/96 local-window bars present; 7 bars have volume 0. RTT 1637 ms. |
| Seven-day 15m audit | `AVAILABLE` | Authenticated request with pagination: HTTP 200, 672/672 unique buckets, 0 missing, 13 zero-volume bars, two pages, total measured RTT 3718 ms. Window `2026-10-19T18:00:00Z`–`2026-10-26T17:45:00Z`. |
| Realtime WebSocket | `PASS` | Connected to `wss://stream.data.alpaca.markets/v1beta3/crypto/us`; authenticated, BTC/USD trades/quotes/bars subscription acknowledged, and BTC/USD quote events received. Latest validation event `2026-10-26T18:08:52.187436578Z`, local receive-time age 867 ms. Earlier event samples were 856–878 ms. |

REST RTT is a single-request observation, and event age is provider event timestamp compared to local receive time. These measurements do not establish a sustained latency percentile, clock-synchronization bound, reconnect recovery, or freshness SLO. Realtime stream was observed, but no long-duration soak or forced-disconnect test was performed.

## Data source, timestamps, completeness

The request uses Alpaca crypto location `us`, instrument `BTC/USD`, market type spot. The event payloads contain symbol and timestamps but no per-event exchange-code field; therefore the report attributes the configured location/provider scope and does not claim a more granular venue for each trade. Alpaca documentation maps `us` to Alpaca US and distinguishes Kraken locations. Provider timestamps are RFC3339 UTC (`Z`). Alpaca documents that crypto bars can use quote midpoints when no trade occurs; the observed zero-volume bars are preserved and must not be described as trade-only bars.

**Correction to earlier V1.0.2 audit:** the previous report's 31-missing/641-matched-bucket result came from PowerShell date-object conversion that did not preserve the source UTC timestamp semantics. Rechecking raw JSON timestamp strings with `System.Text.Json` and explicit Unix-time bucket keys produced 672/672 for seven days and 288/288 for three days. The previous gap count and its dependent comparison statistics are superseded; the corrected paired comparison is in `docs/p0/V1.0.2/BTC_PRICE_COMPARISON_7D_V1_0_2.md`.

## Final result

`ALPACA_CREDENTIALS=VALID` (market-data websocket authentication); `ALPACA_REST_BARS=PASS`; `ALPACA_REST_QUOTES=PASS`; `ALPACA_WEBSOCKET=PASS`; `BTC_15M_DATA=AVAILABLE` for the tested three-day/288-bar and seven-day windows; `SECRET_EXPOSURE=NO`; `OLD_PROJECT_MODIFIED=NO`.

Official references: [Alpaca historical crypto bars](https://docs.alpaca.markets/us/v1.4.2/reference/cryptobars-1), [Alpaca latest crypto quotes](https://docs.alpaca.markets/reference/cryptolatestquotes-1), [Alpaca real-time crypto data](https://docs.alpaca.markets/us/docs/real-time-crypto-pricing-data), [Alpaca WebSocket auth and entitlement behavior](https://docs.alpaca.markets/us/docs/streaming-market-data).
