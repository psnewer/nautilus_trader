"""
_check_and_adjust_size 单元测试

验证 Size 门控逻辑：
- TC-1: 充足流动性，不缩放
- TC-2: 流动性不足，按比例缩放
- TC-3: 缩放后不满足最小 size，机会被拒绝
- TC-4: 无 size 数据（向后兼容），不阻止
"""

import pytest

from src.arbitrage.services.strategy.config import StrategyServiceConfig
from src.arbitrage.services.strategy.service import StrategyService
from src.arbitrage.services.strategy.signals.base import (
    ArbitrageAction,
    ArbitrageDirection,
    ArbitrageLeg,
    ArbitrageVenue,
)


PAIR_ID = "pair-size-test"
SHARE = 15.0


def _make_service(share: float = SHARE) -> StrategyService:
    config = StrategyServiceConfig()
    return StrategyService(config=config, share=share)


def _make_direction(
    poly_market: str = "home",
    poly_action: ArbitrageAction = ArbitrageAction.BUY,
    orbit_market: str = "away",
    orbit_action: ArbitrageAction = ArbitrageAction.BUY,
    orbit_odds: float = 1.82,
) -> ArbitrageDirection:
    """构造标准双腿套利方向"""
    return ArbitrageDirection(
        direction_id="dir-test",
        legs=[
            ArbitrageLeg(
                venue=ArbitrageVenue.POLYMARKET,
                market_type=poly_market,
                action=poly_action,
                probability=55.0,
                raw_odds=0.55,
            ),
            ArbitrageLeg(
                venue=ArbitrageVenue.ORBITEXCH,
                market_type=orbit_market,
                action=orbit_action,
                probability=45.0,
                raw_odds=orbit_odds,
            ),
        ],
        total_probability=100.0,
        rebate_rate=0.05,
        rebate_venue=ArbitrageVenue.POLYMARKET,
        rebate_market=poly_market,
    )


def _inject_odds(service: StrategyService, pair_id: str, poly_odds: dict, orbit_odds: dict):
    """直接注入 odds_cache，跳过信号计算"""
    service._odds_cache[pair_id] = {
        "polymarket": poly_odds,
        "orbitexch": orbit_odds,
    }


# ---- TC-1: 充足流动性，不缩放 ----

class TestTC1SufficientLiquidity:
    """TC-1: available >> intended → adjusted_share = share (无缩放)"""

    def test_no_scaling_when_ample_liquidity(self):
        service = _make_service()
        direction = _make_direction()

        _inject_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 500, "ask_size": 500, "market_type": "home"},
            "away": {"bid": 0.44, "ask": 0.45, "bid_size": 500, "ask_size": 500, "market_type": "away"},
        }, {
            "home": {"back": 1.82, "lay": 1.85, "back_size": 200, "lay_size": 200, "market_type": "home"},
            "away": {"back": 2.30, "lay": 2.35, "back_size": 200, "lay_size": 200, "market_type": "away"},
        })

        result = service._check_and_adjust_size(PAIR_ID, direction)
        assert result == SHARE

    def test_returns_share_when_direction_is_none(self):
        service = _make_service()
        result = service._check_and_adjust_size(PAIR_ID, None)
        assert result == SHARE


# ---- TC-2: 流动性不足，按比例缩放 ----

class TestTC2InsufficientLiquidity:
    """TC-2: available < intended → 等比例缩放"""

    def test_poly_constrains_scaling(self):
        """Polymarket available=10, intended=15 → scale_factor=0.667"""
        service = _make_service()
        # 使用较低 OrbitExch 赔率使缩放后 stake 满足最小值
        direction = _make_direction(orbit_odds=1.20)

        _inject_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 10, "ask_size": 10, "market_type": "home"},
        }, {
            "away": {"back": 1.20, "lay": 1.25, "back_size": 200, "lay_size": 200, "market_type": "away"},
        })

        result = service._check_and_adjust_size(PAIR_ID, direction)
        assert result == pytest.approx(10.0, abs=0.01)

    def test_orbit_constrains_scaling(self):
        """OrbitExch available=5 < intended stake → OrbitExch 触发缩放"""
        service = _make_service()
        # share=15, odds=1.20 → intended stake = 15/1.20 = 12.5, available=5 → ratio=0.4
        # adjusted_share = 15*0.4 = 6.0
        # poly: 6.0 >= 5 ✓, orbit: 6.0/1.20 = 5.0 < 7 ✗ → None
        direction = _make_direction(orbit_odds=1.20)

        _inject_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 500, "ask_size": 500, "market_type": "home"},
        }, {
            "away": {"back": 1.20, "lay": 1.25, "back_size": 5, "lay_size": 5, "market_type": "away"},
        })

        result = service._check_and_adjust_size(PAIR_ID, direction)
        # orbit stake = 6.0/1.20 = 5.0 < MIN_SIZE_ORBITEXCH(7) → rejected
        assert result is None

    def test_scaling_passes_min_size(self):
        """缩放后两腿都满足最小值 → 返回 adjusted_share"""
        service = _make_service(share=20.0)
        # share=20, odds=1.50 → intended stake=20/1.50=13.33
        # poly: available=12, intended=20 → ratio=0.6
        # adjusted=20*0.6=12.0
        # poly: 12.0 >= 5 ✓, orbit: 12.0/1.50=8.0 >= 7 ✓
        direction = _make_direction(orbit_odds=1.50)

        _inject_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 12, "ask_size": 12, "market_type": "home"},
        }, {
            "away": {"back": 1.50, "lay": 1.55, "back_size": 200, "lay_size": 200, "market_type": "away"},
        })

        result = service._check_and_adjust_size(PAIR_ID, direction)
        assert result == pytest.approx(12.0, abs=0.01)

    def test_rejected_when_orbit_stake_below_min(self):
        """TC-2 原始数据：share=15, poly available=10, orbit odds=1.82
        adjusted=10, orbit stake=10/1.82=5.49 < 7 → 拒绝"""
        service = _make_service()
        direction = _make_direction(orbit_odds=1.82)

        _inject_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 10, "ask_size": 10, "market_type": "home"},
        }, {
            "away": {"back": 1.82, "lay": 1.85, "back_size": 200, "lay_size": 200, "market_type": "away"},
        })

        result = service._check_and_adjust_size(PAIR_ID, direction)
        assert result is None


# ---- TC-3: 最小 Size 不足 — 机会被拒绝 ----

class TestTC3BelowMinimum:
    """TC-3: 缩放后不满足最小 size → 返回 None"""

    def test_poly_below_min_size(self):
        """available=3 → adjusted=3 < MIN_SIZE_POLYMARKET(5) → 拒绝"""
        service = _make_service()
        direction = _make_direction()

        _inject_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 3, "ask_size": 3, "market_type": "home"},
        }, {
            "away": {"back": 1.82, "lay": 1.85, "back_size": 200, "lay_size": 200, "market_type": "away"},
        })

        result = service._check_and_adjust_size(PAIR_ID, direction)
        assert result is None

    def test_very_small_liquidity(self):
        """available=1 → adjusted=1 → 两腿都不满足"""
        service = _make_service()
        direction = _make_direction()

        _inject_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 1, "ask_size": 1, "market_type": "home"},
        }, {
            "away": {"back": 1.82, "lay": 1.85, "back_size": 1, "lay_size": 1, "market_type": "away"},
        })

        result = service._check_and_adjust_size(PAIR_ID, direction)
        assert result is None


# ---- TC-4: 无 Size 数据 — 向后兼容 ----

class TestTC4NoSizeData:
    """TC-4: 赔率数据不含 size 字段 → available=0 → 跳过缩放，正常返回"""

    def test_no_size_fields_returns_full_share(self):
        """无 bid_size/ask_size/back_size/lay_size → 不阻止"""
        service = _make_service()
        direction = _make_direction()

        _inject_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "market_type": "home"},
        }, {
            "away": {"back": 1.82, "lay": 1.85, "market_type": "away"},
        })

        result = service._check_and_adjust_size(PAIR_ID, direction)
        assert result == SHARE

    def test_partial_size_data_poly_only(self):
        """只有 Polymarket 有 size 数据，OrbitExch 没有 → OrbitExch 不约束"""
        service = _make_service()
        direction = _make_direction()

        _inject_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 500, "ask_size": 500, "market_type": "home"},
        }, {
            "away": {"back": 1.82, "lay": 1.85, "market_type": "away"},  # 无 size
        })

        result = service._check_and_adjust_size(PAIR_ID, direction)
        assert result == SHARE

    def test_empty_odds_cache_returns_full_share(self):
        """odds_cache 为空但有 direction → 跳过缩放（all available=0）"""
        service = _make_service()
        direction = _make_direction()

        # 不注入 odds_cache，默认空
        result = service._check_and_adjust_size(PAIR_ID, direction)
        assert result == SHARE


# ---- 边界条件 ----

class TestEdgeCases:
    """边界条件测试"""

    def test_exactly_at_min_poly(self):
        """adjusted_share 恰好等于 MIN_SIZE_POLYMARKET=5 → 通过"""
        service = _make_service(share=5.0)
        direction = _make_direction(orbit_odds=1.0)
        # share=5, poly intended=5, orbit intended=5/1.0=5
        # available 充足 → no scaling
        # poly: 5 >= 5 ✓, orbit: 5/1.0=5 < 7 ✗ → rejected
        # 需要 orbit stake >= 7, so odds must be <= 5/7 ≈ 0.714
        # 换个赔率让 orbit 也满足
        direction2 = _make_direction(orbit_odds=0.70)

        _inject_odds(service, PAIR_ID, {
            "home": {"bid": 0.55, "ask": 0.56, "bid_size": 500, "ask_size": 500, "market_type": "home"},
        }, {
            "away": {"back": 0.70, "lay": 0.75, "back_size": 200, "lay_size": 200, "market_type": "away"},
        })

        result = service._check_and_adjust_size(PAIR_ID, direction2)
        # poly: 5 >= 5 ✓, orbit: 5/0.70 = 7.14 >= 7 ✓
        assert result == 5.0

    def test_sell_actions_use_bid_and_lay_sizes(self):
        """SELL 动作使用 bid_size (poly) 和 lay_size (orbit)"""
        service = _make_service()
        direction = _make_direction(
            poly_action=ArbitrageAction.SELL,
            orbit_action=ArbitrageAction.SELL,
        )

        _inject_odds(service, PAIR_ID, {
            "home": {
                "bid": 0.55, "ask": 0.56,
                "bid_size": 500,  # SELL 用 bid_size
                "ask_size": 3,    # BUY 用 ask_size (不影响)
                "market_type": "home",
            },
        }, {
            "away": {
                "back": 1.82, "lay": 1.85,
                "back_size": 1,     # BUY 用 back_size (不影响)
                "lay_size": 200,    # SELL 用 lay_size
                "market_type": "away",
            },
        })

        result = service._check_and_adjust_size(PAIR_ID, direction)
        assert result == SHARE
