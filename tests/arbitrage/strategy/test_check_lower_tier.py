"""LowerTierCheck 复用 venue_replace 的 OE 原始赛事分级。"""

import pytest

from src.arbitrage.strategy.checks.lower_tier import LowerTierCheck
from tests.arbitrage.strategy._live_state import live_context


@pytest.mark.parametrize(
    "competition",
    [
        "ATP Challenger Porto 2",
        "WTA Beijing",
        "UTR Pro Tennis Series",
        "ITF M25 Monastir",
    ],
)
def test_lower_tier_matches_oe_raw_competition(competition):
    ctx = live_context(infos={"Y.ORBITEXCH": {"competition": "Tennis"}})
    ctx.cache.instrument("Y.ORBITEXCH").competition_name = competition

    assert LowerTierCheck().passes(ctx) is True


def test_lower_tier_does_not_use_grouped_competition_or_missing_oe():
    grouped = live_context(infos={"Y.ORBITEXCH": {"competition": "WTA Beijing"}})
    grouped.cache.instrument("Y.ORBITEXCH").competition_name = "ATP Masters 1000 Shanghai"
    pm_only = live_context(infos={"Y.POLYMARKET": {"claim": "yes"}})

    assert LowerTierCheck().passes(grouped) is False
    assert LowerTierCheck().passes(pm_only) is False
