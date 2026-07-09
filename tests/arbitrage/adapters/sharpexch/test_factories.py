"""SharpExch factories 离线接线测试。

不注册 launcher、不启动真实 browser,只验证 factory 构造与 ArbContext 注入。
"""

import asyncio
from unittest.mock import ANY
from unittest.mock import MagicMock

import pytest

import src.arbitrage.bootstrap as bootstrap
from nautilus_trader.adapters.sharpexch import factories as se_factories
from nautilus_trader.adapters.sharpexch.config import SharpExchDataClientConfig
from nautilus_trader.adapters.sharpexch.config import SharpExchExecClientConfig
from nautilus_trader.adapters.sharpexch.data import SharpExchDataClient
from nautilus_trader.adapters.sharpexch.execution import SharpExchExecutionClient
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from src.arbitrage.common.params import ArbitrageParams
from src.arbitrage.common.venue_configs import SharpExchVenueConfig
from src.arbitrage.common.venue_configs import SportConfig
from src.arbitrage.common.venues import SHARPEXCH
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness


@pytest.fixture(autouse=True)
def _reset_ctx():
    bootstrap.reset_arb_context()
    yield
    bootstrap.reset_arb_context()


def _args(config):
    clock = LiveClock()
    return dict(
        loop=asyncio.new_event_loop(),
        name="SHARPEXCH",
        config=config,
        msgbus=MessageBus(trader_id=TraderId("TESTER-000"), clock=clock),
        cache=TestComponentStubs.cache(),
        clock=clock,
    )


def test_data_factory_uses_placeholder_provider_when_discovery_config_missing(monkeypatch):
    dc = MagicMock(name="se_dc")
    monkeypatch.setattr(se_factories, "PlaywrightBrowserManager", MagicMock())
    monkeypatch.setattr(se_factories, "SharpExchDataClient", dc)
    bootstrap.prepare_arb_context()

    se_factories.SharpExchLiveDataClientFactory.create(
        **_args(SharpExchDataClientConfig(username="u", password="p")),
    )

    assert dc.called
    provider = dc.call_args.kwargs["instrument_provider"]
    from nautilus_trader.adapters.sharpexch.providers import SharpExchInstrumentProvider

    assert not isinstance(provider, SharpExchInstrumentProvider)


def test_data_factory_constructs_provider_when_discovery_config_present(monkeypatch):
    dc = MagicMock(name="se_dc")
    bm = MagicMock(name="BM")
    discovery = MagicMock(name="Discovery")
    provider = MagicMock(name="Provider")
    monkeypatch.setattr(se_factories, "PlaywrightBrowserManager", bm)
    monkeypatch.setattr(se_factories, "SharpExchDataClient", dc)
    monkeypatch.setattr(se_factories, "SharpExchDiscoveryClient", discovery)
    monkeypatch.setattr(se_factories, "SharpExchInstrumentProvider", provider)
    se_venue = SharpExchVenueConfig(
        enabled=True,
        sports=[SportConfig(sport="Tennis", competitions=["Men's Wimbledon 2026"])],
    )
    bootstrap.prepare_arb_context(
        discovery_config_by_venue={SHARPEXCH: se_venue},
        sport_aliases_by_venue={SHARPEXCH: {"Tennis": "Tennis"}},
        competition_aliases_by_venue={SHARPEXCH: {"Men's Wimbledon 2026": "ATP"}},
        arbitrage_params=ArbitrageParams(fx=1.25),
    )
    cfg = SharpExchDataClientConfig(username="u", password="p")

    se_factories.SharpExchLiveDataClientFactory.create(**_args(cfg))

    discovery.assert_called_once_with(
        base_url=cfg.base_url,
        json_fetcher=ANY,
        target_competitions=["Men's Wimbledon 2026"],
    )
    provider.assert_called_once()
    _, kwargs = provider.call_args
    assert kwargs["discovery"] is discovery.return_value
    assert kwargs["sport_aliases"] == {"Tennis": "Tennis"}
    assert kwargs["competition_aliases"] == {"Men's Wimbledon 2026": "ATP"}
    assert kwargs["sport_configs"] == se_venue.sports
    assert kwargs["fx"] == 1.25
    assert bootstrap.get_arb_context().instrument_provider_by_venue[SHARPEXCH] is provider.return_value


def test_data_and_exec_factories_share_browser_manager(monkeypatch):
    created = []

    class BrowserManager:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(se_factories, "PlaywrightBrowserManager", BrowserManager)
    liveness = VenueExecutionLiveness()
    bootstrap.prepare_arb_context(
        venue_liveness=liveness,
        session_timeout_secs_by_venue={SHARPEXCH: 45.0},
    )
    data_cfg = SharpExchDataClientConfig(username="u", password="p")
    exec_cfg = SharpExchExecClientConfig(username="u", password="p")

    data_client = se_factories.SharpExchLiveDataClientFactory.create(**_args(data_cfg))
    exec_client = se_factories.ArbSharpExchLiveExecClientFactory.create(**_args(exec_cfg))

    assert isinstance(data_client, SharpExchDataClient)
    assert isinstance(exec_client, SharpExchExecutionClient)
    assert data_client._browser_manager is exec_client._browser_manager
    assert bootstrap.get_arb_context().browser_manager_by_venue[SHARPEXCH] is data_client._browser_manager
    assert bootstrap.get_arb_context().browser_lock_by_venue[SHARPEXCH] is exec_client._browser_lock
    assert bootstrap.get_arb_context().browser_login_state_by_venue[SHARPEXCH] is exec_client._login_state
    assert len(created) == 1


def test_exec_factory_raises_when_context_unset():
    with pytest.raises(RuntimeError, match="prepare_arb_context"):
        se_factories.ArbSharpExchLiveExecClientFactory.create(
            **_args(SharpExchExecClientConfig(username="u", password="p")),
        )


def test_exec_factory_requires_session_timeout_keyed_value():
    bootstrap.prepare_arb_context(venue_liveness=VenueExecutionLiveness())

    with pytest.raises(RuntimeError, match=r"session_timeout_secs_by_venue\['SHARPEXCH'\] is required"):
        se_factories.ArbSharpExchLiveExecClientFactory.create(
            **_args(SharpExchExecClientConfig(username="u", password="p")),
        )


def test_exec_factory_create_with_context_returns_client():
    liveness = VenueExecutionLiveness()
    bootstrap.prepare_arb_context(
        venue_liveness=liveness,
        session_timeout_secs_by_venue={SHARPEXCH: 45.0},
        arbitrage_params=ArbitrageParams(fx=1.25),
    )

    client = se_factories.ArbSharpExchLiveExecClientFactory.create(
        **_args(SharpExchExecClientConfig(username="u", password="p")),
    )

    assert isinstance(client, SharpExchExecutionClient)
    assert client._venue_liveness is liveness
    assert client._session_timeout_ns == int(45.0 * 1e9)
    assert client._current_fx() == 1.0
    assert client._browser_manager is bootstrap.get_arb_context().browser_manager_by_venue[SHARPEXCH]
