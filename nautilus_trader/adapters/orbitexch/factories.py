"""
Arb OE 执行客户端 factory —— 给 `TradingNode.add_exec_client_factory(...)` 用。

构造同目录 `execution.py:OrbitExchExecutionClient`。NT 的 `LiveExecClientFactory.create` 签名
固定,套利额外依赖(venue_liveness / 间隔)经 `src.arbitrage.bootstrap.ArbContext` 注入。

**位置(#33 校准)**:OE 适配器全套住 `nautilus_trader/adapters/orbitexch/`(P9 唯一例外);
本文件 `factories.py` 与上游无冲突(OE 无上游)。

2026-07-03: 迁移到 `sport/details` API,与 SE 对齐,替代原有 DOM 抓取方式。
"""

from __future__ import annotations

import asyncio

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.live.factories import LiveExecClientFactory

from nautilus_trader.adapters.orbitexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.orbitexch.config import OrbitExchDataClientConfig
from nautilus_trader.adapters.orbitexch.config import OrbitExchExecClientConfig
from nautilus_trader.adapters.orbitexch.data import OrbitExchDataClient
from nautilus_trader.adapters.orbitexch.discovery_client import OrbitExchDiscoveryClient
from nautilus_trader.adapters.orbitexch.discovery_client import OrbitExchSportDetailsRequest
from nautilus_trader.adapters.orbitexch.execution import OrbitExchExecutionClient
from nautilus_trader.adapters.orbitexch.providers import OrbitExchInstrumentProvider
from nautilus_trader.adapters.orbitexch.web import oe_fetch_json
from nautilus_trader.adapters.orbitexch.web import oe_login

from src.arbitrage.bootstrap import ctx_map_get
from src.arbitrage.bootstrap import ctx_map_get_or_create
from src.arbitrage.bootstrap import ctx_map_require
from src.arbitrage.bootstrap import ctx_map_set
from src.arbitrage.bootstrap import get_arb_context
from src.arbitrage.common.venues import ORBITEXCH


def _shared_oe_browser_manager(ctx, config) -> PlaywrightBrowserManager:
    """§6.2 单例:复用 keyed map 中的 BrowserManager(#62)。

    NT build 顺序 data→exec:data factory 先建回写,exec factory 复用同一实例 → **一个浏览器**
    (data/exec 各取专属 page),而非各 `new` 一个(headless=false 时是两个窗口的 bug)。
    """
    return ctx_map_get_or_create(
        ctx,
        "browser_manager_by_venue",
        ORBITEXCH,
        lambda: PlaywrightBrowserManager(
            browser_type=config.browser_type,
            headless=config.headless,
            user_data_dir=config.user_data_dir,
        ),
    )


def _shared_oe_browser_lock(ctx) -> asyncio.Lock:
    """OE browser context 级操作锁。

    Data discovery 与 Exec 共用同一 browser context;启动期 NT 会并发 connect
    data/exec clients。把 discovery fetch 这种操作串行化,避免并发问题。
    """
    return ctx_map_get_or_create(ctx, "browser_lock_by_venue", ORBITEXCH, asyncio.Lock)


def _oe_browser_json_fetcher(browser_manager, config, browser_lock: asyncio.Lock):
    """创建 OE sport/details API fetcher。

    OE API 需要登录后的 CSRF token(从 CSRF-TOKEN cookie 读取)。
    首次 fetch 时登录,后续复用已登录 session。
    """
    _logged_in = False

    async def _fetch(request: OrbitExchSportDetailsRequest) -> dict:
        nonlocal _logged_in
        await browser_manager.start()
        page = await browser_manager.create_page("oe-discovery")
        page.set_default_timeout(config.page_timeout)
        async with browser_lock:
            # 首次 fetch 时登录,确保 CSRF token cookie 存在
            if not _logged_in:
                await oe_login(page, config)
                _logged_in = True
            payload = await oe_fetch_json(
                page,
                request.url,
                params=request.params,
                body=request.body,
            )
        if not payload.get("ok") or not isinstance(payload.get("json"), dict):
            raise RuntimeError(
                f"OE sport/details failed: status={payload.get('status')} text={payload.get('text')!r}",
            )
        return payload["json"]

    return _fetch


class OrbitExchLiveDataClientFactory(LiveDataClientFactory):
    """OrbitExch 自写适配器的 data client factory(Step 2)。

    2026-07-03: 迁移到 `sport/details` API,与 SE 对齐。
    """

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: OrbitExchDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> OrbitExchDataClient:
        ctx = get_arb_context()
        # OE 共享 BrowserManager(§6.2 单例;#62:exec factory 复用同一实例 → 一个浏览器)
        browser_manager = _shared_oe_browser_manager(ctx, config)
        browser_lock = _shared_oe_browser_lock(ctx)
        oe_discovery_cfg = ctx_map_get(ctx, "discovery_config_by_venue", ORBITEXCH)
        if oe_discovery_cfg is not None:
            sport_configs = list(getattr(oe_discovery_cfg, "sports", []) or [])
            target_competitions = [
                comp
                for sport in sport_configs
                for comp in getattr(sport, "competitions", []) or []
            ]
            discovery = OrbitExchDiscoveryClient(
                base_url=config.base_url,
                json_fetcher=_oe_browser_json_fetcher(browser_manager, config, browser_lock),
                target_competitions=target_competitions,
            )
            provider = OrbitExchInstrumentProvider(
                discovery=discovery,
                sport_aliases=dict(
                    ctx_map_get(ctx, "sport_aliases_by_venue", ORBITEXCH, {}),
                ),
                competition_aliases=dict(
                    ctx_map_get(ctx, "competition_aliases_by_venue", ORBITEXCH, {}),
                ),
                sport_configs=sport_configs,
                fx=getattr(ctx.arbitrage_params, "fx", 1.0) if ctx.arbitrage_params is not None else 1.0,
            )
        else:
            provider = InstrumentProvider()  # discovery 禁用时占位
        ctx_map_set(ctx, "instrument_provider_by_venue", ORBITEXCH, provider)
        debug = ctx.debug_config
        if debug is not None and getattr(debug, "enabled", False):
            from src.arbitrage.debug.data_clients import DebugOrbitExchDataClient
            return DebugOrbitExchDataClient(
                loop=loop,
                browser_manager=browser_manager,
                msgbus=msgbus,
                cache=cache,
                clock=clock,
                instrument_provider=provider,
                config=config,
                debug=debug,
            )
        return OrbitExchDataClient(
            loop=loop,
            browser_manager=browser_manager,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
        )


class ArbOrbitExchLiveExecClientFactory(LiveExecClientFactory):
    """OrbitExch 自写适配器的 exec client factory。"""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: OrbitExchExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> OrbitExchExecutionClient:
        ctx = get_arb_context()
        if ctx.venue_liveness is None:
            raise RuntimeError(
                "ArbContext.venue_liveness is None —— `prepare_arb_context(venue_liveness=...)` "
                "必须在 node.build() 之前调用",
            )

        # §6.2 共享单例:复用 data factory 建的 BrowserManager(#62:避免各 new 一个 → 两窗口)
        browser_manager = _shared_oe_browser_manager(ctx, config)
        from nautilus_trader.common.providers import InstrumentProvider
        provider = InstrumentProvider()  # exec 端仅需占位 provider;发现由 DataClient 持有

        debug = ctx.debug_config
        common_kwargs = dict(
            loop=loop,
            browser_manager=browser_manager,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
            venue_liveness=ctx.venue_liveness,
            pair_registry=ctx.pair_registry,
            pair_inflight=getattr(ctx, "pair_inflight", None),  # §6.10 §7:per-pair 串行
            session_timeout_secs=ctx_map_require(ctx, "session_timeout_secs_by_venue", ORBITEXCH),
            fx=getattr(ctx.arbitrage_params, "fx", 1.0) if ctx.arbitrage_params is not None else 1.0,
        )
        if debug is not None and getattr(debug, "enabled", False):
            from src.arbitrage.debug.execution_clients import SkipExecutionOrbitExchClient
            return SkipExecutionOrbitExchClient(debug=debug, **common_kwargs)
        return OrbitExchExecutionClient(**common_kwargs)
