# BTC Visual Price Action Lab V1.0 — Acceptance Matrix V1.0.2

- Parent: `docs/spec/ACCEPTANCE_MATRIX_V1.md` (V1.0.1; preserved).
- Adds market-source and provenance assertions without relaxing existing acceptance criteria.
- All fixtures are deterministic and versioned. Live-source evidence is separate from synthetic fixture evidence.

| ID | Test/evidence | Deterministic pass condition | Gate |
|---|---|---|---|
| MKT-ADP-01 | Alpaca fixture → normalized contract | BTC/USD symbol, `SPOT`, source/location, UTC time, decimal values and hashes map correctly; no provider field leaks from M01 | P1 |
| MKT-ADP-02 | Binance mark/UM fixture → normalized contract | Future adapter uses BTCUSDT/UM and mark-price typed reference; no enablement in Alpaca mode | P1 |
| MKT-ADP-03 | Adapter contract conformance | Same normalized quote/trade/book/candle inputs yield same downstream behavior independent of raw JSON schema | P1 |
| MKT-DATA-01 | 672-bucket fixture with pagination token, missing, duplicate and end-boundary bars | All pages consumed; duplicates removed; exact expected bucket grid and explicit missing set; incomplete window rejected, never filled forward | P1 |
| MKT-DATA-02 | zero-volume Alpaca bar | Preserve OHLC, `volume=0`, `trade_count`, fallback price-basis label; no trade-only claim | P1 |
| MKT-DATA-03 | late `updatedBars` correction | Append correction and preserve evaluated snapshot/hash; no silent mutation | P1 |
| MKT-DATA-04 | reconnect without sequence continuity | New generation plus gap; market degraded, opens blocked, protection uncertain until reconciliation | P1/P3 |
| MKT-REST-01 | Read-only public evidence replay | Store request UTC, URL, auth mode, status, latency and response hash; no API key, trading route or order call | P0/P6 |
| MKT-COMP-01 | Paired Alpaca spot and Binance UM 15m fixtures | Same UTC bucket only; report count/gaps and signed/absolute OHLC deviations by documented denominator; market types stay separate | P0/P6 |
| STP-SRC-01 | Alpaca/Binance trigger-reference fixtures | Alpaca trigger iff last trade crosses stop; Binance trigger iff mark price crosses; equality triggers; never mix source | P3 |
| STP-REC-04 | Trigger persisted then book missing/stale/restart | Pending durable before fill; no fake fill; recovered single fill only after valid book; repeated events cannot duplicate fee/ledger/close | P3 |
| RSK-CUR-01 | USD quote instrument vs USDT account without conversion | Reject sizing/entry/valuation and shadow comparison; no implicit 1:1 conversion | P3/P5 |
| RSK-COST-01 | Fixed book + fee/slippage config fixture | Walk L2, compute weighted price, apply side-adverse buffer, per-side fee, compute planned risk and 1x; exact reproducibility | P3 |
| RSK-COST-02 | Missing fee/slippage/precision config or insufficient book | Reject, never assume zero or fabricate depth/partial fill | P3 |
| UI-SRC-01 | Six-page fixture snapshots | Each page shows source/market/instrument context where relevant; main chart preserves A/B/C colors and only committed fill markers | P4 |
| UI-SRC-02 | Incomplete feed / REST-only / degraded recovery fixture | UI never says LIVE/PROTECTED; exposes gap/REST-only/source status and blocks opens consistent with API | P4 |
| EVO-SRC-01 | Shadow samples with different source/cost/currency epoch | Comparison is INDETERMINATE; no automatic promotion | P5 |

## V1.0.2 external evidence result (not a runtime acceptance)

On 2026-10-26, unauthenticated read-only Alpaca REST GETs returned HTTP 200; sampled RTTs: 15m historical bars 2363 ms, latest quote 1391 ms, latest orderbook 2705 ms, latest trade 1396 ms. One 7-day request required pagination. Replayed response records included a quote timestamp `2026-10-26T17:15:02.891946525Z`, trade timestamp `2026-10-26T17:13:12.988722787Z`, and latest-book timestamp in a later sample `2026-10-26T17:16:34.706389736Z`. These are point observations, not latency SLOs or proof of continuous stream freshness.

The sampled Alpaca and Binance sources do not satisfy market-data runtime acceptance: 31 of 672 expected Alpaca UTC 15m buckets were absent in the paired 2026-10-19…26 window, Binance archive had 672/672, and no historical position stop levels were provided to count actual stop-trigger disagreements. Mark the comparison artifact `OHLC_COMPARISON=COMPLETED`, `ACTUAL_STOP_TRIGGER_REPLAY=NOT_ASSESSABLE`.
