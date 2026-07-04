---
name: reference_live_test_skill
description: Pointer to the project-level live-test skill that orchestrates human-in-loop scenario runs for the polymarket/orbitexch/sharpexch arbitrage system on NautilusTrader.
type: reference
originSessionId: 218d30e5-cb09-4c32-bdb4-6d3c7214a6f7
updatedSessionId: feature/venues-session
---

The user's preferred way to run live (real-money) tests on this system is the **`live-test` skill** at `.claude/skills/live-test/SKILL.md` (project-level).

## Architecture (NautilusTrader)

The system runs on NautilusTrader framework:
- **Entry point**: `launchers/arb_node.py`
- **Config**: JSON files in `configs/` following `ArbConfig` schema
- **Debug mode**: `debug.enabled=true` + `debug.overrides` / `debug.mock_data` in config

## Invocation

The skill is invoked by user input like:
- `/live-test`
- "实盘测试"
- "live test"
- "跑一遍"

## What the skill does

1. **Phase 0 (Preflight)**: Reads config, optionally runs `--preflight-polymarket` to check geoblock/balance
2. **Phase 1 (Launch)**: Runs `python launchers/arb_node.py --config <path>`
3. **Phase 2 (Monitor)**: Tails log, tracks phase transitions (adapters connect → discovery → matching → strategy → execution)
4. **Phase 3 (Anomaly)**: Surfaces non-scripted ERRORs to user with three-option menu (fix code / change config / abort)
5. **Phase 4 (Teardown)**: Clean shutdown, report results

## Key differences from old services architecture

| Old (services) | New (NT) |
|----------------|----------|
| `src.arbitrage.testing` | `launchers/arb_node.py` |
| `web_gateway` + `/api/pipeline/start` | `TradingNode.run()` |
| `debug_config.json` separate file | `debug` section in main config JSON |
| `TestScenario` classes | Config files with debug overrides |

## Skill location policy

Per user instruction (2026-04-30), the skill stays **project-level until it matures**, then will be promoted to user-level. Don't unilaterally move it to `~/.claude/skills/`.

When the user mentions "live test" / "实盘测试" / asks to run the system, prefer invoking this skill over rolling something ad-hoc.
