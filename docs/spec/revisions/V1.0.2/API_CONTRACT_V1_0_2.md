# BTC Visual Price Action Lab V1.0 — Normalized Market API Contract V1.0.2

- Parent: `docs/spec/API_CONTRACT_V1.md` (V1.0.1; unchanged and preserved).
- API namespace remains `/api/v1`; responses keep `schema_version`, `request_id`, UTC timestamps, decimal-string money/price/quantity, and structured error semantics.
- All routes are internal read APIs. No Alpaca/Binance order, trading account, signing, deposit, or broker command API is defined.

## Source-independent public/internal contract

### `GET /api/v1/market/sources`

Returns configured source modes and state: `source_id`, `provider`, `provider_location`, `market_type`, `instrument`, `base_currency`, `quote_currency`, supported event types, active flag, last event/receive times, health, and source configuration hash. Alpaca mode must render `ALPACA_CRYPTO_SPOT_US / SPOT / BTC/USD`; Binance mode, if later enabled, must render `BINANCE_UM_PERPETUAL / USD_M_PERPETUAL / BTCUSDT`.

### `GET /api/v1/market/candles`

Inputs: `source_id`, `instrument`, `interval=15m`, `from`, `to`, `closed_only=true`, optional internal cursor. Output `MarketCandle[]`, `completeness`, `expected_count`, `received_count`, `missing_open_times[]`, `duplicate_count`, `late_corrections[]`, `next_cursor`, and `data_basis`. Normalized candle fields: `source_id`, `market_type`, `instrument`, `interval`, `open_time_utc`, `close_time_utc`, decimal OHLC, decimal `volume_base`, `trade_count|null`, `is_closed`, `price_basis` (`TRADE_ONLY`, `TRADE_AND_QUOTE_MID_FALLBACK`, `PROVIDER_DEFINED`), `provider_event_time|null`, `received_at_utc`, `source_record_hash`. Alpaca mapping preserves `n` and `v`; zero volume does not imply missing OHLC. Pagination is mandatory until cursor is empty.

### `GET /api/v1/market/quote`

Returns normalized best bid/ask: `source_id`, `market_type`, `instrument`, `bid_price`, `bid_size`, `ask_price`, `ask_size`, `event_time_utc`, `received_at_utc`, local `age_ms`, `health`, `quote_id`. It does not contain generic `mark_price`; trigger reference is a separate typed object.

### `GET /api/v1/market/orderbook`

Returns timestamped L2 `bids[]` and `asks[]` price/size levels, `source_id`, `instrument`, `event_time_utc`, `received_at_utc`, `book_id`, `book_kind=SNAPSHOT|DELTA`, `depth_count`, `health`, `age_ms`. Alpaca REST latest orderbook is mapped as a snapshot. REST evidence in this audit showed 86 bid and 98 ask levels; this is the observed response size, not a frozen guaranteed depth. Never assume hidden depth. Reject fill simulation if book does not cover requested quantity or book freshness is unknown.

### `GET /api/v1/market/trade`

Returns normalized latest trade/event: `trade_id`, `source_id`, `instrument`, `price`, `size`, `event_time_utc`, `received_at_utc`, `taker_side|null`, `health`. Alpaca mappings: `i,p,s,t,tks` respectively. In Alpaca spot mode this normalized trade price is the stop trigger reference.

### `GET /api/v1/market/funding-events`

Returns an empty collection in Alpaca spot mode with `applicability=NOT_APPLICABLE_SPOT`, `source_id=ALPACA_CRYPTO_SPOT_US`. It must not query or accrue Binance funding. Future Binance mode enables typed funding events from its own adapter; the Alpaca ledger retains no synthetic funding values.

### `GET /api/v1/market/status`

Returns per-event-type connectivity, latest provider event and local receive time, age, reconnect generation, pagination/candle completeness status, last known gap, and protection impact. `LIVE` is forbidden without a configured freshness policy and successful stream health; REST reachability alone is `REST_REACHABLE`.

## Existing API amendments

- `GET /api/v1/system/status` includes `active_market_source`, `market_type`, `instrument`, `market_health`, `protection_reference`, `currency_conversion_health`, and source-scoped last event times.
- `GET /api/v1/agents/{id}/account` and equity curve declare ledger currency. They may not compare a USD instrument notional to USDT equity without an approved conversion snapshot/source.
- `GET /api/v1/trades`, `/fills`, `/decisions/{id}` include immutable market source, market type, instrument, source record ids/hashes, trigger reference and cost-config version.
- Existing `mark_price` field is removed from generic `MarketQuote`; it is replaced by `TriggerReference {kind,price,event_time_utc,source_id,event_id}`. This avoids treating Alpaca spot trades as Binance marks.

## Alpaca adapter mapping (M01 only)

| Alpaca payload | Normalized field |
|---|---|
| Path location `us`; symbol `BTC/USD` | source `ALPACA_CRYPTO_SPOT_US`, market `SPOT`, instrument `BTC/USD` |
| Bar `t,o,h,l,c,v,n,vw` | candle time/OHLC/base volume/trade count/VWAP; zero volume gets `TRADE_AND_QUOTE_MID_FALLBACK` eligibility, not false trade provenance |
| Quote `bp,bs,ap,as,t` | bid/size/ask/size/event time |
| Trade `i,p,s,t,tks` | trade id/price/size/event time/taker side |
| Orderbook `b[],a[],t` and level `p,s` | timestamped L2 snapshot levels; receive time assigned locally |
| Stream bars/updatedBars | immutable bar create/correction events; updated bar never silently overwrites a decision snapshot |

Alpaca's provider payload does not supply a normalized cross-provider sequence number in the evidenced REST records. M01 assigns a local receive sequence scoped to connection generation; a reconnect starts a new generation and marks the gap unproven until a fresh snapshot/reconciliation succeeds.

## Authentication and observed availability

No credential was used. During the 2026-10-26 audit, unauthenticated REST GETs for historical bars, latest quote, latest orderbook and latest trade returned HTTP 200. This is an observation, not a promised access policy. Official Alpaca documentation says historical crypto data is an authentication exception; latest market data and streaming docs describe authenticated access. No websocket was opened. Do not search local secret stores or use trading credentials. Any future credential must be data-only and separately authorized.
