# Strategies and research boundaries

The configured formal profile identifies A V2.5 as breakout/retest, B V3.0 as reversal, and C V3.1 as a Tony-inspired price-action approximation. Formal rules live in `config/rule_v26/`; original documents remain under `strategies/`. Signals use closed 15-minute data; rule-driven decisions are not model-autonomous orders.

The authorized `QUOTE_SIDE_FULL_FILL_V1` simulation uses bid or ask with 0.02% adverse slippage and a 0.05% fee per fill, with conservative rounding. A/B are limited to 1x and C to 5x; configured planned risk is 2%. Consult the code for validation, exits, margin and pending recovery. This is a simulation assumption, not observed order-book execution.

The separate forward worker maintains independent accounts; 40 closed trades may trigger a bounded review, not automatic promotion to the formal system.
