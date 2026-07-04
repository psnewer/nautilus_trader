"""Slice 7A:`OrbitExchLiveDataClientFactory.create` 真接 discovery + Provider 分支。

不构造真 NT DataClient(同 debug factory test 模式):monkeypatch 客户端类 + discovery /
Provider 类,验证 factory 按 ArbContext.discovery_config_by_venue 选生产 Provider 还是 InstrumentProvider 占位。

2026-07-03: 迁移到 `sport/details` API,与 SE 对齐。
"""

from unittest.mock import MagicMock

import pytest

import src.arbitrage.bootstrap as bootstrap
from nautilus_trader.adapters.orbitexch import factories as oe_factories
from src.arbitrage.common.params import ArbitrageParams
from src.arbitrage.common.venue_configs import OrbitExchVenueConfig
from src.arbitrage.common.venue_configs import SportConfig
from src.arbitrage.common.venues import ORBITEXCH


@pytest.fixture(autouse=True)
def _reset_ctx():
    bootstrap.reset_arb_context()
    yield
    bootstrap.reset_arb_context()


def _args():
    return dict(
        loop=MagicMock(),
        name="ORBITEXCH",
        config=MagicMock(),
        msgbus=MagicMock(),
        cache=MagicMock(),
        clock=MagicMock(),
    )


def _stub_heavy(monkeypatch, dc_class=None, discovery_class=None, prov_class=None):
    monkeypatch.setattr(oe_factories, "PlaywrightBrowserManager", MagicMock())
    monkeypatch.setattr(oe_factories, "OrbitExchDataClient", dc_class or MagicMock())
    # stub browser lock and fetcher
    monkeypatch.setattr(oe_factories, "_shared_oe_browser_lock", lambda ctx: MagicMock())
    monkeypatch.setattr(oe_factories, "_oe_browser_json_fetcher", lambda *args: MagicMock())
    if discovery_class is not None:
        monkeypatch.setattr(oe_factories, "OrbitExchDiscoveryClient", discovery_class)
    if prov_class is not None:
        monkeypatch.setattr(oe_factories, "OrbitExchInstrumentProvider", prov_class)


def test_factory_uses_placeholder_provider_when_scraper_config_missing(monkeypatch):
    """discovery_config_by_venue 缺 ORBITEXCH → 走占位 InstrumentProvider 分支。"""
    dc = MagicMock(name="oe_dc")
    _stub_heavy(monkeypatch, dc_class=dc)
    bootstrap.prepare_arb_context()

    oe_factories.OrbitExchLiveDataClientFactory.create(**_args())

    # OrbitExchInstrumentProvider 没被构造(provider 是占位 InstrumentProvider)
    assert dc.called
    provider_passed = dc.call_args.kwargs["instrument_provider"]
    # 默认占位是 `InstrumentProvider()`,非 OrbitExchInstrumentProvider
    from nautilus_trader.adapters.orbitexch.providers import OrbitExchInstrumentProvider
    assert not isinstance(provider_passed, OrbitExchInstrumentProvider)


def test_factory_constructs_real_provider_when_scraper_config_present(monkeypatch):
    """discovery_config_by_venue 存在 ORBITEXCH → 装真 OrbitExchInstrumentProvider。"""
    dc = MagicMock(name="oe_dc")
    discovery_class = MagicMock(name="OrbitExchDiscoveryClientStub")
    prov_class = MagicMock(name="OrbitExchInstrumentProviderStub")
    _stub_heavy(monkeypatch, dc_class=dc, discovery_class=discovery_class, prov_class=prov_class)

    oe_venue = OrbitExchVenueConfig(
        enabled=True,
        sports=[SportConfig(sport="Tennis", competitions=["Men's Roland Garros 2026"])],
    )
    bootstrap.prepare_arb_context(
        discovery_config_by_venue={ORBITEXCH: oe_venue},
        sport_aliases_by_venue={ORBITEXCH: {"Tennis": "Tennis"}},
        competition_aliases_by_venue={ORBITEXCH: {"Men's Roland Garros 2026": "ATP"}},
        arbitrage_params=ArbitrageParams(fx=1.25),
    )

    oe_factories.OrbitExchLiveDataClientFactory.create(**_args())

    # discovery 类被构造(base_url + json_fetcher + target_competitions)
    discovery_class.assert_called_once()
    _, discovery_kwargs = discovery_class.call_args
    assert "base_url" in discovery_kwargs
    assert "json_fetcher" in discovery_kwargs
    assert discovery_kwargs["target_competitions"] == ["Men's Roland Garros 2026"]

    # 真 Provider 类被构造(discovery + aliases 传入)
    prov_class.assert_called_once()
    _, prov_kwargs = prov_class.call_args
    assert prov_kwargs["sport_aliases"] == {"Tennis": "Tennis"}
    assert prov_kwargs["competition_aliases"] == {"Men's Roland Garros 2026": "ATP"}
    assert prov_kwargs["fx"] == 1.25


def test_factory_uses_keyed_venue_context(monkeypatch):
    """Venue registry 第二阶段:OE data factory 只从 keyed map 读 discovery / aliases。"""
    dc = MagicMock(name="oe_dc")
    discovery_class = MagicMock(name="OrbitExchDiscoveryClientStub")
    prov_class = MagicMock(name="OrbitExchInstrumentProviderStub")
    _stub_heavy(monkeypatch, dc_class=dc, discovery_class=discovery_class, prov_class=prov_class)

    oe_venue = OrbitExchVenueConfig(
        enabled=True,
        sports=[SportConfig(sport="Tennis", competitions=["Men's Wimbledon 2026"])],
    )
    bootstrap.prepare_arb_context(
        discovery_config_by_venue={ORBITEXCH: oe_venue},
        sport_aliases_by_venue={ORBITEXCH: {"Tennis": "Tennis"}},
        competition_aliases_by_venue={ORBITEXCH: {"Men's Wimbledon 2026": "ATP"}},
        arbitrage_params=ArbitrageParams(fx=1.25),
    )

    oe_factories.OrbitExchLiveDataClientFactory.create(**_args())

    discovery_class.assert_called_once()
    _, prov_kwargs = prov_class.call_args
    assert prov_kwargs["sport_aliases"] == {"Tennis": "Tennis"}
    assert prov_kwargs["competition_aliases"] == {"Men's Wimbledon 2026": "ATP"}
    assert bootstrap.get_arb_context().instrument_provider_by_venue[ORBITEXCH] is prov_class.return_value
