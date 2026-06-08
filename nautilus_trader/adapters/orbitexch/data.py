# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------

"""
OrbitExchDataClient —— OE 自写 `LiveMarketDataClient` 子类(Step 2,整体重写自半成品 data.py)。

设计见 `docs/arbitrage/architectures/data/architecture.md`(refactor.md §5.2/§6.2,Q2)。

= 自写 `LiveMarketDataClient` 子类:WS 价格帧(`multiple-market-prices`)→ NT 标准
`OrderBookDeltas`(snapshot 一次 CLEAR + 多 ADD 入 NT 标准管道)。BACK=BUY 侧 / LAY=SELL 侧。

**位置**:`nautilus_trader/adapters/orbitexch/`(P9 唯一例外:OE 适配器整套住 adapter 目录;#33)。
**BrowserManager 仅消费**(Q2 共享单例;不再 start/close):`get_page("data")` 拿专属页。
**OE 健康检查**(宿主 = 本类,见 execution §4.3):`_connect` 挂 `HealthCheckLoop`。
Phase 1(已落地)= 时间维度——competition 页赔率冻结超阈值 → reload(对齐 PM 行情 WS 重连)。
Phase 2(代码已接,安全闸 `health_check_exec_reload_enabled` 默认关,待真单 live 验)= 状态维度——
`leg_settled` 有未结算腿 → 经共享 browser_manager reload execution 页(A 方案)。

**离线可测**:`oe_runner_to_book_deltas` 纯映射(WS runner → OrderBookDeltas)。
**live 验**:`_connect` / WS 接帧 / 订阅 routing(集成路径多变,/live-test 验)。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Optional

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

from src.arbitrage.execution.health_check import HealthCheckLoop


ORBITEXCH = "ORBITEXCH"


def oe_runner_to_book_deltas(
    instrument_id: InstrumentId,
    runner: dict,
    ts_init_ns: int,
    *,
    price_precision: int = 2,
    size_precision: int = 2,
) -> Optional[OrderBookDeltas]:
    """OE WS 一个 runner 的 back/lay 快照 → `OrderBookDeltas`(CLEAR + N×ADD)。

    WS 给的是**snapshot of best N levels**(非增量),故每帧:
    - 1 个 `CLEAR`(刷掉旧本子)
    - 每 back 档:`ADD(BUY)`(BACK = 买入该方向)
    - 每 lay 档: `ADD(SELL)`(LAY = 卖出该方向)
    包成 `OrderBookDeltas`(NT 标准 batch)入 DataEngine。

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
    for level in back:
        price = level.get("price")
        size = level.get("size")
        if not price or not size or float(size) <= 0:
            continue
        deltas.append(_make_add(instrument_id, OrderSide.BUY, price, size, order_id, ts_init_ns, price_precision, size_precision))
        order_id += 1
    for level in lay:
        price = level.get("price")
        size = level.get("size")
        if not price or not size or float(size) <= 0:
            continue
        deltas.append(_make_add(instrument_id, OrderSide.SELL, price, size, order_id, ts_init_ns, price_precision, size_precision))
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
        leg_settled=None,
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
        self._leg_settled = leg_settled  # §6.8.3 状态维度(Phase 2);None → 只跑时间维度
        self._browser_manager = browser_manager  # 共享单例,仅消费(Q2)
        self._parser = OrbitExchMessageParser()
        # #68:每 competition 一页(key=f"{sport_id}_{competition_id}"),新开/刷新统一。
        self._comp_pages: dict[str, object] = {}                          # page_key -> Page
        self._comp_handlers: dict[str, OrbitExchWebSocketHandler] = {}    # page_key -> WS handler
        self._comp_pages_lock = asyncio.Lock()                            # 防并发订阅双开同一 competition
        # 路由:market_id(str) -> {selection_id(str): InstrumentId}(全局,跨所有 competition 页)
        self._market_to_instruments: dict[str, dict[str, InstrumentId]] = {}
        # market_id(str) -> page_key:_on_price_frame 廉价定位帧所属 competition 页(§6.8.3 时间维度)
        self._market_to_page_key: dict[str, str] = {}
        self._price_frames_seen = 0
        self._price_deltas_published = 0
        # #58(slice A):DataClient 拥有周期发现(替代退役的 InstrumentRefresher)
        self._update_instruments_task: Optional[asyncio.Task] = None
        # §6.8.3 健康检查(Phase 1 时间维度:competition 页赔率防冻 reload)。
        self._comp_last_update_ns: dict[str, int] = {}   # page_key -> 最近收帧 ts(收帧只写,廉价)
        self._exec_active_count = 0                       # Q19 互斥:execution.* msgbus ref-count
        self._health: Optional[HealthCheckLoop] = None

    # ── 生命周期(live;共享 browser,但 start 防御性 idempotent)──────────
    async def _connect(self) -> None:
        # #68:不再开 `inplay/highlights` 单页 / 不再起单一 WS handler。competition 页按订阅惰性开
        # (`_subscribe_order_book_deltas`→`_ensure_competition_page`,eager)。`start()` 幂等(共享单例)。
        await self._browser_manager.start()

        # #58(slice A):DataClient 拥有发现 —— 首次抓全 + 周期重抓(替代退役的 InstrumentRefresher)。
        # 发现用 provider 自己的 scraper 浏览器(#62 解耦),不依赖 competition 页。
        # 直调 load_all_async(不走 initialize 的 load_all config 闸,Gap α);灌 cache 经 _handle_data→DataEngine。
        await self._instrument_provider.load_all_async()
        self._send_all_instruments_to_data_engine()
        if self._config.update_instruments_interval_mins:
            self._update_instruments_task = self.create_task(
                self._update_instruments(self._config.update_instruments_interval_mins),
            )

        # §6.8.3 健康检查(宿主 = 本 DataClient)。Q19 互斥用 msgbus ref-count(DataClient ≠ ExecClient)。
        self._msgbus.subscribe(topic="execution.started", handler=self._on_execution_started)
        self._msgbus.subscribe(topic="execution.finished", handler=self._on_execution_finished)
        self._health = HealthCheckLoop(
            name=f"health_check:{ORBITEXCH}",
            clock=self._clock,
            msgbus=self._msgbus,
            loop=self._loop,
            log=self._log,
            interval_secs_provider=lambda: self._config.health_interval_secs,
            is_execution_active=self._is_execution_active,
            run_check=self._run_health_check,
        )
        self._health.start()
        self._log.info("OrbitExchDataClient connected (competition pages opened on subscribe; health check started)")

    async def _disconnect(self) -> None:
        if self._health is not None:
            self._health.stop()
            self._health = None
            self._msgbus.unsubscribe(topic="execution.started", handler=self._on_execution_started)
            self._msgbus.unsubscribe(topic="execution.finished", handler=self._on_execution_finished)
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
                await self._instrument_provider.load_all_async()
                self._send_all_instruments_to_data_engine()
        except asyncio.CancelledError:
            self._log.debug("Canceled task 'update_instruments'")

    # ── §6.8.3 健康检查(宿主 = DataClient;Phase 1 = 时间维度赔率防冻)──────
    # Q19 互斥(§6.10):DataClient 与 ExecClient 不同对象,故订 `execution.*` 自维护 ref-count。
    def _on_execution_started(self, msg) -> None:
        self._exec_active_count += 1

    def _on_execution_finished(self, msg) -> None:
        self._exec_active_count = max(0, self._exec_active_count - 1)

    def _is_execution_active(self) -> bool:
        return self._exec_active_count > 0

    async def _run_health_check(self) -> None:
        """健康检查 tick(execution 在飞时 `HealthCheckLoop` 已整 tick 让路,本方法不重复判)。

        **Phase 1 = 时间维度**:competition 页 `now-last_update_ns>staleness_timeout` → reload
        (赔率冻结兜底,对齐 PM 的行情 WS 重连)。reload 走 `_open_or_reload_competition_page`
        的 reload 分支(page-level 监听跨 reload 存活,#67)。
        **+ 连接重试维度**:已订阅但未开的 competition 页(初次 goto 失败 / 未开)→ 本 tick 补开;
        补开失败不本轮重试,留下一次健康检查(对齐 PM `_delayed_connect` 连接失败重排)。

        **Phase 2 = 状态维度**:`leg_settled` 有未结算腿 → reload execution 页(其 `CURRENT_BETS`
        WS 重推 → ExecClient `_on_current_bets` 标 leg_settled)。execution 页归 ExecClient,本宿主
        经共享 `browser_manager` 取页 reload(A 方案)。**安全闸** `health_check_exec_reload_enabled`
        默认 False:reload 已登录交易页的弹窗/会话行为待真单 live 验,验前不自动 reload。
        """
        now = self._clock.timestamp_ns()
        threshold_ns = secs_to_nanos(self._config.staleness_timeout_secs)
        # 时间维度:competition 页赔率防冻
        for page_key in list(self._comp_pages):
            last = self._comp_last_update_ns.get(page_key)
            if last is None:
                continue  # 刚开页未收帧,不算 stale(避免开页即 reload)
            if now - last <= threshold_ns:
                continue
            sport_id, _, competition_id = page_key.partition("_")
            self._log.info(
                f"OE health check: competition {page_key} stale "
                f"({(now - last) / 1e9:.1f}s > {self._config.staleness_timeout_secs}s); reloading",
            )
            async with self._comp_pages_lock:
                if page_key in self._comp_pages:  # 重查:可能并发解订/重开
                    await self._open_or_reload_competition_page(page_key, sport_id, competition_id)

        # 连接重试维度:已订阅(应开)但未开的 competition 页(初次 goto 失败 / 未开)→ 本 tick 补开。
        # 对齐 PM `_delayed_connect` 的"连接失败 → 重排重试"。**补开失败不本轮重试,留下一次健康检查**
        # (每页 try/except 吞掉,不打断其它页 / Phase 2;复用 loop "异常吞掉、下轮重排"节奏)。
        for page_key in set(self._market_to_page_key.values()) - set(self._comp_pages):
            sport_id, _, competition_id = page_key.partition("_")
            try:
                async with self._comp_pages_lock:
                    if page_key not in self._comp_pages:  # 重查:并发订阅可能已开
                        self._log.info(f"OE health check: reopening missing competition page {page_key}")
                        await self._open_or_reload_competition_page(page_key, sport_id, competition_id)
            except Exception as e:  # noqa: BLE001 — 单页补开失败不打断本 tick;留下次健康检查重试
                self._log.warning(
                    f"OE health check: reopen competition {page_key} failed: {e!r}; retry next tick")

        # 状态维度(Phase 2,安全闸默认关):leg_settled 有未结算腿 → reload execution 页
        if (
            self._leg_settled is not None
            and self._config.health_check_exec_reload_enabled
            and self._leg_settled.has_any_unsettled()
        ):
            await self._reload_execution_page()

    async def _reload_execution_page(self) -> None:
        """状态维度兜底:reload execution 页 → `CURRENT_BETS` WS 重推(ExecClient 标 leg_settled)。

        execution 页归 ExecClient(页名 `"execution"`),本宿主经共享 `browser_manager` 取页 reload
        (page-level 监听跨 reload 存活 #67;ExecClient 的 general WS handler 自动捕获重推帧)。"""
        exec_page = await self._browser_manager.get_page("execution")
        if exec_page is None:
            self._log.warning("OE health check: execution page not found; skip leg_settled reload")
            return
        self._log.info("OE health check: leg_settled has unsettled leg(s); reloading execution page")
        await exec_page.reload(wait_until="networkidle", timeout=self._config.page_timeout)

    # ── NT type-specific subscribe 钩子 ──────────────────────────────
    async def _subscribe_order_book_deltas(self, command) -> None:
        """#68:注册路由 + **eager 开 competition 页**(订阅即开 → 价格 WS 流起,供 strategy 评估)。
        page_key = `{sport_id}_{competition_id}`(同 competition 多腿共用一页)。"""
        self._register_instrument_routing(command.instrument_id)
        await self._ensure_competition_page(command.instrument_id)

    async def _unsubscribe_order_book_deltas(self, command) -> None:
        # #68:关页 = 保持打开(对齐老 odds_client;competition 数有界,空页成本可接受)。
        self._unregister_instrument_routing(command.instrument_id)

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
            await self._open_or_reload_competition_page(page_key, sport_id, competition_id)

    async def _open_or_reload_competition_page(
        self, page_key: str, sport_id: str, competition_id: str,
    ) -> None:
        """**新开/刷新统一入口**(#68;对齐老 odds_client `_open_or_reload_page`)。
        - page 不存在 → `create_page` + 挂 WS 监听(#67 先挂后 goto)+ goto
        - 已存在 → `reload`(page-level 监听跨 reload 自动存活,#67 实测,无需重挂)

        §4.3 健康检查 reload 将来复用本方法的 reload 分支。"""
        url = f"{self._config.base_url}/customer/sport/{sport_id}/competition/{competition_id}"
        # #68:competition 页加载重(走代理 + 页面 JS 建价格 WS 握手),用配置 page_timeout(默认 120s),
        # 对齐老 odds_client(`networkidle` + `timeout=page_load_timeout_sec*1000`);默认 30s 不够会超时。
        timeout_ms = self._config.page_timeout
        page = self._comp_pages.get(page_key)
        if page is None:
            page_name = f"comp-{page_key}"
            page = await self._browser_manager.create_page(page_name)
            handler = OrbitExchWebSocketHandler(page)
            handler.on_price_update(self._on_price_frame)
            try:
                await handler.start()                   # #67:先挂监听
                await page.goto(url, wait_until="networkidle", timeout=timeout_ms)   # 再导航(价格 WS 此时建,被抓)
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
            await page.reload(wait_until="networkidle", timeout=timeout_ms)
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
        inst = self._cache.instrument(instrument_id)
        if inst is None:
            self._log.warning(f"OE subscribe: instrument {instrument_id} not in cache; skip")
            return
        market_id = getattr(inst, "market_id", None)
        selection_id = getattr(inst, "selection_id", None)
        if market_id is None or selection_id is None:
            self._log.warning(f"OE subscribe: {instrument_id} missing market_id/selection_id; skip")
            return
        self._market_to_instruments.setdefault(str(market_id), {})[str(selection_id)] = instrument_id
        # market → page_key(§6.8.3 时间维度:_on_price_frame 廉价定位帧所属 competition 页)
        sport_id = str(getattr(inst, "event_type_id", "") or "")
        competition_id = str(getattr(inst, "competition_id", "") or "")
        if sport_id and competition_id:
            self._market_to_page_key[str(market_id)] = f"{sport_id}_{competition_id}"

    def _unregister_instrument_routing(self, instrument_id: InstrumentId) -> None:
        for sel_map in list(self._market_to_instruments.values()):
            stale = [k for k, v in sel_map.items() if v == instrument_id]
            for k in stale:
                del sel_map[k]

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
        # slice 9(#49):透 marketDefinition.inPlay 到 instrument.info["in_play"]
        in_play = bool(parsed.get("in_play", False))
        ts = self._clock.timestamp_ns()
        # §6.8.3 时间维度:收帧只写 last_update_ns(廉价),staleness 仅健康检查 tick 评估
        page_key = self._market_to_page_key.get(market_id)
        if page_key is not None:
            self._comp_last_update_ns[page_key] = ts
        for runner in parsed.get("runners", []):
            sel_id = str(runner.get("selection_id", ""))
            instrument_id = routing.get(sel_id)
            if instrument_id is None:
                continue
            write_inplay_to_instrument_info(self._cache, instrument_id, in_play)
            deltas = oe_runner_to_book_deltas(instrument_id, runner, ts)
            if deltas is not None:
                self._price_deltas_published += 1
                if self._price_deltas_published == 1:
                    self._log.info(
                        f"OE OrderBookDeltas published: instrument_id={instrument_id}, "
                        f"deltas={len(deltas.deltas)}",
                    )
                self._handle_data(deltas)


def write_inplay_to_instrument_info(cache, instrument_id, in_play: bool) -> None:
    """slice 9(#49)module-level helper:把 inplay 写到 `cache.instrument.info["in_play"]`。

    Strategy snapshot 派生 in_play 从这读(避走 SignalStore 二跳);PM-only 事件触发评估时
    仍能读到 OE 最近一次写入值(`instrument.info` 是 cache-resident mutable dict)。

    冷启动 / 路由表跟 cache 不同步(instrument 还没进 cache)→ 跳过 inplay 写,不 raise。
    """
    inst = cache.instrument(instrument_id)
    if inst is not None and getattr(inst, "info", None) is not None:
        inst.info["in_play"] = in_play
