# P0 Alpaca Public Market-Data Audit — V1.0.2 (Updated)

## Scope

Read-only unauthenticated GETs were originally sampled on 2026-10-26. Authenticated BTC/USD market-data REST and WebSocket checks were completed on 2026-10-26/27 using the user-authorized values from `C:\Users\admin\Documents\量化\.env`, held only in process memory. No secret values were output, logged or written. No account, asset, order, position, trade-execution or write endpoint was called.

## Current authenticated validation

- Historical `1Min` bars: HTTP 200, 10 bars, `2026-10-26T18:01:00Z` through `18:10:00Z`, fields `o,h,l,c,v,n,t,vw`; RTT 1448 ms.
- Latest quote: HTTP 200, bid 80221.15 / size 0.0009933, ask 80245.27 / size 0.0010094, event time `2026-10-26T18:05:30.845359412Z`; event age 2734 ms; RTT 349 ms.
- Latest trade: HTTP 200, trade id present, price 80229.1 / size 0.0003, event time `2026-10-26T18:04:20.911734534Z`; event age 73059 ms; RTT 370 ms.
- Three-day `15Min`: 288/288 expected unique bars; 96/96 local-window bars; no missing buckets; 7 zero-volume bars; RTT 1637 ms.
- Seven-day `15Min`: 672/672 expected unique bars; 0 missing; 13 zero-volume bars; two pages; combined RTT 3718 ms.
- Crypto WebSocket: successful connection, authentication, subscription acknowledgement for BTC/USD trades/quotes/bars, and quote events; latest verified event `2026-10-26T18:08:52.187436578Z`, observed age 867 ms.

RTTs are individual observations, not SLOs. Sustained latency, clock synchronization bounds, forced reconnect and recovery were not tested.

## Source attribution

Instrument is Alpaca crypto `BTC/USD`, location `us`, spot market. Alpaca documentation identifies `us` as Alpaca US and other locations as Kraken feeds. These response payloads do not provide a per-event exchange code; record source/location rather than invent a venue. Timestamps are RFC3339 UTC. Alpaca crypto bars may use quote midpoint prices when there is no trade; zero-volume bars are preserved with their OHLC and are not called trade-only bars.

## Correction to initial public-data sample

An earlier unauthenticated seven-day analysis reported 31 missing bars and only 641 aligned buckets. This was a parsing error: PowerShell date-object conversion did not preserve JSON UTC timestamps consistently. Rechecking raw JSON timestamp strings and matching explicit UTC bucket keys yields 672/672 for seven days and 288/288 for three days. The previous gap assertion is withdrawn; corrected cross-market metrics are in `BTC_PRICE_COMPARISON_7D_V1_0_2.md`.

## Result

Market-data history, latest quotes/trades and a live WebSocket subscription are verified for the sampled requests. This does not authorize P1 development or trading simulation; the separate USD/USDT valuation and paper-cost configuration gates remain unresolved.
