"""Slice 9.5(#49 in-process smoke):mean_rebate 全链路验证。

不依赖真 NT TradingNode / Cache,直接喂 hand-crafted snapshot + 经 JSON loader 拿 Strategy,
跑 `evaluate_tree` + `pending_actions[0].execute(ctx)` —— 验证:
  1. JSON config → JSON loader → Strategy(Check/Action 经 registry 装入)
  2. evaluate_tree:pre_match self_hits signal pass + MeanRebateCheck pass + 写 scratch["legs"]
  3. PlaceBetsAction consume scratch + log 每 leg(Q-D1=A smoke)

也就是 `mean_rebate` 配置→决策→fire(log-only)端到端真实跑一次。
"""

import asyncio
import logging
from unittest.mock import MagicMock

import msgspec
import pytest

from src.arbitrage.config.dispatcher import to_strategy_registry
from src.arbitrage.config.schema import ArbConfig
from src.arbitrage.strategy.actions.place_bets import PlaceBetsAction
from src.arbitrage.strategy.check_action_registry import _reset_for_tests
from src.arbitrage.strategy.check_action_registry import register_action
from src.arbitrage.strategy.check_action_registry import register_check
from src.arbitrage.strategy.checks.mean_rebate import MeanRebateCheck
from src.arbitrage.strategy.checks.mean_rebate_recovery import MeanRebateRecoveryCheck
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.condition import evaluate_tree
from src.arbitrage.strategy.signals import SignalStore
from src.arbitrage.strategy.snapshot import OpportunitySnapshot


@pytest.fixture(autouse=True)
def _registry():
    """每 test 重置 + 注 launcher 用的 3 类(模拟 launcher main 顶部 register_*)。"""
    _reset_for_tests()
    register_check("mean_rebate", MeanRebateCheck)
    register_check("mean_rebate_recovery", MeanRebateRecoveryCheck)
    register_action("place_bets", PlaceBetsAction)
    yield
    _reset_for_tests()


def _fake_book(ask_price):
    book = MagicMock()
    book.best_ask_price = MagicMock(return_value=ask_price)
    return book


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _build_mean_rebate_strategy():
    """读 `arb_config.example.json` 样的配置 → 经 dispatcher → 拿 Strategy。"""
    cfg = msgspec.convert({
        "strategy": {
            "strategies": {
                "mean_rebate": {
                    "arbitrage_tree": {
                        "self_hits": {"signal": "pre_match"},
                        "checktion": [
                            {"type": "mean_rebate", "params": {"min_rate": 0.05}},
                        ],
                        "actions": [{"type": "place_bets"}],
                    },
                },
            },
            "bindings": [{"scope": "competition:ATP", "strategy_id": "mean_rebate"}],
        },
    }, type=ArbConfig)
    registry = to_strategy_registry(cfg)
    strategy = registry.get_for(None, "ATP", None)
    assert strategy is not None, "strategy 应经 binding 装到 competition:ATP"
    return strategy


def _3way_arbitrage_snapshot(in_play: bool = False) -> OpportunitySnapshot:
    """#228:3-way 拆分后的 [yes,no] pair snapshot(OE 两侧更便宜 → mean_rebate 命中)。

    best yes = OE back 4.0 → 0.25;best no = OE 合成 no(ask=lay 原值 2.0)→ 1−1/2=0.50;
    sum 0.75 → rate 0.25。
    """
    books = {
        "HY.POLYMARKET":  _fake_book(0.30),   # home market YES token
        "HN.POLYMARKET":  _fake_book(0.55),   # home market NO token
        "H.ORBITEXCH":    _fake_book(4.0),    # yes(back)prob 0.25
        "HNO.ORBITEXCH":  _fake_book(2.0),    # 合成 no:prob=1−1/2.0=0.50
    }
    infos = {
        "HY.POLYMARKET":  {"selection_role": "home", "claim": "yes", "in_play": in_play},
        "HN.POLYMARKET":  {"selection_role": "home", "claim": "no", "in_play": in_play},
        "H.ORBITEXCH":    {"selection_role": "home", "claim": "yes", "in_play": in_play},
        "HNO.ORBITEXCH":  {"selection_role": "home", "claim": "no",
                           "exec_instrument_id": "H.ORBITEXCH", "in_play": in_play},
    }
    return OpportunitySnapshot(
        pair_id="pair_X",
        instrument_ids=list(books.keys()),
        order_books=books,
        instrument_info=infos,
        in_play=in_play,
        outcomes=["yes", "no"],
    )


# ─── 端到端 smoke ──────────────────────────────────────────────────

def test_mean_rebate_full_pipeline_logs_three_submits(caplog):
    """**核心 e2e**:JSON 配置 → JSON loader → Strategy → evaluate_tree 命中 → PlaceBetsAction.execute → log 3 leg。

    `min(prob) × 3 = 0.25 × 3 = 0.75 → rate = 0.25 > 0.05` → mean_rebate 命中。
    pre_match signal True(snapshot.in_play=False)→ self_hits 通过。
    """
    strategy = _build_mean_rebate_strategy()
    snap = _3way_arbitrage_snapshot(in_play=False)
    ctx = EvalContext(
        pair_id="pair_X",
        snapshot=snap,
        store=SignalStore().view("pair_X"),
        strategy_defaults={"share": 22.5, "max_leg_share": 100.0},
    )
    ctx.store.set_transient("pre_match", True)

    # 1. evaluate(纯求值,无副作用)
    res = evaluate_tree(strategy.arbitrage_tree, ctx)
    assert res.hit is True
    assert len(res.pending_actions) == 1
    assert isinstance(res.pending_actions[0], PlaceBetsAction)

    # 2. Check 写 scratch(#228:[yes,no] pair → 2 条腿)
    assert "legs" in ctx.scratch
    assert len(ctx.scratch["legs"]) == 2
    assert ctx.scratch["mean_rebate_rate"] == 0.25  # 1 - (0.25 + 0.50)

    # 3. fire action(log-only smoke)
    with caplog.at_level(logging.INFO, logger="src.arbitrage.strategy.actions.place_bets"):
        _run(res.pending_actions[0].execute(ctx))

    msgs = [r.message for r in caplog.records]
    # header log
    assert any("PlaceBets[smoke]" in m and "pair=pair_X" in m and "legs=2" in m for m in msgs)
    # 2 个 leg 各一行(yes/no 都选 OE 因更便宜)
    assert sum("ORBITEXCH" in m and "would submit" in m for m in msgs) == 2


def test_in_play_blocks_mean_rebate(caplog):
    """**门控 smoke**:snapshot.in_play=True → pre_match signal False → MeanRebateCheck 不跑 → 无 action fire。"""
    strategy = _build_mean_rebate_strategy()
    snap = _3way_arbitrage_snapshot(in_play=True)
    ctx = EvalContext(
        pair_id="pair_X",
        snapshot=snap,
        store=SignalStore().view("pair_X"),
        strategy_defaults={"share": 22.5, "max_leg_share": 100.0},
    )
    ctx.store.set_transient("pre_match", False)

    res = evaluate_tree(strategy.arbitrage_tree, ctx)
    assert res.hit is False
    assert "legs" not in ctx.scratch    # MeanRebateCheck 没跑 → 没写 scratch


def test_no_arb_below_threshold_no_action(caplog):
    """**阈值 smoke**:rate=0.05(刚好等于阈值 0.05)+ 改阈值 0.10 不命中。"""
    # 构造 rate=0.05(prob sum=0.95)
    books = {
        "H.POLYMARKET": _fake_book(0.50),   # 0.50
        "A.POLYMARKET": _fake_book(0.50),   # 0.50
        "H.ORBITEXCH":  _fake_book(2.5),    # 0.40
        "A.ORBITEXCH":  _fake_book(2.5),    # 0.40
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home", "in_play": False},
        "A.POLYMARKET": {"selection_role": "away", "in_play": False},
        "H.ORBITEXCH":  {"selection_role": "home", "in_play": False},
        "A.ORBITEXCH":  {"selection_role": "away", "in_play": False},
    }
    # 2-way: min(0.50, 0.40) * 2 = 0.80 → rate=0.20
    # 用 min_rate=0.30 → 不命中
    cfg = msgspec.convert({
        "strategy": {
            "strategies": {
                "mr_strict": {
                    "arbitrage_tree": {
                        "self_hits": {"signal": "pre_match"},
                        "checktion": [
                            {"type": "mean_rebate", "params": {"min_rate": 0.30}},
                        ],
                        "actions": [{"type": "place_bets"}],
                    },
                },
            },
            "bindings": [{"scope": "sport:Tennis", "strategy_id": "mr_strict"}],
        },
    }, type=ArbConfig)
    strategy = to_strategy_registry(cfg).get_for(None, None, "Tennis")

    snap = OpportunitySnapshot(
        pair_id="pair_X", instrument_ids=list(books.keys()),
        order_books=books, instrument_info=infos, in_play=False,
    )
    ctx = EvalContext(
        pair_id="pair_X",
        snapshot=snap,
        store=SignalStore().view("pair_X"),
        strategy_defaults={"share": 22.5, "max_leg_share": 100.0},
    )
    ctx.store.set_transient("pre_match", True)
    res = evaluate_tree(strategy.arbitrage_tree, ctx)
    assert res.hit is False


def test_recovery_tree_config_builds_with_recovery_intent():
    cfg = msgspec.convert({
        "strategy": {
            "strategies": {
                "mr_recovery": {
                    "arbitrage_tree": {
                        "self_hits": {"signal": "pre_match"},
                        "checktion": [
                            {"type": "mean_rebate", "params": {"min_rate": 0.30}},
                        ],
                        "actions": [{"type": "place_bets"}],
                    },
                    "compensation_tree": {
                        "checktion": [
                            {"type": "mean_rebate_recovery", "params": {"min_repaired_rebate": -0.05}},
                        ],
                        "actions": [{"type": "place_bets", "params": {"intent": "recovery"}}],
                    },
                },
            },
            "bindings": [{"scope": "competition:ATP", "strategy_id": "mr_recovery"}],
        },
    }, type=ArbConfig)

    registry = to_strategy_registry(cfg)
    strategy = registry.get_for(None, "ATP", None)

    assert strategy is not None
    assert isinstance(strategy.compensation_tree.actions[0], PlaceBetsAction)
