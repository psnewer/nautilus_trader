"""
集成测试共享 fixtures

提供 StrategyService + debug_manager 的标准化测试环境。
"""

import asyncio

import pytest

from src.arbitrage.services.debug import debug_manager
from src.arbitrage.services.strategy.config import StrategyServiceConfig
from src.arbitrage.services.strategy.service import StrategyService


PAIR_ID = "pair-integ-test"
COMPETITION = "Test League"
HOME_TEAM = "Team Home"
AWAY_TEAM = "Team Away"


class DummyRiskService:
    """用于集成测试的假风控服务"""

    def __init__(self, way_rebate_combined=None, way_rebate_by_venue=None):
        self._way_rebate_combined = way_rebate_combined or {}
        self._way_rebate_by_venue = way_rebate_by_venue or {}

    def get_way_rebate(self, pair_id: str) -> dict[str, float]:
        return self._way_rebate_combined

    def get_way_rebate_by_venue(self, pair_id: str) -> dict[str, dict[str, float]]:
        return self._way_rebate_by_venue

    def check_risk(self, pair_id: str):
        class Result:
            allowed = True
        return Result()


class OpportunityCapture:
    """捕获 StrategyService 产生的 opportunity"""

    def __init__(self):
        self.opportunities: list[dict] = []

    def capture(self, service: StrategyService):
        """从 service 获取最新的 opportunity 列表"""
        self.opportunities = list(service.get_opportunities(limit=100))


@pytest.fixture
def strategy_service():
    """创建标准配置的 StrategyService，share=15"""
    config = StrategyServiceConfig()
    risk_service = DummyRiskService()
    service = StrategyService(config=config, risk_service=risk_service, share=15.0)
    service.register_match(
        pair_id=PAIR_ID,
        competition=COMPETITION,
        home_team=HOME_TEAM,
        away_team=AWAY_TEAM,
        is_live=False,
    )
    return service


@pytest.fixture
def debug_enabled():
    """启用 debug 模式，测试后清理"""
    debug_manager.enable()
    debug_manager.set_override("min_rebate_rate", enabled=True, value=-10.0)
    debug_manager.set_override("use_mock_exchange", enabled=True, value=True)
    yield debug_manager
    # 清理
    debug_manager.set_override("min_rebate_rate", enabled=False)
    debug_manager.set_override("use_mock_exchange", enabled=False)
    debug_manager.set_override("polymarket_price", enabled=False)
    debug_manager.set_override("orbitexch_price", enabled=False)
    debug_manager.disable()
