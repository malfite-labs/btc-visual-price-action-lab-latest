# API Contract V1.0.3 — implementation overlay

All routes are local-only under `http://127.0.0.1:8000/api/v1`. Alpaca credentials are used only by fixed Alpaca market-data URLs and never returned from any API. No order, broker paper-order, account, balance, or position-provider route exists. Paper ledger views are internal data.

| Method | Route | Behavior |
|---|---|---|
| GET | `/health` | Service version and live-trading disabled invariant. |
| GET | `/system/status` | Source, USD_RESEARCH, REST/WS/data-health, last quote age, model state, safety mode, research cost assumptions. |
| GET | `/market/candles?limit=672` | Normalized closed 15m candles; source and currency labels, count and completeness status; provider decimal fields retained as strings. |
| GET | `/market/quote` | Latest normalized bid/ask and sizes plus provider and receive timestamps. |
| GET | `/market/orderbook` | Read-only latest depth, source-tagged; unavailable is an explicit status. |
| GET | `/market/snapshot` | Creates one no-indicator 288-global/96-local PNG only with ≥288 closed bars; returns image and candle hashes. |
| GET | `/market/snapshot/image` | Returns the last generated PNG. |
| GET | `/agents` | Agent identity/status and isolated paper account/strategy references. |
| GET | `/agents/{A\|B\|C}/account` | Internal account state only. |
| GET | `/agents/{A\|B\|C}/decisions` | Empty with MODEL_NOT_CONFIGURED until an authorized model is implemented/configured. |
| GET | `/fills`, `/trades`, `/trades/{trade_id}` | Ledger-derived fills/round trips. Chart marks must originate from fills. |
| GET | `/journal` | Model/decision journal state; no fictitious decisions. |
| GET | `/strategies` | Versions, hashes, C import state. |
| GET | `/strategies/{A\|B\|C}/versions` | Agent-specific version history. |
| GET | `/strategies/{A\|B\|C}/diff?left=&right=` | Read-only version diff. |
| POST | `/strategies/C/import` | Candidate import only, validates user core section, never overwrites a prior file or auto-promotes. |
| GET | `/evolutions?agent=` | Persistent full-trade counter, queued review status and 7/14/21/30 checkpoint plan. |
| GET | `/evolutions/{experiment_id}/comparison` | Metrics gate/status; no comparison unless valid samples exist. |
| GET | `/audit/events` | Audit events; currently none unless ledger operation is performed by internal service/tests. |
| WS | `/events/stream` | Local market/health/candle events. Disconnect/reconnect status must remain visible. |

## Market event schema

`MarketCandle` fields: `symbol`, `interval`, `open_at`, `close_at`, decimal-text `open/high/low/close/volume/vwap`, `trade_count`, `is_closed`, `source=ALPACA_US_BTCUSD_SPOT`, `currency=USD_RESEARCH`, `data_basis`. Zero-volume provider bars are retained with provider-supplied OHLC; no candle is synthesized.

`MarketQuote`: `bid`, `ask`, `bid_qty`, `ask_qty` (decimal text), `event_at`, `received_at`, `age_ms`, `source`, `currency`. WS quotes use Alpaca event timestamp and local receive timestamp. Alpaca prices are spot quote prices, never Binance mark price.

The API exposes read routes and the C source import only. No decision-to-order HTTP route exists; live model invocation and automatic paper scheduling stay disabled.
