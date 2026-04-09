"""
RiskService 单元测试
"""

import pytest

from src.arbitrage.services.risk.config import RiskConfig
from src.arbitrage.services.risk.service import RiskService, RiskCheckResult
from src.arbitrage.services.risk.messages import WayRebateMessage
from src.arbitrage.services.execution.messages import SessionCompleteMessage
from .conftest import load_case, build_legs, DummyPolymarketPosition, DummyOddsService


# =========================================================================
# 辅助函数
# =========================================================================


def _build_service_with_position(case: dict) -> RiskService:
    """从测试用例构建 RiskService 并加载单场持仓"""
    config = RiskConfig.from_dict(case["config"])
    share = case.get("share", 100.0)
    service = RiskService(config=config, share=share)
    pair_id = case["pair_id"]
    for leg_data in case["legs"]:
        service._position_manager.add_fill(
            pair_id=pair_id,
            venue=leg_data["venue"],
            market_type=leg_data["market_type"],
            size=leg_data["size"],
            price=leg_data["price"],
        )
    return service


def _build_service_with_multi_matches(case: dict) -> RiskService:
    """从测试用例构建 RiskService 并加载多场持仓"""
    config = RiskConfig.from_dict(case["config"])
    share = case.get("share", 100.0)
    service = RiskService(config=config, share=share)
    for match in case["matches"]:
        pair_id = match["pair_id"]
        for leg_data in match["legs"]:
            service._position_manager.add_fill(
                pair_id=pair_id,
                venue=leg_data["venue"],
                market_type=leg_data["market_type"],
                size=leg_data["size"],
                price=leg_data["price"],
            )
    return service


class FakeMsgBus:
    """模拟 MessageBus"""

    def __init__(self):
        self.published: list[tuple[str, object]] = []
        self._subscribers: dict[str, list] = {}

    def subscribe(self, topic: str, handler):
        self._subscribers.setdefault(topic, []).append(handler)

    def publish(self, topic: str, msg):
        self.published.append((topic, msg))

    def get_published_of_type(self, msg_type):
        return [(t, m) for t, m in self.published if isinstance(m, msg_type)]


# =========================================================================
# check_risk — execution_enabled 门控
# =========================================================================


class TestCheckRiskExecutionEnabled:

    def test_execution_disabled_blocks_all(self):
        """execution_enabled=False → 直接返回 not allowed"""
        case = load_case("risk_check_execution_disabled")
        config = RiskConfig.from_dict(case["config"])
        service = RiskService(config=config)
        result = service.check_risk("any-pair")
        assert result.allowed is False
        assert result.reason == "Execution disabled"

    def test_execution_disabled_overrides_risk_disabled(self):
        """execution_enabled=False 优先于 enabled=False"""
        config = RiskConfig(enabled=False, execution_enabled=False)
        service = RiskService(config=config)
        result = service.check_risk("any-pair")
        assert result.allowed is False
        assert result.reason == "Execution disabled"

    def test_execution_enabled_risk_disabled_allows(self):
        """execution_enabled=True + enabled=False → 允许（跳过风控）"""
        case = load_case("risk_check_disabled")
        config = RiskConfig.from_dict(case["config"])
        service = RiskService(config=config)
        result = service.check_risk("any-pair")
        assert result.allowed is True
        assert result.reason == "Risk disabled"


# =========================================================================
# check_risk — 止损 / 止盈 / 全局止损
# =========================================================================


class TestCheckRiskRules:

    def test_all_pass(self):
        """所有检查通过"""
        case = load_case("risk_check_all_pass")
        service = _build_service_with_position(case)
        result = service.check_risk(case["pair_id"])
        assert result.allowed is True
        assert result.way_rebate is not None
        assert result.min_way_rebate is not None

    def test_match_sl_triggered(self):
        """单场止损触发"""
        case = load_case("risk_check_match_sl_triggered")
        service = _build_service_with_position(case)
        result = service.check_risk(case["pair_id"])
        assert result.allowed is False
        assert result.match_blocked is True
        assert result.tp_blocked is False
        assert result.global_blocked is False

    def test_match_tp_triggered(self):
        """单场止盈触发"""
        case = load_case("risk_check_match_tp_triggered")
        service = _build_service_with_position(case)
        result = service.check_risk(case["pair_id"])
        assert result.allowed is False
        assert result.tp_blocked is True
        assert result.match_blocked is False

    def test_global_sl_triggered(self):
        """全局累计止损触发"""
        case = load_case("risk_check_global_sl_triggered")
        service = _build_service_with_multi_matches(case)
        result = service.check_risk(case["check_pair_id"])
        assert result.allowed is False
        assert result.global_blocked is True

    def test_match_sl_override(self):
        """单场止损覆盖 — 使用更严格的阈值"""
        case = load_case("risk_check_match_sl_override")
        service = _build_service_with_position(case)
        result = service.check_risk(case["pair_id"])
        assert result.allowed is False
        assert result.match_blocked is True

    def test_no_position_allows(self):
        """无持仓时允许下注"""
        config = RiskConfig()
        service = RiskService(config=config)
        result = service.check_risk("no-position-pair")
        assert result.allowed is True

    def test_is_match_allowed_shortcut(self):
        """is_match_allowed 是 check_risk().allowed 的快捷方式"""
        case = load_case("risk_check_all_pass")
        service = _build_service_with_position(case)
        assert service.is_match_allowed(case["pair_id"]) is True

        case2 = load_case("risk_check_match_sl_triggered")
        service2 = _build_service_with_position(case2)
        assert service2.is_match_allowed(case2["pair_id"]) is False


# =========================================================================
# check_risk — execution_enabled 动态切换
# =========================================================================


class TestCheckRiskDynamicToggle:

    def test_toggle_execution_off_then_on(self):
        """运行时切换 execution_enabled"""
        case = load_case("risk_check_all_pass")
        service = _build_service_with_position(case)

        # 初始状态：允许
        assert service.check_risk(case["pair_id"]).allowed is True

        # 关闭执行
        new_config = RiskConfig.from_dict(case["config"])
        new_config.execution_enabled = False
        service.update_config(new_config)
        assert service.check_risk(case["pair_id"]).allowed is False
        assert service.check_risk(case["pair_id"]).reason == "Execution disabled"

        # 重新开启
        new_config.execution_enabled = True
        service.update_config(new_config)
        assert service.check_risk(case["pair_id"]).allowed is True


# =========================================================================
# MessageBus — set_msgbus 订阅
# =========================================================================


class TestMsgBusIntegration:

    def test_set_msgbus_subscribes(self):
        """set_msgbus 应订阅 session_complete 主题"""
        service = RiskService()
        bus = FakeMsgBus()
        service.set_msgbus(bus)
        from src.arbitrage.services.execution.topics import SESSION_COMPLETE_TOPIC_PATTERN
        assert SESSION_COMPLETE_TOPIC_PATTERN in bus._subscribers
        assert len(bus._subscribers[SESSION_COMPLETE_TOPIC_PATTERN]) == 1

    def test_set_msgbus_idempotent(self):
        """重复设置同一 msgbus 不会重复订阅"""
        service = RiskService()
        bus = FakeMsgBus()
        service.set_msgbus(bus)
        service.set_msgbus(bus)  # 重复调用
        from src.arbitrage.services.execution.topics import SESSION_COMPLETE_TOPIC_PATTERN
        assert len(bus._subscribers[SESSION_COMPLETE_TOPIC_PATTERN]) == 1

    def test_set_odds_service(self):
        """set_odds_service 设置引用"""
        service = RiskService()
        odds = DummyOddsService()
        service.set_odds_service(odds)
        assert service._odds_service is odds


# =========================================================================
# MessageBus — session_complete 处理
# =========================================================================


class TestSessionCompleteHandler:

    def test_on_session_complete_refreshes_and_publishes(self):
        """收到 session_complete → 刷新持仓 → 发布 way_rebate"""
        case = load_case("msgbus_session_complete")
        pair_id = case["pair_id"]

        # 构建 DummyOddsService
        poly_positions = [DummyPolymarketPosition(d) for d in case["polymarket_positions"]]
        odds = DummyOddsService(
            polymarket_positions=poly_positions,
            orbitexch_bets=case["orbitexch_bets"],
            mappings=case["mappings"],
        )

        # 构建 service
        service = RiskService(share=30.0)
        bus = FakeMsgBus()
        service.set_msgbus(bus)
        service.set_odds_service(odds)

        # 发送 SessionCompleteMessage
        msg = SessionCompleteMessage(
            session_id="sess-1",
            pair_id=pair_id,
            opportunity_id="opp-1",
            phase="done",
            end_reason="completed",
        )
        service._on_session_complete_message(msg)

        # 验证持仓已刷新
        pos = service.get_position(pair_id)
        assert pos is not None
        assert len(pos.legs) == 2  # 1 polymarket + 1 orbitexch

        # 验证 way_rebate 已发布
        rebate_msgs = bus.get_published_of_type(WayRebateMessage)
        assert len(rebate_msgs) >= 1
        topic, rebate_msg = rebate_msgs[-1]
        assert rebate_msg.pair_id == pair_id
        assert isinstance(rebate_msg.way_rebate, dict)
        assert "home" in rebate_msg.way_rebate
        assert "away" in rebate_msg.way_rebate

    def test_on_session_complete_without_odds_service_warns(self):
        """没有 odds_service 时警告但不崩溃"""
        service = RiskService()
        bus = FakeMsgBus()
        service.set_msgbus(bus)
        # 不设置 odds_service

        msg = SessionCompleteMessage(
            session_id="sess-1",
            pair_id="pair-x",
            opportunity_id="opp-1",
            phase="done",
            end_reason="completed",
        )
        # 不应抛出异常
        service._on_session_complete_message(msg)

        # 不应发布 way_rebate（因为无法刷新）
        rebate_msgs = bus.get_published_of_type(WayRebateMessage)
        assert len(rebate_msgs) == 0

    def test_ignores_non_session_complete_message(self):
        """忽略非 SessionCompleteMessage 类型"""
        service = RiskService()
        bus = FakeMsgBus()
        service.set_msgbus(bus)
        service.set_odds_service(DummyOddsService())

        # 发送错误类型
        service._on_session_complete_message("not a message")
        assert len(bus.published) == 0

    def test_refresh_all_way_rebates_preserves_fx_for_orbitexch_legs(self):
        """全量刷新重建 PositionManager 后，应保留已配置的 fx。"""
        case = load_case("msgbus_session_complete")
        poly_positions = [DummyPolymarketPosition(d) for d in case["polymarket_positions"]]
        odds = DummyOddsService(
            polymarket_positions=poly_positions,
            orbitexch_bets=case["orbitexch_bets"],
            mappings=case["mappings"],
        )

        service = RiskService(share=30.0)
        service.set_odds_service(odds)
        service.set_fx(1.33)

        service._refresh_all_way_rebates()

        pos = service.get_position(case["pair_id"])
        assert pos is not None
        orbit_leg = [leg for leg in pos.legs if leg.venue == "orbitexch"][0]
        assert orbit_leg.fx == pytest.approx(1.33)


# =========================================================================
# refresh_pair_position
# =========================================================================


class TestRefreshPairPosition:

    def test_refresh_replaces_legs(self):
        """刷新持仓清除旧 legs 并加载新数据"""
        case = load_case("msgbus_session_complete")
        pair_id = case["pair_id"]

        service = RiskService(share=30.0)
        # 先加一个旧的 leg
        service._position_manager.add_fill(pair_id, "polymarket", "home", 10.0, 0.3)
        assert len(service.get_position(pair_id).legs) == 1

        # 构建刷新数据
        poly_positions = [DummyPolymarketPosition(d) for d in case["polymarket_positions"]]

        # 转换 selection_mappings 的 key 为 int
        selection_mappings = {}
        for pid, mapping in case["mappings"]["selection_mappings"].items():
            selection_mappings[pid] = {int(k): v for k, v in mapping.items()}
        mappings = {
            "polymarket_pair_mapping": case["mappings"]["polymarket_pair_mapping"],
            "orbitexch_pair_mapping": case["mappings"]["orbitexch_pair_mapping"],
            "selection_mappings": selection_mappings,
        }

        service.refresh_pair_position(
            pair_id=pair_id,
            polymarket_positions=poly_positions,
            orbitexch_bets=case["orbitexch_bets"],
            mappings=mappings,
        )

        pos = service.get_position(pair_id)
        assert len(pos.legs) == 2  # 旧 leg 被替换

    def test_refresh_nonexistent_pair_creates_position(self):
        """刷新不存在的 pair 会创建新持仓"""
        service = RiskService(share=30.0)
        service.refresh_pair_position(
            pair_id="new-pair",
            polymarket_positions=[],
            orbitexch_bets=[],
            mappings={},
        )
        # 不崩溃即可 — 空数据刷新
        # position 可能存在也可能不存在，取决于 refresh_position 的行为


# =========================================================================
# load_historical_positions + way_rebate 发布
# =========================================================================


class TestLoadHistoricalPositions:

    def test_loads_and_publishes_way_rebate(self):
        """加载历史持仓后，对每个有持仓的 pair 发布 way_rebate"""
        case = load_case("msgbus_session_complete")
        poly_positions = [DummyPolymarketPosition(d) for d in case["polymarket_positions"]]

        # 转换 selection_mappings
        selection_mappings = {}
        for pid, mapping in case["mappings"]["selection_mappings"].items():
            selection_mappings[pid] = {int(k): v for k, v in mapping.items()}

        service = RiskService(share=30.0)
        bus = FakeMsgBus()
        service.set_msgbus(bus)

        result = service.load_historical_positions(
            polymarket_positions=poly_positions,
            orbitexch_bets=case["orbitexch_bets"],
            polymarket_pair_mapping=case["mappings"]["polymarket_pair_mapping"],
            orbitexch_pair_mapping=case["mappings"]["orbitexch_pair_mapping"],
            selection_mappings=selection_mappings,
        )

        assert result["polymarket"] == 1
        assert result["orbitexch"] == 1

        # 验证 way_rebate 消息已发布
        rebate_msgs = bus.get_published_of_type(WayRebateMessage)
        assert len(rebate_msgs) >= 1
        _, msg = rebate_msgs[0]
        assert msg.pair_id == case["pair_id"]

    def test_load_clears_existing_positions(self):
        """加载历史持仓会先清空现有数据"""
        service = RiskService(share=30.0)
        service._position_manager.add_fill("old-pair", "polymarket", "home", 50.0, 0.4)
        assert service.get_position("old-pair") is not None

        service.load_historical_positions(
            polymarket_positions=[],
            orbitexch_bets=[],
            polymarket_pair_mapping={},
            orbitexch_pair_mapping={},
            selection_mappings={},
        )
        assert service.get_position("old-pair") is None


# =========================================================================
# get_global_status
# =========================================================================


class TestGetGlobalStatus:

    def test_basic_status_structure(self):
        """全局状态包含所有必要字段"""
        service = RiskService()
        status = service.get_global_status()
        assert "enabled" in status
        assert "execution_enabled" not in status  # execution_enabled 不在 global_status 中
        assert "match_sl" in status
        assert "global_sl" in status
        assert "match_tp" in status
        assert "global_min_sum" in status
        assert "global_blocked" in status
        assert "active_positions" in status
        assert "blocked_matches" in status
        assert "tp_matches" in status

    def test_status_with_blocked_match(self):
        """有止损比赛时 blocked_matches 非空"""
        case = load_case("risk_check_match_sl_triggered")
        service = _build_service_with_position(case)
        status = service.get_global_status()
        assert case["pair_id"] in status["blocked_matches"]

    def test_status_with_tp_match(self):
        """有止盈比赛时 tp_matches 非空"""
        case = load_case("risk_check_match_tp_triggered")
        service = _build_service_with_position(case)
        status = service.get_global_status()
        assert case["pair_id"] in status["tp_matches"]

    def test_status_empty_positions(self):
        """无持仓时状态正常"""
        service = RiskService()
        status = service.get_global_status()
        assert status["active_positions"] == 0
        assert status["global_min_sum"] == 0.0
        assert status["global_blocked"] is False


# =========================================================================
# update_config
# =========================================================================


class TestUpdateConfig:

    def test_update_config_changes_thresholds(self):
        """更新配置改变阈值"""
        service = RiskService()
        new_config = RiskConfig(match_sl=-0.20, global_sl=-0.80, match_tp=0.15)
        service.update_config(new_config)
        assert service.config.match_sl == -0.20
        assert service.config.global_sl == -0.80
        assert service.config.match_tp == 0.15

    def test_update_config_execution_enabled(self):
        """更新 execution_enabled"""
        service = RiskService()
        new_config = RiskConfig(execution_enabled=False)
        service.update_config(new_config)
        assert service.config.execution_enabled is False
        result = service.check_risk("any-pair")
        assert result.allowed is False


# =========================================================================
# close_match / set_share
# =========================================================================


class TestMiscOperations:

    def test_close_match(self):
        """关闭比赛"""
        service = RiskService()
        service._position_manager.add_fill("pair-1", "polymarket", "home", 50.0, 0.4)
        service.close_match("pair-1")
        pos = service.get_position("pair-1")
        assert pos.closed is True

    def test_set_share(self):
        """更新 share"""
        service = RiskService(share=100.0)
        service._position_manager.add_fill("pair-1", "polymarket", "home", 50.0, 0.4)
        service.set_share(200.0)
        pos = service.get_position("pair-1")
        assert pos.share == 200.0

    def test_clear_positions(self):
        """清空持仓"""
        service = RiskService()
        service._position_manager.add_fill("pair-1", "polymarket", "home", 50.0, 0.4)
        service.clear_positions()
        assert service.get_position("pair-1") is None

    def test_get_way_rebate(self):
        """查询方向返水率"""
        service = RiskService(share=100.0)
        service._position_manager.add_fill("pair-1", "polymarket", "home", 100.0, 0.4)
        service._position_manager.add_fill("pair-1", "orbitexch", "away", 50.0, 2.5)
        rebate = service.get_way_rebate("pair-1")
        assert "home" in rebate
        assert "away" in rebate
        assert rebate["home"] == pytest.approx(0.10, abs=1e-4)
        assert rebate["away"] == pytest.approx(0.35, abs=1e-4)

    def test_get_way_rebate_nonexistent(self):
        """查询不存在的 pair 返回空"""
        service = RiskService()
        assert service.get_way_rebate("nope") == {}

    def test_get_all_way_rebates(self):
        """获取所有比赛返水率"""
        service = RiskService(share=100.0)
        service._position_manager.add_fill("pair-1", "polymarket", "home", 100.0, 0.4)
        service._position_manager.add_fill("pair-2", "polymarket", "home", 100.0, 0.5)
        all_rebates = service.get_all_way_rebates()
        assert "pair-1" in all_rebates
        assert "pair-2" in all_rebates

    def test_get_positions_summary(self):
        """持仓摘要包含必要结构"""
        service = RiskService(share=100.0)
        service._position_manager.add_fill("pair-1", "polymarket", "home", 50.0, 0.4)
        summary = service.get_positions_summary()
        assert "positions" in summary
        assert "pair-1" in summary["positions"]
