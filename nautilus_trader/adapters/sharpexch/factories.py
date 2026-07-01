"""SharpExch data/exec client factories.

第一阶段按显式 venue enable 注册进 launcher;第二阶段再抽象通用 venue registry。
"""

from __future__ import annotations

import asyncio

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.live.factories import LiveExecClientFactory

from nautilus_trader.adapters.sharpexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.sharpexch.config import SharpExchDataClientConfig
from nautilus_trader.adapters.sharpexch.config import SharpExchExecClientConfig
from nautilus_trader.adapters.sharpexch.data import SharpExchDataClient
from nautilus_trader.adapters.sharpexch.discovery_client import SharpExchDiscoveryClient
from nautilus_trader.adapters.sharpexch.discovery_client import SharpExchSportDetailsRequest
from nautilus_trader.adapters.sharpexch.execution import SharpExchExecutionClient
from nautilus_trader.adapters.sharpexch.providers import SharpExchInstrumentProvider
from nautilus_trader.adapters.sharpexch.web import se_customer_context
from nautilus_trader.adapters.sharpexch.web import se_fetch_json
from nautilus_trader.adapters.sharpexch.web import se_login

from src.arbitrage.bootstrap import get_arb_context


def _shared_se_browser_manager(ctx, config) -> PlaywrightBrowserManager:
    """SE 共享 BrowserManager:Data/Exec factory 复用同一实例。"""

    bm = getattr(ctx, "se_browser_manager", None)
    if bm is None:
        bm = PlaywrightBrowserManager(
            browser_type=config.browser_type,
            headless=config.headless,
            user_data_dir=config.user_data_dir,
        )
        ctx.se_browser_manager = bm
    return bm


def _shared_se_browser_lock(ctx) -> asyncio.Lock:
    """SE browser context 级操作锁。

    Data discovery 与 Exec login 共用同一 browser context;启动期 NT 会并发 connect
    data/exec clients。SE/Cloudflare 对同一 context 并发登录 + API fetch 敏感,因此把
    login/fetch 这种会改变 customer session 状态的操作串行化。
    """

    lock = getattr(ctx, "se_browser_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        ctx.se_browser_lock = lock
    return lock


def _se_browser_json_fetcher(browser_manager, config, browser_lock: asyncio.Lock):
    async def _fetch(request: SharpExchSportDetailsRequest) -> dict:
        await browser_manager.start()
        page = await browser_manager.create_page("se-discovery")
        page.set_default_timeout(config.page_timeout)
        async with browser_lock:
            await se_login(page, config)
            payload = await se_fetch_json(
                se_customer_context(page),
                request.url,
                params=request.params,
                body=request.body,
            )
        if not payload.get("ok") or not isinstance(payload.get("json"), dict):
            raise RuntimeError(
                f"SE sport/details failed: status={payload.get('status')} text={payload.get('text')!r}",
            )
        return payload["json"]

    return _fetch


class SharpExchLiveDataClientFactory(LiveDataClientFactory):
    """SharpExch data client factory."""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: SharpExchDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> SharpExchDataClient:
        ctx = get_arb_context()
        browser_manager = _shared_se_browser_manager(ctx, config)
        browser_lock = _shared_se_browser_lock(ctx)
        se_discovery_cfg = getattr(ctx, "se_discovery_config", None)
        if se_discovery_cfg is not None:
            sport_configs = list(getattr(se_discovery_cfg, "sports", []) or [])
            target_competitions = [
                comp
                for sport in sport_configs
                for comp in getattr(sport, "competitions", []) or []
            ]
            discovery = SharpExchDiscoveryClient(
                base_url=config.base_url,
                json_fetcher=_se_browser_json_fetcher(browser_manager, config, browser_lock),
                target_competitions=target_competitions,
            )
            provider = SharpExchInstrumentProvider(
                discovery=discovery,
                sport_aliases=dict(getattr(ctx, "se_sport_aliases", {})),
                competition_aliases=dict(getattr(ctx, "se_competition_aliases", {})),
                sport_configs=sport_configs,
                fx=getattr(ctx.arbitrage_params, "fx", 1.0) if ctx.arbitrage_params is not None else 1.0,
            )
        else:
            provider = InstrumentProvider()
        ctx.se_instrument_provider = provider
        return SharpExchDataClient(
            loop=loop,
            browser_manager=browser_manager,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
        )


class ArbSharpExchLiveExecClientFactory(LiveExecClientFactory):
    """SharpExch execution client factory."""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: SharpExchExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> SharpExchExecutionClient:
        ctx = get_arb_context()
        if ctx.venue_liveness is None:
            raise RuntimeError(
                "ArbContext.venue_liveness is None —— `prepare_arb_context(venue_liveness=...)` "
                "必须在 node.build() 之前调用",
            )
        browser_manager = _shared_se_browser_manager(ctx, config)
        browser_lock = _shared_se_browser_lock(ctx)
        provider = InstrumentProvider()
        return SharpExchExecutionClient(
            loop=loop,
            browser_manager=browser_manager,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
            venue_liveness=ctx.venue_liveness,
            pair_registry=ctx.pair_registry,
            pair_inflight=getattr(ctx, "pair_inflight", None),
            session_timeout_secs=ctx.se_session_timeout_secs,
            fx=getattr(ctx.arbitrage_params, "fx", 1.0) if ctx.arbitrage_params is not None else 1.0,
            browser_lock=browser_lock,
        )
