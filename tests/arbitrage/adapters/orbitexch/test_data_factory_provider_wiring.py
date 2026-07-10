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
    monkeypatch.setattr(oe_factories, "_oe_browser_json_fetcher", lambda *args: MagicMock())
    if discovery_class is not None:
        monkeypatch.setattr(oe_factories, "OrbitExchDiscoveryClient", discovery_class)
    if prov_class is not None:
        monkeypatch.setattr(oe_factories, "OrbitExchInstrumentProvider", prov_class)


def test_browser_json_fetcher_waits_for_context_csrf_without_page_or_login(monkeypatch):
    """与 SE 对齐:discovery fetcher 不开页、不登录、无锁,只等共享 context CSRF 后走 context request。"""
    import asyncio

    from nautilus_trader.adapters.orbitexch.discovery_client import sport_details_request

    calls = []

    class BrowserManager:
        context = object()

        async def start(self):
            calls.append(("start",))

        async def create_page(self, name):
            raise AssertionError("OE discovery must not open a page")

    async def wait_for_csrf(context, *, timeout_ms):
        calls.append(("wait_for_csrf", context is BrowserManager.context, timeout_ms))

    async def fetch_json(context, url, *, params, body, timeout_ms):
        calls.append(("fetch_json", context is BrowserManager.context, url, params, body, timeout_ms))
        return {"ok": True, "json": {"marketCatalogueList": {"content": []}}}

    def forbidden_login(*args, **kwargs):
        raise AssertionError("OE discovery must not login")

    monkeypatch.setattr(oe_factories, "oe_wait_for_context_csrf_token", wait_for_csrf)
    monkeypatch.setattr(oe_factories, "oe_fetch_json_with_browser_context", fetch_json)
    monkeypatch.setattr(oe_factories, "oe_login", forbidden_login, raising=False)
    cfg = type("Cfg", (), {"base_url": "https://www.orbitexch.com", "page_timeout": 120000})()
    fetcher = oe_factories._oe_browser_json_fetcher(BrowserManager(), cfg)
    request = sport_details_request(cfg.base_url, SportConfig(sport="Tennis", competitions=[]))

    result = asyncio.run(fetcher(request))

    assert result == {"marketCatalogueList": {"content": []}}
    assert calls[0] == ("start",)
    assert calls[1] == ("wait_for_csrf", True, 120000)
    assert calls[2][0:3] == ("fetch_json", True, "https://www.orbitexch.com/customer/api/sport/details")
    assert calls[2][5] == 120000


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
