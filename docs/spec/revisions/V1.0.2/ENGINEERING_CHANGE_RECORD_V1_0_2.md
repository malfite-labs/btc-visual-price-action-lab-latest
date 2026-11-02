# Engineering Change Record — V1.0.2

| Field | Value |
|---|---|
| Change id | ECR-MARKET-SOURCE-001 |
| Approval | Explicit user-approved source change in current conversation, 2026-10-26/27 |
| Parent baseline | V1.0.1 PRD/engineering/API/risk/acceptance specs; originals and archive retained unchanged |
| New revision | V1.0.2 overlay in `docs/spec/revisions/V1.0.2/` |
| Scope | Alpaca BTC/USD spot initial public market data; provider-neutral adapter; source-specific spot stop reference; paper fee/slippage model; provenance labels and tests |
| Out of scope | Alpaca/Binance order APIs, exchange accounts, live/paper trade loop execution, application code, dependencies, deployment and P1–P6 implementation |

## Approved decisions preserved

Three independent visual AIs, 15m naked candles, global 3-day + local 1-day views, three separate virtual accounts, 1x leverage, 2% risk, A red/B green/C orange markers, immutable MD/C-core protection, 50-trade review, shadow checkpoints days 7/14/21/30, >=20 complete trades per side, frozen metrics, maximum 30 days, auto-upgrade pending natural incumbent close.

## Controlled delta

1. Initial market source changes from Binance BTCUSDT perpetual to Alpaca `us` BTC/USD spot. Alpaca data is never represented as Binance perpetual or mark price.
2. M01 becomes a source-neutral adapter boundary; downstream contracts use normalized records and source provenance.
3. Alpaca spot stop trigger is latest trade price; exit simulation uses the opposing side of a fresh Alpaca L2 book and internal deterministic paper costs. Future Binance mode uses typed mark-price trigger.
4. Alpaca mode has no Binance funding; ledger records not applicable. Internal fee/slippage formulas are explicit; numeric values remain pending approval.
5. UI and acceptance label market/provider/instrument and separate spot/perpetual performance epochs.

## Evidence and unresolved gates

Official docs: Alpaca historical crypto bars path supports `15Min`, `BTC/USD`, pagination; historical crypto data is an auth exception; latest quote/orderbook/trade and websocket contracts are documented. Latest REST calls also returned 200 without auth in this single audit, but websocket and sustained feed were not tested. Binance official USDⓈ-M `data.binance.vision` daily 15m archives were readable for 2026-10-19…25. Paired 7-day data: 641 common intervals, 31 missing Alpaca intervals; signed median close basis Alpaca minus Binance UM = -0.269314% of Binance close; absolute median = 0.535820%, P95 = 1.819998%, maximum = 3.120944%. Spot-vs-perpetual divergence is real and material.

Still blocks runtime readiness: no continuous websocket test/auth, missing 31 Alpaca bars in the sample, unknown missing-candle policy is fail-closed, no historical strategy stop levels for trigger-difference replay, USDT account vs USD instrument conversion rule unresolved, paper fee/slippage values and precision not approved, and CFG-09 remains awaiting user MD import. This is not a claim that a market feed is unavailable forever; it records the evidence and present gates.

## Change control

No V1.0.1 file or archived original was edited. New V1.0.2 revision files are additive. `docs/SHA256SUMS.txt` is regenerated to cover all payload files; preserved source-file hashes are rechecked against their previous values. This ECR and audit results are not implementation authorization.
