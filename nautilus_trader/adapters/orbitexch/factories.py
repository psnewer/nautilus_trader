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
from nautilus_trader.live.factories import LiveExecClientFactory

from nautilus_trader.adapters.orbitexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.orbitexch.config import OrbitExchExecClientConfig
from nautilus_trader.adapters.orbitexch.execution import OrbitExchExecutionClient

from src.arbitrage.bootstrap import get_arb_context


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

        return OrbitExchExecutionClient(
            loop=loop,
            browser_manager=browser_manager,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
            leg_settled=ctx.leg_settled,
            session_timeout_secs=ctx.oe_session_timeout_secs,
        )
