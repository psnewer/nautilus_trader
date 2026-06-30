"""套利腿 venue 过滤:拒绝所有腿都来自同一 venue 的套利机会。"""

from __future__ import annotations

from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


class RequireCrossVenueCheck(Check):
    """要求套利 legs/candidates 至少包含两个 venue。

    放在生成 `ctx.scratch["legs"]` 或 `ctx.scratch["candidates"]` 的 Check 后面。
    Recovery 树可能天然是单腿补偿,不要在 recovery checktion 中使用本 Check。
    """

    def passes(self, ctx: EvalContext) -> bool:
        candidates = ctx.scratch.get("candidates")
        if candidates is not None:
            kept = [c for c in candidates if _has_multiple_venues(c.get("legs") or [])]
            if not kept:
                ctx.scratch.pop("candidates", None)
                return False
            ctx.scratch["candidates"] = kept
            ctx.scratch["cross_venue_filter"] = {
                "before": len(candidates),
                "after": len(kept),
            }
            return True

        legs = ctx.scratch.get("legs")
        if legs is not None:
            if not _has_multiple_venues(legs):
                ctx.scratch.pop("legs", None)
                return False
            return True

        return False


def _has_multiple_venues(legs: list[dict]) -> bool:
    venues = {str(leg.get("venue") or "").upper() for leg in legs if leg.get("venue")}
    return len(venues) >= 2
