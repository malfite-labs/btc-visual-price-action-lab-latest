# Recovery notes

Protect active simulated positions and pending exits. Do not delete locks, replay missed entries, fabricate historic fills, or clear an unresolved shadow gap. The existing maintenance scripts have known limitations; read them and verify current process ownership, configuration and accounts before use.

`LONG_PROTECTION` is not evidence that REST and WebSocket are equally healthy. A connected stream alone does not authorize automatic re-arming. The protected-timeout retry fix is a source change pending live deployment verification. Verify one owner, fresh stream data, protection state, current ledger and subsequent natural decisions separately.
