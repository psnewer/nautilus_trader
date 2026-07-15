"""SharpExch data client 与 price frame → `OrderBookDeltas` 映射。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Optional

from nautilus_trader.adapters.sharpexch.config import SharpExchDataClientConfig
from nautilus_trader.adapters.sharpexch.message_parser import SharpExchMessageParser
from nautilus_trader.adapters.sharpexch.websocket_handler import SharpExchWebSocketHandler
from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


SHARPEXCH = "SHARPEXCH"
_COMP_RELOAD_COOLDOWN_SECS = 5.0
_COMP_REOPEN_RETRY_SECS = 5.0


class SharpExchDataClient(LiveMarketDataClient):
    """SE data client。

    负责 discovery instruments、订阅时打开 competition 页、路由 price frame 并发布 `OrderBookDeltas`。
    """

    def __init__(
        self,
        loop,
        browser_manager,
        msgbus,
        cache,
        clock,
        instrument_provider,
        config: SharpExchDataClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(SHARPEXCH),
            venue=Venue(SHARPEXCH),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            config=config,
        )
        self._config = config
        self._browser_manager = browser_manager
        # #228:同一 (market, selection) 可路由到多条 instrument(yes + 合成 no),值为 (iid, claim) 列表
        self._market_to_instruments: dict[str, dict[str, list[tuple[InstrumentId, str]]]] = {}
        self._market_to_page_key: dict[str, str] = {}
        self._comp_page_refs: dict[str, tuple[str, str]] = {}
        self._comp_pages: dict[str, object] = {}
        self._comp_pages_lock = asyncio.Lock()
        self._comp_handlers: dict[str, object] = {}
        self._comp_reloading: set[str] = set()
        self._comp_last_reload_ns: dict[str, int] = {}
        self._disconnecting = False
        self._update_instruments_task: Optional[asyncio.Task] = None
        self._price_frames_seen = 0
        self._price_deltas_published = 0

    async def _connect(self) -> None:
        self._disconnecting = False
        await self._browser_manager.start()

        try:
            await self._instrument_provider.load_all_async()
            self._send_all_instruments_to_data_engine()
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                f"SE initial instruments load failed: {exc!r}; retrying next cycle",
            )
        if self._config.update_instruments_interval_mins:
            self._update_instruments_task = self.create_task(
                self._update_instruments(self._config.update_instruments_interval_mins),
            )

        self._log.info("SharpExchDataClient connected (competition pages opened on subscribe)")

    async def _disconnect(self) -> None:
        self._disconnecting = True
        if self._update_instruments_task:
            self._update_instruments_task.cancel()
            self._update_instruments_task = None
        for handler in self._comp_handlers.values():
            await handler.stop()
        self._comp_handlers.clear()
        self._comp_pages.clear()

    def _send_all_instruments_to_data_engine(self) -> None:
        for instrument in self._instrument_provider.get_all().values():
            self._handle_data(instrument)

    async def _update_instruments(self, interval_mins: int) -> None:
        try:
            while True:
                await asyncio.sleep(interval_mins * 60)
                try:
                    await self._instrument_provider.load_all_async()
                    self._send_all_instruments_to_data_engine()
                except Exception as exc:  # noqa: BLE001
                    self._log.warning(
                        f"SE update_instruments failed: {exc!r}; retrying next cycle",
                    )
        except asyncio.CancelledError:
            self._log.debug("Canceled task 'update_instruments'")

    async def _subscribe_order_book_deltas(self, command) -> None:
        inst = self._cache.instrument(command.instrument_id)
        if inst is None:
            return
        plan = se_update_subscription_state(
            market_routing=self._market_to_instruments,
            market_to_page_key=self._market_to_page_key,
            comp_page_refs=self._comp_page_refs,
            instrument_id=command.instrument_id,
            inst=inst,
        )
        try:
            async with self._comp_pages_lock:
                await se_ensure_competition_page(
                    plan=plan,
                    comp_pages=self._comp_pages,
                    open_page=self._open_or_reload_competition_page,
                )
        except Exception as exc:  # noqa: BLE001
            if plan is None:
                return
            self._log.warning(
                f"SE open competition {plan['page_key']} failed: {exc!r}; "
                f"retry in {_COMP_REOPEN_RETRY_SECS}s",
            )
            self._schedule_task(
                self._delayed_reopen(
                    plan["page_key"],
                    delay_secs=_COMP_REOPEN_RETRY_SECS,
                ),
            )

    async def _unsubscribe_order_book_deltas(self, command) -> None:
        se_remove_subscription_state(
            market_routing=self._market_to_instruments,
            market_to_page_key=self._market_to_page_key,
            comp_page_refs=self._comp_page_refs,
            instrument_id=command.instrument_id,
        )

    async def _open_or_reload_competition_page(
        self,
        *,
        page_key: str,
        sport_id: str,
        competition_id: str,
    ) -> dict:
        return await se_open_or_reload_competition_page(
            page_key=page_key,
            sport_id=sport_id,
            competition_id=competition_id,
            base_url=self._config.base_url,
            browser_manager=self._browser_manager,
            comp_pages=self._comp_pages,
            comp_handlers=self._comp_handlers,
            price_callback=self._on_price_frame,
            disconnect_callback=self._on_comp_disconnect,
            logger=self._log,
            page_timeout=self._config.page_timeout,
            clock=self._clock,
            liveness_timeout_secs=self._config.staleness_timeout_secs,
            liveness_name=f"se_comp_ws_liveness:{page_key}",
            liveness_ws_type="prices",
        )

    def _on_price_frame(self, message) -> None:
        out = se_handle_price_frame(
            message,
            self._market_to_instruments,
            self._clock.timestamp_ns(),
            self._handle_data,
            write_in_play=self._write_in_play_to_instrument_info,
        )
        if out is None:
            return
        self._price_frames_seen += 1
        if self._price_frames_seen == 1:
            self._log.info(
                f"SE price frame routed: market_id={out.get('market_id')}, "
                f"runners={out.get('runners')}, "
                f"subscribed_selections={out.get('subscribed_selections')}",
            )
        published_count = int(out.get("published_count", 0))
        previous_published = self._price_deltas_published
        self._price_deltas_published += published_count
        if published_count and previous_published == 0:
            first_deltas = next(iter(out.get("deltas") or []), None)
            instrument_id = getattr(first_deltas, "instrument_id", None)
            delta_count = len(first_deltas.deltas) if first_deltas is not None else 0
            self._log.info(
                f"SE OrderBookDeltas published: instrument_id={instrument_id}, "
                f"deltas={delta_count}",
            )

    def _on_comp_disconnect(self, page_key: str, reason: str) -> None:
        if not se_should_reload_on_disconnect(
            page_key=page_key,
            reason=reason,
            comp_pages=self._comp_pages,
            comp_reloading=self._comp_reloading,
            comp_last_reload_ns=self._comp_last_reload_ns,
            now_ns=self._clock.timestamp_ns(),
            cooldown_ns=secs_to_nanos(_COMP_RELOAD_COOLDOWN_SECS),
            disconnecting=self._disconnecting,
        ):
            return
        self._schedule_task(self._reload_comp_on_disconnect(page_key))

    async def _reload_comp_on_disconnect(self, page_key: str) -> None:
        page_ref = self._comp_page_refs.get(page_key)
        if page_ref is None:
            return
        sport_id, competition_id = page_ref
        self._comp_reloading.add(page_key)
        try:
            async with self._comp_pages_lock:
                if page_key in self._comp_pages and not self._disconnecting:
                    await self._open_or_reload_competition_page(
                        page_key=page_key,
                        sport_id=sport_id,
                        competition_id=competition_id,
                    )
        except Exception as exc:  # noqa: BLE001
            self._log.error(f"SE competition page {page_key} disconnect-driven reload failed: {exc}")
        finally:
            self._comp_reloading.discard(page_key)

    async def _delayed_reopen(self, page_key: str, *, delay_secs: float = _COMP_REOPEN_RETRY_SECS) -> None:
        await asyncio.sleep(delay_secs)
        try:
            async with self._comp_pages_lock:
                await se_reopen_missing_page(
                    page_key=page_key,
                    market_to_page_key=self._market_to_page_key,
                    comp_page_refs=self._comp_page_refs,
                    comp_pages=self._comp_pages,
                    open_page=self._open_or_reload_competition_page,
                    disconnecting=self._disconnecting,
                )
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                f"SE reopen competition {page_key} failed: {exc!r}; retry in {_COMP_REOPEN_RETRY_SECS}s",
            )
            self._schedule_task(
                self._delayed_reopen(
                    page_key,
                    delay_secs=_COMP_REOPEN_RETRY_SECS,
                ),
            )

    def _schedule_task(self, coro) -> None:
        self._loop.create_task(coro)

    def _write_in_play_to_instrument_info(self, instrument_id: InstrumentId, in_play: bool) -> None:
        inst = self._cache.instrument(instrument_id)
        if inst is not None and getattr(inst, "info", None) is not None:
            inst.info["in_play"] = in_play


def se_should_reload_on_disconnect(
    *,
    page_key: str,
    reason: str,
    comp_pages: dict[str, object],
    comp_reloading: set[str],
    comp_last_reload_ns: dict[str, int],
    now_ns: int,
    cooldown_ns: int,
    disconnecting: bool = False,
) -> bool:
    """判断 SE competition 页 disconnect 是否应触发 reload。

    只对 prices feed close / liveness timeout 响应;允许时会写入 `comp_last_reload_ns`。
    """

    if reason not in {"liveness_timeout", "close:prices"}:
        return False
    if disconnecting or page_key in comp_reloading:
        return False
    if page_key not in comp_pages:
        return False
    last = comp_last_reload_ns.get(page_key, 0)
    if last and (now_ns - last) < cooldown_ns:
        return False
    comp_last_reload_ns[page_key] = now_ns
    return True


def se_routing_entry_from_instrument(inst) -> tuple[str, str] | None:
    """SE instrument → `(market_id, selection_id)` routing key。

    DataClient 从 cache 取到 instrument 后调用;缺任一字段则返回 None。
    """

    market_id = str(getattr(inst, "market_id", "") or "")
    selection_id = str(getattr(inst, "selection_id", "") or "")
    if not market_id or not selection_id:
        return None
    return market_id, selection_id


def se_update_market_routing(
    routing: dict[str, dict[str, list[tuple[InstrumentId, str]]]],
    instrument_id: InstrumentId,
    inst,
) -> bool:
    """把一个 SE instrument 加入 `market_id -> selection_id -> [(InstrumentId, claim)]` routing(#228 多值)。"""

    entry = se_routing_entry_from_instrument(inst)
    if entry is None:
        return False
    market_id, selection_id = entry
    claim = str((getattr(inst, "info", None) or {}).get("claim") or "yes").lower()
    entries = routing.setdefault(market_id, {}).setdefault(selection_id, [])
    if not any(iid == instrument_id for iid, _ in entries):
        entries.append((instrument_id, claim))
    return True


def se_remove_market_routing(
    routing: dict[str, dict[str, list[tuple[InstrumentId, str]]]],
    instrument_id: InstrumentId,
) -> None:
    """从 routing 中移除一个 instrument id。"""

    for selection_map in list(routing.values()):
        for selection_id, entries in list(selection_map.items()):
            remaining = [(iid, c) for iid, c in entries if iid != instrument_id]
            if remaining:
                selection_map[selection_id] = remaining
            else:
                del selection_map[selection_id]


def se_subscription_plan_from_instrument(inst) -> dict | None:
    """SE instrument → DataClient 订阅所需的 routing + page ref。"""

    routing_entry = se_routing_entry_from_instrument(inst)
    page_ref = se_competition_page_ref_from_instrument(inst)
    if routing_entry is None or page_ref is None:
        return None
    market_id, selection_id = routing_entry
    page_key, sport_id, competition_id = page_ref
    return {
        "market_id": market_id,
        "selection_id": selection_id,
        "page_key": page_key,
        "sport_id": sport_id,
        "competition_id": competition_id,
    }


def se_update_subscription_state(
    *,
    market_routing: dict[str, dict[str, list[tuple[InstrumentId, str]]]],
    market_to_page_key: dict[str, str],
    comp_page_refs: dict[str, tuple[str, str]],
    instrument_id: InstrumentId,
    inst,
) -> dict | None:
    """注册 SE instrument 的订阅状态。

    更新:
    - `market_id -> selection_id -> [(InstrumentId, claim)]`(#228:同 selection 可有 yes+no 两条)
    - `market_id -> page_key`
    - `page_key -> (sport_id, competition_id)`
    """

    plan = se_subscription_plan_from_instrument(inst)
    if plan is None:
        return None
    market_id = plan["market_id"]
    selection_id = plan["selection_id"]
    page_key = plan["page_key"]
    claim = str((getattr(inst, "info", None) or {}).get("claim") or "yes").lower()
    entries = market_routing.setdefault(market_id, {}).setdefault(selection_id, [])
    if not any(iid == instrument_id for iid, _ in entries):
        entries.append((instrument_id, claim))
    market_to_page_key[market_id] = page_key
    comp_page_refs[page_key] = (plan["sport_id"], plan["competition_id"])
    return plan


def se_remove_subscription_state(
    *,
    market_routing: dict[str, dict[str, list[tuple[InstrumentId, str]]]],
    market_to_page_key: dict[str, str],
    comp_page_refs: dict[str, tuple[str, str]],
    instrument_id: InstrumentId,
) -> None:
    """移除 SE instrument 的订阅状态。"""

    removed_markets: list[str] = []
    for market_id, selection_map in list(market_routing.items()):
        for selection_id, entries in list(selection_map.items()):
            remaining = [(iid, c) for iid, c in entries if iid != instrument_id]
            if remaining:
                selection_map[selection_id] = remaining
            else:
                del selection_map[selection_id]
        if not selection_map:
            del market_routing[market_id]
            removed_markets.append(market_id)

    removed_page_keys = []
    for market_id in removed_markets:
        page_key = market_to_page_key.pop(market_id, None)
        if page_key is not None:
            removed_page_keys.append(page_key)

    active_page_keys = set(market_to_page_key.values())
    for page_key in removed_page_keys:
        if page_key not in active_page_keys:
            comp_page_refs.pop(page_key, None)


def se_competition_page_ref_from_instrument(inst) -> tuple[str, str, str] | None:
    """SE instrument → `(page_key, sport_id, competition_id)`。

    `BettingInstrument.event_type_id` 存 sport id,`competition_id` 存 competition id。
    """

    sport_id = str(getattr(inst, "event_type_id", "") or "")
    competition_id = str(getattr(inst, "competition_id", "") or "")
    if not sport_id or not competition_id:
        return None
    return f"{sport_id}_{competition_id}", sport_id, competition_id


def se_competition_page_url(base_url: str, sport_id: str, competition_id: str) -> str:
    """SE competition 页 URL。"""

    return f"{str(base_url).rstrip('/')}/customer/sport/{sport_id}/competition/{competition_id}"


def se_should_reopen_missing_page(
    *,
    page_key: str,
    market_to_page_key: dict[str, str],
    comp_pages: dict[str, object],
    disconnecting: bool = False,
) -> bool:
    """判断 SE 开页失败后的延迟重试是否仍应继续。"""

    if disconnecting:
        return False
    if page_key in comp_pages:
        return False
    return page_key in set(market_to_page_key.values())


async def se_reopen_missing_page(
    *,
    page_key: str,
    market_to_page_key: dict[str, str],
    comp_page_refs: dict[str, tuple[str, str]],
    comp_pages: dict[str, object],
    open_page: Callable,
    disconnecting: bool = False,
) -> dict | None:
    """SE 开页失败后的显式重试 helper。

    不 sleep、不创建 task;调用方负责延迟调度。
    """

    if not se_should_reopen_missing_page(
        page_key=page_key,
        market_to_page_key=market_to_page_key,
        comp_pages=comp_pages,
        disconnecting=disconnecting,
    ):
        return None
    page_ref = comp_page_refs.get(page_key)
    if page_ref is None:
        return None
    sport_id, competition_id = page_ref
    return await open_page(
        page_key=page_key,
        sport_id=sport_id,
        competition_id=competition_id,
    )


async def se_ensure_competition_page(
    *,
    plan: dict | None,
    comp_pages: dict[str, object],
    open_page: Callable,
) -> dict | None:
    """按订阅计划确保 competition 页已开。

    `open_page` 由调用方注入,通常绑定到 `se_open_or_reload_competition_page` 的偏函数。
    """

    if plan is None:
        return None
    page_key = str(plan.get("page_key", "") or "")
    sport_id = str(plan.get("sport_id", "") or "")
    competition_id = str(plan.get("competition_id", "") or "")
    if not page_key or not sport_id or not competition_id:
        return None
    if page_key in comp_pages:
        return {"action": "already_open", "page_key": page_key}
    return await open_page(
        page_key=page_key,
        sport_id=sport_id,
        competition_id=competition_id,
    )


def se_websocket_summary(handler) -> str:
    """SE WS handler active websockets 摘要,用于开页/reload 日志。"""

    active = handler.get_active_websockets()
    counts: dict[str, int] = {}
    for item in active:
        ws_type = str(item.get("type", "unknown"))
        counts[ws_type] = counts.get(ws_type, 0) + 1
    frame_counts = {}
    if hasattr(handler, "get_frame_counts"):
        frame_counts = handler.get_frame_counts()
    return f"ws_count={len(active)}, ws_types={counts}, frame_counts={frame_counts}"


async def se_open_or_reload_competition_page(
    *,
    page_key: str,
    sport_id: str,
    competition_id: str,
    base_url: str,
    browser_manager,
    comp_pages: dict[str, object],
    comp_handlers: dict[str, object],
    price_callback: Callable,
    disconnect_callback: Callable | None = None,
    logger=None,
    page_timeout: int = 120000,
    clock=None,
    liveness_timeout_secs: float | None = None,
    liveness_name: str | None = None,
    liveness_ws_type: str | None = None,
    handler_factory=SharpExchWebSocketHandler,
) -> dict:
    """SE competition 页新开/刷新统一 helper。

    不创建 runtime task、不接 NT;调用方显式传入 browser/page registry 与 callbacks。
    """

    url = se_competition_page_url(base_url, sport_id, competition_id)
    page = comp_pages.get(page_key)
    if page is None:
        page_name = f"comp-{page_key}"
        page = await browser_manager.create_page(page_name)
        handler = handler_factory(
            page,
            logger=logger,
            clock=clock,
            liveness_timeout_secs=liveness_timeout_secs,
            liveness_name=liveness_name,
            liveness_ws_type=liveness_ws_type,
        )
        handler.on_price_update(price_callback)
        if disconnect_callback is not None:
            handler.on_disconnect(lambda reason, pk=page_key: disconnect_callback(pk, reason))
        try:
            await handler.start()
            await page.bring_to_front()
            await page.goto(url, wait_until="domcontentloaded", timeout=page_timeout)
        except Exception:
            with suppress(Exception):
                await handler.stop()
            with suppress(Exception):
                await browser_manager.close_page(page_name)
            raise
        comp_pages[page_key] = page
        comp_handlers[page_key] = handler
        return {"action": "opened", "url": url, "summary": se_websocket_summary(handler)}

    handler = comp_handlers.get(page_key)
    await page.bring_to_front()
    await page.reload(wait_until="domcontentloaded", timeout=page_timeout)
    summary = se_websocket_summary(handler) if handler is not None else "ws_count=unknown"
    return {"action": "reloaded", "url": url, "summary": summary}


async def se_reload_competition_on_disconnect(
    *,
    page_key: str,
    reason: str,
    comp_page_refs: dict[str, tuple[str, str]],
    base_url: str,
    browser_manager,
    comp_pages: dict[str, object],
    comp_handlers: dict[str, object],
    comp_reloading: set[str],
    comp_last_reload_ns: dict[str, int],
    now_ns: int,
    cooldown_ns: int,
    price_callback: Callable,
    disconnect_callback: Callable | None = None,
    logger=None,
    page_timeout: int = 120000,
    clock=None,
    liveness_timeout_secs: float | None = None,
    liveness_name: str | None = None,
    liveness_ws_type: str | None = None,
    handler_factory=SharpExchWebSocketHandler,
    disconnecting: bool = False,
) -> dict | None:
    """SE disconnect reload 组合 helper。

    仅组合判定、`comp_reloading` 保护与显式 open/reload;不创建后台任务。
    """

    page_ref = comp_page_refs.get(page_key)
    if page_ref is None:
        return None
    if not se_should_reload_on_disconnect(
        page_key=page_key,
        reason=reason,
        comp_pages=comp_pages,
        comp_reloading=comp_reloading,
        comp_last_reload_ns=comp_last_reload_ns,
        now_ns=now_ns,
        cooldown_ns=cooldown_ns,
        disconnecting=disconnecting,
    ):
        return None

    sport_id, competition_id = page_ref
    comp_reloading.add(page_key)
    try:
        return await se_open_or_reload_competition_page(
            page_key=page_key,
            sport_id=sport_id,
            competition_id=competition_id,
            base_url=base_url,
            browser_manager=browser_manager,
            comp_pages=comp_pages,
            comp_handlers=comp_handlers,
            price_callback=price_callback,
            disconnect_callback=disconnect_callback,
            logger=logger,
            page_timeout=page_timeout,
            clock=clock,
            liveness_timeout_secs=liveness_timeout_secs,
            liveness_name=liveness_name,
            liveness_ws_type=liveness_ws_type,
            handler_factory=handler_factory,
        )
    finally:
        comp_reloading.discard(page_key)


def se_price_message_to_book_deltas(
    message,
    routing: dict[str, list[tuple[InstrumentId, str]]],
    ts_init_ns: int,
    *,
    price_precision: int = 2,
    size_precision: int = 2,
) -> list[OrderBookDeltas]:
    """SE price WS message + selection routing → `OrderBookDeltas` 列表。

    `routing` 是 DataClient 维护的 `selection_id -> [(InstrumentId, claim)]`;未订阅 runner 跳过。
    """

    parsed = SharpExchMessageParser().parse_price_message(message)
    if not parsed:
        return []

    out = []
    for runner in parsed.get("runners", []):
        selection_id = str(runner.get("selection_id", "") or "")
        for instrument_id, claim in routing.get(selection_id, []):
            deltas = se_runner_to_book_deltas(
                instrument_id,
                runner,
                ts_init_ns,
                price_precision=price_precision,
                size_precision=size_precision,
                claim=claim,
            )
            if deltas is not None:
                out.append(deltas)
    return out


def se_market_price_message_to_book_deltas(
    message,
    market_routing: dict[str, dict[str, list[tuple[InstrumentId, str]]]],
    ts_init_ns: int,
    *,
    price_precision: int = 2,
    size_precision: int = 2,
) -> dict | None:
    """SE price WS message + market routing → routed book-delta payload。

    `market_routing` 是 DataClient 维护的 `market_id -> selection_id -> [(InstrumentId, claim)]`。
    """

    parsed = SharpExchMessageParser().parse_price_message(message)
    if not parsed:
        return None
    market_id = str(parsed.get("market_id", "") or "")
    routing = market_routing.get(market_id)
    if not routing:
        return None
    deltas: list[OrderBookDeltas] = []
    for runner in parsed.get("runners", []):
        selection_id = str(runner.get("selection_id", "") or "")
        # #228:同一 selection 可有 yes + 合成 no 两条 instrument,同帧各发一份 deltas
        for instrument_id, claim in routing.get(selection_id, []):
            item = se_runner_to_book_deltas(
                instrument_id,
                runner,
                ts_init_ns,
                price_precision=price_precision,
                size_precision=size_precision,
                claim=claim,
            )
            if item is not None:
                deltas.append(item)
    return {
        "market_id": market_id,
        "in_play": bool(parsed.get("in_play", False)),
        "runners": len(parsed.get("runners", [])),
        "subscribed_selections": len(routing),
        "deltas": deltas,
    }


def se_publish_routed_book_deltas(
    routed_payload: dict | None,
    publish: Callable,
    *,
    write_in_play: Callable | None = None,
) -> int:
    """发布 `se_market_price_message_to_book_deltas` 的 routed payload。

    返回实际发布的 `OrderBookDeltas` 数量。
    """

    if not routed_payload:
        return 0
    in_play = bool(routed_payload.get("in_play", False))
    count = 0
    for deltas in routed_payload.get("deltas") or []:
        instrument_id = getattr(deltas, "instrument_id", None)
        if write_in_play is not None and instrument_id is not None:
            write_in_play(instrument_id, in_play)
        publish(deltas)
        count += 1
    return count


def se_handle_price_frame(
    message,
    market_routing: dict[str, dict[str, InstrumentId]],
    ts_init_ns: int,
    publish: Callable,
    *,
    write_in_play: Callable | None = None,
    price_precision: int = 2,
    size_precision: int = 2,
) -> dict | None:
    """SE price frame → routed deltas → publish 的组合 helper。"""

    routed = se_market_price_message_to_book_deltas(
        message,
        market_routing,
        ts_init_ns,
        price_precision=price_precision,
        size_precision=size_precision,
    )
    if routed is None:
        return None
    published_count = se_publish_routed_book_deltas(
        routed,
        publish,
        write_in_play=write_in_play,
    )
    return {**routed, "published_count": published_count}


def se_runner_to_book_deltas(
    instrument_id: InstrumentId,
    runner: dict,
    ts_init_ns: int,
    *,
    price_precision: int = 2,
    size_precision: int = 2,
    claim: str = "yes",
) -> Optional[OrderBookDeltas]:
    """SE WS 一个 runner 的 back/lay 快照 → `OrderBookDeltas`(CLEAR + N×ADD)。

    #228 `claim="no"`(3-way 合成 no 腿):两侧换位重挂,ask ← LAY 列原值、bid ← BACK 列原值。
    """

    back = runner.get("back") or []
    lay = runner.get("lay") or []
    if not back and not lay:
        return None

    deltas: list[OrderBookDelta] = [
        OrderBookDelta(
            instrument_id=instrument_id,
            action=BookAction.CLEAR,
            order=None,
            flags=0,
            sequence=0,
            ts_event=ts_init_ns,
            ts_init=ts_init_ns,
        ),
    ]
    order_id = 1
    is_no = str(claim or "").lower() == "no"
    # decimal odds 只发布 top-of-book:back 赔率越高越好,lay 赔率越低越好。
    best_back = _best_level(back, highest=True)
    best_lay = _best_level(lay, highest=False)
    # yes 腿:ask=back / bid=lay;no 腿:ask=lay / bid=back(原值换位)。
    ask_level = best_lay if is_no else best_back
    bid_level = best_back if is_no else best_lay
    if ask_level is not None:
        price, size = ask_level
        deltas.append(_make_add(instrument_id, OrderSide.SELL, price, size, order_id, ts_init_ns, price_precision, size_precision))
        order_id += 1
    if bid_level is not None:
        price, size = bid_level
        deltas.append(_make_add(instrument_id, OrderSide.BUY, price, size, order_id, ts_init_ns, price_precision, size_precision))
        order_id += 1

    if len(deltas) == 1:
        return None
    return OrderBookDeltas(instrument_id=instrument_id, deltas=deltas)


def _make_add(instrument_id, side, price, size, order_id, ts_init_ns, price_precision, size_precision):
    return OrderBookDelta(
        instrument_id=instrument_id,
        action=BookAction.ADD,
        order=BookOrder(
            side=side,
            price=Price(float(price), precision=price_precision),
            size=Quantity(float(size), precision=size_precision),
            order_id=order_id,
        ),
        flags=0,
        sequence=order_id,
        ts_event=ts_init_ns,
        ts_init=ts_init_ns,
    )


def _best_level(levels, *, highest: bool) -> tuple[float, float] | None:
    valid: list[tuple[float, float]] = []
    for level in levels:
        try:
            price = float(level.get("price"))
            size = float(level.get("size"))
        except (AttributeError, TypeError, ValueError):
            continue
        if price <= 0 or size <= 0:
            continue
        valid.append((price, size))
    if not valid:
        return None
    return max(valid, key=lambda item: item[0]) if highest else min(valid, key=lambda item: item[0])
