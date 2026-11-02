# Architecture

```mermaid
flowchart LR
  Alpaca[Alpaca BTC/USD] --> REST[REST quotes and closed 15-minute bars]
  Alpaca --> WS[Single leased WebSocket manager]
  REST --> Hub[FastAPI market cache and archive]
  WS --> Hub
  Hub --> UI[React chart and statistics]
  Hub --> Rules[Rule-driven A/B/C scheduler]
  Rules --> Risk[Risk and simulated execution]
  Risk --> Formal[Formal paper ledger]
  Hub --> Shadow[Forward shadow worker]
  Shadow --> ShadowDB[Separate shadow ledger]
  ShadowDB --> Review[40 closed trades: bounded review]
  Hub --> Charts[288-bar chart renderer]
  Charts -. optional research .-> Vision[Vision model adapter]
  Vision -. optional decisions .-> Risk
```

The current formal profile is rule-driven; visual inference is separate and must not be presented as the source of each formal decision. Shadow consumes the local API and maintains its own ledger. Review generation, historical comparisons and promotion are distinct; automatic deployment to formal accounts is not established. See [known issues](KNOWN_ISSUES.md).
