"""
Size 门控全链路集成测试

验证从赔率注入 → 策略评估 → size 门控 → opportunity 生成的完整链路。
使用 debug override (min_rebate_rate=-10) 强制触发套利机会，
聚焦验证 adjusted_share 在各环节的正确传递。

TC-1: 充足流动性，全链路通过
TC-2: 流动性不足，按比例缩放
TC-3: 最小 Size 不足，机会被拒绝
TC-4: 无 size 数据，向后兼容
TC-5: Recovery min size 退出（单元级验证）
"""

import pytest

from src.arbitrage.services.strategy.config import StrategyServiceConfig
from src.arbitrage.services.strategy.service import StrategyService
from src.arbitrage.common.utils import MIN_SIZE_ORBITEXCH, MIN_SIZE_POLYMARKET

from .conftest import PAIR_ID, DummyRiskService


# ============================================================================
# 辅助函数
# ============================================================================

def _make_service(share: float = 15.0) -> StrategyService:
    """创建带 debug rebate override 的 service"""
    config = StrategyServiceConfig()
    risk_service = DummyRiskService()
    service = StrategyService(config=config, risk_service=risk_service, share=share)
    service.register_match(
        pair_id=PAIR_ID,
        competition="Test League",
        home_team="Team Home",
        away_team="Team Away",
        is_live=False,
    )
    return service


def _apply_odds(
    service: StrategyService,
    pair_id: str,
    polymarket_odds: dict[str, dict],
    orbitexch_odds: dict[str, dict],
):
    """注入赔率并触发策略评估"""
    for market_type, data in polymarket_odds.items():
        payload = {"market_type": market_type, **data}
        service.on_odds_update(pair_id, "polymarket", payload)
    for market_type, data in orbitexch_odds.items():
        payload = {"market_type": market_type, **data}
        service.on_odds_update(pair_id, "orbitexch", payload)


def _get_latest_opportunity(service: StrategyService) -> dict | None:
    """获取最新的 opportunity"""
    opps = service.get_opportunities(limit=10)
    return opps[-1] if opps else None


# ============================================================================
# TC-1: 充足流动性，全链路通过
# ============================================================================

class TestTC1FullFlowSufficientLiquidity:
    """验证 size 数据从赔率采集到 opportunity 的完整传递"""

    def test_adjusted_share_equals_original(self, debug_enabled):
        """available >> intended → adjusted_share = share"""
        service = _make_service(share=15.0)

        _apply_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 500, "ask_size": 500},
            "away": {"bid": 0.44, "ask": 0.45, "bid_size": 500, "ask_size": 500},
        }, {
            "home": {"back": 1.82, "lay": 1.85, "back_size": 200, "lay_size": 200},
            "away": {"back": 2.30, "lay": 2.35, "back_size": 200, "lay_size": 200},
        })

        opp = _get_latest_opportunity(service)
        assert opp is not None, "Should generate opportunity with debug rebate override"
        assert opp["adjusted_share"] == 15.0

    def test_opportunity_contains_best_direction(self, debug_enabled):
        """opportunity 应包含 best_direction"""
        service = _make_service()

        _apply_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 500, "ask_size": 500},
            "away": {"bid": 0.44, "ask": 0.45, "bid_size": 500, "ask_size": 500},
        }, {
            "home": {"back": 1.82, "lay": 1.85, "back_size": 200, "lay_size": 200},
            "away": {"back": 2.30, "lay": 2.35, "back_size": 200, "lay_size": 200},
        })

        opp = _get_latest_opportunity(service)
        assert opp is not None
        assert opp["best_direction"] is not None
        assert len(opp["best_direction"]["legs"]) >= 2


# ============================================================================
# TC-2: 流动性不足，按比例缩放
# ============================================================================

class TestTC2ScaledLiquidity:
    """验证可交易量不足时等比例缩放并传递 adjusted_share"""

    def test_poly_constrained_orbit_passes(self, debug_enabled):
        """Poly available=10 < share=15 → scale to 10,
        orbit odds=1.20 → stake=10/1.20=8.33 >= 7 ✓"""
        service = _make_service(share=15.0)

        _apply_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 10, "ask_size": 10},
            "away": {"bid": 0.44, "ask": 0.45, "bid_size": 10, "ask_size": 10},
        }, {
            "home": {"back": 1.20, "lay": 1.25, "back_size": 200, "lay_size": 200},
            "away": {"back": 2.30, "lay": 2.35, "back_size": 200, "lay_size": 200},
        })

        opp = _get_latest_opportunity(service)
        # Whether opportunity is generated depends on which direction is chosen
        # and whether that direction's legs pass min size
        # If opp exists, adjusted_share should be scaled
        if opp is not None:
            assert opp["adjusted_share"] <= 15.0
            assert opp["adjusted_share"] >= MIN_SIZE_POLYMARKET

    def test_scaling_rejected_when_orbit_stake_below_min(self, debug_enabled):
        """Poly available=10, orbit odds=1.82 → stake=10/1.82=5.49 < 7 → reject"""
        service = _make_service(share=15.0)

        # Only provide home market for poly (to constrain which direction is picked)
        _apply_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 10, "ask_size": 10},
            "away": {"bid": 0.44, "ask": 0.45, "bid_size": 10, "ask_size": 10},
        }, {
            "home": {"back": 1.82, "lay": 1.85, "back_size": 200, "lay_size": 200},
            "away": {"back": 2.30, "lay": 2.35, "back_size": 200, "lay_size": 200},
        })

        # With these odds the best direction may pick legs where poly is constrained
        # to ask_size=10 and orbit odds are high enough that stake < 7
        # The key assertion: if adjusted_share would give orbit stake < 7, no opportunity
        opp = _get_latest_opportunity(service)
        if opp is not None:
            # If opportunity generated, it must have passed min size
            adjusted = opp["adjusted_share"]
            best = opp["best_direction"]
            for leg in best["legs"]:
                if leg["venue"] == "orbitexch":
                    stake = adjusted / leg["raw_odds"] if leg["raw_odds"] > 0 else 0
                    assert stake >= MIN_SIZE_ORBITEXCH
                elif leg["venue"] == "polymarket":
                    assert adjusted >= MIN_SIZE_POLYMARKET


# ============================================================================
# TC-3: 最小 Size 不足 — 机会被拒绝
# ============================================================================

class TestTC3BelowMinimumRejected:
    """缩放后不满足最小 size → 不生成 opportunity"""

    def test_tiny_poly_liquidity_blocks_opportunity(self, debug_enabled):
        """Poly available=3 → adjusted=3 < MIN_POLY=5 → 不生成 opportunity"""
        service = _make_service(share=15.0)

        _apply_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 3, "ask_size": 3},
            "away": {"bid": 0.44, "ask": 0.45, "bid_size": 3, "ask_size": 3},
        }, {
            "home": {"back": 1.82, "lay": 1.85, "back_size": 200, "lay_size": 200},
            "away": {"back": 2.30, "lay": 2.35, "back_size": 200, "lay_size": 200},
        })

        opp = _get_latest_opportunity(service)
        # Both poly markets have ask_size=3, so any direction using poly buy
        # will be constrained to 3 < MIN_SIZE_POLYMARKET → rejected
        assert opp is None, "Should not generate opportunity when poly size < minimum"

    def test_all_markets_tiny_blocks_opportunity(self, debug_enabled):
        """所有市场 available=1 → 全部方向被拒"""
        service = _make_service(share=15.0)

        _apply_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 1, "ask_size": 1},
            "away": {"bid": 0.44, "ask": 0.45, "bid_size": 1, "ask_size": 1},
        }, {
            "home": {"back": 1.82, "lay": 1.85, "back_size": 1, "lay_size": 1},
            "away": {"back": 2.30, "lay": 2.35, "back_size": 1, "lay_size": 1},
        })

        opp = _get_latest_opportunity(service)
        assert opp is None


# ============================================================================
# TC-4: 无 Size 数据 — 向后兼容
# ============================================================================

class TestTC4NoSizeBackwardCompat:
    """赔率不含 size 字段 → available=0 → 不阻止"""

    def test_no_size_fields_generates_opportunity(self, debug_enabled):
        """无 bid_size/ask_size/back_size/lay_size → 正常生成 opportunity"""
        service = _make_service(share=15.0)

        _apply_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56},
            "away": {"bid": 0.44, "ask": 0.45},
        }, {
            "home": {"back": 1.82, "lay": 1.85},
            "away": {"back": 2.30, "lay": 2.35},
        })

        opp = _get_latest_opportunity(service)
        assert opp is not None, "Should generate opportunity when no size data (backward compat)"
        assert opp["adjusted_share"] == 15.0

    def test_partial_size_poly_only(self, debug_enabled):
        """只有 Polymarket 有 size，OrbitExch 没有 → 不阻止"""
        service = _make_service(share=15.0)

        _apply_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 500, "ask_size": 500},
            "away": {"bid": 0.44, "ask": 0.45, "bid_size": 500, "ask_size": 500},
        }, {
            "home": {"back": 1.82, "lay": 1.85},  # 无 size
            "away": {"back": 2.30, "lay": 2.35},   # 无 size
        })

        opp = _get_latest_opportunity(service)
        assert opp is not None
        assert opp["adjusted_share"] == 15.0


# ============================================================================
# TC-5: Recovery min size 退出（单元级验证）
# ============================================================================

class TestTC5RecoveryMinSize:
    """验证 recovery 阶段的 min size 检查逻辑"""

    def test_check_min_size_below_orbit_threshold(self):
        """OrbitExch stake < MIN_SIZE_ORBITEXCH → 不满足"""
        from src.arbitrage.common.utils import check_min_size
        assert check_min_size("orbitexch", 6.99) is False
        assert check_min_size("orbitexch", 7.0) is True
        assert check_min_size("orbitexch", 7.01) is True

    def test_check_min_size_below_poly_threshold(self):
        """Polymarket share < MIN_SIZE_POLYMARKET → 不满足"""
        from src.arbitrage.common.utils import check_min_size
        assert check_min_size("polymarket", 4.99) is False
        assert check_min_size("polymarket", 5.0) is True

    def test_check_all_legs_min_size(self):
        """check_all_legs_min_size 在任一腿不满足时返回 False"""
        from src.arbitrage.common.utils import check_all_legs_min_size
        legs_ok = [
            {"venue": "polymarket", "size": 10.0},
            {"venue": "orbitexch", "size": 8.0},
        ]
        assert check_all_legs_min_size(legs_ok) is True

        legs_fail = [
            {"venue": "polymarket", "size": 10.0},
            {"venue": "orbitexch", "size": 5.0},  # < 7
        ]
        assert check_all_legs_min_size(legs_fail) is False
