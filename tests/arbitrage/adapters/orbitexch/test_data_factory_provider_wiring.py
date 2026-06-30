"""Slice 7A:`OrbitExchLiveDataClientFactory.create` 真接 scraper + Provider 分支。

不构造真 NT DataClient(同 debug factory test 模式):monkeypatch 客户端类 + scraper /
Provider 类,验证 factory 按 ArbContext.oe_scraper_config 选生产 Provider 还是 InstrumentProvider 占位。
"""

from unittest.mock import MagicMock

import pytest

import src.arbitrage.bootstrap as bootstrap
from nautilus_trader.adapters.orbitexch import factories as oe_factories
from src.arbitrage.common.params import ArbitrageParams
from src.arbitrage.common.venue_configs import OrbitExchVenueConfig
from src.arbitrage.common.venue_configs import SportConfig


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


def _stub_heavy(monkeypatch, dc_class=None, scraper_class=None, prov_class=None):
    monkeypatch.setattr(oe_factories, "PlaywrightBrowserManager", MagicMock())
    monkeypatch.setattr(oe_factories, "OrbitExchDataClient", dc_class or MagicMock())
    if scraper_class is not None:
        import nautilus_trader.adapters.orbitexch.discovery_scraper as ds
        monkeypatch.setattr(ds, "OrbitExchScraper", scraper_class)
    if prov_class is not None:
        import nautilus_trader.adapters.orbitexch.providers as pv
        monkeypatch.setattr(pv, "OrbitExchInstrumentProvider", prov_class)


def test_factory_uses_placeholder_provider_when_scraper_config_missing(monkeypatch):
    """ArbContext.oe_scraper_config=None(默认)→ 走占位 InstrumentProvider 分支。"""
    dc = MagicMock(name="oe_dc")
    _stub_heavy(monkeypatch, dc_class=dc)
    bootstrap.prepare_arb_context()  # 默认 oe_scraper_config=None

    oe_factories.OrbitExchLiveDataClientFactory.create(**_args())

    # OrbitExchInstrumentProvider 没被构造(provider 是占位 InstrumentProvider)
    assert dc.called
    provider_passed = dc.call_args.kwargs["instrument_provider"]
    # 默认占位是 `InstrumentProvider()`,非 OrbitExchInstrumentProvider
    from nautilus_trader.adapters.orbitexch.providers import OrbitExchInstrumentProvider
    assert not isinstance(provider_passed, OrbitExchInstrumentProvider)


def test_factory_constructs_real_provider_when_scraper_config_present(monkeypatch):
    """ArbContext.oe_scraper_config 存在 → 装真 OrbitExchInstrumentProvider(scraper, aliases)。"""
    dc = MagicMock(name="oe_dc")
    scraper_class = MagicMock(name="OrbitExchScraperStub")
    prov_class = MagicMock(name="OrbitExchInstrumentProviderStub")
    _stub_heavy(monkeypatch, dc_class=dc, scraper_class=scraper_class, prov_class=prov_class)

    oe_venue = OrbitExchVenueConfig(
        enabled=True,
        sports=[SportConfig(sport="Tennis", competitions=["Men's Roland Garros 2026"])],
    )
    bootstrap.prepare_arb_context(
        oe_scraper_config=oe_venue,
        oe_sport_aliases={"Tennis": "Tennis"},
        oe_competition_aliases={"Men's Roland Garros 2026": "ATP"},
        arbitrage_params=ArbitrageParams(fx=1.25),
    )

    oe_factories.OrbitExchLiveDataClientFactory.create(**_args())

    # 真 Provider 类被构造(scraper + aliases 传入)
    scraper_class.assert_called_once_with(config=oe_venue)
    prov_class.assert_called_once()
    _, prov_kwargs = prov_class.call_args
    assert prov_kwargs["sport_aliases"] == {"Tennis": "Tennis"}
    assert prov_kwargs["competition_aliases"] == {"Men's Roland Garros 2026": "ATP"}
    assert prov_kwargs["fx"] == 1.25
