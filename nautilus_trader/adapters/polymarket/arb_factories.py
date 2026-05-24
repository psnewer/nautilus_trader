"""
Arb PM 执行客户端 factory —— 给 `TradingNode.add_exec_client_factory(...)` 用。

替代上游 `PolymarketLiveExecClientFactory`,构造 `ArbPolymarketExecutionClient`(同目录
`arb_execution.py`)。NT 的 `LiveExecClientFactory.create` 签名固定,套利额外依赖
(leg_settled / settlement / positions_fetcher / 间隔)经 `src.arbitrage.bootstrap.ArbContext`
进程级共享件注入。

**位置(#33 校准)**:按 NT 约定,venue factory 与该 venue 的 client 同目录;命名
`arb_factories.py` 避开上游 `factories.py`(upstream merge 不冲突)。

启动顺序(launcher,详见同 ArbContext 文档):
1. `install_arbitrage_engines()`  → 替换 kernel.Portfolio / .LiveRiskEngine
2. `node = TradingNode(config)`   → kernel 原生构造 ArbitragePortfolio / ArbitrageLiveRiskEngine
3. `prepare_arb_context(...)`     → 填好 leg_settled / settlement / fetcher / 间隔
4. `node.add_exec_client_factory("POLYMARKET", ArbPolymarketLiveExecClientFactory)`
5. `node.build()`                 → factory.create 读 ArbContext 构造 client
6. `wire_arbitrage_runtime(node, params=)` → 复用同份 leg_settled
7. `node.run()`
"""

from __future__ import annotations

import asyncio

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.live.factories import LiveExecClientFactory

from nautilus_trader.adapters.polymarket.arb_execution import ArbPolymarketExecutionClient
from nautilus_trader.adapters.polymarket.common.credentials import PolymarketWebSocketAuth
from nautilus_trader.adapters.polymarket.common.credentials import get_polymarket_api_key
from nautilus_trader.adapters.polymarket.common.credentials import get_polymarket_api_secret
from nautilus_trader.adapters.polymarket.common.credentials import get_polymarket_passphrase
from nautilus_trader.adapters.polymarket.config import PolymarketExecClientConfig
from nautilus_trader.adapters.polymarket.factories import get_polymarket_http_client
from nautilus_trader.adapters.polymarket.factories import get_polymarket_instrument_provider

from src.arbitrage.bootstrap import get_arb_context


class ArbPolymarketLiveExecClientFactory(LiveExecClientFactory):
    """替代上游 `PolymarketLiveExecClientFactory`,构造 `ArbPolymarketExecutionClient`。"""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: PolymarketExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> ArbPolymarketExecutionClient:
        ctx = get_arb_context()
        if ctx.leg_settled is None:
            raise RuntimeError(
                "ArbContext.leg_settled is None —— `prepare_arb_context(leg_settled=...)` "
                "必须在 node.build() 之前调用",
            )

        http_client = get_polymarket_http_client(
            private_key=config.private_key,
            signature_type=config.signature_type,
            funder=config.funder,
            api_key=config.api_key,
            api_secret=config.api_secret,
            passphrase=config.passphrase,
            base_url=config.base_url_http,
        )
        ws_auth = PolymarketWebSocketAuth(
            apiKey=config.api_key or get_polymarket_api_key(),
            secret=config.api_secret or get_polymarket_api_secret(),
            passphrase=config.passphrase or get_polymarket_passphrase(),
        )
        provider = get_polymarket_instrument_provider(
            client=http_client, clock=clock, config=config.instrument_config,
        )
        return ArbPolymarketExecutionClient(
            loop=loop,
            http_client=http_client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            ws_auth=ws_auth,
            config=config,
            name=name,
            leg_settled=ctx.leg_settled,
            settlement=ctx.pm_settlement,
            positions_fetcher=ctx.pm_positions_fetcher,
            session_timeout_secs=ctx.pm_session_timeout_secs,
            health_interval_secs=ctx.pm_health_interval_secs,
        )
