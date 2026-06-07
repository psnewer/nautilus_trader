---
name: bug_compensating_cancel_missing
description: Two-leg arbitrage has no compensating cancel — when one leg places successfully and the other fails, the successful leg sits live on the venue and locks funds. Manual cancel is currently the only recovery.
type: project
originSessionId: 218d30e5-cb09-4c32-bdb4-6d3c7214a6f7
---
When the system attempts a two-leg arbitrage (Polymarket + OrbitExch) and one leg succeeds while the other errors out (e.g. Polymarket `order_version_mismatch` failure with OrbitExch already placed), there is **no automatic rollback**. The successful leg stays live on the venue with locked margin until manually cancelled.

**Why**: The `cleanup_enabled` path in `RiskService.initialize_cleanup` and `PostSessionCleanup` is **post-session merge & redemption** of CTF tokens, not real-time leg-rollback. The realtime "cancel the other leg if this leg fails" mechanism either doesn't exist or isn't wired up in `ExecutionOrchestrator`. Confirmed empirically across multiple test runs (2026-04-29 and 2026-04-30) — every PM-fail / OE-success combo required manual cancel via the OrbitExch web UI.

**How to apply**:
- Before launching any live test that places real orders, remind the user that single-leg failures will need manual cleanup.
- If you propose adding a new live-test scenario, point out that compensating cancel is missing as part of the test plan — don't pretend it exists.
- This is **a real-money safety gap**, not a test-framework problem. Filing a fix is independent from any test work and should not be bundled into a "fix the test" PR.
- Do not bypass via `skip_check_open_orders`-style overrides without saying so explicitly — leftover orders also feed back into the next run's risk checks (the "Open orders detected" gate).
