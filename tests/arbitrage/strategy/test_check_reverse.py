"""ReverseCheck:即时返水相对动态 standard 的回撤门。"""

import pytest

import src.arbitrage.strategy.checks.reverse as reverse_module
from src.arbitrage.strategy.checks.reverse import ReverseCheck
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.runtime_store import StrategyRuntimeStore


def _ctx(*, standard=0.4):
    store = StrategyRuntimeStore()
    if standard is not None:
        store.update("head_rebate", "pair-1", {"standard": standard})
    return EvalContext(
        pair_id="pair-1",
        strategy_id="head_rebate",
        runtime_store=store,
    )


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (0.1, True),   # 等于 0.5 * 0.4 - 0.1
        (0.09, True),
        (0.11, False),
    ],
)
def test_reverse_check_uses_inclusive_rt_standard_minus_retrieve(monkeypatch, current, expected):
    ctx = _ctx()
    monkeypatch.setattr(reverse_module, "instant_rebate", lambda _: current)

    assert ReverseCheck(rt=0.5, retrieve=0.1).passes(ctx) is expected


def test_reverse_check_fails_closed_without_current_standard_or_runtime_identity(monkeypatch):
    monkeypatch.setattr(reverse_module, "instant_rebate", lambda _: None)
    assert ReverseCheck(rt=1.0, retrieve=0.1).passes(_ctx()) is False

    monkeypatch.setattr(reverse_module, "instant_rebate", lambda _: 0.0)
    assert ReverseCheck(rt=1.0, retrieve=0.1).passes(_ctx(standard=None)) is False
    ctx = _ctx()
    ctx.strategy_id = None
    assert ReverseCheck(rt=1.0, retrieve=0.1).passes(ctx) is False


@pytest.mark.parametrize(("rt", "retrieve"), [(float("nan"), 0.1), (1.0, float("inf"))])
def test_reverse_check_rejects_non_finite_params(rt, retrieve):
    with pytest.raises(ValueError, match="must be finite"):
        ReverseCheck(rt=rt, retrieve=retrieve)
