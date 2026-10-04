"""VenueReplaceAction:非 PM 腿替换为同 outcome 的 PM 当前报价腿。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.strategy.actions.share_limit import ShareLimitModification
from src.arbitrage.strategy.actions.venue_replace import VenueReplaceAction
from tests.arbitrage.strategy._live_state import live_context


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _book(ask, bid=None):
    book = MagicMock()
    book.best_ask_price.return_value = ask
    book.best_bid_price.return_value = bid
    return book


def _ctx(*, with_start=True):
    ctx = live_context(
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
    if with_start:
        store = PairPriceStore(ctx.cache)
        store.initialize(ctx.pair_id, ("yes", "no"))
        store.capture_start(ctx.pair_id, {"yes": 0.40, "no": 0.60})
    return ctx


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


def _dynamic_ctx(*, yes_ask, no_ask, yes_bid, no_bid, start_yes, start_no):
    ctx = live_context(
        books={
            "Y.POLYMARKET": _book(yes_ask, yes_bid),
            "N.POLYMARKET": _book(no_ask, no_bid),
        },
        infos={
            "Y.POLYMARKET": {"claim": "yes"},
            "N.POLYMARKET": {"claim": "no"},
        },
    )
    store = PairPriceStore(ctx.cache)
    store.initialize(ctx.pair_id, ("yes", "no"))
    store.capture_start(ctx.pair_id, {"yes": start_yes, "no": start_no})
    return ctx


def _with_period(ctx, period):
    instrument_ids = ctx.pair_registry.instrument_ids_for_pair(ctx.pair_id)
    ctx.pair_registry.register(ctx.pair_id, instrument_ids, game_id=7)
    ctx.sports_store = SimpleNamespace(
        get=lambda game_id: SimpleNamespace(period=period) if game_id == 7 else None,
    )
    return ctx


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


def test_invalid_convert_param_raises():
    with pytest.raises(ValueError, match="convert must be a boolean"):
        VenueReplaceAction(convert="true")


@pytest.mark.parametrize("name", ["deviate_convert", "attitude"])
def test_invalid_dynamic_convert_param_raises(name):
    with pytest.raises(ValueError, match=f"{name} must be a boolean"):
        VenueReplaceAction(**{name: "true"})


@pytest.mark.parametrize("value", [True, False, "true", "invalid"])
def test_invalid_tier_convert_param_raises(value):
    with pytest.raises(ValueError, match="tier_convert must be 'pre' or 'post'"):
        VenueReplaceAction(tier_convert=value)


@pytest.mark.parametrize("value", [True, "invalid", float("nan"), float("inf")])
def test_invalid_tier_ignore_param_raises(value):
    with pytest.raises(ValueError, match="tier_ignore must be a finite number"):
        VenueReplaceAction(tier_ignore=value)


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "1"])
def test_invalid_set_exempt_param_raises(value):
    with pytest.raises(ValueError, match="set_exempt must be a positive integer"):
        VenueReplaceAction(set_exempt=value)


def test_set_exempt_disables_tier_convert_and_keeps_default_replacement():
    ctx = _with_period(_ctx(), "S2")
    ctx.cache.instrument("Y.ORBITEXCH").competition_name = "WTA Beijing"
    ctx.scratch["legs"] = [{
        "instrument_id": "Y.ORBITEXCH",
        "venue": "ORBITEXCH",
        "role": "yes",
        "prob": 0.35,
        "share_if_wins": 75.0,
    }]

    _run(VenueReplaceAction(tier_convert="pre", set_exempt=2).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "Y.POLYMARKET"


def test_set_exempt_disables_convert_for_native_pm_leg():
    ctx = _with_period(_ctx(), "S2")
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(VenueReplaceAction(convert=True, set_exempt=2).execute(ctx))

    assert ctx.scratch["legs"] == [pm_yes]


def test_set_exempt_disables_attitude():
    ctx = _with_period(_dynamic_ctx(
        yes_ask=0.41,
        no_ask=0.59,
        yes_bid=0.40,
        no_bid=0.58,
        start_yes=0.40,
        start_no=0.60,
    ), "S2")
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(VenueReplaceAction(attitude=True, set_exempt=2).execute(ctx))

    assert ctx.scratch["legs"] == [pm_yes]


def test_set_exempt_disables_deviate_convert():
    ctx = _with_period(_dynamic_ctx(
        yes_ask=0.49,
        no_ask=0.51,
        yes_bid=0.48,
        no_bid=0.50,
        start_yes=0.40,
        start_no=0.60,
    ), "s2")
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(VenueReplaceAction(deviate_convert=True, set_exempt=2).execute(ctx))

    assert ctx.scratch["legs"] == [pm_yes]


@pytest.mark.parametrize("period", ["S1", "Q2", "", None])
def test_set_exempt_does_not_apply_without_matching_explicit_set(period):
    ctx = _with_period(_ctx(), period)
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(VenueReplaceAction(convert=True, set_exempt=2).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "N.POLYMARKET"


@pytest.mark.parametrize(
    "competition",
    [
        "ATP Challenger Porto 2",
        "WTA 125K Bari",
        "WTA Beijing",
        "UTR Pro Tennis Series",
        "ITF M25 Monastir",
    ],
)
def test_tier_convert_pre_uses_oe_raw_competition_and_flips_external_leg(competition):
    ctx = _ctx()
    ctx.cache.instrument("Y.ORBITEXCH").competition_name = competition
    ctx.cache.instrument("Y.ORBITEXCH").info["competition"] = "Tennis"
    ctx.scratch["legs"] = [{
        "instrument_id": "Y.ORBITEXCH",
        "venue": "ORBITEXCH",
        "role": "yes",
        "prob": 0.35,
        "share_if_wins": 75.0,
    }]

    _run(VenueReplaceAction(tier_convert="pre", convert=True).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "N.POLYMARKET"
    assert ctx.scratch["legs"][0]["price"] == 0.55


def test_tier_convert_does_not_use_grouped_info_competition():
    ctx = _ctx()
    ctx.cache.instrument("Y.ORBITEXCH").info["competition"] = "WTA 125K Bari"
    ctx.scratch["legs"] = [{
        "instrument_id": "Y.ORBITEXCH",
        "venue": "ORBITEXCH",
        "role": "yes",
        "prob": 0.35,
        "share_if_wins": 75.0,
    }]

    _run(VenueReplaceAction(tier_convert="pre").execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "Y.POLYMARKET"


@pytest.mark.parametrize("tier_convert", ["pre", "post"])
def test_tier_convert_non_lower_tier_keeps_existing_behavior(tier_convert):
    ctx = _ctx()
    ctx.cache.instrument("Y.ORBITEXCH").competition_name = "ATP Masters 1000 Shanghai"
    ctx.scratch["legs"] = [{
        "instrument_id": "Y.ORBITEXCH",
        "venue": "ORBITEXCH",
        "role": "yes",
        "prob": 0.35,
        "share_if_wins": 75.0,
    }]

    _run(VenueReplaceAction(tier_convert=tier_convert, convert=True).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "Y.POLYMARKET"


def test_tier_convert_has_priority_and_flips_native_pm_only_once():
    ctx = _ctx(with_start=False)
    ctx.cache.instrument("Y.ORBITEXCH").competition_name = "WTA Beijing"
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(
        VenueReplaceAction(
            tier_convert="pre",
            convert=True,
            attitude=True,
            deviate_convert=True,
        ).execute(ctx),
    )

    assert ctx.scratch["legs"][0]["instrument_id"] == "N.POLYMARKET"
    assert ctx.scratch["legs"][0]["price"] == 0.55


def test_tier_convert_post_flips_after_default_replacement():
    ctx = _ctx()
    ctx.cache.instrument("Y.ORBITEXCH").competition_name = "ATP Challenger Porto 2"
    ctx.scratch["legs"] = [{
        "instrument_id": "Y.ORBITEXCH",
        "venue": "ORBITEXCH",
        "role": "yes",
        "prob": 0.35,
        "share_if_wins": 75.0,
    }]

    _run(VenueReplaceAction(tier_convert="post").execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "N.POLYMARKET"
    assert ctx.scratch["legs"][0]["price"] == 0.55


@pytest.mark.parametrize(
    ("start_yes", "expected_instrument"),
    [(0.29, "Y.POLYMARKET"), (0.30, "N.POLYMARKET"), (None, "Y.POLYMARKET")],
)
@pytest.mark.parametrize("tier_convert", ["pre", "post"])
def test_tier_ignore_uses_original_start_with_strict_lower_bound(
    tier_convert,
    start_yes,
    expected_instrument,
):
    ctx = _ctx(with_start=False)
    if start_yes is not None:
        store = PairPriceStore(ctx.cache)
        store.initialize(ctx.pair_id, ("yes", "no"))
        store.capture_start(ctx.pair_id, {"yes": start_yes, "no": 0.70})
    ctx.cache.instrument("Y.ORBITEXCH").competition_name = "ATP Challenger Porto 2"
    ctx.scratch["legs"] = [{
        "instrument_id": "Y.ORBITEXCH",
        "venue": "ORBITEXCH",
        "role": "yes",
        "prob": 0.35,
        "share_if_wins": 75.0,
    }]

    _run(VenueReplaceAction(tier_convert=tier_convert, tier_ignore=0.30).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == expected_instrument


def test_tier_ignore_pre_miss_continues_default_replacement():
    ctx = _ctx(with_start=False)
    store = PairPriceStore(ctx.cache)
    store.initialize(ctx.pair_id, ("yes", "no"))
    store.capture_start(ctx.pair_id, {"yes": 0.20, "no": 0.80})
    ctx.cache.instrument("Y.ORBITEXCH").competition_name = "WTA Beijing"
    ctx.scratch["legs"] = [{
        "instrument_id": "Y.ORBITEXCH",
        "venue": "ORBITEXCH",
        "role": "yes",
        "prob": 0.35,
        "share_if_wins": 75.0,
    }]

    _run(VenueReplaceAction(tier_convert="pre", tier_ignore=0.30).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "Y.POLYMARKET"


def test_tier_convert_post_flips_after_convert():
    ctx = _ctx(with_start=False)
    ctx.cache.instrument("Y.ORBITEXCH").competition_name = "WTA Beijing"
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(VenueReplaceAction(tier_convert="post", convert=True).execute(ctx))

    # convert 先把 YES 反转为 NO,post 再把最终腿反转回 YES。
    assert ctx.scratch["legs"][0]["instrument_id"] == "Y.POLYMARKET"
    assert ctx.scratch["legs"][0]["price"] == 0.40


def test_tier_ignore_post_miss_preserves_convert_result():
    ctx = _ctx(with_start=False)
    store = PairPriceStore(ctx.cache)
    store.initialize(ctx.pair_id, ("yes", "no"))
    store.capture_start(ctx.pair_id, {"yes": 0.20, "no": 0.80})
    ctx.cache.instrument("Y.ORBITEXCH").competition_name = "WTA Beijing"
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(
        VenueReplaceAction(
            tier_convert="post",
            tier_ignore=0.30,
            convert=True,
        ).execute(ctx),
    )

    assert ctx.scratch["legs"][0]["instrument_id"] == "N.POLYMARKET"
    assert ctx.scratch["legs"][0]["price"] == 0.55


def test_tier_convert_post_flips_after_dynamic_convert():
    ctx = live_context(
        books={
            "Y.POLYMARKET": _book(0.41, 0.40),
            "N.POLYMARKET": _book(0.59, 0.58),
            "Y.ORBITEXCH": _book(0.35),
        },
        infos={
            "Y.POLYMARKET": {"claim": "yes"},
            "N.POLYMARKET": {"claim": "no"},
            "Y.ORBITEXCH": {"claim": "yes"},
        },
    )
    store = PairPriceStore(ctx.cache)
    store.initialize(ctx.pair_id, ("yes", "no"))
    store.capture_start(ctx.pair_id, {"yes": 0.40, "no": 0.60})
    ctx.cache.instrument("Y.ORBITEXCH").competition_name = "ITF M25 Monastir"
    ctx.scratch["legs"] = [{
        "instrument_id": "Y.ORBITEXCH",
        "venue": "ORBITEXCH",
        "role": "yes",
        "prob": 0.35,
        "share_if_wins": 75.0,
    }]

    _run(VenueReplaceAction(tier_convert="post", attitude=True).execute(ctx))

    # attitude 先 YES -> NO,post 再 NO -> YES,价格取最终 YES 实时 ask。
    assert ctx.scratch["legs"][0]["instrument_id"] == "Y.POLYMARKET"
    assert ctx.scratch["legs"][0]["price"] == 0.41


def test_missing_start_price_allows_default_replacement():
    ctx = _ctx(with_start=False)
    _, candidate = _mixed_candidate()
    ctx.scratch["candidates"] = [candidate]

    _run(VenueReplaceAction().execute(ctx))

    assert [leg["instrument_id"] for leg in ctx.scratch["candidates"][0]["legs"]] == [
        "Y.POLYMARKET",
        "N.POLYMARKET",
    ]


def test_dynamic_convert_uses_source_outcome_start_without_requiring_complete_pair_start():
    ctx = live_context(
        books={
            "Y.POLYMARKET": _book(0.40, 0.40),
            "N.POLYMARKET": _book(0.60, 0.59),
            "Y.ORBITEXCH": _book(0.35),
        },
        infos={
            "Y.POLYMARKET": {"claim": "yes"},
            "N.POLYMARKET": {"claim": "no"},
            "Y.ORBITEXCH": {"claim": "yes"},
        },
    )
    store = PairPriceStore(ctx.cache)
    # 模拟旧 Cache 中仅当前 outcome 有 start；动态判断不再要求 pair 两侧都存在。
    store.initialize(ctx.pair_id, ("yes",))
    store.capture_start(ctx.pair_id, {"yes": 0.40})
    ctx.scratch["legs"] = [{
        "instrument_id": "Y.ORBITEXCH",
        "venue": "ORBITEXCH",
        "role": "yes",
        "prob": 0.35,
        "share_if_wins": 75.0,
    }]

    _run(VenueReplaceAction(attitude=True).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "N.POLYMARKET"


@pytest.mark.parametrize("params", [{"attitude": True}, {"deviate_convert": True}])
def test_missing_start_price_skips_dynamic_rule_and_keeps_default_replacement(params):
    ctx = _ctx(with_start=False)
    ctx.scratch["legs"] = [{
        "instrument_id": "Y.ORBITEXCH",
        "venue": "ORBITEXCH",
        "role": "yes",
        "prob": 0.35,
        "share_if_wins": 75.0,
    }]

    _run(VenueReplaceAction(**params).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "Y.POLYMARKET"


def test_convert_still_takes_priority_when_start_price_is_missing():
    ctx = _ctx(with_start=False)
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(VenueReplaceAction(convert=True, attitude=True, deviate_convert=True).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "N.POLYMARKET"
    assert ctx.scratch["legs"][0]["price"] == 0.55


def test_convert_target_pm_leg_uses_opposite_pm_live_price():
    ctx = _ctx()
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(VenueReplaceAction(convert=True).execute(ctx))

    assert ctx.scratch["legs"] == [{
        "instrument_id": "N.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "BUY",
        "price": 0.55,
        "prob": 0.55,
        "role": "no",
        "claim": "no",
        "qty": 80.0,
        "share_if_wins": 80.0,
        "cost": 44.0,
    }]


def test_convert_target_pm_leg_ignores_pm_price_false_and_uses_opposite_live_price():
    ctx = _ctx()
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(VenueReplaceAction(pm_price=False, convert=True).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "N.POLYMARKET"
    assert ctx.scratch["legs"][0]["price"] == 0.55
    assert ctx.scratch["legs"][0]["prob"] == 0.55
    assert ctx.scratch["legs"][0]["qty"] == 80.0
    assert ctx.scratch["legs"][0]["cost"] == 44.0


def test_convert_takes_priority_over_dynamic_commission_gate():
    ctx = _dynamic_ctx(
        yes_ask=0.40,
        no_ask=0.65,
        yes_bid=0.39,
        no_bid=0.64,
        start_yes=0.40,
        start_no=0.60,
    )
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(VenueReplaceAction(convert=True, attitude=True).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "N.POLYMARKET"
    assert ctx.scratch["legs"][0]["price"] == 0.65


@pytest.mark.parametrize(
    ("yes_ask", "no_ask"),
    [(0.40, 0.58), (0.44, 0.58)],
)
def test_attitude_converts_on_bid_at_or_below_start_with_clean_commission_boundaries(
    yes_ask,
    no_ask,
):
    ctx = _dynamic_ctx(
        yes_ask=yes_ask,
        no_ask=no_ask,
        yes_bid=0.40,
        no_bid=0.57,
        start_yes=0.40,
        start_no=0.60,
    )
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(VenueReplaceAction(attitude=True).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "N.POLYMARKET"
    assert ctx.scratch["legs"][0]["price"] == no_ask


@pytest.mark.parametrize("yes_bid", [0.48, 0.52, 0.60])
def test_deviate_convert_triggers_at_1_2_and_has_no_upper_bound(yes_bid):
    ctx = _dynamic_ctx(
        yes_ask=yes_bid + 0.01,
        no_ask=0.99 - yes_bid,
        yes_bid=yes_bid,
        no_bid=0.98 - yes_bid,
        start_yes=0.40,
        start_no=0.60,
    )
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(VenueReplaceAction(deviate_convert=True).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "N.POLYMARKET"
    assert ctx.scratch["legs"][0]["price"] == pytest.approx(0.99 - yes_bid)


def test_dynamic_convert_does_not_trigger_outside_ranges():
    ctx = _dynamic_ctx(
        yes_ask=0.48,
        no_ask=0.52,
        yes_bid=0.47,
        no_bid=0.51,
        start_yes=0.40,
        start_no=0.60,
    )
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(VenueReplaceAction(attitude=True, deviate_convert=True).execute(ctx))

    assert ctx.scratch["legs"] == [pm_yes]


def test_dynamic_convert_requires_clean_pm_ask_commission():
    ctx = _dynamic_ctx(
        yes_ask=0.40,
        no_ask=0.65,
        yes_bid=0.39,
        no_bid=0.64,
        start_yes=0.40,
        start_no=0.60,
    )
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(VenueReplaceAction(attitude=True).execute(ctx))

    assert ctx.scratch["legs"] == [pm_yes]


def test_dynamic_convert_can_flip_external_leg_when_convert_does_not_apply():
    ctx = _dynamic_ctx(
        yes_ask=0.41,
        no_ask=0.59,
        yes_bid=0.40,
        no_bid=0.58,
        start_yes=0.40,
        start_no=0.60,
    )
    ctx.scratch["legs"] = [{
        "instrument_id": "Y.ORBITEXCH",
        "venue": "ORBITEXCH",
        "role": "yes",
        "prob": 0.35,
        "share_if_wins": 75.0,
    }]

    _run(VenueReplaceAction(convert=True, attitude=True).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "N.POLYMARKET"
    assert ctx.scratch["legs"][0]["price"] == 0.59


def test_convert_does_not_flip_external_leg_before_replacement():
    ctx = _ctx()
    ctx.scratch["legs"] = [{
        "instrument_id": "Y.ORBITEXCH",
        "venue": "ORBITEXCH",
        "role": "yes",
        "prob": 0.35,
        "share_if_wins": 75.0,
    }]

    _run(VenueReplaceAction(convert=True).execute(ctx))

    assert ctx.scratch["legs"][0]["instrument_id"] == "Y.POLYMARKET"
    assert ctx.scratch["legs"][0]["claim"] == "yes"


def test_convert_target_pm_leg_without_opposite_quote_is_dropped():
    ctx = live_context(
        books={"Y.POLYMARKET": _book(0.40)},
        infos={"Y.POLYMARKET": {"claim": "yes"}},
    )
    pm_yes, _ = _mixed_candidate()
    ctx.scratch["legs"] = [pm_yes]

    _run(VenueReplaceAction(convert=True).execute(ctx))

    assert ctx.scratch["legs"] == []


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
