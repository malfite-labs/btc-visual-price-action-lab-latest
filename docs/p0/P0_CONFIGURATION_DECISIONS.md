# P0 Configuration Decisions — implementation V1.0.3

Parameters are centralized in `backend/settings.py` and `backend/domain.py`. Unless marked observed, the values below are research assumptions that require review before user trial; none is presented as an Alpaca trading fee or calibrated execution statistic.

| CFG | Implemented setting/evidence | State and approval | Fail-closed behavior / acceptance |
|---|---|---|---|
| CFG-01 | Quote and book freshness ≤2,000ms; live quote sample observed at 870ms | Research assumption; user approval/calibration required | Reject entry/exit quote if stale; `MKT-02/03`, `STP-03` |
| CFG-02 | Fee 10bp on each simulated fill | Research assumption only; not an Alpaca published/account-specific fee | Non-zero fee reserve; reject below fee; `RSK-02`, `LED-02` |
| CFG-03 | Additional adverse slip 10bp/side; depth-walk VWAP; 50bp L1 deviation guard | Research assumption; user approval/calibration required | Reject insufficient depth/price guard; `RSK-02`, `STP-01` |
| CFG-04 | Simulated tick $0.01, BTC step/minimum 1e-8, minimum notional $1 | Internal research precision, not an exchange order filter | Floor quantity and recheck; reject if outside rule; `RSK-02/03` |
| CFG-05 | Model timeout 60s, 0 retries, decision deadline at next cycle boundary | Conservative code default; provider/budget not selected or authorized | MODEL_NOT_CONFIGURED; stale/late decision rejected; no paid requests; `AG-01/04` |
| CFG-06 | At most one recovery attempt per checkpoint; 30-day deadline unchanged | Bounded implementation policy; not based on provider reliability history | No timer extension; failure/expiry cannot promote; `EVO-06/07` |
| CFG-07 | Alpaca spot: funding `NOT_APPLICABLE`; future Binance adapter is disabled | Frozen market-source distinction | No funding charge or Binance mark price applied to spot; `MKT-01`, `LED-02` |
| CFG-08 | Internal USD_RESEARCH accounts, 10,000.00 per agent, one-position 1x collateral reserve | Implemented paper-account definition; USD/USDT FX is not assumed | No conversion or mixed equity curves; `RSK-01/03`, `LED-03` |
| CFG-09 | C remains `C_NOT_CONFIGURED`; import validates core section and stores hash as candidate | User must supply/approve original MD and evolvable sections | No C model calls/decisions/upgrade; `STR-01`, `AG-02`, `EVO-05` |

All accounts and pages show `USD_RESEARCH`. Formal USDT remains blocked until a trustworthy, auditable conversion source and temporal alignment rule are separately approved. No external model credential was searched or imported. `MODEL_CALLS_ENABLED` is not enabled in production code.
