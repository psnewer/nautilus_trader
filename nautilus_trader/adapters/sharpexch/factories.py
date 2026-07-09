"""SharpExch data/exec client factories."""

from __future__ import annotations

import asyncio
import logging

from nautilus_trader.cache.cache import Cache

_log = logging.getLogger(__name__)
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
from nautilus_trader.adapters.sharpexch.web import SharpExchLoginState

from src.arbitrage.bootstrap import ctx_map_get
from src.arbitrage.bootstrap import ctx_map_get_or_create
from src.arbitrage.bootstrap import ctx_map_require
from src.arbitrage.bootstrap import ctx_map_set
from src.arbitrage.bootstrap import get_arb_context
from src.arbitrage.common.venues import SHARPEXCH


def _shared_se_browser_manager(ctx, config) -> PlaywrightBrowserManager:
    """SE 共享 BrowserManager:Data/Exec factory 复用同一实例。"""

    return ctx_map_get_or_create(
        ctx,
        "browser_manager_by_venue",
        SHARPEXCH,
        lambda: PlaywrightBrowserManager(
            browser_type=config.browser_type,
            headless=config.headless,
            user_data_dir=config.user_data_dir,
        ),
    )


def _shared_se_browser_lock(ctx) -> asyncio.Lock:
    """SE browser context 级操作锁。

    Data discovery 与 Exec login 共用同一 browser context;启动期 NT 会并发 connect
    data/exec clients。SE/Cloudflare 对同一 context 并发登录 + API fetch 敏感,因此把
    login/fetch 这种会改变 customer session 状态的操作串行化。
    """

    return ctx_map_get_or_create(ctx, "browser_lock_by_venue", SHARPEXCH, asyncio.Lock)


def _shared_se_login_state(ctx) -> SharpExchLoginState:
    """SE browser context 级登录状态:串行化登录并避免二次提交旧登录页。"""

    return ctx_map_get_or_create(ctx, "browser_login_state_by_venue", SHARPEXCH, SharpExchLoginState)


def _se_browser_json_fetcher(browser_manager, config, login_state: SharpExchLoginState):
    async def _fetch(request: SharpExchSportDetailsRequest) -> dict:
        await browser_manager.start()
        page = await browser_manager.create_page("se-discovery")
        page.set_default_timeout(config.page_timeout)

        # se_login 内部使用 browser context 级状态串行化登录,避免并发/二次登录触发 Cloudflare
        await se_login(page, config, login_state=login_state)

        # Retry fetch up to 3 times for transient failures (including frame detachment)
        last_error = None
        for attempt in range(3):
            # Re-acquire context on each attempt to handle frame detachment
            frame_ctx = se_customer_context(page)
            if frame_ctx is None:
                if attempt < 2:
                    _log.warning(f"SE fetch attempt {attempt + 1}: customer context not available, retrying")
                    await asyncio.sleep(2.0)
                    continue
                raise RuntimeError("SE login completed but customer context not available after retries")
            try:
                payload = await se_fetch_json(
                    frame_ctx,
                    request.url,
                    params=request.params,
                    body=request.body,
                )
            except Exception as e:
                # Handle frame detachment and other Playwright errors
                err_msg = str(e).lower()
                if "frame was detached" in err_msg or "frame" in err_msg:
                    _log.warning(f"SE fetch attempt {attempt + 1}: frame detached, re-acquiring context")
                    if attempt < 2:
                        await asyncio.sleep(2.0)
                        continue
                raise
            if payload.get("ok") and isinstance(payload.get("json"), dict):
                return payload["json"]
            last_error = payload
            if attempt < 2:
                await asyncio.sleep(2.0)  # Wait before retry
        raise RuntimeError(
            f"SE sport/details failed after 3 attempts: "
            f"status={last_error.get('status')} text={last_error.get('text')!r}",
        )

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
        login_state = _shared_se_login_state(ctx)
        se_discovery_cfg = ctx_map_get(ctx, "discovery_config_by_venue", SHARPEXCH)
        if se_discovery_cfg is not None:
            sport_configs = list(getattr(se_discovery_cfg, "sports", []) or [])
            target_competitions = [
                comp
                for sport in sport_configs
                for comp in getattr(sport, "competitions", []) or []
            ]
            discovery = SharpExchDiscoveryClient(
                base_url=config.base_url,
                json_fetcher=_se_browser_json_fetcher(browser_manager, config, login_state),
                target_competitions=target_competitions,
            )
            provider = SharpExchInstrumentProvider(
                discovery=discovery,
                sport_aliases=dict(
                    ctx_map_get(ctx, "sport_aliases_by_venue", SHARPEXCH, {}),
                ),
                competition_aliases=dict(
                    ctx_map_get(ctx, "competition_aliases_by_venue", SHARPEXCH, {}),
                ),
                sport_configs=sport_configs,
                fx=getattr(ctx.arbitrage_params, "fx", 1.0) if ctx.arbitrage_params is not None else 1.0,
            )
        else:
            _log.warning("SE factory: no discovery config found, using empty InstrumentProvider")
            provider = InstrumentProvider()
        ctx_map_set(ctx, "instrument_provider_by_venue", SHARPEXCH, provider)
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
        login_state = _shared_se_login_state(ctx)
        provider = InstrumentProvider()

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
            pair_inflight=getattr(ctx, "pair_inflight", None),
            session_timeout_secs=ctx_map_require(ctx, "session_timeout_secs_by_venue", SHARPEXCH),
            fx=getattr(ctx.arbitrage_params, "fx", 1.0) if ctx.arbitrage_params is not None else 1.0,
            browser_lock=browser_lock,
            login_state=login_state,
        )
        if debug is not None and getattr(debug, "enabled", False):
            from src.arbitrage.debug.execution_clients import SkipExecutionSharpExchClient
            return SkipExecutionSharpExchClient(debug=debug, **common_kwargs)
        return SharpExchExecutionClient(**common_kwargs)
