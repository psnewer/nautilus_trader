---
name: feedback_verify_path_and_schema_source
description: "Two recurring mistakes to avoid in this migration — claiming validation from a passing test without confirming the code path ran, and trusting curated debug logs over legacy code for venue schemas."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 218d30e5-cb09-4c32-bdb4-6d3c7214a6f7
---

Two mistakes I made (2026-06-06) that the user called out; avoid repeating both.

**1. Don't claim an adapter is validated from a green test without confirming the code path actually ran.** I saw `OrbitExchExecutor - INFO - Order placed` and declared Gap C (NT `OrbitExchExecutionClient`) live-validated. But the `place_and_cancel` scenario runs the **legacy `src/arbitrage/services/` web_gateway stack**, and `OrbitExchExecutor` is *shared* between legacy and NT — its log line doesn't identify the caller. The NT client never ran.
**Why:** a shared component's signal was mistaken for a specific component's signal; I didn't know which stack the harness drives.
**How to apply:** before saying "X works/validated," grep the run log for X's *own* signature (the NT class name / `TradingNode` / a log line only X emits). Know which stack a test harness launches (`runner.py` → legacy web_gateway; NT path → `launchers/arb_node.py`). See [[gap_c_oe_exec_live_validated]].

**2. The authoritative source for a venue payload schema is the legacy code's actual field reads, not a curated debug log.** I concluded "OE CURRENT_BETS bet has no `side`" from odds_client's `CURRENT_BETS raw fields:` log — which prints only 5 selected fields. The legacy `orchestrator.py`/`tracker.py` actually read `side`, `sizePlaced`, `placedDate`, `price`, etc. My code worked but was built on a wrong, partial schema.
**How to apply:** when porting a venue payload to NT, first `grep` every `bet.get(...)`/`.get(...)` across the legacy consumers (orchestrator/tracker/service/odds_client/executor) to enumerate the real schema. Reuse the legacy *field semantics* as the single source of truth; the NT adapter writes thin translators producing NT events/reports (don't call the legacy code — it's being retired).
