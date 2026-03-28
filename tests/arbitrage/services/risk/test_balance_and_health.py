"""
RiskService 余额门控和健康检查测试
"""

import pytest
from unittest.mock import Mock, AsyncMock, MagicMock
from dataclasses import dataclass

from src.arbitrage.services.risk.config import RiskConfig
from src.arbitrage.services.risk.service import RiskService, RiskCheckResult


# =========================================================================
# Mock 数据结构
# =========================================================================

@dataclass
class MockLeg:
    """模拟 Direction 的 Leg"""
    venue: Mock
    raw_odds: float = 1.0


@dataclass
class MockDirection:
    """模拟 Direction"""
    legs: list[MockLeg]


class MockVenue:
    """模拟 Venue enum"""
    def __init__(self, value: str):
        self.value = value


# =========================================================================
# 余额更新测试
# =========================================================================

class TestBalanceUpdate:
    """测试余额更新功能"""

    def test_update_orbitexch_balance(self):
        """测试 OrbitExch 余额更新"""
        service = RiskService()

        # 初始余额为 0
        assert service._oe_balance == 0.0

        # 更新余额
        service.update_orbitexch_balance(100.50)
        assert service._oe_balance == 100.50

        # 再次更新
        service.update_orbitexch_balance(200.75)
        assert service._oe_balance == 200.75

    def test_polymarket_balance_update_during_health_check(self):
        """测试 Polymarket 余额在健康检查时更新"""
        service = RiskService()

        # 初始余额为 0
        assert service._pm_balance == 0.0

        # 模拟健康检查更新余额
        service._pm_balance = 150.25
        assert service._pm_balance == 150.25

    def test_get_balances(self):
        """测试获取余额状态"""
        service = RiskService()
        service._pm_balance = 100.0
        service._oe_balance = 200.0

        balances = service.get_balances()
        assert balances["polymarket"] == 100.0
        assert balances["orbitexch"] == 200.0


# =========================================================================
# 余额门控测试
# =========================================================================

class TestBalanceGating:
    """测试余额门控功能"""

    def test_polymarket_balance_sufficient(self):
        """测试 Polymarket 余额充足的情况"""
        service = RiskService()
        service._pm_balance = 100.0  # 余额 100
        service._fx = 1.0

        # 模拟只在 Polymarket 下单，需要 50
        direction = MockDirection(legs=[
            MockLeg(venue=MockVenue("polymarket"), raw_odds=1.0),
        ])

        result = service.check_balance(
            pair_id="test-pair",
            share=50.0,  # 需要 50
            direction=direction
        )

        assert result.allowed is True

    def test_polymarket_balance_insufficient(self):
        """测试 Polymarket 余额不足的情况"""
        service = RiskService()
        service._pm_balance = 30.0  # 余额只有 30
        service._fx = 1.0

        # 模拟只在 Polymarket 下单，需要 50
        direction = MockDirection(legs=[
            MockLeg(venue=MockVenue("polymarket"), raw_odds=1.0),
        ])

        result = service.check_balance(
            pair_id="test-pair",
            share=50.0,  # 需要 50，但只有 30
            direction=direction
        )

        assert result.allowed is False
        assert "PM balance insufficient" in result.reason

    def test_orbitexch_balance_sufficient(self):
        """测试 OrbitExch 余额充足的情况"""
        service = RiskService()
        service._oe_balance = 100.0  # 余额 100
        service._fx = 1.5  # 汇率 1.5

        # 模拟只在 OrbitExch 下单，odds=2.0
        # size = share / odds / fx = 50 / 2.0 / 1.5 = 16.67
        direction = MockDirection(legs=[
            MockLeg(venue=MockVenue("orbitexch"), raw_odds=2.0),
        ])

        result = service.check_balance(
            pair_id="test-pair",
            share=50.0,
            direction=direction
        )

        assert result.allowed is True

    def test_orbitexch_balance_insufficient(self):
        """测试 OrbitExch 余额不足的情况"""
        service = RiskService()
        service._oe_balance = 10.0  # 余额只有 10
        service._fx = 1.5  # 汇率 1.5

        # 模拟只在 OrbitExch 下单，odds=2.0
        # size = share / odds / fx = 50 / 2.0 / 1.5 = 16.67，但只有 10
        direction = MockDirection(legs=[
            MockLeg(venue=MockVenue("orbitexch"), raw_odds=2.0),
        ])

        result = service.check_balance(
            pair_id="test-pair",
            share=50.0,
            direction=direction
        )

        assert result.allowed is False
        assert "OE balance insufficient" in result.reason

    def test_multi_platform_balance_check(self):
        """测试跨平台多单的余额检查"""
        service = RiskService()
        service._pm_balance = 100.0
        service._oe_balance = 50.0
        service._fx = 1.0

        # 模拟跨平台下单：PM需要50，OE需要30
        direction = MockDirection(legs=[
            MockLeg(venue=MockVenue("polymarket"), raw_odds=1.0),
            MockLeg(venue=MockVenue("orbitexch"), raw_odds=2.0),  # 50/2/1=25
        ])

        result = service.check_balance(
            pair_id="test-pair",
            share=50.0,  # PM需要50，OE需要25
            direction=direction
        )

        assert result.allowed is True

    def test_balance_with_active_orders(self):
        """测试余额检查包含活跃订单"""
        service = RiskService()
        service._pm_balance = 100.0
        service._fx = 1.0

        # 模拟 OddsService 和 clients
        mock_odds_service = Mock()
        mock_pm_client = Mock()
        mock_pm_client.get_active_orders = Mock(return_value=[{"size": 60.0}])
        mock_odds_service._polymarket_client = mock_pm_client
        mock_oe_client = Mock()
        mock_oe_client.get_active_orders = Mock(return_value=[])
        mock_odds_service._orbitexch_client = mock_oe_client
        service._odds_service = mock_odds_service

        # 新订单需要 50，总共需要 110，但只有 100
        direction = MockDirection(legs=[
            MockLeg(venue=MockVenue("polymarket"), raw_odds=1.0),
        ])

        result = service.check_balance(
            pair_id="test-pair",
            share=50.0,
            direction=direction
        )

        # 应该被拒绝，因为 100 < 50(新单) + 60(已有订单)
        assert result.allowed is False
        assert "PM balance insufficient" in result.reason


# =========================================================================
# 健康检查状态测试
# =========================================================================

class TestHealthCheck:
    """测试健康检查功能"""

    def test_initial_health_status(self):
        """测试初始健康状态"""
        service = RiskService()

        status = service.get_health_status()
        assert status["health_ok"] is False
        assert status["polymarket"] is False
        assert status["orbitexch"] is False
        assert status["way_rebate"] is False

    def test_health_status_update(self):
        """测试健康状态更新"""
        service = RiskService()

        # 模拟健康检查通过
        service._pm_healthy = True
        service._oe_healthy = True
        service._rebate_healthy = True
        service._health_ok = True

        status = service.get_health_status()
        assert status["health_ok"] is True
        assert status["polymarket"] is True
        assert status["orbitexch"] is True
        assert status["way_rebate"] is True

    def test_partial_health_failure(self):
        """测试部分健康检查失败"""
        service = RiskService()

        # 模拟只有 Polymarket 健康
        service._pm_healthy = True
        service._oe_healthy = False
        service._rebate_healthy = False
        service._health_ok = False  # 整体不健康

        status = service.get_health_status()
        assert status["health_ok"] is False
        assert status["polymarket"] is True
        assert status["orbitexch"] is False
        assert status["way_rebate"] is False


# =========================================================================
# check_risk 健康门控测试
# =========================================================================

class TestCheckRiskHealthGate:
    """测试 check_risk 中的健康门控"""

    def test_health_check_failed_blocks_opportunity(self):
        """测试健康检查失败时阻止机会"""
        config = RiskConfig(execution_enabled=True, enabled=True)
        service = RiskService(config=config)

        # 模拟健康检查失败
        service._pm_healthy = False
        service._oe_healthy = False
        service._rebate_healthy = False
        service._health_ok = False

        result = service.check_risk("test-pair")

        assert result.allowed is False
        assert "Health check failed" in result.reason
        assert "pm=FAIL" in result.reason
        assert "oe=FAIL" in result.reason
        assert "rebate=FAIL" in result.reason

    def test_partial_health_failure_blocks(self):
        """测试部分健康失败也会阻止"""
        config = RiskConfig(execution_enabled=True, enabled=True)
        service = RiskService(config=config)

        # 模拟只有 OrbitExch 失败
        service._pm_healthy = True
        service._oe_healthy = False
        service._rebate_healthy = True
        service._health_ok = False  # 有一个失败就整体不健康

        result = service.check_risk("test-pair")

        assert result.allowed is False
        assert "Health check failed" in result.reason
        assert "pm=OK" in result.reason
        assert "oe=FAIL" in result.reason

    def test_health_check_passes_continues_to_next_check(self):
        """测试健康检查通过后继续其他检查"""
        config = RiskConfig(execution_enabled=True, enabled=False)  # 禁用风控简化测试
        service = RiskService(config=config)

        # 模拟健康检查通过
        service._pm_healthy = True
        service._oe_healthy = True
        service._rebate_healthy = True
        service._health_ok = True

        # 设置 odds_service 避免 NoneType 错误
        service._odds_service = Mock()
        service._odds_service.get_polymarket_open_orders = Mock(return_value=[])
        service._odds_service.get_orbitexch_bets = Mock(return_value=[])

        result = service.check_risk("test-pair")

        # 健康检查通过，风控禁用，应该允许
        # 这证明健康检查门控后，流程继续到了后续检查
        assert result.allowed is True
        assert result.reason == "Risk disabled"

    def test_execution_disabled_takes_priority_over_health(self):
        """测试 execution_disabled 优先于健康检查"""
        config = RiskConfig(execution_enabled=False)
        service = RiskService(config=config)

        # 即使健康检查通过
        service._pm_healthy = True
        service._oe_healthy = True
        service._rebate_healthy = True
        service._health_ok = True

        result = service.check_risk("test-pair")

        # 应该因为 execution_disabled 被阻止，而不是健康检查
        assert result.allowed is False
        assert result.reason == "Execution disabled"


# =========================================================================
# 集成测试
# =========================================================================

class TestBalanceHealthIntegration:
    """测试余额和健康检查的集成"""

    def test_health_ok_but_balance_insufficient(self):
        """测试健康检查通过但余额不足"""
        config = RiskConfig(execution_enabled=True, enabled=False)  # 禁用风控简化测试
        service = RiskService(config=config)

        # 健康检查通过
        service._pm_healthy = True
        service._oe_healthy = True
        service._rebate_healthy = True
        service._health_ok = True

        # 但余额不足
        service._pm_balance = 10.0
        service._fx = 1.0

        # 先通过 check_risk（健康检查）
        risk_result = service.check_risk("test-pair")
        # check_risk 应该通过（健康OK，风控禁用）
        assert risk_result.allowed is True

        # 然后在 check_balance 中被拒绝
        direction = MockDirection(legs=[
            MockLeg(venue=MockVenue("polymarket"), raw_odds=1.0),
        ])

        balance_result = service.check_balance(
            pair_id="test-pair",
            share=50.0,  # 需要 50，但只有 10
            direction=direction
        )

        assert balance_result.allowed is False
        assert "balance insufficient" in balance_result.reason.lower()

    def test_both_health_and_balance_ok(self):
        """测试健康和余额都OK"""
        config = RiskConfig(execution_enabled=True, enabled=False)  # 禁用风控简化测试
        service = RiskService(config=config)

        # 健康检查通过
        service._pm_healthy = True
        service._oe_healthy = True
        service._rebate_healthy = True
        service._health_ok = True

        # 余额充足
        service._pm_balance = 1000.0
        service._oe_balance = 1000.0
        service._fx = 1.0

        # check_risk 应该通过
        risk_result = service.check_risk("test-pair")
        assert risk_result.allowed is True

        # check_balance 应该通过
        direction = MockDirection(legs=[
            MockLeg(venue=MockVenue("polymarket"), raw_odds=1.0),
        ])

        balance_result = service.check_balance(
            pair_id="test-pair",
            share=50.0,
            direction=direction
        )

        assert balance_result.allowed is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
