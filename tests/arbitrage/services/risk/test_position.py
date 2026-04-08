"""
PositionLeg / MatchPosition / PositionManager 单元测试
"""

import pytest

from src.arbitrage.services.risk.position import PositionLeg, MatchPosition, PositionManager
from .conftest import load_case, build_legs, build_position, DummyPolymarketPosition


# =========================================================================
# PositionLeg — 盈亏计算
# =========================================================================


class TestPositionLeg:

    def test_polymarket_profit_and_loss(self):
        """Polymarket: profit = size*(1-price), loss = size*price"""
        case = load_case("position_leg_polymarket_home")
        leg = PositionLeg(
            venue=case["venue"],
            market_type=case["market_type"],
            size=case["size"],
            price=case["price"],
        )
        assert leg.profit_if_wins() == pytest.approx(case["expected_profit"])
        assert leg.loss_if_loses() == pytest.approx(case["expected_loss"])

    def test_orbitexch_profit_and_loss(self):
        """OrbitExch: profit = size*(price-1), loss = size"""
        case = load_case("position_leg_orbitexch_away")
        leg = PositionLeg(
            venue=case["venue"],
            market_type=case["market_type"],
            size=case["size"],
            price=case["price"],
        )
        assert leg.profit_if_wins() == pytest.approx(case["expected_profit"])
        assert leg.loss_if_loses() == pytest.approx(case["expected_loss"])

    def test_override_takes_precedence(self):
        """profit_override / loss_override 优先于计算值"""
        case = load_case("position_leg_with_override")
        leg = PositionLeg(
            venue=case["venue"],
            market_type=case["market_type"],
            size=case["size"],
            price=case["price"],
            profit_override=case["profit_override"],
            loss_override=case["loss_override"],
        )
        assert leg.profit_if_wins() == pytest.approx(case["expected_profit"])
        assert leg.loss_if_loses() == pytest.approx(case["expected_loss"])

    def test_to_dict_includes_calculated_fields(self):
        leg = PositionLeg(venue="polymarket", market_type="home", size=100.0, price=0.4)
        d = leg.to_dict()
        assert d["venue"] == "polymarket"
        assert d["market_type"] == "home"
        assert d["profit_if_wins"] == pytest.approx(60.0)
        assert d["loss_if_loses"] == pytest.approx(40.0)


# =========================================================================
# MatchPosition — way_rebate 核心逻辑
# =========================================================================


class TestMatchPosition:

    def test_basic_two_way_rebate(self):
        """基础双向套利 — 来自 position.py docstring 的示例"""
        case = load_case("case_basic_two_way")
        pos = build_position(case)
        rebate = pos.calculate_way_rebate()
        for outcome, expected in case["expected_way_rebate"].items():
            assert rebate[outcome] == pytest.approx(expected, abs=1e-4), \
                f"{outcome}: got {rebate[outcome]}, expected {expected}"

    def test_negative_rebate_direction(self):
        """有方向亏损的套利"""
        case = load_case("case_negative_rebate")
        pos = build_position(case)
        rebate = pos.calculate_way_rebate()
        for outcome, expected in case["expected_way_rebate"].items():
            assert rebate[outcome] == pytest.approx(expected, abs=1e-3), \
                f"{outcome}: got {rebate[outcome]}, expected {expected}"

    def test_three_way_market_has_draw_key(self):
        """三向市场包含 draw 方向"""
        case = load_case("case_three_way_with_draw")
        pos = build_position(case)
        rebate = pos.calculate_way_rebate()
        assert "home" in rebate
        assert "away" in rebate
        assert "draw" in rebate

    def test_single_leg_produces_two_outcomes(self):
        """单腿持仓仍产生 home 和 away 两个方向"""
        case = load_case("case_single_leg_only")
        pos = build_position(case)
        rebate = pos.calculate_way_rebate()
        assert set(rebate.keys()) == {"home", "away"}

    def test_empty_position_returns_empty(self):
        """无腿持仓返回空字典"""
        case = load_case("case_no_legs")
        pos = MatchPosition(pair_id=case["pair_id"], share=case["share"])
        assert pos.calculate_way_rebate() == {}

    def test_min_way_rebate(self):
        """get_min_way_rebate 返回最低方向"""
        case = load_case("case_negative_rebate")
        pos = build_position(case)
        min_rebate = pos.get_min_way_rebate()
        assert min_rebate is not None
        assert min_rebate == pytest.approx(-0.2667, abs=1e-3)

    def test_min_way_rebate_empty_position(self):
        """空持仓的 min_way_rebate 返回 None"""
        pos = MatchPosition(pair_id="empty")
        assert pos.get_min_way_rebate() is None

    def test_way_rebate_by_venue(self):
        """按平台分组计算 way_rebate"""
        case = load_case("case_basic_two_way")
        pos = build_position(case)
        by_venue = pos.calculate_way_rebate_by_venue()
        assert "polymarket" in by_venue
        assert "orbitexch" in by_venue
        # Polymarket 只有 home，所以 home 方向盈利，away 方向亏损
        assert by_venue["polymarket"]["home"] == pytest.approx(0.60, abs=1e-2)
        assert by_venue["polymarket"]["away"] == pytest.approx(-0.40, abs=1e-2)

    def test_add_leg_dedup_by_order_id(self):
        """同 order_id 的腿更新而非新增"""
        pos = MatchPosition(pair_id="dedup", share=100.0)
        leg1 = PositionLeg(venue="polymarket", market_type="home", size=50.0, price=0.4, order_id="ord-1")
        leg2 = PositionLeg(venue="polymarket", market_type="home", size=80.0, price=0.4, order_id="ord-1")
        pos.add_leg(leg1)
        pos.add_leg(leg2)
        assert len(pos.legs) == 1
        assert pos.legs[0].size == 80.0

    def test_add_leg_without_order_id_appends(self):
        """无 order_id 的腿始终新增"""
        pos = MatchPosition(pair_id="append", share=100.0)
        pos.add_leg(PositionLeg(venue="polymarket", market_type="home", size=50.0, price=0.4))
        pos.add_leg(PositionLeg(venue="polymarket", market_type="home", size=50.0, price=0.4))
        assert len(pos.legs) == 2

    def test_get_total_cost(self):
        """总成本 = 所有腿的 loss 之和"""
        case = load_case("case_basic_two_way")
        pos = build_position(case)
        # poly loss=40 + orbit loss=50 = 90
        assert pos.get_total_cost() == pytest.approx(90.0)

    def test_to_dict_structure(self):
        """to_dict 包含所有必要字段"""
        case = load_case("case_basic_two_way")
        pos = build_position(case)
        d = pos.to_dict()
        assert d["pair_id"] == case["pair_id"]
        assert "legs" in d
        assert "way_rebate" in d
        assert "way_rebate_by_venue" in d
        assert "min_way_rebate" in d
        assert "total_cost" in d
        assert d["closed"] is False

    def test_zero_share_returns_zero_rebate(self):
        """share=0 时返水率全部为 0"""
        pos = MatchPosition(pair_id="zero-share", share=0.0)
        pos.add_leg(PositionLeg(venue="polymarket", market_type="home", size=100.0, price=0.4))
        rebate = pos.calculate_way_rebate()
        assert all(v == 0.0 for v in rebate.values())


# =========================================================================
# PositionManager — 多场比赛管理
# =========================================================================


class TestPositionManager:

    def test_add_fill_creates_position(self):
        pm = PositionManager(default_share=100.0)
        pm.add_fill("pair-1", "polymarket", "home", 50.0, 0.4, competition="EPL")
        pos = pm.get_position("pair-1")
        assert pos is not None
        assert len(pos.legs) == 1
        assert pos.competition == "EPL"

    def test_get_or_create_idempotent(self):
        pm = PositionManager()
        p1 = pm.get_or_create_position("pair-1", competition="EPL")
        p2 = pm.get_or_create_position("pair-1")
        assert p1 is p2

    def test_get_position_nonexistent_returns_none(self):
        pm = PositionManager()
        assert pm.get_position("nope") is None

    def test_close_match(self):
        pm = PositionManager()
        pm.add_fill("pair-1", "polymarket", "home", 50.0, 0.4)
        pm.close_match("pair-1")
        pos = pm.get_position("pair-1")
        assert pos.closed is True

    def test_active_positions_excludes_closed(self):
        pm = PositionManager()
        pm.add_fill("pair-1", "polymarket", "home", 50.0, 0.4)
        pm.add_fill("pair-2", "polymarket", "home", 50.0, 0.4)
        pm.close_match("pair-1")
        active = pm.get_active_positions()
        assert len(active) == 1
        assert active[0].pair_id == "pair-2"

    def test_all_positions_includes_closed(self):
        pm = PositionManager()
        pm.add_fill("pair-1", "polymarket", "home", 50.0, 0.4)
        pm.add_fill("pair-2", "polymarket", "home", 50.0, 0.4)
        pm.close_match("pair-1")
        assert len(pm.get_all_positions()) == 2

    def test_global_min_rebate_sum(self):
        """全局 min_rebate 之和"""
        pm = PositionManager(default_share=100.0)
        # match 1: basic two way — min rebate = 0.10 (home)
        pm.add_fill("pair-1", "polymarket", "home", 100.0, 0.4)
        pm.add_fill("pair-1", "orbitexch", "away", 50.0, 2.5)
        # match 2: negative — min rebate ~ -0.2667 (home)
        pm.add_fill("pair-2", "polymarket", "home", 30.0, 0.6)
        pm.add_fill("pair-2", "orbitexch", "away", 20.0, 2.0)
        # 但 pair-2 share 也是 100（manager 默认 share）
        # pair-2: poly home profit=30*0.4=12, loss=30*0.6=18
        #         orbit away profit=20*1=20, loss=20
        # home wins: 12 - 20 = -8 → -8/100 = -0.08
        # away wins: 20 - 18 = 2 → 2/100 = 0.02
        # min = -0.08
        # pair-1 min = 0.10 (from basic case, but with share=100)
        global_sum = pm.get_global_min_rebate_sum()
        assert global_sum == pytest.approx(0.10 + (-0.08), abs=1e-3)

    def test_refresh_position_clears_legs(self):
        pm = PositionManager()
        pm.add_fill("pair-1", "polymarket", "home", 50.0, 0.4, competition="EPL")
        pm.refresh_position("pair-1")
        pos = pm.get_position("pair-1")
        assert pos is not None
        assert len(pos.legs) == 0
        assert pos.competition == "EPL"  # 元信息保留

    def test_set_share_updates_all(self):
        pm = PositionManager(default_share=100.0)
        pm.add_fill("pair-1", "polymarket", "home", 50.0, 0.4)
        pm.add_fill("pair-2", "polymarket", "home", 50.0, 0.4)
        pm.set_share(200.0)
        for pos in pm.get_all_positions():
            assert pos.share == 200.0

    def test_clear(self):
        pm = PositionManager()
        pm.add_fill("pair-1", "polymarket", "home", 50.0, 0.4)
        pm.clear()
        assert len(pm.get_all_positions()) == 0

    def test_load_polymarket_positions(self):
        """从 Polymarket API 数据加载持仓"""
        case = load_case("load_polymarket_positions")
        pm = PositionManager(default_share=30.0)
        positions = [DummyPolymarketPosition(d) for d in case["positions"]]
        count = pm.load_polymarket_positions(
            positions=positions,
            pair_mapping=case["pair_mapping"],
        )
        assert count == case["expected_count"]
        pos = pm.get_position("pair-load-1")
        assert pos is not None
        assert len(pos.legs) == 2

    def test_load_polymarket_skips_unmapped(self):
        """未映射的 event_id 被跳过"""
        case = load_case("load_polymarket_positions")
        pm = PositionManager()
        positions = [DummyPolymarketPosition(d) for d in case["positions"]]
        pm.load_polymarket_positions(positions=positions, pair_mapping=case["pair_mapping"])
        # evt-unknown 不在 pair_mapping 中
        assert pm.get_position("evt-unknown") is None

    def test_load_polymarket_uses_override(self):
        """从 API 加载时 profit/loss override 生效"""
        case = load_case("load_polymarket_positions")
        pm = PositionManager()
        positions = [DummyPolymarketPosition(d) for d in case["positions"]]
        pm.load_polymarket_positions(positions=positions, pair_mapping=case["pair_mapping"])
        pos = pm.get_position("pair-load-1")
        home_leg = [l for l in pos.legs if l.market_type == "home"][0]
        assert home_leg.profit_if_wins() == pytest.approx(27.5)
        assert home_leg.loss_if_loses() == pytest.approx(22.5)

    def test_load_orbitexch_bets(self):
        """从 OrbitExch API 数据加载订单"""
        case = load_case("load_orbitexch_bets")
        pm = PositionManager(default_share=30.0)
        # selection_mappings 的 key 在 JSON 中是字符串，转为 int
        selection_mappings = {}
        for pair_id, mapping in case["selection_mappings"].items():
            selection_mappings[pair_id] = {int(k): v for k, v in mapping.items()}
        count = pm.load_orbitexch_bets(
            bets=case["bets"],
            pair_mapping=case["pair_mapping"],
            selection_mappings=selection_mappings,
        )
        assert count == case["expected_count"]
        pos = pm.get_position("pair-load-2")
        assert pos is not None
        assert len(pos.legs) == 2

    def test_load_orbitexch_back_profit_loss(self):
        """BACK 订单: profit=marketProfit, loss=marketLiability"""
        case = load_case("load_orbitexch_bets")
        pm = PositionManager()
        selection_mappings = {}
        for pair_id, mapping in case["selection_mappings"].items():
            selection_mappings[pair_id] = {int(k): v for k, v in mapping.items()}
        pm.load_orbitexch_bets(
            bets=case["bets"],
            pair_mapping=case["pair_mapping"],
            selection_mappings=selection_mappings,
        )
        pos = pm.get_position("pair-load-2")
        home_leg = [l for l in pos.legs if l.market_type == "home"][0]
        assert home_leg.profit_if_wins() == pytest.approx(37.5)
        assert home_leg.loss_if_loses() == pytest.approx(25.0)
        assert home_leg.price == pytest.approx(2.5)

    def test_load_orbitexch_bets_skips_missing_average_price(self, caplog):
        """缺失 averagePrice 的 OrbitExch bet 不应回退到 price。"""
        pm = PositionManager()
        selection_mappings = {"pair-load-2": {101: "home"}}

        with caplog.at_level("WARNING"):
            count = pm.load_orbitexch_bets(
                bets=[{
                    "marketId": "mkt-1",
                    "selectionId": 101,
                    "sizeMatched": 25.0,
                    "price": 9.99,
                    "side": "BACK",
                    "marketProfit": 37.5,
                    "marketLiability": 25.0,
                }],
                pair_mapping={"mkt-1": "pair-load-2"},
                selection_mappings=selection_mappings,
            )

        assert count == 0
        assert pm.get_position("pair-load-2") is None
        assert "missing averagePrice" in caplog.text

    def test_to_dict_structure(self):
        pm = PositionManager(default_share=100.0)
        pm.add_fill("pair-1", "polymarket", "home", 50.0, 0.4)
        d = pm.to_dict()
        assert "default_share" in d
        assert "positions" in d
        assert "global_min_rebate_sum" in d
        assert "active_count" in d
        assert "total_count" in d
        assert d["total_count"] == 1

    def test_way_rebate_nonexistent_pair(self):
        pm = PositionManager()
        assert pm.get_way_rebate("nope") == {}
        assert pm.get_min_way_rebate("nope") is None
