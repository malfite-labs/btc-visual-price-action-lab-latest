# Implementation change record — V1.0.3

- Derives from frozen V1.0.1 documents and archived source pack, plus the additive V1.0.2 Alpaca spot source change.
- Authorized implementation request supersedes earlier handoff text that prohibited development; it does not authorize real orders, paid model calls, or changes to old project.
- Alpaca BTC/USD is explicitly spot data from Alpaca US. Internal accounts use USD_RESEARCH, not USDT. USDT mode remains disabled pending an approved auditable FX source.
- Implements application scaffold, normalized data adapter, paper ledger/risk/state primitives, dashboard, tests, and local launch scripts. Existing frozen source documents and archives remain immutable.
- Real visual model calls, C core strategy import, and 7–30 day live shadow evidence remain blocked; no fake/test output counts as real evidence.
- Configuration assumptions are explicitly non-production: 10 bp fee per side, 10 bp adverse slippage per side, 2,000 ms quote/book age, 0.01 USD price tick, 1e-8 BTC quantity increment, 1 USD minimum notional, 50 bp simulated price guard.
- P1 current-data acceptance requires 288 consecutive closed 15m bars, no duplicates, no non-15m spacing, and live fresh quote; an incomplete feed is fail-closed.
