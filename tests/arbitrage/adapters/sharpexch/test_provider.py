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


def test_build_legs_fills_q9_keys_and_venue():
    provider = SharpExchInstrumentProvider(SimpleNamespace())
    legs = list(provider._build_legs(_event()))
    assert len(legs) == 2
    assert [leg.info["selection_role"] for leg in legs] == ["home", "away"]
    assert str(legs[0].id).endswith(".SHARPEXCH")
    assert legs[0].market_id == "1.259502313"
    assert "111" in str(legs[0].id)
    required = {"sport", "competition", "home_team", "away_team", "start_ts", "selection_role"}
    assert required <= set(legs[0].info.keys())
    assert legs[0].info["competition"] == "Men's Wimbledon 2026"
    assert legs[0].info["start_ts"] == 1782768600000 * 1_000_000


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
