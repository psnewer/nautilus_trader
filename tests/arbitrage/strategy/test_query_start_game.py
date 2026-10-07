"""StartGameQuery 只命中明确赛中且该 pair 从未提交过订单。"""

from types import SimpleNamespace

from src.arbitrage.strategy.queries.start_game import StartGameQuery
from tests.arbitrage.strategy._live_state import live_context


class _PhaseStore:
    def __init__(self, phase):
        self._phase = phase

    def get(self, game_id):
        return SimpleNamespace(phase=self._phase) if self._phase is not None else None


def _ctx(phase="IN_PLAY", orders=()):
    ctx = live_context(
        infos={
            "Y.POLYMARKET": {"claim": "yes"},
            "N.POLYMARKET": {"claim": "no"},
        },
        orders=orders,
        phase_store=_PhaseStore(phase),
    )
    ctx.pair_registry.register(
        "p",
        ["Y.POLYMARKET", "N.POLYMARKET"],
        game_id=42,
    )
    return ctx


def test_start_game_matches_in_play_pair_without_any_order_history():
    assert StartGameQuery().matches(_ctx()) is True


def test_start_game_allows_initialized_or_risk_denied_order_without_submission():
    orders = [
        SimpleNamespace(instrument_id="Y.POLYMARKET", status="INITIALIZED", ts_submitted=0),
        SimpleNamespace(instrument_id="N.POLYMARKET", status="DENIED", ts_submitted=0),
    ]
    assert StartGameQuery().matches(_ctx(orders=orders)) is True


def test_start_game_rejects_order_which_reached_submitted():
    order = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        status="SUBMITTED",
        ts_submitted=123,
    )
    assert StartGameQuery().matches(_ctx(orders=[order])) is False


def test_start_game_rejects_terminal_order_which_was_previously_submitted():
    order = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        status="FILLED",
        ts_submitted=123,
    )
    assert StartGameQuery().matches(_ctx(orders=[order])) is False


def test_start_game_can_repeat_after_unfilled_start_game_cancel_when_enabled():
    order = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        status="CANCELED",
        ts_submitted=123,
        filled_qty=0,
        tags=["arb:intent=start_game"],
    )
    assert StartGameQuery(repeat_after_cancel=True).matches(_ctx(orders=[order])) is True
    assert StartGameQuery().matches(_ctx(orders=[order])) is False


def test_start_game_repeat_rejects_partial_fill_other_intent_and_open_order():
    partial = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        status="CANCELED",
        ts_submitted=123,
        filled_qty=1,
        tags=["arb:intent=start_game"],
    )
    other = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        status="CANCELED",
        ts_submitted=123,
        filled_qty=0,
        tags=["arb:intent=arbitrage"],
    )
    opened = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        status="ACCEPTED",
        ts_submitted=123,
        filled_qty=0,
        tags=["arb:intent=start_game"],
    )
    query = StartGameQuery(repeat_after_cancel=True)
    assert query.matches(_ctx(orders=[partial])) is False
    assert query.matches(_ctx(orders=[other])) is False
    assert query.matches(_ctx(orders=[opened])) is False


def test_start_game_repeat_after_cancel_requires_boolean():
    try:
        StartGameQuery(repeat_after_cancel="true")
    except ValueError as exc:
        assert "repeat_after_cancel must be a boolean" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_start_game_rejects_pre_unknown_and_missing_runtime_state():
    assert StartGameQuery().matches(_ctx(phase="PRE")) is False
    assert StartGameQuery().matches(_ctx(phase=None)) is False
    assert StartGameQuery().matches(live_context()) is False
