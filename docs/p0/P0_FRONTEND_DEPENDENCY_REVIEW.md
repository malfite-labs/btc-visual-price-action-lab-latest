# P0 Frontend Dependency Review — V1.0.3

The React/TypeScript UI calls the local FastAPI contract only. It does not read Alpaca fields directly. The source/currency badge (`Alpaca US BTC/USD spot · USD research`) and `LIVE TRADING DISABLED` are visible on the workbench. Six navigation views exist.

| View | Backend surface | Status |
|---|---|---|
| Main workspace and shared candle chart | `/market/candles`, `/market/quote`, `/fills`, `/system/status`, `/events/stream` | Chart supports zoom/pan/crosshair through Lightweight Charts; A/B/C color and open/close/long/short markers derive from ledger fills. Actual fill data currently empty. |
| Agent accounts | `/agents`, `/agents/{agent}/account` | Three isolated USD_RESEARCH accounts, equity/mark status, position/stop fields; stale/unavailable mark is not reported as a zero estimate. |
| Journal and trade details | `/fills`, `/trades`, `/trades/{id}`, `/journal` | Agent filtering and 10-row UI pagination; click fill for detail. Decision journal accurately reports no model. |
| MD manager | `/strategies`, `/strategies/{agent}/versions`, `/strategies/{agent}/diff`, `/strategies/C/import` | Hashes, versions, C import to review-only candidate, core lock hash; no history overwrite or auto-activation. |
| Evolution | `/evolutions`, `/evolutions/{id}/comparison` | 50-trade progress and 7/14/21/30-day criteria shown; candidate performance remains unavailable until real full trades exist. |
| System/config | `/system/status`, `/market/sources` | REST/WS, source, model, paper mode, cost hypotheses and stale/incomplete gates. |

No indicator series are included. Market data is normalized server-side. UI tests only assert explicit spot/currency/trading-disabled labels; local API live rendering and screenshot evidence are collected separately. Remaining UI gaps: order-entry/decision action details cannot be populated until model and simulator orchestration are authorized and wired; no production trade sample exists; narrow-screen visual inspection is still required.
