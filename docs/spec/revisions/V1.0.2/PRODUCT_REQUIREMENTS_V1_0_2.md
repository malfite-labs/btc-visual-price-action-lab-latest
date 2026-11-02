# BTC Visual Price Action Lab V1.0 — Frozen Product Requirements V1.0.2

- Status: approved source-market change; this is a controlled overlay on V1.0.1, not a rewrite of the archived source pack or V1.0.1 baseline.
- Parent baseline: `docs/spec/PRODUCT_REQUIREMENTS_V1_FROZEN.md` (V1.0.1; retained unchanged).
- Change authority: user instruction in this conversation dated 2026-10-26/27.
- Effective scope: data provenance, normalized market interface, stop trigger reference, and paper costs only. All unchanged PR-001…PR-015 semantics remain as in the parent baseline except the explicit amendments below.

## Frozen product invariants (unchanged)

Three independent visual agents A/B/C; BTC 15-minute naked-candlestick input; 72-hour/288-bar global plus 24-hour/96-bar local views from the same data; three independent virtual accounts; 1x maximum notional, 2% planned risk; A red `#E45454`, B green `#16A777`, C orange `#ED992F` actual-fill markers on one shared chart; immutable MD strategy versions and protected user-authored C core; every 50 closed round trips queues a review; shadow checkpoints on days 7/14/21/30, each side at least 20 complete trades, frozen net-return/average-win-loss-ratio/MDD gates, maximum 30 days, and activation only after incumbent positions naturally close. No real orders or exchange account connection.

## Approved amendments

| Requirement | V1.0.2 rule | Parent clause |
|---|---|---|
| PR-002 source | Initial source is Alpaca `us` crypto spot `BTC/USD`; the decision interval remains closed 15-minute candles and the same 288+96 view. Data is identified as Alpaca spot and never labelled Binance perpetual. Binance BTCUSDT USDⓈ-M is a future adapter only. | PR-002 |
| PR-005 currency | The risk budget remains 2% of decision-time account equity and the notional cap remains 1x. The existing account denomination is USDT; no USD=USDT parity is assumed. Until an approved USD/USDT conversion rule or account-unit amendment exists, sizing and fills are disabled. | PR-004/005 |
| PR-006 trigger | In Alpaca spot mode, stop trigger reference is the normalized Alpaca BTC/USD latest trade price (`trade.price`, event timestamp). Exit simulation uses a fresh normalized Alpaca L2 book and walks opposing levels. Future Binance mode maps its adapter's mark price as trigger reference. These are distinct source-specific references. | PR-006 |
| PR-008 costs | Alpaca mode has no Binance perpetual funding charge. Ledger records funding as `0 / NOT_APPLICABLE_SPOT` for the Alpaca spot instrument, not as an assumed market funding rate. Paper fees and adverse slippage are calculated by the internal cost model in `STATE_MACHINE_AND_RISK_SPEC_V1_0_2.md`; rates remain TBC until approved. | PR-008 |
| PR-014 data health | Any missing expected candle, stale/invalid quote/book/trade, source mismatch, or unproven post-reconnect continuity makes that feed degraded; new opens are blocked. Agents receive one common snapshot only after its source and completeness gates pass. | PR-014 |
| PR-015 evidence | Every rendered candle, decision, fill, account valuation and acceptance artifact carries source id, market type, instrument, event/receive time and evidence class. Alpaca spot evidence cannot satisfy a Binance-perpetual acceptance claim. | PR-015 |

## Explicit non-changes

No change to the three agents, chart timeframe/layout, account isolation, leverage/risk limits, marker colors, 50-trade review, shadow rules, or MD governance. Alpaca is a market-data source only: no Alpaca order API, broker account API, or order endpoint is in scope. Binance remains a future read-only data adapter option; no perpetual behavior is active in Alpaca mode.

## Configuration still not frozen

Pending approval/evidence: Alpaca streaming access and sustained freshness limits; internal paper taker fee rate; slippage buffer; internal quantity precision/minimum notional; USD-to-USDT valuation convention; model budget/timeout; C MD source/core boundaries; exact fee/slippage configuration versioning and effective date. Missing values fail closed and block affected runtime behavior; this document does not invent defaults.

## Version relation

V1.0.2 overlays V1.0.1 only where listed. V1.0.1 plus this document is the effective product baseline. Source pack, V1.0.1 files and their prior SHA-256 records remain unchanged.
