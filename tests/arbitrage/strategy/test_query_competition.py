"""CompetitionIncludeQuery:self_hits 按 OE 赛事名称子串过滤。"""

import pytest

from src.arbitrage.strategy.queries.competition import CompetitionIncludeQuery
from tests.arbitrage.strategy._live_state import live_context


@pytest.mark.parametrize(
    ("include", "competition", "expected"),
    [
        ("WTA", "WTA Tokyo 2026", True),
        ("WTA", "wta beijing", True),          # 大小写不敏感
        ("WTA", "ATP Masters 1000 Shanghai", False),
        ("WTA", "ATP Challenger Porto", False),
    ],
)
def test_matches_oe_competition_substring(include, competition, expected):
    ctx = live_context(infos={"Y.ORBITEXCH": {"competition": "Tennis"}})
    ctx.cache.instrument("Y.ORBITEXCH").competition_name = competition

    assert CompetitionIncludeQuery(include).matches(ctx) is expected


def test_pm_only_pair_fails_closed():
    ctx = live_context(infos={"Y.POLYMARKET": {"claim": "yes"}})

    assert CompetitionIncludeQuery("WTA").matches(ctx) is False


def test_does_not_use_grouped_info_competition():
    ctx = live_context(infos={"Y.ORBITEXCH": {"competition": "WTA Beijing"}})
    ctx.cache.instrument("Y.ORBITEXCH").competition_name = "ATP Masters Shanghai"

    assert CompetitionIncludeQuery("WTA").matches(ctx) is False


@pytest.mark.parametrize("include", ["", "  ", None])
def test_invalid_include_fails_fast(include):
    with pytest.raises(ValueError, match="include"):
        CompetitionIncludeQuery(include)
