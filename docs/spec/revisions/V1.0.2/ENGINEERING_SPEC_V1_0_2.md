# BTC Visual Price Action Lab V1.0 — Engineering Specification V1.0.2

- Parent: `docs/spec/ENGINEERING_SPEC_V1_REVISED.md` (V1.0.1; preserved).
- Product authority: `PRODUCT_REQUIREMENTS_V1_0_2.md` overlays the V1.0.1 frozen baseline.
- Change record: `ENGINEERING_CHANGE_RECORD_V1_0_2.md`.
- Scope: read-only specification revision and Alpaca/Binance public-data audit. No code, application scaffold, credential, trade, or runtime was created or started.

## Architecture delta

M01 is now a provider-neutral Market Adapter boundary. Provider-specific Alpaca and future Binance connectors terminate there; M02 Candle Store, M03 renderer, M05/M06 visual decision, M08 paper execution, M09 ledger and M11 API consume only normalized contracts. No Alpaca JSON field names or Binance payload structures may escape M01. `source_id`, market type, instrument, price basis and source event time remain explicit provenance fields in normalized records.

| Module | V1.0.2 responsibility / dependency |
|---|---|
| M01 Market Adapter | Provider connectors, source configuration, transport health, timestamps, provenance, snapshots and reconnect gap signaling. Depends on public data only. |
| M02 Candle Store | Normalize bars, enforce UTC 15m buckets, paginate, deduplicate, detect gaps/late corrections, preserve trade count/volume and price-basis semantics. Depends on M01. |
| M03 Chart Renderer | Render only validated closed normalized candles; same byte-identical 288+96 source-bound image for A/B/C. |
| M04–M07 | Unchanged strategy registry, agent, vision and decision validation modules; bind every decision to the market snapshot/source fingerprint. |
| M08 Risk & Paper Execution | Consume normalized trade trigger, L2 book, currency/precision config, and internal paper fee/slippage config; reject if any required input is unresolved or stale. Never calls provider order APIs. |
| M09 Ledger & Journal | Store canonical instrument, currency, costs, source provenance, event/receive times and idempotency/version keys. Alpaca spot funding is explicitly not applicable. |
| M10–M13 | Existing evolution, read API/UI and supervisor responsibilities remain; they consume normalized records and surface source-health/gap state. |

Dependency remains acyclic: M01 → M02 → M03 → M05 → M06 → M07 → M08 → M09 → M10; M04 → M05/M10; M11 reads M01–M10; M12 reads M11; M13 observes and reports only. Provider adapters are leaf connectors under M01 and do not depend on M08/M09. No market-provider dependency is permitted in model, chart, execution or ledger modules.

## Market mode and provenance

- Active proposed mode: `ALPACA_CRYPTO_SPOT_US`, canonical instrument `BTC/USD`, base BTC, quote USD, market type `SPOT`, provider/venue scope Alpaca `us`.
- Alpaca official docs identify `us` as Alpaca US, `us-1` as Kraken US, `eu-1` as Kraken EU; latest provider statements distinguish these locations. Audit responses themselves carry symbol/time/price but no exchange-code field. Therefore store configured location and provider scope, not a fabricated per-trade exchange code.
- Alpaca crypto bars can use quote midpoint price during no-trade bars with volume 0. Preserve this field semantic; do not call every bar trade-only OHLC.
- Future mode: `BINANCE_UM_PERPETUAL`, instrument `BTCUSDT`, price basis for stop trigger `MARK_PRICE`; it is disabled until separately approved and validated.
- A source mode change creates a new run/config epoch. Never mix bar histories, stops, fills, equity valuations or evolution samples across modes.

## Data quality and runtime gates

Each 15m query must follow every `next_page_token`, deduplicate on `(source_id,instrument,interval,open_time)`, enforce expected bucket grid, and produce an explicit completeness report. An absent candle is not silently forward-filled. A late corrected bar is an immutable correction event with old/new hashes; it cannot mutate an already evaluated decision snapshot. A decision window with any missing required bucket is `INCOMPLETE` and must not trigger an AI decision. All three agents share identical validated snapshot hash.

Latest quote/book REST success is not proof of an authenticated continuous stream. The observed anonymous GET succeeded during this audit; official docs state live websocket authentication. Streaming, reconnect continuity and sustained freshness remain unverified. On stream disconnect, mark source `DEGRADED`, block new opens, resubscribe only after authorization, obtain a fresh book snapshot, record an unobserved interval, and keep protection uncertain until stop recovery can be proven from persisted trade events.

## Change to phase gates

P0 market-source feasibility is PARTIAL. Before P1 market/visual implementation can be declared ready, resolve currency denomination, internal paper-cost parameters, source freshness policy, and required data-only streaming access; settle whether the observed 31/672 historical bucket gaps are acceptable or represent a blocker (baseline fail-closed rule says incomplete required window blocks decisions). This is a readiness assessment, not authorization to implement P1.

P1–P6 ordering and product scope from V1.0.1 remain unchanged. Stage-specific P2 items remain allocated to P4/P6. No new product route or feature is introduced by this revision.
