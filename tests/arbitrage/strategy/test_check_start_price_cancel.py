"""start_price_cancel：按 start_game intent 精确撤销领先/超时无比分挂单。"""

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


def test_no_score_over_timeout_generates_targeted_cancel():
    submitted = 1_000_000_000
    ctx = _ctx("", [_order(ts=submitted)], now_ns=submitted + 600_000_000_001)

    assert StartPriceCancelCheck(timeout_ms=600_000).passes(ctx) is True
    assert ctx.scratch["cancel_pair_orders"]["reason"] == "start_price_cancel:no_score_timeout"


def test_no_score_exactly_at_timeout_does_not_cancel():
    submitted = 1_000_000_000
    ctx = _ctx("", [_order(ts=submitted)], now_ns=submitted + 600_000_000_000)

    assert StartPriceCancelCheck(timeout_ms=600_000).passes(ctx) is False


def test_non_start_game_order_is_not_canceled():
    ctx = _ctx("3-2", [_order(intent="arbitrage", ts=100)], now_ns=700_000_000_000)

    assert StartPriceCancelCheck().passes(ctx) is False


def test_existing_unparseable_score_does_not_use_no_score_timeout():
    ctx = _ctx("6-6", [_order(ts=100)], now_ns=700_000_000_000)

    assert StartPriceCancelCheck().passes(ctx) is False
