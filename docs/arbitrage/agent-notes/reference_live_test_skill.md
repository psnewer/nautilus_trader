---
name: reference_live_test_skill
description: Pointer to the project-level live-test skill that orchestrates human-in-loop scenario runs for the polymarket/orbitexch arbitrage system.
type: reference
originSessionId: 218d30e5-cb09-4c32-bdb4-6d3c7214a6f7
---
The user's preferred way to run live (real-money) tests on this system is the **`live-test` skill** at `.claude/skills/live-test/SKILL.md` (project-level).

The skill is invoked by user input like `/live-test <scenario>`, "实盘测试 <scenario>", or "跑一遍 <scenario>". It:
- Reads scenario classes from `src/arbitrage/testing/scenarios/<name>.py`
- Reuses the existing `src/arbitrage/testing/runner.py` for override application + scripted success/failure anchoring
- Adds a human-in-loop layer on top: I (Claude) tail the log via `Monitor`, surface non-scripted ERRORs to the user with a three-option menu (fix code / change config / abort), and never auto-act without confirmation

Per user instruction (2026-04-30), the skill stays **project-level until it matures**, then will be promoted to user-level. Don't unilaterally move it to `~/.claude/skills/`.

When the user mentions "live test" / "实盘测试" / asks me to run a scenario, prefer invoking this skill over rolling something ad-hoc.
