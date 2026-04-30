"""
场景: place_and_cancel

测试两个平台 (polymarket / orbitexch) 的下单 + 撤单链路。
- 强制触发套利机会 (min_rebate_rate=-10)
- 极端价格保证不会成交 (poly=0.01 ~ 1% 概率, orbit=100 ~ 1% 概率)
- 最小订单尺寸 (poly=5, orbit=7)
- 撤单走 cleanup 流程

成功条件: 两边下单 → 两边撤单
失败条件: 任一 ERROR 级别日志（限 executor）
超时: 5 分钟
"""

from src.arbitrage.testing.conditions import AllOf, AnyOf, LogMatch, Sequence
from src.arbitrage.testing.scenario import TestScenario


class PlaceAndCancelScenario(TestScenario):
    name = "place_and_cancel"
    description = "下单 -> 撤单 -> 退出，验证 polymarket/orbitexch 下单撤单链路"

    debug_overrides = {
        # 价格掉包：极端价确保不成交
        "polymarket_price": (True, 0.01),
        "orbitexch_price": (True, 100.0),
        # 最小尺寸
        "polymarket_size": (True, 5),
        "orbitexch_size": (True, 7),
        # 强制触发
        "min_rebate_rate": (True, -10.0),
        # 不跳过执行（真发单）
        "skip_execution": (False, False),
        # 不用 mock exchange
        "use_mock_exchange": (False, False),
        # 跳过策略层 size 检查（mock_data 注入的赔率会让 stake 远低于平台 min）
        "skip_check_size": (True, True),
    }

    # 不强制覆盖策略，用配置默认（mean_rebate）。
    # 如需切换策略，赋值 strategy = "<strategy_name_from_config>"。
    strategy = None

    success = Sequence(
        AllOf(
            LogMatch(
                logger="PolymarketExecutor",
                level="INFO",
                contains="Order placed",
                name="polymarket placed",
            ),
            LogMatch(
                logger="OrbitExchExecutor",
                level="INFO",
                contains="Order placed",
                name="orbitexch placed",
            ),
            name="both placed",
        ),
        AllOf(
            LogMatch(
                logger="PolymarketExecutor",
                level="INFO",
                contains="Order cancelled",
                name="polymarket cancelled",
            ),
            LogMatch(
                logger="OrbitExchExecutor",
                level="INFO",
                contains="Order cancelled",
                name="orbitexch cancelled",
            ),
            name="both cancelled",
        ),
        name="placed -> cancelled",
    )

    failure = AnyOf(
        LogMatch(
            logger="PolymarketExecutor",
            level="ERROR",
            contains="Failed to place order",
            name="polymarket place error",
        ),
        LogMatch(
            logger="OrbitExchExecutor",
            level="ERROR",
            contains="Failed to place order",
            name="orbitexch place error",
        ),
        LogMatch(
            logger="PolymarketExecutor",
            level="ERROR",
            contains="Failed to cancel order",
            name="polymarket cancel error",
        ),
        LogMatch(
            logger="OrbitExchExecutor",
            level="ERROR",
            contains="Failed to cancel order",
            name="orbitexch cancel error",
        ),
        name="any executor error",
    )

    timeout_sec = 300.0
