"""ShareLimitModification:单一 legs + candidate 数组直接缩放。"""

import asyncio
from types import SimpleNamespace

from src.arbitrage.common.venues import PositionOutcomeInvariantError
from src.arbitrage.strategy.actions.share_limit import ShareLimitModification
from src.arbitrage.strategy.condition import EvalContext
from tests.arbitrage.strategy._live_state import live_context


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class _Portfolio:
    def __init__(self, pm=None, oe=None, se=None):
        self._pm = pm or {}
        self._oe = oe or {}
        self._se = se or {}

    def outcome_shares_for_venue(self, pair_id, venue, account_id):
        if venue == "polymarket":
            return self._pm
        if venue == "orbitexch":
            return self._oe
        if venue == "sharpexch":
            return self._se
        return {}

    def outcome_shares(self, pair_id, account_id):
        outcomes = set(self._pm) | set(self._oe) | set(self._se)
        return {
            outcome: sum(shares.get(outcome, 0.0) for shares in (self._pm, self._oe, self._se))
            for outcome in outcomes
        }


class _RecordingPortfolio(_Portfolio):
    def __init__(self, shares=None):
        super().__init__()
        self.calls = []
        self._shares = shares or {}

    def outcome_shares_for_venue(self, pair_id, venue, account_id):
        self.calls.append((pair_id, venue, account_id))
        return self._shares


class _InvariantPortfolio(_Portfolio):
    def outcome_shares_for_venue(self, pair_id, venue, account_id):
        raise PositionOutcomeInvariantError("position instrument missing claim")


def test_single_legs_are_adjusted_in_share_limit():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"home": 60.0}, oe={"away": 20.0}))
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "home", "price": 0.4, "share_if_wins": 50.0},
        {"venue": "ORBITEXCH", "role": "away", "price": 2.0, "share_if_wins": 50.0},
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert ctx.scratch["share_limit_scale"] == 0.8
    assert ctx.scratch["adjusted_share"] == 40.0
    assert ctx.scratch["legs"][0]["qty"] == 40.0
    assert ctx.scratch["legs"][0]["share_if_wins"] == 40.0
    assert ctx.scratch["legs"][1]["qty"] == 20.0
    assert ctx.scratch["legs"][1]["share_if_wins"] == 40.0


def test_single_legs_missing_share_are_cleared():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"home": 0.0}))
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "home", "price": 0.4},
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert ctx.scratch["legs"] == []


def test_single_legs_are_cleared_when_portfolio_invariant_is_broken():
    ctx = EvalContext(pair_id="p", portfolio=_InvariantPortfolio())
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "yes", "price": 0.4, "share_if_wins": 50.0},
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert ctx.scratch["legs"] == []


def test_candidates_are_individually_share_limited_and_output_as_array():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"home": 20.0}, oe={"away": 50.0}))
    ctx.scratch["candidates"] = [
        {
            "candidate_id": "A",
            "base_share": 100.0,
            "legs": [
                {"venue": "POLYMARKET", "role": "home", "qty": 100.0, "share_if_wins": 100.0},
                {"venue": "ORBITEXCH", "role": "away", "price": 2.0, "qty": 50.0, "share_if_wins": 100.0},
            ],
        },
        {
            "candidate_id": "B",
            "base_share": 40.0,
            "legs": [
                {"venue": "POLYMARKET", "role": "home", "qty": 40.0, "share_if_wins": 40.0},
                {"venue": "ORBITEXCH", "role": "away", "price": 2.0, "qty": 20.0, "share_if_wins": 40.0},
            ],
        },
    ]
    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    adjusted = ctx.scratch["candidates"]
    assert [c["candidate_id"] for c in adjusted] == ["A", "B"]
    assert adjusted[0]["share_limit_scale"] == 0.5
    assert adjusted[0]["adjusted_share"] == 50.0
    assert adjusted[0]["legs"][0]["qty"] == 50.0
    assert adjusted[0]["legs"][1]["qty"] == 25.0
    assert adjusted[1]["share_limit_scale"] == 1.0
    assert adjusted[1]["adjusted_share"] == 40.0


def test_sharpexch_legs_are_adjusted_like_decimal_odds_venue():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(se={"home": 60.0, "away": 40.0}))
    ctx.scratch["legs"] = [
        {"venue": "SHARPEXCH", "role": "home", "price": 2.0, "share_if_wins": 100.0},
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert ctx.scratch["share_limit_scale"] == 0.8
    assert ctx.scratch["adjusted_share"] == 80.0
    assert ctx.scratch["legs"][0]["qty"] == 40.0
    assert ctx.scratch["legs"][0]["share_if_wins"] == 80.0


def test_candidates_with_no_remaining_are_removed():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"home": 100.0}))
    ctx.scratch["candidates"] = [
        {
            "candidate_id": "blocked",
            "base_share": 20.0,
            "legs": [{"venue": "POLYMARKET", "role": "home", "qty": 20.0, "share_if_wins": 20.0}],
        },
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert ctx.scratch["candidates"] == []


def test_candidates_missing_share_are_removed():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"home": 0.0}))
    ctx.scratch["candidates"] = [
        {
            "candidate_id": "missing",
            "legs": [{"venue": "POLYMARKET", "role": "home", "price": 0.4}],
        },
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert ctx.scratch["candidates"] == []


def test_share_limit_uses_strategy_default_max_leg_share_when_param_absent():
    ctx = EvalContext(
        pair_id="p",
        portfolio=_Portfolio(pm={"home": 60.0}),
        strategy_defaults={"share": 50.0, "max_leg_share": 100.0},
    )
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "home", "price": 0.4, "share_if_wins": 50.0},
    ]

    _run(ShareLimitModification().execute(ctx))

    assert ctx.scratch["share_limit_scale"] == 0.8
    assert ctx.scratch["legs"][0]["qty"] == 40.0


def test_probability_venue_remaining_uses_leg_venue_for_portfolio_lookup():
    portfolio = _RecordingPortfolio({"home": 60.0})
    ctx = EvalContext(pair_id="p", portfolio=portfolio)
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "home", "price": 0.4, "share_if_wins": 50.0},
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert portfolio.calls == [("p", "polymarket", None)]


def test_share_limit_param_max_leg_share_overrides_strategy_default():
    ctx = EvalContext(
        pair_id="p",
        portfolio=_Portfolio(pm={"home": 60.0}),
        strategy_defaults={"share": 50.0, "max_leg_share": 100.0},
    )
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "home", "price": 0.4, "share_if_wins": 50.0},
    ]

    _run(ShareLimitModification(max_leg_share=80.0).execute(ctx))

    assert ctx.scratch["share_limit_scale"] == 0.4
    assert ctx.scratch["legs"][0]["qty"] == 20.0


def test_current_position_gate_defaults_to_disabled():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"yes": 20.0}))
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "no", "price": 0.4, "share_if_wins": 10.0},
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["no"]


def test_current_position_gate_false_keeps_existing_behavior():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"yes": 20.0}))
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "no", "price": 0.4, "share_if_wins": 10.0},
    ]

    _run(
        ShareLimitModification(
            max_leg_share=100.0,
            current_position_gate=False,
        ).execute(ctx),
    )

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["no"]


def test_current_position_gate_keeps_only_legs_matching_existing_outcome():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(oe={"yes": 20.0, "no": 0.0}))
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "yes", "price": 0.6, "share_if_wins": 10.0},
        {"venue": "POLYMARKET", "role": "no", "price": 0.4, "share_if_wins": 10.0},
    ]

    _run(
        ShareLimitModification(
            max_leg_share=100.0,
            current_position_gate=True,
        ).execute(ctx),
    )

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["yes"]


def test_current_position_gate_does_not_filter_when_there_is_no_position():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"yes": 0.0, "no": 0.0}))
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "yes", "price": 0.6, "share_if_wins": 10.0},
        {"venue": "POLYMARKET", "role": "no", "price": 0.4, "share_if_wins": 10.0},
    ]

    _run(ShareLimitModification(current_position_gate=True).execute(ctx))

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["yes", "no"]


def test_current_position_gate_keeps_both_outcomes_when_both_are_held():
    ctx = EvalContext(
        pair_id="p",
        portfolio=_Portfolio(pm={"yes": 20.0}, oe={"no": 15.0}),
    )
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "yes", "price": 0.6, "share_if_wins": 10.0},
        {"venue": "POLYMARKET", "role": "no", "price": 0.4, "share_if_wins": 10.0},
    ]

    _run(ShareLimitModification(current_position_gate=True).execute(ctx))

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["yes", "no"]


def test_current_position_gate_filters_candidate_legs_and_drops_empty_candidates():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"yes": 20.0, "no": 0.0}))
    ctx.scratch["candidates"] = [
        {
            "candidate_id": "mixed",
            "rate": 0.1,
            "legs": [
                {"venue": "POLYMARKET", "role": "yes", "qty": 10.0, "share_if_wins": 10.0},
                {"venue": "POLYMARKET", "role": "no", "qty": 10.0, "share_if_wins": 10.0},
            ],
        },
        {
            "candidate_id": "opposite-only",
            "legs": [
                {"venue": "POLYMARKET", "role": "no", "qty": 10.0, "share_if_wins": 10.0},
            ],
        },
    ]

    _run(
        ShareLimitModification(
            max_leg_share=100.0,
            current_position_gate=True,
        ).execute(ctx),
    )

    assert [candidate["candidate_id"] for candidate in ctx.scratch["candidates"]] == ["mixed"]
    assert ctx.scratch["candidates"][0]["rate"] == 0.1
    assert [leg["role"] for leg in ctx.scratch["candidates"][0]["legs"]] == ["yes"]


def test_current_position_gate_requires_boolean_param():
    try:
        ShareLimitModification(current_position_gate="true")
    except ValueError as exc:
        assert "current_position_gate must be a boolean" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_current_order_gate_defaults_to_disabled():
    order = SimpleNamespace(instrument_id="N.ORBITEXCH")
    ctx = live_context(
        instrument_ids=["Y.POLYMARKET", "N.ORBITEXCH"],
        orders=[order],
        portfolio=_Portfolio(pm={"yes": 0.0}),
    )
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "yes", "price": 0.4, "share_if_wins": 10.0},
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["yes"]


def test_current_order_gate_blocks_all_legs_for_any_pair_open_order():
    order = SimpleNamespace(instrument_id="N.ORBITEXCH")
    ctx = live_context(
        instrument_ids=["Y.POLYMARKET", "N.ORBITEXCH"],
        orders=[order],
        portfolio=_Portfolio(pm={"yes": 0.0}),
    )
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "yes", "price": 0.4, "share_if_wins": 10.0},
    ]

    _run(ShareLimitModification(current_order_gate={"enable": True}).execute(ctx))

    assert ctx.scratch["legs"] == []


def test_current_order_gate_blocks_all_candidates_before_outcome_filtering():
    order = SimpleNamespace(instrument_id="Y.POLYMARKET")
    ctx = live_context(
        instrument_ids=["Y.POLYMARKET", "N.POLYMARKET"],
        orders=[order],
        portfolio=_Portfolio(pm={"yes": 20.0}),
    )
    ctx.scratch["candidates"] = [
        {
            "candidate_id": "opposite",
            "legs": [
                {"venue": "POLYMARKET", "role": "no", "qty": 10.0, "share_if_wins": 10.0},
            ],
        },
    ]
    ctx.scratch["selected_candidate"] = dict(ctx.scratch["candidates"][0])
    ctx.scratch["legs"] = list(ctx.scratch["candidates"][0]["legs"])

    _run(
        ShareLimitModification(
            current_position_gate=True,
            current_order_gate={"enable": True},
        ).execute(ctx),
    )

    assert ctx.scratch["candidates"] == []
    assert ctx.scratch["selected_candidate"] == {}
    assert ctx.scratch["legs"] == []


def test_current_order_gate_allows_when_pair_has_no_open_order():
    ctx = live_context(
        instrument_ids=["Y.POLYMARKET"],
        orders=[],
        portfolio=_Portfolio(pm={"yes": 0.0}),
    )
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "yes", "price": 0.4, "share_if_wins": 10.0},
    ]

    _run(ShareLimitModification(current_order_gate={"enable": True}).execute(ctx))

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["yes"]


def test_current_order_gate_ignores_open_order_outside_pair():
    outside_order = SimpleNamespace(instrument_id="X.ORBITEXCH")
    ctx = live_context(
        instrument_ids=["Y.POLYMARKET"],
        orders=[outside_order],
        portfolio=_Portfolio(pm={"yes": 0.0}),
    )
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "yes", "price": 0.4, "share_if_wins": 10.0},
    ]

    _run(ShareLimitModification(current_order_gate={"enable": True}).execute(ctx))

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["yes"]


def test_current_order_gate_missing_live_state_fails_closed():
    ctx = EvalContext(pair_id="p")
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "yes", "price": 0.4, "share_if_wins": 10.0},
    ]

    _run(ShareLimitModification(current_order_gate={"enable": True}).execute(ctx))

    assert ctx.scratch["legs"] == []


def test_current_order_gate_requires_object_param():
    try:
        ShareLimitModification(current_order_gate=True)
    except ValueError as exc:
        assert "current_order_gate must be an object" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_current_order_gate_can_ignore_first_and_only_submitted_open_order():
    open_order = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        client_order_id="O-1",
        ts_submitted=123,
    )
    historical_view = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        client_order_id="O-1",
        ts_submitted=123,
    )
    ctx = live_context(
        instrument_ids=["Y.POLYMARKET"],
        orders=[open_order],
        order_history=[historical_view],
        portfolio=_Portfolio(pm={"yes": 0.0}),
    )
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "yes", "price": 0.4, "share_if_wins": 10.0},
    ]

    _run(ShareLimitModification(
        current_order_gate={
            "enable": True,
            "ignore_start_price": True,
        },
    ).execute(ctx))

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["yes"]


def test_current_order_gate_blocks_unsubmitted_open_order():
    order = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        client_order_id="O-1",
        ts_submitted=0,
    )
    ctx = live_context(
        instrument_ids=["Y.POLYMARKET"],
        orders=[order],
        portfolio=_Portfolio(pm={"yes": 0.0}),
    )
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "yes", "price": 0.4, "share_if_wins": 10.0},
    ]

    _run(ShareLimitModification(
        current_order_gate={
            "enable": True,
            "ignore_start_price": True,
        },
    ).execute(ctx))

    assert ctx.scratch["legs"] == []


def test_current_order_gate_ignores_unsubmitted_history_when_current_is_first_submission():
    current = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        client_order_id="O-2",
        ts_submitted=200,
    )
    denied = SimpleNamespace(
        instrument_id="N.POLYMARKET",
        client_order_id="O-1",
        ts_submitted=0,
    )
    ctx = live_context(
        instrument_ids=["Y.POLYMARKET", "N.POLYMARKET"],
        orders=[current],
        order_history=[denied, current],
        portfolio=_Portfolio(pm={"yes": 0.0}),
    )
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "yes", "price": 0.4, "share_if_wins": 10.0},
    ]

    _run(ShareLimitModification(
        current_order_gate={
            "enable": True,
            "ignore_start_price": True,
        },
    ).execute(ctx))

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["yes"]


def test_current_order_gate_blocks_when_another_order_was_submitted():
    current = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        client_order_id="O-2",
        ts_submitted=200,
    )
    prior = SimpleNamespace(
        instrument_id="N.POLYMARKET",
        client_order_id="O-1",
        ts_submitted=100,
    )
    ctx = live_context(
        instrument_ids=["Y.POLYMARKET", "N.POLYMARKET"],
        orders=[current],
        order_history=[prior, current],
        portfolio=_Portfolio(pm={"yes": 0.0}),
    )
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "yes", "price": 0.4, "share_if_wins": 10.0},
    ]

    _run(ShareLimitModification(
        current_order_gate={
            "enable": True,
            "ignore_start_price": True,
        },
    ).execute(ctx))

    assert ctx.scratch["legs"] == []


def test_current_order_gate_rejects_removed_spread_param():
    try:
        ShareLimitModification(
            current_order_gate={
                "enable": True,
                "ignore_start_price": True,
                "spread": 0.05,
            },
        )
    except ValueError as exc:
        assert "unknown fields: ['spread']" in str(exc)
    else:
        raise AssertionError("expected ValueError")
