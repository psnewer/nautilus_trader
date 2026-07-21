# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------

"""
OrbitExchDataClient —— OE 自写 `LiveMarketDataClient` 子类(Step 2,整体重写自半成品 data.py)。

设计见 `docs/arbitrage/architectures/data/architecture.md`(refactor.md §5.2/§6.2,Q2)。

= 自写 `LiveMarketDataClient` 子类:WS 价格帧(`multiple-market-prices`)→ NT 标准
`OrderBookDeltas`(snapshot 一次 CLEAR + 全档 ADD 入 NT 标准管道,#256 深度改造)。
BACK 写入 SELL/ask 侧,LAY 写入 BUY/bid 侧;存入值是 `probability_from_price` 换算后的
隐含概率而非原始赔率(原因见 `oe_runner_to_book_deltas` docstring)。

**位置**:`nautilus_trader/adapters/orbitexch/`(P9 唯一例外:OE 适配器整套住 adapter 目录;#33)。
**BrowserManager 仅消费**(Q2 共享单例;不再 start/close):`get_page("data")` 拿专属页。
**OE competition 页存活**(#109,见 data §4.3):**无 HealthCheckLoop / 周期 scan**;存活封装进 `OrbitExchWebSocketHandler`
(内部心跳超时 + close → `on_disconnect`),DataClient 只收事件 → reload,对称 PM `_schedule_delayed_connect`;开页失败 → `_delayed_reopen` 事件化重试。
**离线可测**:`oe_runner_to_book_deltas` 纯映射(WS runner → OrderBookDeltas)。
**live 验**:`_connect` / WS 接帧 / 订阅 routing(集成路径多变,/live-test 验)。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Optional

from src.arbitrage.common.venues import probability_from_price

from nautilus_trader.adapters.orbitexch.config import OrbitExchDataClientConfig
from nautilus_trader.adapters.orbitexch.message_parser import OrbitExchMessageParser
from nautilus_trader.adapters.orbitexch.websocket_handler import OrbitExchWebSocketHandler
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



ORBITEXCH = "ORBITEXCH"

# #108:WS disconnect → reload 的冷却窗(防 venue 持续不可用时 reload 风暴;首次 disconnect 仍即时反应)。
_COMP_RELOAD_COOLDOWN_SECS = 5.0
# #109:开页失败 → 延迟重试间隔(事件驱动 connect-retry,对齐 PM `_delayed_connect`)。
_COMP_REOPEN_RETRY_SECS = 5.0


def oe_runner_to_book_deltas(
    instrument_id: InstrumentId,
    runner: dict,
    ts_init_ns: int,
    *,
    price_precision: int = 8,
    size_precision: int = 2,
    claim: str = "yes",
) -> Optional[OrderBookDeltas]:
    """OE WS 一个 runner 的 back/lay 全档快照 → `OrderBookDeltas`(CLEAR + N×ADD)。

    WS 给的是**snapshot of N levels**(非增量),故每帧:
    - 1 个 `CLEAR`(刷掉旧本子)
    - back 全部档:`ADD(SELL)`(NT ask)
    - lay 全部档: `ADD(BUY)`(NT bid)
    包成 `OrderBookDeltas`(NT 标准 batch)入 DataEngine。

    #256(深度改造,取代 #85 的 top-of-book-only):cache 不再存 venue 原始赔率,存
    `probability_from_price(ORBITEXCH, raw, claim)` 换算后的隐含概率。原因:decimal odds
    是"back 越高越好、lay 越低越好",跟 NT book 的固定排序规则(ask 取原始值 min 为
    best,bid 取 max 为 best)方向不一致——只发一档时这个不一致被 N=1 的退化掩盖,发全档
    后 NT 原生 best_ask/best_bid 会真实生效,必须让存入值的单调方向配平。`1/price`
    (claim=yes)对 price 严格递减、`1-1/price`(claim=no)严格递增,恰好让 back/lay 在
    同一 claim 下换位后仍能被 NT 原生排序正确识别 best 与 worst(推导见 refactor.md
    #256 决策记录)。精度选 8 位,避免相邻赔率档位换算后在高赔率区间(如 999 vs 1000)
    撞位。读侧经 `checks/quote_legs.py`(仍读 best_ask,同一 claim 直接得到概率)/
    `matching/actor.py`(同理)/`web/actor.py`(`price_from_probability` 逆变换取回真实
    赔率用于展示)消费,详见 docs/arbitrage/architectures/data/architecture.md。

    #228 `claim="no"`(3-way 合成 no 腿):两侧换位——ask ← LAY 列、bid ← BACK 列,换位后
    仍用同一个 `claim` 做概率变换(数学上这正好让方向配平,见上)。

    返回 None 表示本 runner 无可用档(`back` + `lay` 都空)→ 调用方跳过 publish。
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
    back_levels = _valid_levels(back)
    lay_levels = _valid_levels(lay)
    # yes 腿:ask←back / bid←lay;no 腿:ask←lay / bid←back(换位,claim 不变)。
    ask_levels = lay_levels if is_no else back_levels
    bid_levels = back_levels if is_no else lay_levels
    for price, size in ask_levels:
        probability = probability_from_price(ORBITEXCH, price, claim)
        deltas.append(_make_add(instrument_id, OrderSide.SELL, probability, size, order_id, ts_init_ns, price_precision, size_precision))
        order_id += 1
    for price, size in bid_levels:
        probability = probability_from_price(ORBITEXCH, price, claim)
        deltas.append(_make_add(instrument_id, OrderSide.BUY, probability, size, order_id, ts_init_ns, price_precision, size_precision))
        order_id += 1

    if len(deltas) == 1:  # 只有 CLEAR,无实际档位 → 不发(避免空簿噪音)
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


def _valid_levels(levels) -> list[tuple[float, float]]:
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
    return valid


def oe_subscription_plan_from_instrument(inst) -> dict | None:
    """OE instrument → 订阅状态写入计划(#251,对齐 SE `se_subscription_plan_from_instrument`)。

    routing 必需 market_id/selection_id(缺任一 → None,调用方记 warning 跳过);
    page_key 可缺(不建页映射)。selection 优先 `info["venue_selection_id"]`
    (#228 合成 no 腿复用真实 venue selection)。
    """
    if inst is None:
        return None
    market_id = getattr(inst, "market_id", None)
    info = getattr(inst, "info", None) or {}
    selection_id = info.get("venue_selection_id")
    if selection_id is None:
        selection_id = getattr(inst, "selection_id", None)
    if market_id is None or selection_id is None:
        return None
    sport_id = str(getattr(inst, "event_type_id", "") or "")
    competition_id = str(getattr(inst, "competition_id", "") or "")
    return {
        "market_id": str(market_id),
        "selection_id": str(selection_id),
        "claim": str(info.get("quote_claim") or "yes").lower(),
        # market → page_key(§6.8.3:`_on_price_frame` 廉价定位帧所属 competition 页;
        # `_delayed_reopen` 据此判断页是否仍被订阅)
        "page_key": f"{sport_id}_{competition_id}" if sport_id and competition_id else None,
    }


def oe_update_subscription_state(
    *,
    market_routing: dict[str, dict[str, list[tuple[InstrumentId, str]]]],
    market_to_page_key: dict[str, str],
    instrument_id: InstrumentId,
    plan: dict,
) -> None:
    """按 plan 写入订阅状态两表(幂等;对齐 SE `se_update_subscription_state`)。"""
    entries = market_routing.setdefault(plan["market_id"], {}).setdefault(plan["selection_id"], [])
    if not any(iid == instrument_id for iid, _ in entries):
        entries.append((instrument_id, plan["claim"]))
    if plan["page_key"] is not None:
        market_to_page_key[plan["market_id"]] = plan["page_key"]


def oe_remove_subscription_state(
    *,
    market_routing: dict[str, dict[str, list[tuple[InstrumentId, str]]]],
    market_to_page_key: dict[str, str],
    instrument_id: InstrumentId,
) -> list[str]:
    """移除该 instrument 的订阅状态;返回因此失去全部 market 订阅的 page_key 列表(#251,供关页)。

    同时清理空 market 条目与 `market_to_page_key` 映射(对齐 SE `se_remove_subscription_state`)。
    """
    removed_markets: list[str] = []
    for market_id, sel_map in list(market_routing.items()):
        for sel_key, entries in list(sel_map.items()):
            remaining = [(iid, c) for iid, c in entries if iid != instrument_id]
            if remaining:
                sel_map[sel_key] = remaining
            else:
                del sel_map[sel_key]
        if not sel_map:
            del market_routing[market_id]
            removed_markets.append(market_id)
    orphaned: list[str] = []
    for market_id in removed_markets:
        page_key = market_to_page_key.pop(market_id, None)
        if page_key is not None and page_key not in orphaned:
            orphaned.append(page_key)
    active_page_keys = set(market_to_page_key.values())
    return [pk for pk in orphaned if pk not in active_page_keys]


class OrbitExchDataClient(LiveMarketDataClient):
    def __init__(
        self,
        loop,
        browser_manager,
        msgbus,
        cache,
        clock,
        instrument_provider,
        config: OrbitExchDataClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(ORBITEXCH),
            venue=Venue(ORBITEXCH),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            config=config,
        )
        self._config = config
        self._browser_manager = browser_manager  # 共享单例,仅消费(Q2)
        self._parser = OrbitExchMessageParser()
        # #68:每 competition 一页(key=f"{sport_id}_{competition_id}"),新开/刷新统一。
        self._comp_pages: dict[str, object] = {}                          # page_key -> Page
        self._comp_handlers: dict[str, OrbitExchWebSocketHandler] = {}    # page_key -> WS handler
        self._comp_pages_lock = asyncio.Lock()                            # 防并发订阅双开同一 competition
        # 路由:market_id(str) -> {selection_id(str): InstrumentId}(全局,跨所有 competition 页)
        # #228:同一 (market, selection) 可路由到多条 instrument(yes + 合成 no),值为 (iid, claim) 列表
        self._market_to_instruments: dict[str, dict[str, list[tuple[InstrumentId, str]]]] = {}
        # market_id(str) -> page_key:`_delayed_reopen` 据此判断页是否仍被订阅(决定是否继续重试开页)
        self._market_to_page_key: dict[str, str] = {}
        self._price_frames_seen = 0
        self._price_deltas_published = 0
        # #58(slice A):DataClient 拥有周期发现(替代退役的 InstrumentRefresher)
        self._update_instruments_task: Optional[asyncio.Task] = None
        # #105 赔率页 WS 存活:competition 页**帧存活锚**(page_key -> 最近**任一** WS 帧 ts,含 SockJS 心跳)。
        # 健康检查据此判 WS 死活,而非赔率更新时刻 —— 安静市场(无赔率变化但心跳在)不误 reload,
        # 死 WS(连心跳都停)仍在 staleness_timeout 内被捕获。对齐执行页 A1(`_last_frame_ns`/`_exec_ws_fresh`)。
        # #109:competition 页存活封装进 WS handler(内部心跳超时 + close → on_disconnect)。DataClient 只收事件 reload。
        # `_comp_reloading` 防 reload 期间 disconnect 自触发死循环;`_comp_last_reload_ns` 冷却;`_disconnecting` 关停防触发。
        self._comp_reloading: set[str] = set()
        self._comp_last_reload_ns: dict[str, int] = {}
        self._disconnecting = False

    # ── 生命周期(live;共享 browser,但 start 防御性 idempotent)──────────
    async def _connect(self) -> None:
        # #68:不再开 `inplay/highlights` 单页 / 不再起单一 WS handler。competition 页按订阅惰性开
        # (`_subscribe_order_book_deltas`→`_ensure_competition_page`,eager)。`start()` 幂等(共享单例)。
        await self._browser_manager.start()

        # #58(slice A):DataClient 拥有发现 —— 首次抓全 + 周期重抓(替代退役的 InstrumentRefresher)。
        # 直调 load_all_async(不走 initialize 的 load_all config 闸,Gap α);灌 cache 经 _handle_data→DataEngine。
        # 首轮失败不杀 DataClient(与 SE 对齐):discovery 等 exec 登录写入的 CSRF,
        # 启动期可能等不到,warning 后交周期任务下一轮。
        try:
            await self._instrument_provider.load_all_async()
            self._send_all_instruments_to_data_engine()
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                f"OE initial instruments load failed: {exc!r}; retrying next cycle",
            )
        if self._config.update_instruments_interval_mins:
            self._update_instruments_task = self.create_task(
                self._update_instruments(self._config.update_instruments_interval_mins),
            )

        # #109:OE DataClient 不再有 HealthCheckLoop / 周期 scan —— competition 页存活封装进 WS handler
        # (内部心跳超时 + close → on_disconnect),DataClient 只收事件 → reload,对称 PM `_schedule_delayed_connect`。
        self._log.info("OrbitExchDataClient connected (competition pages opened on subscribe; event-driven WS liveness)")

    async def _disconnect(self) -> None:
        self._disconnecting = True  # #108/#109:关停时 WS close / 心跳超时不触发 reload、连接重试放弃
        if self._update_instruments_task:
            self._update_instruments_task.cancel()
            self._update_instruments_task = None
        for handler in self._comp_handlers.values():
            await handler.stop()
        self._comp_handlers.clear()
        self._comp_pages.clear()
        # **不**关 browser(共享单例;关停由 factory 层)

    # ── 周期发现(#58 slice A:DataClient-owned,NT 原生 _update_instruments 范式)──
    def _send_all_instruments_to_data_engine(self) -> None:
        """provider 已加载的 instrument 经 _handle_data → DataEngine → cache + on_instrument。"""
        for instrument in self._instrument_provider.get_all().values():
            self._handle_data(instrument)

    async def _update_instruments(self, interval_mins: int) -> None:
        try:
            while True:
                await asyncio.sleep(interval_mins * 60)
                try:
                    await self._instrument_provider.load_all_async()
                    self._send_all_instruments_to_data_engine()
                except Exception as e:
                    self._log.warning(
                        f"OE update_instruments failed: {e!r}; retrying next cycle",
                    )
        except asyncio.CancelledError:
            self._log.debug("Canceled task 'update_instruments'")

    # ── NT type-specific subscribe 钩子 ──────────────────────────────
    async def _subscribe_order_book_deltas(self, command) -> None:
        """#68:注册路由 + **eager 开 competition 页**(订阅即开 → 价格 WS 流起,供 strategy 评估)。
        page_key = `{sport_id}_{competition_id}`(同 competition 多腿共用一页)。"""
        self._register_instrument_routing(command.instrument_id)
        await self._ensure_competition_page(command.instrument_id)

    async def _unsubscribe_order_book_deltas(self, command) -> None:
        # #251:退订对称关页 —— 该 competition 页无任何剩余 market 订阅时关闭
        # (#68"保持打开"废除;#250 eviction 驱动真实退订后,空页 = Chromium tab + 价格 WS 泄漏)。
        for page_key in self._unregister_instrument_routing(command.instrument_id):
            await self._close_competition_page(page_key)

    async def _close_competition_page(self, page_key: str) -> None:
        """关闭失去全部 market 订阅的 competition 页。**整个动作持 `_comp_pages_lock`**:
        摘表使 `_on_comp_disconnect` no-op(不触发 reload);关闭完成前锁住同 competition
        的再订阅开页,防"摘表后-关闭前"窗口内同名新页与旧页关闭交错(#251)。"""
        async with self._comp_pages_lock:
            page = self._comp_pages.pop(page_key, None)
            handler = self._comp_handlers.pop(page_key, None)
            if page is None and handler is None:
                return
            if handler is not None:
                with suppress(Exception):
                    await handler.stop()
            with suppress(Exception):
                await self._browser_manager.close_page(f"comp-{page_key}")
        self._log.info(f"OE competition page closed (no remaining subscriptions): {page_key}")

    async def _discard_dead_competition_page(self, page_key: str) -> None:
        """摘掉已死(tab 崩/被关)的 competition 页,使 `_open_or_reload_competition_page`
        能降级到新建分支。**调用方必须已持 `_comp_pages_lock`**(与 `_close_competition_page`
        自持锁不同,本方法在开/刷新路径内部调用)。`close_page` 先 pop 注册表再 close,
        因此即使 close 抛异常,`create_page` 下次也必定新建而不是取回死 page。"""
        self._comp_pages.pop(page_key, None)
        handler = self._comp_handlers.pop(page_key, None)
        if handler is not None:
            with suppress(Exception):
                await handler.stop()
        with suppress(Exception):
            await self._browser_manager.close_page(f"comp-{page_key}")

    # ── #68 每 competition 一页:新开/刷新统一 ────────────────────────
    async def _ensure_competition_page(self, instrument_id: InstrumentId) -> None:
        """确保该 instrument 所属 competition 页已开(去重);未开则 open。"""
        inst = self._cache.instrument(instrument_id)
        if inst is None:
            return  # routing 注册时已 warn
        sport_id = str(getattr(inst, "event_type_id", "") or "")
        competition_id = str(getattr(inst, "competition_id", "") or "")
        if not sport_id or not competition_id:
            self._log.warning(
                f"OE open page: {instrument_id} missing sport_id/competition_id; skip")
            return
        page_key = f"{sport_id}_{competition_id}"
        async with self._comp_pages_lock:
            if page_key in self._comp_pages:
                return  # 已开,复用
            try:
                await self._open_or_reload_competition_page(page_key, sport_id, competition_id)
            except Exception as e:  # noqa: BLE001 — #109:开页失败 → 事件化延迟重试(对齐 PM _delayed_connect)
                self._log.warning(
                    f"OE open competition {page_key} failed: {e!r}; retry in {_COMP_REOPEN_RETRY_SECS}s")
                self._loop.create_task(self._delayed_reopen(page_key, sport_id, competition_id))

    async def _delayed_reopen(self, page_key: str, sport_id: str, competition_id: str) -> None:
        """#109:开页失败后延迟重试,直到成功 / 关停 / 解订(对齐 PM `_delayed_connect`)。不再靠 health tick 补开。"""
        await asyncio.sleep(_COMP_REOPEN_RETRY_SECS)
        if self._disconnecting or page_key not in set(self._market_to_page_key.values()):
            return  # 关停 / 已解订 → 放弃重试
        async with self._comp_pages_lock:
            if page_key in self._comp_pages:
                return  # 期间已开
            try:
                self._log.info(f"OE reopening missing competition page {page_key}")
                await self._open_or_reload_competition_page(page_key, sport_id, competition_id)
            except Exception as e:  # noqa: BLE001 — 仍失败 → 再排一次,直到成功
                self._log.warning(
                    f"OE reopen competition {page_key} failed: {e!r}; retry in {_COMP_REOPEN_RETRY_SECS}s")
                self._loop.create_task(self._delayed_reopen(page_key, sport_id, competition_id))

    async def _open_or_reload_competition_page(
        self, page_key: str, sport_id: str, competition_id: str,
    ) -> None:
        """**新开/刷新统一入口**(#68;对齐老 odds_client `_open_or_reload_page`)。
        - page 不存在 → `create_page` + 挂 WS 监听(#67 先挂后 goto)+ goto
        - 已存在但**已死**(`is_closed()`)→ 摘账关页,降级走新建分支(见下)
        - 已存在且活着 → `reload`(page-level 监听跨 reload 自动存活,#67 实测,无需重挂)

        §4.3 健康检查 reload 将来复用本方法的 reload 分支。"""
        url = f"{self._config.base_url}/customer/sport/{sport_id}/competition/{competition_id}"
        # #68:competition 页加载重(走代理 + 页面 JS 建价格 WS 握手),用配置 page_timeout(默认 120s)。
        # 使用 domcontentloaded(不用 networkidle):OE prices WS 长连会让 networkidle 不稳定或超时。
        timeout_ms = self._config.page_timeout
        page = self._comp_pages.get(page_key)
        if page is not None and page.is_closed():
            # 死页逃生口:reload 对已死 page 永不可能成功,而死 page 占着 `_comp_pages` 会让
            # 本方法永远进不了新建分支 → liveness_timeout 每轮 reload 报错、盘口静默停更且
            # venue 仍 alive(data 侧不接 VenueExecutionLiveness)。摘账后 fallthrough 新建。
            self._log.warning(f"OE competition page {page_key} is closed; discarding and reopening")
            await self._discard_dead_competition_page(page_key)
            page = None
        if page is None:
            page_name = f"comp-{page_key}"
            page = await self._browser_manager.create_page(page_name)
            # #109:handler 自带存活封装(内部心跳超时 + close → on_disconnect),timeout=staleness_timeout_secs。
            handler = OrbitExchWebSocketHandler(
                page, logger=self._log,
                clock=self._clock,
                liveness_timeout_secs=self._config.staleness_timeout_secs,
                liveness_name=f"comp_ws_liveness:{page_key}",
                liveness_ws_type="prices",
            )
            handler.on_price_update(self._on_price_frame)
            handler.on_disconnect(lambda reason, pk=page_key: self._on_comp_disconnect(pk, reason))  # #109:断开 → reload
            try:
                await handler.start()                   # #67:先挂监听
                await page.bring_to_front()             # #87:OE prices socket 受页面/market 可见性影响
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)   # 再导航(价格 WS 此时建,被抓)
            except Exception:
                with suppress(Exception):
                    await handler.stop()
                with suppress(Exception):
                    await self._browser_manager.close_page(page_name)
                raise
            self._comp_pages[page_key] = page
            self._comp_handlers[page_key] = handler
            self._log.info(f"OE competition page opened: {page_key} ({self._websocket_summary(handler)})")
        else:
            handler = self._comp_handlers.get(page_key)
            await page.bring_to_front()
            await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
            summary = self._websocket_summary(handler) if handler is not None else "ws_count=unknown"
            self._log.info(f"OE competition page reloaded: {page_key} ({summary})")

    @staticmethod
    def _websocket_summary(handler: OrbitExchWebSocketHandler) -> str:
        active = handler.get_active_websockets()
        counts: dict[str, int] = {}
        for item in active:
            ws_type = str(item.get("type", "unknown"))
            counts[ws_type] = counts.get(ws_type, 0) + 1
        return f"ws_count={len(active)}, ws_types={counts}"

    def _register_instrument_routing(self, instrument_id: InstrumentId) -> None:
        # #251:状态机抽为模块级纯函数(对齐 SE);client 只留 cache 读取与 warning。
        inst = self._cache.instrument(instrument_id)
        if inst is None:
            self._log.warning(f"OE subscribe: instrument {instrument_id} not in cache; skip")
            return
        plan = oe_subscription_plan_from_instrument(inst)
        if plan is None:
            self._log.warning(f"OE subscribe: {instrument_id} missing market_id/selection_id; skip")
            return
        oe_update_subscription_state(
            market_routing=self._market_to_instruments,
            market_to_page_key=self._market_to_page_key,
            instrument_id=instrument_id,
            plan=plan,
        )

    def _unregister_instrument_routing(self, instrument_id: InstrumentId) -> list[str]:
        return oe_remove_subscription_state(
            market_routing=self._market_to_instruments,
            market_to_page_key=self._market_to_page_key,
            instrument_id=instrument_id,
        )

    # ── #109:WS handler on_disconnect(close 或心跳超时)→ reload(对称 PM `_schedule_delayed_connect`)──
    def _on_comp_disconnect(self, page_key: str, reason: str) -> None:
        """handler.on_disconnect 同步回调。`reason`:`"liveness_timeout"`(心跳停=静默死亡)或 `"close:<ws_type>"`。
        只对 **prices 关闭 / 心跳超时** reload(其它 ws 关闭忽略);三重防护(冷却 + reload-in-flight + disconnecting)防风暴/自触发/关停误触。"""
        if reason != "liveness_timeout" and reason != "close:prices":
            return  # 只关心赔率 feed 的存活(其它 ws close 忽略)
        if self._disconnecting or page_key in self._comp_reloading:
            return  # 关停中 / 本页正在 reload(自身 reload 关旧 WS 会再触发 disconnect)
        if page_key not in self._comp_pages:
            return  # 页已解订/未开,交事件化 connect-retry(`_delayed_reopen`)处理
        now = self._clock.timestamp_ns()
        last = self._comp_last_reload_ns.get(page_key, 0)
        if last and (now - last) < secs_to_nanos(_COMP_RELOAD_COOLDOWN_SECS):
            return  # 冷却窗内(venue 持续不可用)→ 跳过,防风暴
        self._comp_last_reload_ns[page_key] = now
        self._log.info(f"OE competition page {page_key} disconnect ({reason}) → reload (#109)")
        self._loop.create_task(self._reload_comp_on_disconnect(page_key))

    async def _reload_comp_on_disconnect(self, page_key: str) -> None:
        self._comp_reloading.add(page_key)
        try:
            sport_id, _, competition_id = page_key.partition("_")
            async with self._comp_pages_lock:
                if page_key in self._comp_pages and not self._disconnecting:
                    await self._open_or_reload_competition_page(page_key, sport_id, competition_id)
        except Exception as e:  # noqa: BLE001 — reload 失败不抛;下次 disconnect(close/心跳超时)兜底重试
            self._log.error(f"OE competition page {page_key} disconnect-driven reload failed: {e}")
        finally:
            self._comp_reloading.discard(page_key)

    # ── WS 帧回调 → OrderBookDeltas → DataEngine ─────────────────────
    def _on_price_frame(self, message) -> None:
        parsed = self._parser.parse_price_message(message)
        if not parsed:
            return
        market_id = str(parsed.get("market_id", ""))
        routing = self._market_to_instruments.get(market_id)
        if not routing:
            return  # 未订阅此 market,丢弃
        self._price_frames_seen += 1
        if self._price_frames_seen == 1:
            self._log.info(
                f"OE price frame routed: market_id={market_id}, runners={len(parsed.get('runners', []))}, "
                f"subscribed_selections={len(routing)}",
            )
        ts = self._clock.timestamp_ns()
        # #109:WS 存活由 handler 内部封装(心跳超时 + close → on_disconnect),此处不写任何存活锚。
        # #250:instrument.info["in_play"] 写回已废除 —— 比赛状态归 PMSPORTS sports 管线
        # (snapshot.sports_state),赔率帧只产出 OrderBookDeltas。
        for runner in parsed.get("runners", []):
            sel_id = str(runner.get("selection_id", ""))
            # #228:同一 selection 可有 yes + 合成 no 两条 instrument,同帧各发一份 deltas
            for instrument_id, claim in routing.get(sel_id, []):
                deltas = oe_runner_to_book_deltas(instrument_id, runner, ts, claim=claim)
                if deltas is not None:
                    self._price_deltas_published += 1
                    if self._price_deltas_published == 1:
                        self._log.info(
                            f"OE OrderBookDeltas published: instrument_id={instrument_id}, "
                            f"deltas={len(deltas.deltas)}",
                        )
                    self._handle_data(deltas)
