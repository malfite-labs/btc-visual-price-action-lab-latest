# Frontend Data Provenance and Dependency Review V1.0.2

- Supersedes only the market-source-dependent statements in `docs/p0/P0_FRONTEND_DEPENDENCY_REVIEW.md`; original review remains preserved.
- Six frozen pages/routes and visual features remain unchanged.

## Required source labels and page dependencies

| Page | Required source label and data dependency |
|---|---|
| Main Workbench `/` | Shared chart header explicitly shows `Alpaca crypto spot · BTC/USD · location us · 15m`; show candle completeness, event/receive times and degraded state. Three independent account cards remain separate. Chart fill markers are only committed fills, A red/B green/C orange, tied to `fill_id` and source mode. |
| Trade Records `/trades` | Each row/detail includes market type/instrument/source id, entry/exit source refs, trigger kind, book id, paper-cost config version, quote and receive times. Show USD/USDT compatibility state; no net PnL while conversion gate is unresolved. |
| Strategies & MD `/strategies` | Same MD version, permission and diff contracts; include source/run epoch in evaluation evidence so different markets cannot be merged into one candidate result. |
| Journal `/journal` | Decision detail binds to normalized snapshot/source hash and completeness. If a required candle is missing, decision state is not silently presented as evaluated. |
| Evolution `/evolution` | Candidate/control comparison only within the same source id, market type, instrument, currency valuation rule and cost-config version; otherwise `INDETERMINATE`. Checkpoints/20-trade and promotion rules unchanged. |
| System Health `/system` | Display active source/provider location/market type/instrument, latest event and receive times, completeness, reconnect generation, streaming-vs-REST status, currency gate, protection reference, and `PROTECTION_UNCERTAIN` faithfully. |

Future source label is explicitly `Binance USDⓈ-M perpetual · BTCUSDT · mark-price stop reference` after a separate source-mode activation. Never use the generic title “BTC” alone where it could imply spot/perpetual equivalence. No new route, trading control, or feature is added.

## Dependency conclusion

Design coverage is specified, but runtime dependency readiness is not proven: anonymous latest REST was reachable in the audit, official websocket auth is required, a continuous stream was not tested, the history window had 31 missing buckets, and USD/USDT plus paper-cost config is unresolved. Existing API responses must expose these gates; frontend must not infer “live” from a successful REST request.
