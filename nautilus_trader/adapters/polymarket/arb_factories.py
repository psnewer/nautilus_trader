"""
Arb PM 执行客户端 factory —— 给 `TradingNode.add_exec_client_factory(...)` 用。

替代上游 `PolymarketLiveExecClientFactory`,构造 `ArbPolymarketExecutionClient`(同目录
`arb_execution.py`)。NT 的 `LiveExecClientFactory.create` 签名固定,套利额外依赖
(venue_liveness / settlement / positions_fetcher / 间隔)经 `src.arbitrage.bootstrap.ArbContext`
进程级共享件注入。

**位置(#33 校准)**:按 NT 约定,venue factory 与该 venue 的 client 同目录;命名
`arb_factories.py` 避开上游 `factories.py`(upstream merge 不冲突)。

启动顺序(launcher,详见同 ArbContext 文档):
1. `install_arbitrage_engines()`  → 替换 kernel.Portfolio / .LiveRiskEngine
2. `node = TradingNode(config)`   → kernel 原生构造 ArbitragePortfolio / ArbitrageLiveRiskEngine
3. `prepare_arb_context(...)`     → 填好 venue_liveness / settlement / fetcher / 间隔
4. `node.add_exec_client_factory("POLYMARKET", ArbPolymarketLiveExecClientFactory)`
5. `node.build()`                 → factory.create 读 ArbContext 构造 client
6. `wire_arbitrage_runtime(node, params=)` → 复用同份 venue_liveness
7. `node.run()`
"""

from __future__ import annotations

import asyncio

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.core.nautilus_pyo3 import HttpClient
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.live.factories import LiveExecClientFactory

from nautilus_trader.adapters.polymarket.arb_execution import ArbPolymarketExecutionClient
from nautilus_trader.adapters.polymarket.arb_provider import ArbPolymarketInstrumentProvider
from nautilus_trader.adapters.polymarket.common.credentials import PolymarketWebSocketAuth
from nautilus_trader.adapters.polymarket.common.credentials import get_polymarket_api_key
from nautilus_trader.adapters.polymarket.common.credentials import get_polymarket_api_secret
from nautilus_trader.adapters.polymarket.common.credentials import get_polymarket_passphrase
from nautilus_trader.adapters.polymarket.config import PolymarketDataClientConfig
from nautilus_trader.adapters.polymarket.config import PolymarketExecClientConfig
from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
from nautilus_trader.adapters.polymarket.factories import get_polymarket_http_client
from nautilus_trader.adapters.polymarket.factories import get_polymarket_instrument_provider
from nautilus_trader.adapters.polymarket.sports import PolymarketSportsDataClient
from nautilus_trader.adapters.polymarket.sports import PolymarketSportsInstrumentProvider

from src.arbitrage.bootstrap import ctx_map_get
from src.arbitrage.bootstrap import ctx_map_require
from src.arbitrage.bootstrap import ctx_map_set
from src.arbitrage.bootstrap import get_arb_context
from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import SPORTS_CLIENT


class PolymarketSportsLiveDataClientFactory(LiveDataClientFactory):
    """#60:PM Sports 比分 firehose DataClient(`PMSPORTS` —— 名不含 `-`,否则 NT node_builder
    会按 `partition("-")[0]` 前缀路由到 POLYMARKET 主 factory)。`_connect` 先加载 PMSPORTS
    synthetic event anchors,再开 WS firehose。"""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> PolymarketSportsDataClient:
        ctx = get_arb_context()
        provider = PolymarketSportsInstrumentProvider(
            target_competitions=ctx_map_get(
                ctx,
                "target_competitions_by_data_source",
                SPORTS_CLIENT,
                [],
            ),
            competition_to_sport=ctx_map_get(
                ctx,
                "competition_to_sport_by_data_source",
                SPORTS_CLIENT,
                {},
            ),
            competition_aliases=ctx_map_get(
                ctx,
                "competition_aliases_by_venue",
                POLYMARKET,
                {},
            ),
            # Gamma discovery 与 PM 主链同路由(venues.polymarket.proxy_url 经 dispatcher 传入)
            http_client=HttpClient(
                timeout_secs=30,
                proxy_url=getattr(config, "proxy_url", None),
            ),
        )
        return PolymarketSportsDataClient(
            loop=loop,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
        )


class ArbPolymarketLiveDataClientFactory(LiveDataClientFactory):
    """替代上游 `PolymarketLiveDataClientFactory`,用 `ArbPolymarketInstrumentProvider`
    给 instrument.info 补 matching 字段。"""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: PolymarketDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> PolymarketDataClient:
        http_client = get_polymarket_http_client(
            private_key=config.private_key,
            signature_type=config.signature_type,
            funder=config.funder,
            api_key=config.api_key,
            api_secret=config.api_secret,
            passphrase=config.passphrase,
            base_url=config.base_url_http,
            # #276:漏传即 CLOB REST 直连(WS/Gamma 走代理而 REST 超时的根因)
            proxy_url=config.proxy_url,
        )
        ctx = get_arb_context()
        # #55:ArbPolymarketInstrumentProvider.load_all_async 整体 override(series-based 发现),
        # 不再依赖 upstream event_slug_builder;直接读 ArbContext data-source keyed map。
        # #58(slice A):强制 load_all=True —— PM 上游 `_update_instruments` 走 `initialize(reload=True)`,
        # 而 `initialize` 仅 load_all=True 才调 load_all_async(否则 "No loading configured" 加载 0 → cache 空,
        # 历史上靠 refresher 直调 load_all_async 兜底;refresher 退役后必须让原生路径自己能 load)。
        import msgspec

        from nautilus_trader.config import InstrumentProviderConfig

        instrument_config = config.instrument_config or InstrumentProviderConfig()
        if not instrument_config.load_all:
            instrument_config = msgspec.structs.replace(instrument_config, load_all=True)
        provider = ArbPolymarketInstrumentProvider(
            client=http_client,
            clock=clock,
            config=instrument_config,
            # Gamma discovery 与 CLOB 同路由(否则 proxy 场景下交易通、discovery 断)
            http_client=HttpClient(timeout_secs=30, proxy_url=config.proxy_url),
        )
        ctx_map_set(ctx, "instrument_provider_by_venue", POLYMARKET, provider)
        debug = ctx.debug_config
        if debug is not None and getattr(debug, "enabled", False):
            from src.arbitrage.debug.data_clients import DebugPolymarketDataClient
            return DebugPolymarketDataClient(
                loop=loop,
                http_client=http_client,
                msgbus=msgbus,
                cache=cache,
                clock=clock,
                instrument_provider=provider,
                config=config,
                name=name,
                debug=debug,
            )
        return PolymarketDataClient(
            loop=loop,
            http_client=http_client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
            name=name,
        )


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
        if ctx.venue_liveness is None:
            raise RuntimeError(
                "ArbContext.venue_liveness is None —— `prepare_arb_context(venue_liveness=...)` "
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
            # #276:漏传即 CLOB REST 直连(WS/Gamma 走代理而 REST 超时的根因)
            proxy_url=config.proxy_url,
        )
        ws_auth = PolymarketWebSocketAuth(
            apiKey=config.api_key or get_polymarket_api_key(),
            secret=config.api_secret or get_polymarket_api_secret(),
            passphrase=config.passphrase or get_polymarket_passphrase(),
        )
        provider = get_polymarket_instrument_provider(
            client=http_client, clock=clock, config=config.instrument_config,
        )
        debug = ctx.debug_config
        common_kwargs = dict(
            loop=loop,
            http_client=http_client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            ws_auth=ws_auth,
            config=config,
            name=name,
            venue_liveness=ctx.venue_liveness,
            settlement=ctx.pm_settlement,
            realized_pnl_ledger=ctx.realized_pnl_ledger,
            session_timeout_secs=ctx_map_require(ctx, "session_timeout_secs_by_venue", POLYMARKET),
            # #110:merge/redeem 改由 NT 连续 position 对账驱动;不再需要 positions_fetcher / health_interval。
        )
        if debug is not None and getattr(debug, "enabled", False):
            from src.arbitrage.debug.execution_clients import SkipExecutionPolymarketClient
            return SkipExecutionPolymarketClient(debug=debug, **common_kwargs)
        return ArbPolymarketExecutionClient(**common_kwargs)
