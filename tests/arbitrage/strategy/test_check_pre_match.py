"""Slice 9(#49):`PreMatchCheck` 读 `ctx.snapshot.in_play`。"""

from src.arbitrage.strategy.checks.pre_match import PreMatchCheck
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.snapshot import OpportunitySnapshot


def _ctx(in_play: bool) -> EvalContext:
    snap = OpportunitySnapshot(pair_id="p", in_play=in_play)
    return EvalContext(pair_id="p", snapshot=snap)


def test_pre_match_passes_when_not_in_play():
    assert PreMatchCheck().passes(_ctx(in_play=False)) is True


def test_pre_match_fails_when_in_play():
    assert PreMatchCheck().passes(_ctx(in_play=True)) is False


def test_pre_match_passes_when_snapshot_none():
    """无 snapshot 不阻塞(让其他 Check 决定)。"""
    ctx = EvalContext(pair_id="p", snapshot=None)
    assert PreMatchCheck().passes(ctx) is True
