---
name: bug_polymarket_order_version_mismatch
description: Polymarket POST /order 400 order_version_mismatch — did NOT reproduce on 2026-06-06 (PM placed+cancelled OK); likely resolved/transient, watch for recurrence.
type: project
originSessionId: 218d30e5-cb09-4c32-bdb4-6d3c7214a6f7
---
**Status (2026-06-06)**: Likely resolved / not reproducing — but note the path. `place_and_cancel` live run (non-skip, real money) placed the Polymarket leg fine (`Order placed: venue_order_id=0x62f1e76d…, status=live`) and cancelled it cleanly — **no order_version_mismatch**. Both legs placed→cancelled, scenario PASS. **Caveat**: this run was the **legacy `src/arbitrage/services/` pipeline** (`PolymarketExecutor`), NOT the NT `ArbPolymarketExecutionClient` — see [[gap_c_oe_exec_live_validated]]. So this confirms the legacy PM placement path works now; the NT-adapter PM path still needs its own live check via `launchers/arb_node.py`. Cause of the original failure was never definitively found; it cleared without a targeted code fix (venue-side-change hypothesis). **Do not assume PM placement fails** — if it recurs, resume the investigation below.

**Original status (2026-04-30)**: Open. Polymarket order placement failed with `PolyApiException[status_code=400, error_message={'error': 'order_version_mismatch'}]` on POST `/order`. OrbitExch leg placed fine; failure was Polymarket-only.

**Why**: Most likely a Polymarket venue-side change to the order schema or contract version that py-clob-client 0.34.6 (the latest available on PyPI) doesn't yet match. py-clob-client signs the EIP-712 domain with `version="1"` (`py_order_utils/builders/base_builder.py`); if the venue now expects a newer version, every signed order is rejected.

**How to apply**: Before suggesting code-level fixes for this error, remember what's already been ruled out so you don't waste a cycle on it:

- **Branch refactor is NOT the cause.** `feature/orbitexch-adapter` (baseline) and `refactor/adapter` (current) have byte-identical executor / odds_client code; the migration commit `4018e8796a` only changed import paths.
- **eth-utils / eth-typing v5 vs v6 is NOT the cause.** Tested with `eth-utils==5.3.1, eth-typing==5.2.1` on .venv: same error. `keccak` and `to_checksum_address` produce identical bytes across versions.
- **Network / proxy / API-key auth is NOT the cause.** L2-authenticated GETs (`/balance-allowance`, `/data/orders`, `/tick-size`, `/neg-risk`, `/fee-rate`) all return 200; only POST `/order` is rejected.
- **neg_risk lookup is fine.** `/neg-risk` returns 19-byte response, consistent with `{"neg_risk": false}` for binary tennis markets — the expected value.

Next investigation steps NOT yet tried (in order of cost):
1. Search github.com/Polymarket/py-clob-client issues for `order_version_mismatch` reports
2. Add a temp debug log in `nautilus_trader/adapters/polymarket/odds_client.py:239` dumping the signed order's full fields before posting, then compare against current Polymarket API docs
3. Run a bare-metal py-clob-client script (no wrapper) directly invoking `create_and_post_order` — if that also fails, definitively rules out the wrapper

Reproduction recipe: scenario `place_and_cancel` (`src/arbitrage/testing/scenarios/place_and_cancel.py`), `python3 -u -m src.arbitrage.testing --scenario place_and_cancel`. **As of 2026-06-06 this no longer reproduces** — both legs place (real money) and the scenario auto-cancels both (OE cancel is now wired, [[gap_c_oe_exec_live_validated]]). If it recurs (PM-only failure), the single-leg risk returns: [[bug_compensating_cancel_missing]] is still open, so a live OE leg can be left un-cancelled — cancel it manually.
