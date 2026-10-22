"""position_close：筛选仓位并直接生成带实时价格的 SELL 平仓腿。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.actions.place_bets import PlaceBetsAction
from src.arbitrage.strategy.actions.score_selection import ScoreSelectionAction
from src.arbitrage.strategy.checks.position_close import PositionCloseCheck
from src.arbitrage.strategy.in_game_positions import InGamePositionRuntimeStore
from src.arbitrage.strategy.queries.exposure_source import POSITION_CANDIDATES
from tests.arbitrage.strategy._live_state import live_context


_INFOS = {
    "Y.POLYMARKET": {"claim": "yes", "selection_role": "home"},
    "N.POLYMARKET": {"claim": "no", "selection_role": "away"},
}


class _SportsStore:
    def __init__(self, period, score=""):
        self._state = SimpleNamespace(period=period, score=score)

    def get(self, game_id):
        return self._state


def _position(
    *,
    side="LONG",
    instrument_id="Y.POLYMARKET",
    position_id="P-1",
    opening_order_id=None,
):
    return SimpleNamespace(
        id=position_id,
        instrument_id=instrument_id,
        side=side,
        quantity=5.25,
        opening_order_id=opening_order_id,
    )


def _ctx(*, period="S3", positions=None, history=None):
    ctx = live_context(
        books={
            "Y.POLYMARKET": {"ask": 0.62, "bid": 0.60},
            "N.POLYMARKET": {"ask": 0.40, "bid": 0.38},
        },
        infos=_INFOS,
        positions=positions if positions is not None else [_position()],
        order_history=history,
        sports_store=_SportsStore(period),
    )
    ctx.pair_registry.register(ctx.pair_id, list(_INFOS), game_id=42)
    return ctx


def test_without_min_set_ignores_period_and_runtime_override():
    ctx = _ctx(period="")
    store = InGamePositionRuntimeStore()
    store.set_min_tp_set(ctx.pair_id, 5)
    store.set_min_sl_set(ctx.pair_id, 5)
    ctx.in_game_position_store = store

    assert PositionCloseCheck().passes(ctx) is True
    assert ctx.scratch["legs"] == [{
        "instrument_id": "Y.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "SELL",
        "role": "yes",
        "claim": "yes",
        "qty": 5.25,
        "position_id": "P-1",
        "price": 0.60,
        "prob": 0.60,
    }]


@pytest.mark.parametrize("period", ["S3", "TB3"])
def test_emits_sell_leg_at_or_after_min_tp_set(period):
    ctx = _ctx(period=period)

    assert PositionCloseCheck(min_tp_set=3).passes(ctx) is True
    assert ctx.scratch["legs"][0]["position_id"] == "P-1"


def test_tie_break_period_and_default_score_selection_keep_close_leg():
    ctx = _ctx(period="TB3")
    ctx.sports_store = _SportsStore("TB3", score="6-6")

    assert PositionCloseCheck(min_tp_set=1).passes(ctx) is True
    asyncio.run(ScoreSelectionAction(standing="win|draw").execute(ctx))

    assert [leg["position_id"] for leg in ctx.scratch["legs"]] == ["P-1"]


def test_runtime_tp_and_sl_overrides_are_independent():
    store = InGamePositionRuntimeStore()
    store.set_min_tp_set("p", 4)
    store.set_min_sl_set("p", 2)
    ctx = _ctx(period="S3")
    ctx.in_game_position_store = store

    assert PositionCloseCheck(min_tp_set=3).passes(ctx) is False
    assert PositionCloseCheck(min_sl_set=3).passes(ctx) is True

    ctx.sports_store = _SportsStore("S4")
    assert PositionCloseCheck(min_tp_set=3).passes(ctx) is True


@pytest.mark.parametrize("period", ["S2", "", "SET3"])
def test_before_min_set_or_unknown_period_fails_closed(period):
    assert PositionCloseCheck(min_sl_set=3).passes(_ctx(period=period)) is False


@pytest.mark.parametrize(
    "position",
    [
        _position(side="SHORT"),
        _position(instrument_id="X.ORBITEXCH"),
        _position(position_id=None),
    ],
)
def test_ignores_non_closable_positions(position):
    assert PositionCloseCheck().passes(_ctx(positions=[position])) is False


def test_origin_filters_positions_by_opening_order():
    positions = [
        _position(position_id="P-PRE", opening_order_id="PRE"),
        _position(
            instrument_id="N.POLYMARKET",
            position_id="P-IN",
            opening_order_id="IN",
        ),
    ]
    history = [
        SimpleNamespace(client_order_id="PRE", tags=["arb:origin=pre_game"]),
        SimpleNamespace(client_order_id="IN", tags=["arb:origin=in_game"]),
    ]
    ctx = _ctx(positions=positions, history=history)

    assert PositionCloseCheck(origin="in_game").passes(ctx) is True
    assert [leg["position_id"] for leg in ctx.scratch["legs"]] == ["P-IN"]


def test_consumes_position_candidates_selected_by_condition():
    ctx = _ctx(positions=[
        _position(position_id="P-YES"),
        _position(instrument_id="N.POLYMARKET", position_id="P-NO"),
    ])
    ctx.scratch[POSITION_CANDIDATES] = ["P-NO"]

    assert PositionCloseCheck().passes(ctx) is True
    assert [leg["position_id"] for leg in ctx.scratch["legs"]] == ["P-NO"]


@pytest.mark.parametrize("name", ["min_tp_set", "min_sl_set"])
@pytest.mark.parametrize("min_value", [0, -1, True])
def test_rejects_invalid_min_set(name, min_value):
    with pytest.raises(ValueError, match="positive integer"):
        PositionCloseCheck(**{name: min_value})


def test_rejects_both_min_set_kinds():
    with pytest.raises(ValueError, match="mutually exclusive"):
        PositionCloseCheck(min_tp_set=1, min_sl_set=1)


def test_rejects_invalid_origin():
    with pytest.raises(ValueError, match="origin must be pre_game or in_game"):
        PositionCloseCheck(origin="live")


def test_missing_bid_fails_closed():
    ctx = _ctx()
    ctx.cache._books["Y.POLYMARKET"] = {"ask": 0.62}

    assert PositionCloseCheck().passes(ctx) is False
    assert "legs" not in ctx.scratch


def test_pre_game_sell_applies_limit_then_spread():
    ctx = _ctx()
    assert PositionCloseCheck(origin=None).passes(ctx) is True

    asyncio.run(
        PlaceBetsAction(
            limit=True,
            spread=0.03,
            intent="recovery",
            origin="pre_game",
        ).execute(ctx),
    )

    spec = ctx.scratch["execution_plan"].orders[0].spec
    assert spec["side"] == "SELL"
    assert spec["price"] == pytest.approx(0.65)
    assert spec["position_id"] == "P-1"
