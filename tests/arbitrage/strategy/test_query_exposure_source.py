"""补偿树按订单 origin 与仓位 opening order 来源分流。"""

from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.queries.exposure_source import ExposureSourceQuery
from tests.arbitrage.strategy._live_state import live_context


_INFOS = {
    "Y.POLYMARKET": {"claim": "yes"},
    "N.POLYMARKET": {"claim": "no"},
}


def _order(order_id, origin, instrument_id="Y.POLYMARKET"):
    return SimpleNamespace(
        client_order_id=order_id,
        instrument_id=instrument_id,
        tags=["arb:intent=arbitrage", f"arb:origin={origin}"],
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


def test_open_order_has_priority_over_position():
    opening = _order("OPEN", "pre_game")
    active = _order("ACTIVE", "in_game")
    ctx = _ctx(
        orders=[active],
        positions=[_position("OPEN")],
        history=[opening, active],
    )

    assert ExposureSourceQuery("in_game_order").matches(ctx) is True
    assert ExposureSourceQuery("pre_game_position").matches(ctx) is False


def test_position_uses_opening_order_origin():
    opening = _order("OPEN", "pre_game")
    ctx = _ctx(positions=[_position("OPEN")], history=[opening])

    assert ExposureSourceQuery("pre_game_position").matches(ctx) is True
    assert ExposureSourceQuery("in_game_position").matches(ctx) is False


def test_mixed_open_order_origins_match_each_order_source():
    pre_game = _order("PRE", "pre_game")
    in_game = _order("IN", "in_game", instrument_id="N.POLYMARKET")
    ctx = _ctx(orders=[pre_game, in_game])

    assert ExposureSourceQuery("pre_game_order").matches(ctx) is True
    assert ExposureSourceQuery("in_game_order").matches(ctx) is True


def test_mixed_position_origins_match_each_position_source():
    pre_game = _order("PRE", "pre_game")
    in_game = _order("IN", "in_game", instrument_id="N.POLYMARKET")
    ctx = _ctx(
        positions=[_position("PRE"), _position("IN", instrument_id="N.POLYMARKET")],
        history=[pre_game, in_game],
    )

    assert ExposureSourceQuery("pre_game_position").matches(ctx) is True
    assert ExposureSourceQuery("in_game_position").matches(ctx) is True


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
