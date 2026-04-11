import asyncio
import json
from pathlib import Path

import pytest

from src.arbitrage.services.strategy.config import MatchConfig, StrategyServiceConfig
from src.arbitrage.services.strategy.service import StrategyService


class DummyRiskService:
    def __init__(
        self,
        way_rebate_by_venue: dict[str, dict[str, float]] | None = None,
        way_rebate_combined: dict[str, float] | None = None,
    ):
        self._way_rebate_by_venue = way_rebate_by_venue or {}
        self._way_rebate_combined = way_rebate_combined or {}

    def get_way_rebate_by_venue(self, pair_id: str) -> dict[str, dict[str, float]]:
        return self._way_rebate_by_venue

    def get_way_rebate(self, pair_id: str) -> dict[str, float]:
        return self._way_rebate_combined

    def check_risk(self, pair_id: str):
        class Result:
            allowed = True
        return Result()

    def check_balance(self, pair_id: str, share: float, direction):
        class Result:
            allowed = True
            reason = ""
        return Result()


def _load_case(case_id: str) -> dict:
    data_path = Path(__file__).with_name("fixtures").joinpath("strategy_cases.json")
    with data_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data[case_id]


def _inject_way_rebate(service: StrategyService, pair_id: str, case: dict) -> None:
    """将 way_rebate 数据注入 service 的内部缓存（模拟 MessageBus 推送）"""
    risk = case.get("risk", {})
    if "way_rebate_combined" in risk:
        service._way_rebate_cache[pair_id] = risk["way_rebate_combined"]
    if "way_rebate_by_venue" in risk:
        service._way_rebate_by_venue_cache[pair_id] = risk["way_rebate_by_venue"]


def _apply_odds(strategy_service: StrategyService, case: dict) -> None:
    for payload in case["odds"]["polymarket"]:
        strategy_service.on_odds_update(case["pair"]["pair_id"], "polymarket", payload)
    for payload in case["odds"]["orbitexch"]:
        strategy_service.on_odds_update(case["pair"]["pair_id"], "orbitexch", payload)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def test_single_platform_update_triggers_rebate_signal():
    case = _load_case("case_1")
    config = StrategyServiceConfig()
    service = StrategyService(config=config)
    service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )

    _apply_odds(service, case)

    signals = service.get_signals(case["pair"]["pair_id"])["signals"]
    assert "rebate" in signals


def test_multi_platform_update_triggers_strategy_and_opportunity():
    case = _load_case("case_2")
    config = StrategyServiceConfig()
    service = StrategyService(config=config)
    service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )

    _apply_odds(service, case)

    strategy_results = service.get_strategy_results(case["pair"]["pair_id"])["strategies"]
    assert strategy_results.get("default") is True
    assert len(service.get_opportunities(limit=10)) >= 1


def test_strategy_selects_best_direction():
    case = _load_case("case_2")
    service = StrategyService(config=StrategyServiceConfig())
    service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )

    _apply_odds(service, case)

    best = service.get_arbitrage_directions(case["pair"]["pair_id"])["best_direction"]
    assert best is not None


def test_pre_match_and_live_signals():
    case = _load_case("case_3")
    config = StrategyServiceConfig()
    config.add_strategy("pre_only", ["pre-match"])
    config.add_strategy("live_only", ["live"])
    config.default_strategies = ["pre_only", "live_only"]
    service = StrategyService(config=config)
    service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )

    _apply_odds(service, case)
    signals = service.get_signals(case["pair"]["pair_id"])["signals"]
    assert signals["pre-match"]["satisfied"] is True
    assert signals["live"]["satisfied"] is False

    service.update_match_status(case["pair"]["pair_id"], True)
    _apply_odds(service, case)
    signals = service.get_signals(case["pair"]["pair_id"])["signals"]
    assert signals["pre-match"]["satisfied"] is False
    assert signals["live"]["satisfied"] is True


def test_multi_way_filters_directions():
    case = _load_case("case_4")
    config = StrategyServiceConfig()
    config.add_signal("multi-way", params={"strict": True})
    config.add_strategy("rebate_multi", ["rebate", "multi-way"])
    config.default_strategies = ["rebate_multi"]
    risk_service = DummyRiskService(
        way_rebate_by_venue=case["risk"]["way_rebate_by_venue"],
        way_rebate_combined=case["risk"]["way_rebate_combined"],
    )
    service = StrategyService(config=config, risk_service=risk_service)
    service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )
    _inject_way_rebate(service, case["pair"]["pair_id"], case)

    _apply_odds(service, case)
    signals = service.get_signals(case["pair"]["pair_id"])["signals"]
    multi = signals["multi-way"]
    assert multi["details"]["removed_count"] > 0
    assert multi["details"]["remaining_count"] <= multi["details"]["original_count"]


def test_match_config_override_applies():
    case = _load_case("case_5")
    config = StrategyServiceConfig()
    config.match_config = [
        MatchConfig(
            competition=case["pair"]["competition"],
            home=case["pair"]["home_team"],
            away=case["pair"]["away_team"],
            signal_overrides=case["match_config"]["signal_overrides"],
        )
    ]
    service = StrategyService(config=config)
    service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )

    _apply_odds(service, case)
    signals = service.get_signals(case["pair"]["pair_id"])["signals"]
    assert signals["rebate"]["details"]["rate_threshold"] == 0.1


def test_unregistered_match_does_not_trigger():
    case = _load_case("case_6")
    service = StrategyService(config=StrategyServiceConfig())
    _apply_odds(service, case)
    signals = service.get_signals(case["pair"]["pair_id"])["signals"]
    assert signals == {}


def test_mean_rebate_signal():
    case = _load_case("case_7")
    config = StrategyServiceConfig()
    config.default_strategies = ["mean"]
    service = StrategyService(config=config)
    service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )

    _apply_odds(service, case)
    signals = service.get_signals(case["pair"]["pair_id"])["signals"]
    assert signals["mean_rebate"]["value"] is not None


def test_mean_rebate_strict_filter_blocks_positive_leg_direction():
    case = _load_case("case_7")
    config = StrategyServiceConfig()
    config.default_strategies = ["mean"]
    config.strategies["mean"].strict_filter = True
    risk_service = DummyRiskService(
        way_rebate_by_venue={
            "polymarket": {"home": 0.01, "away": 0.02},
            "orbitexch": {"home": 0.01, "away": -0.03},
        },
        way_rebate_combined={"home": 0.01, "away": -0.03},
    )
    service = StrategyService(config=config, risk_service=risk_service)
    service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )

    _apply_odds(service, case)

    strategy_results = service.get_strategy_results(case["pair"]["pair_id"])["strategies"]
    directions = service.get_arbitrage_directions(case["pair"]["pair_id"])
    assert strategy_results.get("mean") is False
    assert directions["best_direction"] is None
    assert directions["directions"] == []
    assert service.get_opportunities(limit=10) == []


def test_platform_negative_rebate_blocks_same_venue_positive_leg():
    case = _load_case("case_7")
    config = StrategyServiceConfig()
    config.default_strategies = ["mean"]
    risk_service = DummyRiskService(
        way_rebate_by_venue={
            "polymarket": {"home": 0.01, "away": -0.02},
            "orbitexch": {"home": 0.01, "away": 0.02},
        },
        way_rebate_combined={"home": 0.01, "away": -0.02},
    )
    service = StrategyService(config=config, risk_service=risk_service)
    service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )

    _apply_odds(service, case)

    best = service.get_arbitrage_directions(case["pair"]["pair_id"])["best_direction"]
    assert best is not None
    assert all(
        not (leg["venue"] == "polymarket" and leg["market_type"] == "home")
        for leg in best["legs"]
    )


def test_rebate_threshold_override():
    case = _load_case("case_9")

    high_config = StrategyServiceConfig()
    high_config.match_config = [
        MatchConfig(
            competition=case["pair"]["competition"],
            home=case["pair"]["home_team"],
            away=case["pair"]["away_team"],
            signal_overrides=case["match_config"]["signal_overrides_high"],
        )
    ]
    high_service = StrategyService(config=high_config)
    high_service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )
    _apply_odds(high_service, case)
    high_signals = high_service.get_signals(case["pair"]["pair_id"])["signals"]
    assert high_signals["rebate"]["satisfied"] is False

    low_config = StrategyServiceConfig()
    low_config.match_config = [
        MatchConfig(
            competition=case["pair"]["competition"],
            home=case["pair"]["home_team"],
            away=case["pair"]["away_team"],
            signal_overrides=case["match_config"]["signal_overrides_low"],
        )
    ]
    low_service = StrategyService(config=low_config)
    low_service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )
    _apply_odds(low_service, case)
    low_signals = low_service.get_signals(case["pair"]["pair_id"])["signals"]
    assert low_signals["rebate"]["satisfied"] is True


def test_three_way_market_rebate_signal():
    case = _load_case("case_10")
    service = StrategyService(config=StrategyServiceConfig())
    service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )

    _apply_odds(service, case)
    signals = service.get_signals(case["pair"]["pair_id"])["signals"]
    assert "error" not in signals["rebate"]["details"]


def test_priority_negative_way_rebate_selected():
    case = _load_case("case_11")
    risk_service = DummyRiskService(
        way_rebate_combined=case["risk"]["way_rebate_combined"],
        way_rebate_by_venue=case["risk"]["way_rebate_by_venue"],
    )
    service = StrategyService(config=StrategyServiceConfig(), risk_service=risk_service)
    service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )
    _inject_way_rebate(service, case["pair"]["pair_id"], case)

    _apply_odds(service, case)
    best = service.get_arbitrage_directions(case["pair"]["pair_id"])["best_direction"]
    assert best["rebate_market"] == "home"
    assert best["rebate_venue"] == "polymarket"


def test_priority_min_positive_way_rebate_selected():
    case = _load_case("case_12")
    risk_service = DummyRiskService(way_rebate_combined=case["risk"]["way_rebate_combined"])
    service = StrategyService(config=StrategyServiceConfig(), risk_service=risk_service)
    service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )
    _inject_way_rebate(service, case["pair"]["pair_id"], case)

    _apply_odds(service, case)
    best = service.get_arbitrage_directions(case["pair"]["pair_id"])["best_direction"]
    assert best["rebate_market"] == "home"


def test_priority_rebate_rate_selected_without_way_rebate():
    case = _load_case("case_13")
    risk_service = DummyRiskService(way_rebate_combined=case["risk"]["way_rebate_combined"])
    service = StrategyService(config=StrategyServiceConfig(), risk_service=risk_service)
    service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )

    _apply_odds(service, case)
    best = service.get_arbitrage_directions(case["pair"]["pair_id"])["best_direction"]
    assert best["rebate_rate"] >= max(d["rebate_rate"] for d in service.get_arbitrage_directions(case["pair"]["pair_id"])["directions"])


def test_missing_other_platform_prices_still_keep_directions():
    case = _load_case("case_14")
    service = StrategyService(config=StrategyServiceConfig())
    service.register_match(
        pair_id=case["pair"]["pair_id"],
        competition=case["pair"]["competition"],
        home_team=case["pair"]["home_team"],
        away_team=case["pair"]["away_team"],
        is_live=False,
    )

    _apply_odds(service, case)
    directions = service.get_arbitrage_directions(case["pair"]["pair_id"])["directions"]
    assert directions
