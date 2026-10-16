"""补偿树按订单 origin 与仓位 opening order 来源分流。"""

from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.queries.exposure_source import CANCEL_CANDIDATES
from src.arbitrage.strategy.queries.exposure_source import POSITION_CANDIDATES
from src.arbitrage.strategy.queries.exposure_source import ExposureSourceQuery
from tests.arbitrage.strategy._live_state import live_context


_INFOS = {
    "Y.POLYMARKET": {"claim": "yes"},
    "N.POLYMARKET": {"claim": "no"},
}


def _order(order_id, origin, instrument_id="Y.POLYMARKET", side="BUY", magic=None):
    tags = ["arb:intent=arbitrage", f"arb:origin={origin}"]
    if magic is not None:
        tags.append(f"arb:magic={magic}")
    return SimpleNamespace(
        client_order_id=order_id,
        instrument_id=instrument_id,
        side=side,
        tags=tags,
    )


def _position(opening_order_id, instrument_id="Y.POLYMARKET"):
    return SimpleNamespace(
        id=f"P-{opening_order_id}",
        instrument_id=instrument_id,
        opening_order_id=opening_order_id,
    )


def _ctx(*, orders=(), positions=(), history=None):
    return live_context(
        infos=_INFOS,
        orders=orders,
        positions=positions,
        order_history=history,
    )


def test_open_order_and_position_sources_match_independently():
    opening = _order("OPEN", "pre_game")
    active = _order("ACTIVE", "in_game")
    ctx = _ctx(
        orders=[active],
        positions=[_position("OPEN")],
        history=[opening, active],
    )

    assert ExposureSourceQuery("in_game_order").matches(ctx) is True
    assert ctx.scratch[CANCEL_CANDIDATES] == ["ACTIVE"]
    assert ExposureSourceQuery("pre_game_position").matches(ctx) is True
    assert ctx.scratch[POSITION_CANDIDATES] == ["P-OPEN"]


def test_position_uses_opening_order_origin():
    opening = _order("OPEN", "pre_game")
    ctx = _ctx(positions=[_position("OPEN")], history=[opening])

    assert ExposureSourceQuery("pre_game_position").matches(ctx) is True
    assert ExposureSourceQuery("in_game_position").matches(ctx) is False


def test_mixed_open_order_origins_match_each_order_source():
    pre_game = _order("PRE", "pre_game")
    in_game = _order("IN", "in_game", instrument_id="N.POLYMARKET")
    ctx = _ctx(orders=[pre_game, in_game])

    assert ExposureSourceQuery("pre_game_order").eval(ctx) is True
    assert ctx.scratch[CANCEL_CANDIDATES] == ["PRE"]
    assert ExposureSourceQuery("in_game_order").eval(ctx) is True
    assert ctx.scratch[CANCEL_CANDIDATES] == ["IN"]


def test_mixed_position_origins_match_each_position_source():
    pre_game = _order("PRE", "pre_game")
    in_game = _order("IN", "in_game", instrument_id="N.POLYMARKET")
    ctx = _ctx(
        positions=[_position("PRE"), _position("IN", instrument_id="N.POLYMARKET")],
        history=[pre_game, in_game],
    )

    assert ExposureSourceQuery("pre_game_position").eval(ctx) is True
    assert ctx.scratch[POSITION_CANDIDATES] == ["P-PRE"]
    assert ExposureSourceQuery("in_game_position").eval(ctx) is True
    assert ctx.scratch[POSITION_CANDIDATES] == ["P-IN"]


def test_order_source_can_select_position_orders_in_condition():
    ordinary = _order("ORDINARY", "in_game")
    position_order = _order("POSITION", "in_game", side="SELL")
    ctx = _ctx(orders=[ordinary, position_order])
    ctx.cache.position_id = lambda order_id: "P" if str(order_id) == "POSITION" else None

    assert ExposureSourceQuery("in_game_order", position_order=True).eval(ctx) is True
    assert ctx.scratch[CANCEL_CANDIDATES] == ["POSITION"]
    assert ExposureSourceQuery("in_game_order", position_order=False).eval(ctx) is True
    assert ctx.scratch[CANCEL_CANDIDATES] == ["ORDINARY"]


def test_order_source_can_filter_orders_by_magic():
    first = _order("FIRST", "in_game", magic="position_close")
    second = _order("SECOND", "in_game", instrument_id="N.POLYMARKET", magic="order_cancel")
    ctx = _ctx(orders=[first, second])

    assert ExposureSourceQuery("in_game_order", magic="position_close").eval(ctx) is True
    assert ctx.scratch[CANCEL_CANDIDATES] == ["FIRST"]
    assert ExposureSourceQuery("in_game_order", magic="missing").eval(ctx) is False
    assert ctx.scratch[CANCEL_CANDIDATES] == ["FIRST"]


def test_failed_source_query_does_not_overwrite_existing_candidates():
    ctx = _ctx(orders=[_order("PRE", "pre_game")])
    ctx.scratch[CANCEL_CANDIDATES] = ["BASELINE"]

    assert ExposureSourceQuery("in_game_order").eval(ctx) is False
    assert ctx.scratch[CANCEL_CANDIDATES] == ["BASELINE"]


def test_missing_origin_or_opening_order_fails_closed():
    unknown_order = SimpleNamespace(
        client_order_id="UNKNOWN",
        instrument_id="Y.POLYMARKET",
        tags=["arb:intent=arbitrage"],
    )
    assert ExposureSourceQuery("pre_game_order").matches(_ctx(orders=[unknown_order])) is False
    assert ExposureSourceQuery("pre_game_position").matches(
        _ctx(positions=[_position("MISSING")]),
    ) is False


@pytest.mark.parametrize("source", ["pre", "position", "recovery", ""])
def test_rejects_unknown_source(source):
    with pytest.raises(ValueError, match="unsupported source"):
        ExposureSourceQuery(source)


def test_rejects_position_order_for_position_source():
    with pytest.raises(ValueError, match="only valid for order sources"):
        ExposureSourceQuery("pre_game_position", position_order=True)


def test_rejects_magic_for_position_source_or_empty_magic():
    with pytest.raises(ValueError, match="only valid for order sources"):
        ExposureSourceQuery("pre_game_position", magic="position_close")
    with pytest.raises(ValueError, match="non-empty string"):
        ExposureSourceQuery("pre_game_order", magic=" ")
    with pytest.raises(ValueError, match="non-empty string"):
        ExposureSourceQuery("pre_game_order", magic=1)
