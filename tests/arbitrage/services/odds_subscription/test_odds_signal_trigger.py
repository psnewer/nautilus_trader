import asyncio
import json
from pathlib import Path

import pytest

from nautilus_trader.common.component import MessageBus, TestClock
from nautilus_trader.model.identifiers import TraderId
from src.arbitrage.services.market_matching.service import MatchedPair
from src.arbitrage.services.odds_subscription.service import OddsSubscriptionService
from src.arbitrage.services.strategy.config import StrategyServiceConfig
from src.arbitrage.services.strategy.service import StrategyService


def _build_pair(pair_id: str, event_id: str) -> MatchedPair:
    return MatchedPair(
        pair_id=pair_id,
        sport="football",
        competition="Test League",
        polymarket_home_team="Team A",
        polymarket_away_team="Team B",
        polymarket_event_id=event_id,
        polymarket_data={"event_id": event_id},
        orbitexch_home_team="Team A",
        orbitexch_away_team="Team B",
        orbitexch_data={
            "market_id": "market-1",
            "home_selection_id": "111",
            "away_selection_id": "222",
        },
        home_similarity=10,
        away_similarity=10,
        total_similarity=20,
        confidence=1.0,
    )


def _setup_services(register_match: bool, config: StrategyServiceConfig | None = None):
    msgbus = MessageBus(trader_id=TraderId("TEST-001"), clock=TestClock(), name="TestMsgBus")
    odds_service = OddsSubscriptionService(msgbus=msgbus)
    strategy_service = StrategyService(config=config or StrategyServiceConfig(), msgbus=msgbus)

    pair_id = "pair-1"
    event_id = "event-1"
    pair = _build_pair(pair_id, event_id)
    odds_service._subscribed_pairs[pair_id] = pair

    if register_match:
        strategy_service.register_match(
            pair_id=pair_id,
            competition=pair.competition,
            home_team=pair.polymarket_home_team,
            away_team=pair.polymarket_away_team,
            is_live=False,
        )

    return odds_service, strategy_service, pair, msgbus


def _load_case(case_id: str) -> dict:
    data_path = Path(__file__).with_name("fixtures").joinpath("odds_cases.json")
    with data_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data[case_id]


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def test_odds_update_triggers_signal_calc():
    odds_service, strategy_service, pair, _ = _setup_services(register_match=True)

    case = _load_case("case_1")
    for payload in case["polymarket"]:
        odds_service._on_polymarket_update(payload)

    signals = strategy_service.get_signals(pair.pair_id)["signals"]
    assert "rebate" in signals

    signals = strategy_service.get_signals(pair.pair_id)["signals"]
    assert signals["rebate"]["signal_name"] == "rebate"


def test_odds_update_triggers_signal_calc_multi_platform():
    odds_service, strategy_service, pair, _ = _setup_services(register_match=True)

    case = _load_case("case_2")
    for payload in case["polymarket"]:
        odds_service._on_polymarket_update(payload)
    for payload in case["orbitexch"]:
        odds_service._on_orbitexch_update(payload)

    signals = strategy_service.get_signals(pair.pair_id)["signals"]
    assert "rebate" in signals


def test_duplicate_strategy_subscription_not_triggered_twice():
    odds_service, strategy_service, pair, msgbus = _setup_services(register_match=True)
    counter = {"count": 0}

    original = strategy_service.on_odds_update

    def counted_on_odds_update(pair_id, venue, odds_data):
        counter["count"] += 1
        return original(pair_id, venue, odds_data)

    strategy_service.on_odds_update = counted_on_odds_update
    strategy_service.set_msgbus(msgbus)
    strategy_service.set_msgbus(msgbus)

    case = _load_case("case_3")
    for payload in case["polymarket"]:
        odds_service._on_polymarket_update(payload)

    assert counter["count"] == 1


def test_unregistered_match_does_not_trigger_signal():
    odds_service, strategy_service, pair, _ = _setup_services(register_match=False)

    case = _load_case("case_4")
    for payload in case["polymarket"]:
        odds_service._on_polymarket_update(payload)
    for payload in case["orbitexch"]:
        odds_service._on_orbitexch_update(payload)

    signals = strategy_service.get_signals(pair.pair_id)["signals"]
    assert signals == {}


def test_unconfigured_signal_not_triggered():
    config = StrategyServiceConfig.from_dict(
        {
            "default_strategies": ["mean_only"],
            "strategies": {
                "mean_only": {
                    "signals": ["mean_rebate"],
                },
            },
        }
    )
    odds_service, strategy_service, pair, _ = _setup_services(register_match=True, config=config)

    case = _load_case("case_2")
    for payload in case["polymarket"]:
        odds_service._on_polymarket_update(payload)
    for payload in case["orbitexch"]:
        odds_service._on_orbitexch_update(payload)

    signals = strategy_service.get_signals(pair.pair_id)["signals"]
    assert "mean_rebate" in signals
    assert "rebate" not in signals


def test_triggered_signals_only_and_cache_reuse():
    config = StrategyServiceConfig.from_dict(
        {
            "default_strategies": ["rebate_live"],
            "strategies": {
                "rebate_live": {
                    "signals": ["live", "rebate"],
                },
            },
        }
    )
    odds_service, strategy_service, pair, _ = _setup_services(register_match=True, config=config)
    counts = {"rebate": 0, "live": 0}

    original = strategy_service._calculate_signal

    def counted_calculate_signal(pair_id, signal_name, context):
        if signal_name in counts:
            counts[signal_name] += 1
        return original(pair_id, signal_name, context)

    strategy_service._calculate_signal = counted_calculate_signal

    # 先触发状态更新，仅计算 live
    odds_service._on_match_status_update(pair.pair_id, True)
    signals = strategy_service.get_signals(pair.pair_id)["signals"]
    assert "live" in signals
    assert counts["live"] == 1
    assert counts["rebate"] == 1

    # 再触发赔率更新，仅计算 rebate，live 复用缓存
    case = _load_case("case_1")
    for payload in case["polymarket"]:
        odds_service._on_polymarket_update(payload)

    signals = strategy_service.get_signals(pair.pair_id)["signals"]
    assert "rebate" in signals
    assert counts["rebate"] == 2
    assert counts["live"] == 1
