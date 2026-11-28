"""历史策略回放对生产 profit gate 的接线测试。"""

from types import SimpleNamespace

import pytest

from scripts.arbitrage_audit.replay_strategy_log import _paper_payload
from scripts.arbitrage_audit.replay_strategy_log import _PaperReplay
from scripts.arbitrage_audit.replay_strategy_log import _trigger_ratio_at_order
from src.arbitrage.strategy.consecutive_triggers import ConsecutiveTriggerStore
from src.arbitrage.strategy.consecutive_triggers import TriggerObservation
from src.arbitrage.strategy.consecutive_triggers import score_key


def _observation(ts_ns: int, score: str, outcome: str) -> TriggerObservation:
    return TriggerObservation(
        ts_ns=ts_ns,
        event_name="MarketOrderBookDeltas",
        score_raw=score,
        score_key=score_key(score),
        outcome=outcome,
        legs=(),
        candidate_ids=(),
        rates=(),
        passed=True,
    )


def test_trigger_ratio_uses_live_store_and_current_draw_boundary() -> None:
    pair = "Tennis|A|B"
    history_key = "pre_rebate_original_leg"
    store = ConsecutiveTriggerStore()
    for observation in (
        _observation(11, "0-0", "yes"),
        _observation(21, "1-0", "yes"),
        _observation(31, "1-1", "no"),
        _observation(41, "2-1", "yes"),
        _observation(51, "2-2", "no"),
        _observation(61, "3-2", "yes"),
    ):
        store.append(pair, history_key, observation)
    sports = SimpleNamespace(
        get=lambda _game_id: SimpleNamespace(score="3-2"),
        score_history=lambda _game_id: tuple(
            SimpleNamespace(score=score, ts_event=ts_ns)
            for ts_ns, score in (
                (10, "0-0"),
                (20, "1-0"),
                (30, "1-1"),
                (40, "2-1"),
                (50, "2-2"),
                (60, "3-2"),
            )
        ),
    )

    assert _trigger_ratio_at_order(
        trigger_store=store,
        sports_store=sports,
        pair_id=pair,
        game_id=1,
        history_key=history_key,
        when=None,
    ) == "4:2"
    assert _trigger_ratio_at_order(
        trigger_store=store,
        sports_store=sports,
        pair_id=pair,
        game_id=1,
        history_key=history_key,
        when="current_draw",
    ) == "3:1"


@pytest.mark.asyncio
async def test_closed_profit_triggers_production_match_tp_gate() -> None:
    paper = _PaperReplay("Tennis|A|B", 10_000.0)
    token_id = "pmyes.POLYMARKET"
    try:
        await paper.exchange._on_market_prices(token_id, 0.30, 0.31)
        buy = await paper.exchange.place(
            _paper_payload(token_id, "BUY", 0.31, 5.05, 1, "GTC", False),
        )
        assert buy["success"] is True

        await paper.exchange._on_market_prices(token_id, 0.83, 0.84)
        sell = await paper.exchange.place(
            _paper_payload(token_id, "SELL", 0.83, 5.05, 2, "GTC", False),
        )
        assert sell["success"] is True

        exposures = paper.outcome_exposures("Tennis|A|B")
        assert exposures["yes"].net_profit == pytest.approx(2.626)
        assert exposures["no"].net_profit == pytest.approx(2.626)

        allowed, reason = paper.profit_gate_allows(
            [{"instrument_id": "pmno.POLYMARKET", "intent": "arbitrage"}],
            {"match_tp": 0.01, "match_sl": -0.01},
            5.05,
        )
        assert allowed is False
        assert reason == "match_tp gate: pair=Tennis|A|B all_profit>0.0505"
    finally:
        await paper.close()


@pytest.mark.asyncio
async def test_recovery_order_keeps_production_profit_gate_exception() -> None:
    paper = _PaperReplay("Tennis|A|B", 10_000.0)
    try:
        allowed, reason = paper.profit_gate_allows(
            [{"instrument_id": "pmyes.POLYMARKET", "intent": "recovery"}],
            {"match_tp": -1.0, "match_sl": -2.0},
            5.05,
        )
        assert allowed is True
        assert reason == ""
    finally:
        await paper.close()
