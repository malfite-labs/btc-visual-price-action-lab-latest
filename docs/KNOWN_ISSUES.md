# Known issues

- **Protected WebSocket retry:** a `CONNECTION_TIMEOUT` previously ended the protected manager. A source fix passed 28 isolated tests; the last recorded deployment attempt was blocked, so live loading must be checked.
- **Formal protection:** `LONG_PROTECTION` previously stalled decisions. At 2026-11-01 12:06 China time, the local API reported `HEALTHY`, WebSocket `CONNECTED` and A/B/C WAIT decisions at 12:00. This is a time-bounded observation, not a guarantee; REST freshness alone does not establish WebSocket health.
- **Windows maintenance:** existing stop, mode-switch and recovery scripts can affect settings or components outside a targeted task. Verify process ownership, settings, locks and positions before use.
- **Shadow worker:** it was previously blocked by a frozen `market.py` hash change while B held a short. The API reported RUNNING and the 12:00 cycle at 2026-11-01 12:06 China time. Future recoveries and missed bars still require evidence; do not clear a halt or backfill fills.
- **Historical simulated performance:** as of 2026-11-01 10:55 China time, A had 0 closed trades, B 30 trades at -7.25% with 16.67% wins, C 11 trades at +3.13% with 63.64% wins. These small internal samples include modeled fees/slippage, exclude funding, and do not establish future returns.
- **Validation:** historical backtests reuse previously examined data; clean installation and the entire suite have not been verified for this release. Research artifacts need separate sanitization.
- **Editable installation:** `pip install -e .` fails on the current flat layout because setuptools discovers multiple top-level packages. The quick start installs declared dependencies directly; packaging remains uncorrected.
