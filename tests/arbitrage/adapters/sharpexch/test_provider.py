"""SharpExch InstrumentProvider 测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nautilus_trader.adapters.sharpexch.discovery_client import SharpExchMarketEvent
from nautilus_trader.adapters.sharpexch.discovery_client import SharpExchRunner
from nautilus_trader.adapters.sharpexch.providers import SharpExchInstrumentProvider


def _event() -> SharpExchMarketEvent:
    return SharpExchMarketEvent(
        sport="Tennis",
        competition="Men's Wimbledon 2026",
        home_team="Rafael Jodar",
        away_team="Felix Gill",
        sport_id="2",
        competition_id="12597512",
        market_id="1.259502313",
        start_ts=1782768600000 * 1_000_000,
        runners=(
            SharpExchRunner(selection_id="111", runner_name="Rafael Jodar", role="home"),
            SharpExchRunner(selection_id="222", runner_name="Felix Gill", role="away"),
        ),
    )


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_build_legs_fills_matching_info_keys_and_venue():
    provider = SharpExchInstrumentProvider(SimpleNamespace())
    legs = list(provider._build_legs(_event()))
    assert len(legs) == 2
    assert [leg.info["selection_role"] for leg in legs] == ["home", "away"]
    assert [leg.info["claim"] for leg in legs] == ["yes", "no"]
    assert all("exec_instrument_id" not in leg.info for leg in legs)
    assert str(legs[0].id).endswith(".SHARPEXCH")
    assert legs[0].market_id == "1.259502313"
    assert "111" in str(legs[0].id)
    required = {"sport", "competition", "home_team", "away_team", "selection_role"}
    assert required <= set(legs[0].info.keys())
    assert legs[0].info["competition"] == "Men's Wimbledon 2026"


def test_build_legs_three_way_exposes_yes_and_no_legs():
    """#228:3-way(含 draw)每 selection 产 yes + 合成 no 两条腿;2-way 不受影响(见上)。"""
    event = SharpExchMarketEvent(
        sport="Soccer",
        competition="EPL",
        home_team="Arsenal",
        away_team="Chelsea",
        sport_id="1",
        competition_id="10932509",
        market_id="1.259502399",
        start_ts=1782768600000 * 1_000_000,
        runners=(
            SharpExchRunner(selection_id="111", runner_name="Arsenal", role="home"),
            SharpExchRunner(selection_id="333", runner_name="The Draw", role="draw"),
            SharpExchRunner(selection_id="222", runner_name="Chelsea", role="away"),
        ),
    )
    provider = SharpExchInstrumentProvider(SimpleNamespace())
    legs = list(provider._build_legs(event))
    assert [(leg.info["selection_role"], leg.info["claim"]) for leg in legs] == [
        ("home", "yes"), ("home", "no"),
        ("draw", "yes"), ("draw", "no"),
        ("away", "yes"), ("away", "no"),
    ]
    yes_home, no_home = legs[0], legs[1]
    assert no_home.market_id == yes_home.market_id
    assert no_home.selection_id == -(yes_home.selection_id + 1)
    assert no_home.info["venue_selection_id"] == yes_home.selection_id
    assert no_home.id != yes_home.id
    assert no_home.id.symbol.is_composite() is False
    assert no_home.info["quote_claim"] == "no"
    assert no_home.info["exec_instrument_id"] == str(yes_home.id)


def test_build_legs_sets_usd_min_stake():
    provider = SharpExchInstrumentProvider(SimpleNamespace(), fx=1.3)
    leg = next(iter(provider._build_legs(_event())))
    assert leg.min_notional.as_double() == pytest.approx(12.0)
    assert str(leg.min_notional.currency) == "USD"


def test_build_legs_applies_aliases():
    provider = SharpExchInstrumentProvider(
        SimpleNamespace(),
        sport_aliases={"Tennis": "Tennis Normalized"},
        competition_aliases={"Men's Wimbledon 2026": "ATP"},
    )
    leg = next(iter(provider._build_legs(_event())))
    assert leg.info["sport"] == "Tennis Normalized"
    assert leg.info["competition"] == "ATP"


def test_load_all_async_invokes_discovery_and_adds_instruments():
    discovery = SimpleNamespace()
    discovery.discover_events = AsyncMock(return_value=[_event()])
    provider = SharpExchInstrumentProvider(discovery)

    _run(provider.load_all_async())

    discovery.discover_events.assert_awaited_once_with(None)
    instruments = provider.get_all()
    assert len(instruments) == 2


def test_load_all_async_passes_configured_sports_to_discovery():
    discovery = SimpleNamespace()
    discovery.discover_events = AsyncMock(return_value=[_event()])
    sport_config = SimpleNamespace(sport="Tennis", competitions=["Men's Wimbledon 2026"])
    provider = SharpExchInstrumentProvider(discovery, sport_configs=[sport_config])

    _run(provider.load_all_async())

    discovery.discover_events.assert_awaited_once_with([sport_config])
