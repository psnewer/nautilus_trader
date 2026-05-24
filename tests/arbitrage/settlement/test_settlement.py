"""PolymarketSettlement merge/redeem 编排(settlement-8.2~8.7)。

contract IO 用 FakeContract 录调用,不上链。
"""

import pytest

from nautilus_trader.adapters.polymarket.contract import TxResult

from src.arbitrage.settlement.settlement import PolymarketSettlement
from src.arbitrage.settlement.settlement import SettlementPosition

pytestmark = pytest.mark.asyncio


class FakeContract:
    def __init__(self, merge_result=None, redeem_result=None):
        self.merge_calls = []
        self.redeem_calls = []
        self._merge_result = merge_result or TxResult(success=True, tx_hash="0xm")
        self._redeem_result = redeem_result or TxResult(success=True, tx_hash="0xr")

    async def merge_positions(self, condition_id, amount, neg_risk=False):
        self.merge_calls.append((condition_id, amount, neg_risk))
        return self._merge_result

    async def redeem_positions(self, condition_id, neg_risk, amounts=None):
        self.redeem_calls.append((condition_id, neg_risk, amounts))
        return self._redeem_result


def _settlement(contract, **kw):
    return PolymarketSettlement(contract, **kw)


# ── merge(settlement-8.2 / 8.3）─────────────────────────────────────
async def test_merge_two_outcomes_uses_min_size():
    c = FakeContract()
    pos = [SettlementPosition("cond1", 80.0), SettlementPosition("cond1", 50.0)]
    await _settlement(c).run(pos)
    assert c.merge_calls == [("cond1", 50.0, False)]   # amount = min(80,50)


async def test_merge_neg_risk_flag_propagates():
    c = FakeContract()
    pos = [SettlementPosition("cond1", 30.0, neg_risk=True), SettlementPosition("cond1", 40.0)]
    await _settlement(c).run(pos)
    assert c.merge_calls == [("cond1", 30.0, True)]     # neg_risk = any → True


async def test_merge_skipped_when_single_outcome():
    c = FakeContract()
    await _settlement(c).run([SettlementPosition("cond1", 80.0)])
    assert c.merge_calls == []                           # 需 ≥2 outcome


async def test_merge_disabled():
    c = FakeContract()
    pos = [SettlementPosition("cond1", 80.0), SettlementPosition("cond1", 50.0)]
    await _settlement(c, merge_enabled=False).run(pos)
    assert c.merge_calls == []


# ── redeem(settlement-8.4 / 8.5）────────────────────────────────────
async def test_redeem_only_when_redeemable():
    c = FakeContract()
    await _settlement(c).run([SettlementPosition("cond1", 80.0, redeemable=False)])
    assert c.redeem_calls == []                          # 未结算不赎回


async def test_redeem_standard_market_no_amounts():
    c = FakeContract()
    await _settlement(c).run([SettlementPosition("cond1", 80.0, redeemable=True)])
    assert c.redeem_calls == [("cond1", False, None)]


async def test_redeem_neg_risk_passes_amounts():
    c = FakeContract()
    pos = [
        SettlementPosition("cond1", 80.0, neg_risk=True, redeemable=True),
        SettlementPosition("cond1", 50.0, neg_risk=True, redeemable=True),
    ]
    await _settlement(c).run(pos)
    assert c.redeem_calls == [("cond1", True, [80.0, 50.0])]


# ── 结果 / 失败(settlement-8.7）────────────────────────────────────
async def test_failure_logged_not_raised_and_recorded():
    c = FakeContract(merge_result=TxResult(success=False, message="reverted"))
    pos = [SettlementPosition("cond1", 80.0), SettlementPosition("cond1", 50.0)]
    result = await _settlement(c).run(pos)               # 不抛
    assert len(result.merges) == 1 and result.merges[0].success is False
    assert "merge: 0/1" in result.summary


async def test_exception_swallowed_into_errors():
    class Boom(FakeContract):
        async def merge_positions(self, condition_id, amount, neg_risk=False):
            raise RuntimeError("rpc down")

    pos = [SettlementPosition("cond1", 80.0), SettlementPosition("cond1", 50.0)]
    result = await _settlement(Boom()).run(pos)          # 异常吞进 errors,tick 不被打断
    assert result.errors and "rpc down" in result.errors[0]


async def test_empty_positions_noop():
    c = FakeContract()
    result = await _settlement(c).run([])
    assert not result.has_operations and c.merge_calls == [] and c.redeem_calls == []


async def test_zero_size_positions_ignored():
    c = FakeContract()
    await _settlement(c).run([SettlementPosition("cond1", 0.0), SettlementPosition("cond1", 0.0)])
    assert c.merge_calls == []                           # size>0 过滤后不足 2
