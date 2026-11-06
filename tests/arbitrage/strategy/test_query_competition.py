"""CompetitionIsQuery:self_hits 按 PM/OE 原始赛事精确过滤。"""

import pytest

from src.arbitrage.strategy.queries.competition import CompetitionIsQuery
from tests.arbitrage.strategy._live_state import live_context


@pytest.mark.parametrize(
    ("values", "competition_raw", "expected"),
    [
        (["wta"], "wta", True),
        (["atp", "wta"], "wta", True),
        (["WTA"], "wta", False),
        (["wta"], "atp", False),
    ],
)
def test_matches_pm_raw_competition_exactly(values, competition_raw, expected):
    ctx = live_context(infos={"Y.POLYMARKET": {"competition_raw": competition_raw}})

    assert CompetitionIsQuery(**{"is": values}).matches(ctx) is expected


@pytest.mark.parametrize(
    ("values", "competition", "expected"),
    [
        (["WTA Beijing"], "WTA Beijing", True),
        (["wta beijing"], "WTA Beijing", False),
        (["WTA"], "WTA Beijing", False),
        (["ATP Masters", "WTA Beijing"], "WTA Beijing", True),
    ],
)
def test_matches_oe_competition_name_exactly(values, competition, expected):
    ctx = live_context(infos={"Y.ORBITEXCH": {"competition": "Tennis"}})
    ctx.cache.instrument("Y.ORBITEXCH").competition_name = competition

    assert CompetitionIsQuery(**{"is": values}).matches(ctx) is expected


def test_either_pm_or_oe_match_is_enough():
    ctx = live_context(
        infos={
            "Y.POLYMARKET": {"competition_raw": "atp"},
            "N.ORBITEXCH": {"competition": "Tennis"},
        },
    )
    ctx.cache.instrument("N.ORBITEXCH").competition_name = "WTA Beijing"

    assert CompetitionIsQuery(**{"is": ["WTA Beijing"]}).matches(ctx) is True


def test_does_not_use_grouped_info_competition():
    ctx = live_context(infos={"Y.POLYMARKET": {"competition": "wta"}})

    assert CompetitionIsQuery(**{"is": ["wta"]}).matches(ctx) is False


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"is": "wta"},
        {"is": []},
        {"is": [""]},
        {"is": [None]},
        {"include": "WTA"},
        {"is": ["wta"], "include": "WTA"},
    ],
)
def test_invalid_is_fails_fast(params):
    with pytest.raises(ValueError, match="competition"):
        CompetitionIsQuery(**params)
