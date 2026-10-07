"""start_price_cancel: 按 start_game intent 精确撤销指定比分状态的挂单。"""

from types import SimpleNamespace

from src.arbitrage.strategy.checks.start_price_cancel import StartPriceCancelCheck
from tests.arbitrage.strategy._live_state import live_context


_INFOS = {
    "H.POLYMARKET": {"selection_role": "home", "claim": "yes"},
    "A.POLYMARKET": {"selection_role": "away", "claim": "no"},
}


class _SportsStore:
    def __init__(self, score):
        self._state = SimpleNamespace(score=score) if score is not None else None

    def get(self, game_id):
        assert game_id == 42
        return self._state


def _order(*, order_id="O-START", instrument_id="H.POLYMARKET", intent="start_game", ts=1):
    return SimpleNamespace(
        client_order_id=order_id,
        instrument_id=instrument_id,
        side="BUY",
        ts_submitted=ts,
        tags=[f"arb:intent={intent}"],
    )


def _ctx(score, orders, *, now_ns):
    ctx = live_context(
        infos=_INFOS,
        instrument_ids=list(_INFOS),
        orders=orders,
        sports_store=_SportsStore(score),
        ts_now_ns=now_ns,
    )
    ctx.pair_registry.register(ctx.pair_id, list(_INFOS), game_id=42)
    return ctx


def test_leading_start_game_order_generates_targeted_cancel():
    order = _order(ts=100)
    ctx = _ctx("3-2", [order], now_ns=200)

    assert StartPriceCancelCheck().passes(ctx) is True
    assert ctx.scratch["cancel_pair_orders"] == {
        "reason": "start_price_cancel:score_leading",
        "client_order_ids": ["O-START"],
    }


def test_losing_start_game_order_does_not_cancel():
    ctx = _ctx("2-3", [_order(ts=100)], now_ns=200)

    assert StartPriceCancelCheck().passes(ctx) is False


def test_losing_start_game_order_can_be_configured_for_cancel():
    order = _order(ts=100)
    ctx = _ctx("2-3", [order], now_ns=200)

    assert StartPriceCancelCheck(standing="lose").passes(ctx) is True
    assert ctx.scratch["cancel_pair_orders"] == {
        "reason": "start_price_cancel:score_losing",
        "client_order_ids": ["O-START"],
    }


def test_leading_start_game_order_is_kept_when_canceling_loser():
    ctx = _ctx("3-2", [_order(ts=100)], now_ns=200)

    assert StartPriceCancelCheck(standing="lose").passes(ctx) is False


def test_cancel_standing_rejects_unknown_value():
    try:
        StartPriceCancelCheck(standing="draw")
    except ValueError as exc:
        assert "standing must be 'win' or 'lose'" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_no_score_does_not_cancel_regardless_of_order_age():
    ctx = _ctx("", [_order(ts=1)], now_ns=700_000_000_000)

    assert StartPriceCancelCheck(standing="lose").passes(ctx) is False


def test_non_start_game_order_is_not_canceled():
    ctx = _ctx("3-2", [_order(intent="arbitrage", ts=100)], now_ns=700_000_000_000)

    assert StartPriceCancelCheck().passes(ctx) is False


def test_existing_unparseable_score_does_not_use_no_score_timeout():
    ctx = _ctx("6-6", [_order(ts=100)], now_ns=700_000_000_000)

    assert StartPriceCancelCheck().passes(ctx) is False
