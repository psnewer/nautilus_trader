"""ArbPolymarketExecutionClient —— 离线可测部分(纯映射 + MRO)。

完整集成(真 ClobClient/ws_auth/Data API、_submit_order/_run_health_check 接线)经 /live-test 验。
"""

from dataclasses import dataclass

from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient

from nautilus_trader.adapters.polymarket.arb_execution import ArbPolymarketExecutionClient
from nautilus_trader.adapters.polymarket.arb_execution import pm_position_to_settlement
from src.arbitrage.execution.session import ArbExecutionSessionMixin
from src.arbitrage.settlement.settlement import SettlementPosition


@dataclass
class _PMPos:
    condition_id: str
    size: float
    neg_risk: bool = False
    redeemable: bool = False


def test_mro_mixin_before_upstream():
    # mixin 必须在上游前,才能覆盖 _send_order_event / _submit_order
    mro = ArbPolymarketExecutionClient.__mro__
    assert mro.index(ArbExecutionSessionMixin) < mro.index(PolymarketExecutionClient)


def test_position_to_settlement_maps_fields():
    p = _PMPos(condition_id="0xcond", size=80.0, neg_risk=True, redeemable=True)
    assert pm_position_to_settlement(p) == SettlementPosition(
        condition_id="0xcond", size=80.0, neg_risk=True, redeemable=True,
    )


def test_position_to_settlement_defaults():
    p = _PMPos(condition_id="0xc", size=10.0)
    s = pm_position_to_settlement(p)
    assert s.neg_risk is False and s.redeemable is False
