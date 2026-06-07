---
name: user_arbitrage_owner
description: User runs polymarket/orbitexch arbitrage on personal real-money accounts; live tests place actual orders that can lock or lose funds.
type: user
originSessionId: 218d30e5-cb09-4c32-bdb4-6d3c7214a6f7
---
The user owns and operates the polymarket / orbitexch arbitrage system in this repo against **live, real-money accounts**:

- Polymarket: real proxy wallet (Gnosis Safe), signature_type=2, EOA + funder configured in `src/arbitrage/services/web_gateway/default_config.json`
- OrbitExch: account with ~30 GBP balance (observed 2026-04-30), session-cookie auth
- Tennis ATP Madrid 2026 was the active market during testing

Implications for collaboration:
- Any "live test" / "实盘测试" places **real orders** on real venues. Even with extreme prices designed not to fill, partial fills or single-leg-success-other-leg-fail can leave live exposure or locked funds.
- Always announce money-side risk before launching test runs and after partial-failure outcomes (e.g. "OrbitExch order X is still live, please cancel manually").
- User is comfortable with this risk and proceeds intentionally — do not refuse to launch, just be transparent.
- Compensating cancel is currently NOT auto-wired (see project memory `bug_compensating_cancel_missing.md`); a single-leg fail leaves the other leg orphaned.
