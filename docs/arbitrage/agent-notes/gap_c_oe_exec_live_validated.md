---
name: gap_c_oe_exec_live_validated
description: "Gap C status — legacy place_and_cancel still does NOT validate the NT adapter, but NT-native OE ExecutionClient Tier 1 place+cancel has now been live-validated (2026-06-08). Tier 2 true matched-fill remains open."
metadata: 
  node_type: memory
  type: project
  originSessionId: 218d30e5-cb09-4c32-bdb4-6d3c7214a6f7
---

**Correction (do not repeat the earlier mistake).** The `place_and_cancel` scenario (`python3 -u -m src.arbitrage.testing --scenario ...`) launches the **legacy** `src/arbitrage/services/` web_gateway pipeline via `src/arbitrage/testing/runner.py` (`WebGatewayService`) — **NOT** the NT `TradingNode`. So the 2026-06-06 real-money PASS validated the legacy `OrbitExchExecutor` + `OrbitExchOdds` path, which already worked pre-Gap-C. The shared `OrbitExchExecutor.place_order` placed the order, but called by the legacy `ExecutionService`, not by my NT client. Do not use that scenario as proof for NT-native Gap C.

**Lesson**: don't infer "the NT adapter works" from a scenario PASS — the scenario runs the legacy stack. Check the log for `TradingNode`/the specific NT client before claiming an adapter is validated.

**2026-06-08 — NT-native OE Gap C Tier 1 is now live-validated.** Controlled probe:
`python3 -m scripts.gapc_place_cancel_probe --config arb_config.example.json --confirm --size 7`

- True account / true OE login / true order, but non-matched design: `SELL(LAY) @ 1.01`, stake `7`, estimated liability `0.07 GBP`.
- Event: ATP Stuttgart 2026, Pierre-Hugues Herbert v Martin Landaluce, home selection.
- First true run proved `_submit_order` + CURRENT_BETS working + reconcile, then exposed `_cancel_order` bug: legacy cancel Order carried `venue_order_id` only, while `executor.cancel_order` requires `market_id`.
- Residual offer `221972467` was cleaned via probe `--cleanup-only`; final active order count was 0.
- Fix: `OrbitExchExecutionClient._cancel_one` now passes `market_id`/`selection_id` to `executor.cancel_order`, preferring instrument fields and falling back to `_current_bets[venue_order_id]`; regression `test_cancel_order_passes_market_id_from_current_bets`.
- Second true run passed end-to-end with offer `221973242`: `_submit_order → venue_order_id`, CURRENT_BETS working, `generate_order_status_reports`, `_cancel_order`, and cleanup-only active count 0.

**Current Gap C status**:
- ✅ OE `_connect` / login / general WS / real BALANCE frame.
- ✅ OE Tier 1 true place+cancel, non-matched (`SELL(LAY)@1.01`, stake 7).
- ✅ OE CURRENT_BETS unmatched working schema + reconcile path.
- ⬜ Tier 2 true matched-fill sample: `sizeMatched>0`, `averagePrice>0`, `generate_order_filled` values, and MAKER liquidity assumption.

**Still valuable from that run — the real CURRENT_BETS WS item schema** (captured live by `OrbitExchOdds` while a real OE order was live, `offerId=221832455` == the placed venue_order_id):
- Confirmed keys (odds_client `odds_client.py` reads these): `offerId` (== venue_order_id, the join key to the NT order), `marketId`, `selectionId`, `sizeRemaining` (>0 → active/working order), `sizeMatched` (>0 → filled/position), `averagePrice` (fill avg, 0.0 when unmatched), `profitNet`, `liability`.
- The `CURRENT_BETS raw fields: [...]` DEBUG log only prints a curated 5-field subset — the full bet dict also has marketId/sizeRemaining/sizeMatched (the derive code reads them; "1 active order" proves marketId+sizeRemaining present).
- **Only the UNMATCHED state was captured** (averagePrice=0, sizeMatched=0). A MATCHED/filled sample (sizeMatched>0, averagePrice>0) still needs a real fill to confirm — that's real money traded, not just placed-and-cancelled.
- Design target for consuming it: [[oe_order_receipt_design]] — NT exec `_on_current_bets` → `generate_order_*`/reports, reusing odds_client's field access as the single source of truth (architecture.md §3.2).

**2026-06-07 — OE connect path NOW genuinely live-validated (skip=true NT-node smoke)**: ran `python3 -m launchers.arb_node --config arb_config.example.json` (skip_execution=true → real connect, mock orders). Result:
- ✅ OE Gap C `_connect`/`_login` → real browser login to `/customer/` → `OrbitExchExecutionClient connected (live)` → `ExecClient-ORBITEXCH: Connected`.
- ✅ OE **general WS + real BALANCE frame → account state with REAL balance £37.49** (`_on_general_frame` balance branch + WS handler validated live; not just the 0.00 placeholder).
- ✅ Zero real orders (skip mocked them). All 3 DataClients connected.
- ❌ **PM exec `_connect` failed**: balance check `PolyApiException[status_code=None, Request exception!]` ×3 → `Error on '_connect'` — environmental network (couldn't reach CLOB), the [[bug_pm_exec_connect_balance_fatal]] pattern recurring; NOT Gap C.
- **Validated**: OE connect / login / general WS / balance-frame→account-state. **Still NOT live**: OE order placement / cancel / `_on_current_bets` fill receipt / order reconcile (need real orders, skip=false).

**2026-06-07 (#67) — connect path FULLY live-validated after fixing 2 bugs**: first smoke runs showed OE balance stuck at 0.00 (BALANCE frame never arrived). Two bugs found & fixed:
  1. **Missing post-login popup dismiss**: `_login` (ported from `scraper.py`) dropped `_handle_post_login_popup` — the popup overlay blocked the page so the general WS didn't push BALANCE/CURRENT_BETS. Added `_dismiss_post_login_popup`.
  2. **WS listener attached AFTER page created the WS**: `page.on('websocket')` only catches WS created *after* registration; `_connect` was `goto/_login` THEN `ws_handler.start()` → missed the general WS built during login nav. Fixed by moving `ws_handler.start()` BEFORE any `goto/_login` (both exec + data clients). Legacy odds_client comment confirms: "must attach intercept before goto, else miss WS creation."
  - **Result (smoke4)**: popup dismissed + OE account `0.00 → 37.49 GBP` + both legs Connected + MatchedPair + 0 ERROR. Connect path fully done.
  - Note (user Q): no conflict with design §4.3 "reload→re-subscribe" — page-level listener survives reload (reload swaps WS, not the page). §4.3 periodic-reload health-check is still an unwired TODO.

**how to live-test Gap C (#66/#78)**: `skip_execution` was made uniform = **real connect + mock order IO** (PM/OE aligned; the old OE `_connect` no-op was dropped). So:
- **Safe connect smoke (no real orders)**: run `python3 -m launchers.arb_node --config <arb_config.json>` with `debug.enabled=true` + `skip_execution=true`. This now **really logs into OE (browser) + opens general WS + emits account state**, and PM really auths CLOB — validating the Gap C connect/login/WS path — while mocking all order placement/cancel. This is the right first live step.
- **Controlled OE real order test** now exists as `scripts.gapc_place_cancel_probe.py`. Default dry-run logs in and prints the exact order. `--confirm --size 7` places a true `SELL(LAY)@1.01` order and cancels it; `--cleanup-only` checks/cancels existing OE active bets without placing.
- Needs an `arb_config.json` (only `arb_config.example.json` template exists) + creds in `.env`.

**Open**: Tier 2 true matched-fill validation + compensating-cancel trigger ([[bug_compensating_cancel_missing]]); PM NT adapter live validation.
