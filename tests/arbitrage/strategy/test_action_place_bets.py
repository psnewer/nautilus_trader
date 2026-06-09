"""Slice 9(#49):`PlaceBetsAction` log-only smoke + size 算法(PM=share / OE=share/odds)。"""

import asyncio
import logging

from src.arbitrage.strategy.actions.place_bets import PlaceBetsAction
from src.arbitrage.strategy.actions.place_bets import _compute_size
from src.arbitrage.strategy.condition import EvalContext


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_pm_size_equals_share():
    assert _compute_size("POLYMARKET", 22.5, 0.40) == 22.5


def test_oe_size_is_share_over_odds():
    # share=22.5, odds=2.5 → stake = 9.0
    assert _compute_size("ORBITEXCH", 22.5, 2.5) == 9.0


def test_oe_size_zero_when_price_invalid():
    assert _compute_size("ORBITEXCH", 22.5, 0) == 0.0
    assert _compute_size("ORBITEXCH", 22.5, -1) == 0.0


def test_action_log_only_no_raise_when_no_legs(caplog):
    """Check 没写 scratch["legs"] → Action 静默 skip,不 raise。"""
    ctx = EvalContext(pair_id="p")
    action = PlaceBetsAction(share=22.5)
    _run(action.execute(ctx))
    # 没下单 log(只 debug 级别 skip)


def test_action_logs_each_leg(caplog):
    ctx = EvalContext(pair_id="p")    # submitter=None → log-only fallback
    ctx.scratch["legs"] = [
        {"instrument_id": "H.POLYMARKET", "venue": "POLYMARKET", "side": "BUY",
         "role": "home", "price": 0.4, "prob": 0.4},
        {"instrument_id": "A.ORBITEXCH", "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4},
    ]
    ctx.scratch["mean_rebate_rate"] = 0.15

    with caplog.at_level(logging.INFO, logger="src.arbitrage.strategy.actions.place_bets"):
        _run(PlaceBetsAction(share=22.5).execute(ctx))

    msgs = [r.message for r in caplog.records]
    assert any("PlaceBets[smoke]" in m and "legs=2" in m for m in msgs)
    assert any("H.POLYMARKET" in m and "qty=22.5000" in m for m in msgs)
    assert any("A.ORBITEXCH" in m and "qty=9.0000" in m for m in msgs)


# ── slice 10a(#50):submitter 接通 ─────────────────────────────────

def test_action_calls_submitter_when_present(caplog):
    """ctx.submitter 非 None → Action 经 `await submitter(spec)` 真出单(不走 log-only fallback)。"""
    calls = []
    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {"instrument_id": "H.POLYMARKET", "venue": "POLYMARKET", "side": "BUY",
         "role": "home", "price": 0.4, "prob": 0.4},
        {"instrument_id": "A.ORBITEXCH", "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4},
    ]
    ctx.scratch["mean_rebate_rate"] = 0.15

    with caplog.at_level(logging.INFO, logger="src.arbitrage.strategy.actions.place_bets"):
        _run(PlaceBetsAction(share=22.5).execute(ctx))

    # 2 leg → 2 submitter 调用
    assert len(calls) == 2
    # PM leg:size=22.5,price=0.4
    assert calls[0]["instrument_id"] == "H.POLYMARKET"
    assert calls[0]["side"] == "BUY"
    assert calls[0]["qty"] == 22.5
    assert calls[0]["price"] == 0.4
    # OE leg:size=22.5/2.5=9.0,price=2.5
    assert calls[1]["instrument_id"] == "A.ORBITEXCH"
    assert calls[1]["qty"] == 9.0
    assert calls[1]["price"] == 2.5
    # log mode = "submit" 不是 "smoke"
    msgs = [r.message for r in caplog.records]
    assert any("PlaceBets[submit]" in m for m in msgs)
    # log-only fallback "would submit" 不应出现
    assert not any("would submit" in m for m in msgs)


def test_submitter_none_falls_back_to_log_only():
    """显式确认:submitter=None 走 log-only,无 raise(已被 test_action_logs_each_leg 覆盖,但显式再确认)。"""
    ctx = EvalContext(pair_id="p", submitter=None)
    ctx.scratch["legs"] = [{"instrument_id": "X", "venue": "POLYMARKET", "side": "BUY",
                             "role": "home", "price": 0.5, "prob": 0.5}]
    _run(PlaceBetsAction(share=10.0).execute(ctx))   # 不 raise
