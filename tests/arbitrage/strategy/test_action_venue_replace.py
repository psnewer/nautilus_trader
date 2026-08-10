"""VenueReplaceAction:非 PM 腿替换为同 outcome 的 PM 当前报价腿。"""

import asyncio
from unittest.mock import MagicMock

from src.arbitrage.strategy.actions.share_limit import ShareLimitModification
from src.arbitrage.strategy.actions.venue_replace import VenueReplaceAction
from tests.arbitrage.strategy._live_state import live_context


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _book(ask):
    book = MagicMock()
    book.best_ask_price.return_value = ask
    return book


def _ctx():
    return live_context(
        books={
            "Y.POLYMARKET": _book(0.40),
            "N.POLYMARKET": _book(0.55),
            "Y.ORBITEXCH": _book(0.35),
            "N.SHARPEXCH": _book(0.50),
        },
        infos={
            "Y.POLYMARKET": {"claim": "yes"},
            "N.POLYMARKET": {"claim": "no"},
            "Y.ORBITEXCH": {"claim": "yes"},
            "N.SHARPEXCH": {
                "claim": "no",
                "quote_claim": "no",
                "exec_instrument_id": "Y.SHARPEXCH",
            },
        },
    )


def _mixed_candidate():
    pm_yes = {
        "instrument_id": "Y.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "BUY",
        "price": 0.40,
        "prob": 0.40,
        "role": "yes",
        "claim": "yes",
        "qty": 80.0,
        "share_if_wins": 80.0,
        "cost": 32.0,
    }
    return pm_yes, {
        "candidate_id": "mixed",
        "rate": 0.1,
        "legs": [
            pm_yes,
            {
                "instrument_id": "N.SHARPEXCH",
                "exec_instrument_id": "Y.SHARPEXCH",
                "lay_price": 2.0,
                "venue": "SHARPEXCH",
                "side": "BUY",
                "price": 2.0,
                "prob": 0.50,
                "role": "no",
                "claim": "no",
                "qty": 60.0,
                "share_if_wins": 120.0,
                "cost": 60.0,
            },
        ],
    }


def test_pm_price_false_keeps_original_order_prob():
    ctx = _ctx()
    pm_yes, candidate = _mixed_candidate()
    ctx.scratch["candidates"] = [candidate]

    _run(VenueReplaceAction(pm_price=False).execute(ctx))

    candidate = ctx.scratch["candidates"][0]
    yes_leg, no_leg = candidate["legs"]
    assert yes_leg == pm_yes
    # pm_price=False:保持原 order 隐含概率 0.50(不用 PM ask 0.55),PM price=prob,
    # qty=share,cost=share×prob=60(与原 decimal 腿 cost 一致)。
    assert no_leg == {
        "instrument_id": "N.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "BUY",
        "price": 0.50,
        "prob": 0.50,
        "role": "no",
        "claim": "no",
        "qty": 120.0,
        "share_if_wins": 120.0,
        "cost": 60.0,
    }
    assert candidate["rate"] == 0.1


def test_default_uses_pm_live_price():
    ctx = _ctx()
    pm_yes, candidate = _mixed_candidate()
    ctx.scratch["candidates"] = [candidate]

    _run(VenueReplaceAction().execute(ctx))  # 默认 pm_price=True

    candidate = ctx.scratch["candidates"][0]
    yes_leg, no_leg = candidate["legs"]
    assert yes_leg == pm_yes
    # 默认用 PM 实时 ask 0.55(不用原 order prob 0.50);qty=share=120 不变,
    # cost=share×prob=120×0.55=66。
    assert no_leg == {
        "instrument_id": "N.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "BUY",
        "price": 0.55,
        "prob": 0.55,
        "role": "no",
        "claim": "no",
        "qty": 120.0,
        "share_if_wins": 120.0,
        "cost": 66.0,
    }
    assert candidate["rate"] == 0.1


def test_invalid_pm_price_param_raises():
    import pytest

    with pytest.raises(ValueError):
        VenueReplaceAction(pm_price="yes")


def test_legs_only_replaces_every_external_leg():
    ctx = _ctx()
    ctx.scratch["legs"] = [
        {
            "instrument_id": "Y.ORBITEXCH",
            "venue": "ORBITEXCH",
            "role": "yes",
            "share_if_wins": 75.0,
        },
        {
            "instrument_id": "N.SHARPEXCH",
            "venue": "SHARPEXCH",
            "role": "no",
            "share_if_wins": 90.0,
        },
    ]

    _run(VenueReplaceAction().execute(ctx))

    assert [leg["instrument_id"] for leg in ctx.scratch["legs"]] == [
        "Y.POLYMARKET",
        "N.POLYMARKET",
    ]
    assert [leg["share_if_wins"] for leg in ctx.scratch["legs"]] == [75.0, 90.0]
    assert [leg["qty"] for leg in ctx.scratch["legs"]] == [75.0, 90.0]


def test_candidate_without_corresponding_pm_quote_is_dropped():
    ctx = live_context(
        books={"Y.POLYMARKET": _book(0.40), "N.SHARPEXCH": _book(0.50)},
        infos={
            "Y.POLYMARKET": {"claim": "yes"},
            "N.SHARPEXCH": {"claim": "no"},
        },
    )
    ctx.scratch["candidates"] = [
        {
            "candidate_id": "missing-pm-no",
            "legs": [
                {
                    "instrument_id": "N.SHARPEXCH",
                    "venue": "SHARPEXCH",
                    "role": "no",
                    "share_if_wins": 100.0,
                },
            ],
        },
    ]

    _run(VenueReplaceAction().execute(ctx))

    assert ctx.scratch["candidates"] == []


def test_selected_candidate_and_legs_are_updated_together():
    ctx = _ctx()
    selected = {
        "candidate_id": "selected",
        "legs": [
            {
                "instrument_id": "Y.ORBITEXCH",
                "venue": "ORBITEXCH",
                "role": "yes",
                "share_if_wins": 50.0,
            },
        ],
    }
    ctx.scratch["selected_candidate"] = selected
    ctx.scratch["legs"] = selected["legs"]

    _run(VenueReplaceAction().execute(ctx))

    assert ctx.scratch["selected_candidate"]["legs"] == ctx.scratch["legs"]
    assert ctx.scratch["legs"][0]["instrument_id"] == "Y.POLYMARKET"
    assert ctx.scratch["legs"][0]["share_if_wins"] == 50.0


def test_cancel_plan_is_not_rewritten():
    ctx = _ctx()
    original = [{"instrument_id": "N.SHARPEXCH", "venue": "SHARPEXCH", "role": "no"}]
    ctx.scratch["legs"] = original
    ctx.scratch["cancel_pair_orders"] = True

    _run(VenueReplaceAction().execute(ctx))

    assert ctx.scratch["legs"] is original


def test_share_limit_after_replace_reads_pm_position_limit():
    class _Portfolio:
        def __init__(self):
            self.calls = []

        def outcome_shares_for_venue(self, pair_id, venue, account_id):
            self.calls.append((pair_id, venue, account_id))
            return {"yes": 80.0}

    ctx = _ctx()
    portfolio = _Portfolio()
    ctx.portfolio = portfolio
    ctx.scratch["legs"] = [
        {
            "instrument_id": "Y.ORBITEXCH",
            "venue": "ORBITEXCH",
            "role": "yes",
            "share_if_wins": 50.0,
        },
    ]

    _run(VenueReplaceAction().execute(ctx))
    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    # 替换后 venue 变 PM,share_limit 必须按 PM 持仓查额度(不再按原 ORBITEXCH)。
    assert portfolio.calls == [("p", "polymarket", None)]
    assert ctx.scratch["legs"][0]["instrument_id"] == "Y.POLYMARKET"
    assert ctx.scratch["legs"][0]["share_if_wins"] == 20.0
