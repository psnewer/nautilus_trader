"""
Risk service test fixtures
"""

import json
from pathlib import Path

import pytest

from src.arbitrage.services.risk.config import RiskConfig
from src.arbitrage.services.risk.position import PositionLeg, MatchPosition, PositionManager
from src.arbitrage.services.risk.service import RiskService


FIXTURES_DIR = Path(__file__).with_name("fixtures")


def load_case(case_id: str) -> dict:
    """从 fixtures 加载测试用例"""
    data_path = FIXTURES_DIR / "risk_cases.json"
    with data_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data[case_id]


def build_legs(legs_data: list[dict]) -> list[PositionLeg]:
    """从 JSON 数据构建 PositionLeg 列表"""
    return [
        PositionLeg(
            venue=d["venue"],
            market_type=d["market_type"],
            size=d["size"],
            price=d["price"],
            profit_override=d.get("profit_override"),
            loss_override=d.get("loss_override"),
        )
        for d in legs_data
    ]


def build_position(case: dict) -> MatchPosition:
    """从测试用例构建 MatchPosition"""
    pos = MatchPosition(
        pair_id=case["pair_id"],
        share=case.get("share", 100.0),
    )
    for leg in build_legs(case["legs"]):
        pos.add_leg(leg)
    return pos


def build_service(case: dict) -> RiskService:
    """从测试用例构建 RiskService 并加载持仓"""
    config = RiskConfig.from_dict(case["config"])
    service = RiskService(config=config, share=case.get("share", 100.0))
    return service


class DummyPolymarketPosition:
    """模拟 Polymarket 持仓对象"""

    def __init__(self, data: dict):
        self.event_id = data["event_id"]
        self.market_type = data["market_type"]
        self.size = data["size"]
        self.avg_price = data["avg_price"]
        self.profit_if_win = data.get("profit_if_win")
        self.loss_if_lose = data.get("loss_if_lose")


class DummyOddsService:
    """模拟 OddsService"""

    def __init__(
        self,
        polymarket_positions: list | None = None,
        orbitexch_bets: list[dict] | None = None,
        mappings: dict | None = None,
    ):
        self._polymarket_positions = polymarket_positions or []
        self._orbitexch_bets = orbitexch_bets or []
        default_mappings = {
            "polymarket_pair_mapping": {},
            "orbitexch_pair_mapping": {},
            "selection_mappings": {},
        }
        mappings = mappings or default_mappings
        # JSON 的 selection_mappings key 是字符串，实际 OddsService 返回 int key
        if "selection_mappings" in mappings:
            converted = {}
            for pair_id, mapping in mappings["selection_mappings"].items():
                converted[pair_id] = {int(k): v for k, v in mapping.items()}
            mappings["selection_mappings"] = converted
        self._mappings = mappings

    def get_polymarket_positions(self, pair_id: str | None = None) -> list:
        return self._polymarket_positions

    def get_orbitexch_bets(self, pair_id: str | None = None) -> list[dict]:
        return self._orbitexch_bets

    def get_position_mappings(self) -> dict:
        return self._mappings
