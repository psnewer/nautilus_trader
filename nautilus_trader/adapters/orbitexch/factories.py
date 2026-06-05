"""
Arb OE 执行客户端 factory —— 给 `TradingNode.add_exec_client_factory(...)` 用。

构造同目录 `execution.py:OrbitExchExecutionClient`。NT 的 `LiveExecClientFactory.create` 签名
固定,套利额外依赖(leg_settled / 间隔)经 `src.arbitrage.bootstrap.ArbContext` 注入。

**位置(#33 校准)**:OE 适配器全套住 `nautilus_trader/adapters/orbitexch/`(P9 唯一例外);
本文件 `factories.py` 与上游无冲突(OE 无上游)。
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
from nautilus_trader.adapters.orbitexch.execution import OrbitExchExecutionClient

from src.arbitrage.bootstrap import get_arb_context


class OrbitExchLiveDataClientFactory(LiveDataClientFactory):
    """OrbitExch 自写适配器的 data client factory(Step 2)。"""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: OrbitExchDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> OrbitExchDataClient:
        # OE 共享 BrowserManager(单例,§6.2):data + exec + discovery 同一 browser context、各取专属 page
        browser_manager = PlaywrightBrowserManager(
            browser_type=config.browser_type,
            headless=config.headless,
            user_data_dir=config.user_data_dir,
        )
        # slice 7A(#46):真接 scraper + OrbitExchInstrumentProvider(aliases 注入)。
        # `oe_scraper_config` 缺 → 装空 InstrumentProvider 占位(`enabled=False` 路径)。
        ctx = get_arb_context()
        oe_scraper_cfg = getattr(ctx, "oe_scraper_config", None)
        if oe_scraper_cfg is not None:
            from nautilus_trader.adapters.orbitexch.discovery_scraper import OrbitExchScraper
            from nautilus_trader.adapters.orbitexch.providers import OrbitExchInstrumentProvider
            scraper = OrbitExchScraper(config=oe_scraper_cfg)
            provider = OrbitExchInstrumentProvider(
                scraper=scraper,
                sport_aliases=dict(getattr(ctx, "oe_sport_aliases", {})),
                competition_aliases=dict(getattr(ctx, "oe_competition_aliases", {})),
            )
        else:
            provider = InstrumentProvider()  # discovery 禁用时占位
        ctx.oe_instrument_provider = provider  # slice 8A 回写,InstrumentRefresher 取同一实例
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
        if ctx.leg_settled is None:
            raise RuntimeError(
                "ArbContext.leg_settled is None —— `prepare_arb_context(leg_settled=...)` "
                "必须在 node.build() 之前调用",
            )

        # OE browser_manager:遵循 §6.2 共享单例(三方共享 BrowserContext,各自按 page name 取页)
        browser_manager = PlaywrightBrowserManager(
            browser_type=config.browser_type,
            headless=config.headless,
            user_data_dir=config.user_data_dir,
        )
        from nautilus_trader.common.providers import InstrumentProvider
        provider = InstrumentProvider()  # OE 端 provider 由 DataClient/Refresher 维护,exec 端仅占位

        debug = ctx.debug_config
        common_kwargs = dict(
            loop=loop,
            browser_manager=browser_manager,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
            leg_settled=ctx.leg_settled,
            pair_registry=ctx.pair_registry,
            session_timeout_secs=ctx.oe_session_timeout_secs,
        )
        if debug is not None and getattr(debug, "enabled", False):
            from src.arbitrage.debug.execution_clients import SkipExecutionOrbitExchClient
            return SkipExecutionOrbitExchClient(debug=debug, **common_kwargs)
        return OrbitExchExecutionClient(**common_kwargs)
