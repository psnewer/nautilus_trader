---
name: bug_pm_exec_connect_balance_fatal
description: PM ExecClient._connect makes the balance check a hard precondition; a transient network blip there hangs the whole node 120s and the trader never starts any actor (no discovery).
metadata: 
  node_type: memory
  type: project
  originSessionId: 218d30e5-cb09-4c32-bdb4-6d3c7214a6f7
---

PM `ExecClient._connect` (upstream `nautilus_trader/adapters/polymarket/execution.py:255`, called via `arb_execution.py:90 super()._connect()`) does "Checking account balance" against PM CLOB as a **hard connect precondition**. If `get_balance_allowance` hits a transient network error, py_clob_client raises `PolyApiException(status_code=None, error_message="Request exception!")` where `.error_msg` is a plain **string**, not a dict.

Two failure layers observed in a 2026-06-04 live smoke:
1. **Masking bug (FIXED 2026-06-04):** line 273 did `e.error_msg["error"] == ...` → `TypeError(string indices must be integers)` on the string error_msg, hiding the real network cause. Fixed with an `isinstance(e.error_msg, dict)` guard (execution.py:271) so the real `PolyApiException` surfaces.
2. **Fatal-precondition fragility (#59 retry mitigation removed by #272; debug-smoke mitigated 2026-06-08, #79):** because the balance check lives in `_connect` and re-raises, ExecEngine never connects → TradingNode waits the full **120s "Awaiting engine connections" timeout** → then kernel **skips `trader.start()`** → MarketMatchingActor/StrategyEvaluator stay at READY, never RUNNING → **no MatchedPair**. The node looks alive (logs "RUNNING") but is frozen. #59 曾在 `_update_account_state` 内加入 3×/2s 有界重试；2026-07-24 #272 按“默认不重试”原则撤销，当前每次余额刷新只请求一次，失败立即回到调用方。**#79 adds a debug-only mitigation**:when `skip_execution=true`, `SkipExecutionPolymarketClient._connect` tolerates transport-level `PolyApiException(status_code=None)` and returns connected with an explicit warning, because mock order IO cannot place true PM orders. API-level errors still raise, and production `ArbPolymarketExecutionClient` remains strict. Full non-fatal production connect-then-refresh remains an open decision.

**Key diagnostic tell:** if after `TradingNode: RUNNING` you see `ExecEngine.check_connected() == False` and actors stuck at READY (never RUNNING), the trader was never started — look upstream at an exec-client `_connect` failure, not at the actors. (Post-#59: InstrumentRefresher retired; the stuck actors are MarketMatchingActor/StrategyEvaluator.)

**Often transient but can span minutes:** 2026-06-04 it failed **2 consecutive smoke launches** (~minutes apart) then recovered on the 3rd. Trigger is a `Request exception!` (transport, status_code=None), not a config/credential problem. #272 后不再用调用内重试掩盖该故障。

**Old pre-migration reference (`src/arbitrage/services/risk/service.py` @ commit 5cf0538`_check_polymarket_health`):** the old design put the PM balance check inside a **periodic health-check loop** wrapped in `try/except Exception → log.warning + return False` — a network blip just marked health not-OK that round and retried; it never gated pipeline/discovery startup. Used `py_clob_client_v2` (fork) + defensive `response.get("balance", 0)`. The arb layer's `arb_execution.py:116 _run_health_check` is the migrated analog (periodic, non-fatal) — but the balance check itself stayed in upstream `_connect` as a fatal precondition. Whether to make connect tolerant of a transient balance failure (per old design) is an open design decision, not yet implemented.

Related: [[bug_polymarket_order_version_mismatch]] (other PM upstream issue), [[reference_live_test_skill]].
