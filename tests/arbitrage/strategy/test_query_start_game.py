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


def test_start_game_rejects_pre_unknown_and_missing_runtime_state():
    assert StartGameQuery().matches(_ctx(phase="PRE")) is False
    assert StartGameQuery().matches(_ctx(phase=None)) is False
    assert StartGameQuery().matches(live_context()) is False
