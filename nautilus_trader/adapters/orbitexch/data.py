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
**OE 健康检查**(页面 reload + leg_settled reconcile,见 execution §4.3):本类是宿主候选,
拟挂 `HealthCheckLoop` —— 暂留 seam,/live-test 验。

**离线可测**:`oe_runner_to_book_deltas` 纯映射(WS runner → OrderBookDeltas)。
**live 验**:`_connect` / WS 接帧 / 订阅 routing(集成路径多变,/live-test 验)。
"""

from __future__ import annotations

import asyncio
from typing import Optional

from nautilus_trader.adapters.orbitexch.config import OrbitExchDataClientConfig
from nautilus_trader.adapters.orbitexch.message_parser import OrbitExchMessageParser
from nautilus_trader.adapters.orbitexch.websocket_handler import OrbitExchWebSocketHandler
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
        self._page = None
        self._ws_handler: Optional[OrbitExchWebSocketHandler] = None
        # 路由:market_id(str) -> {selection_id(str): InstrumentId}
        self._market_to_instruments: dict[str, dict[str, InstrumentId]] = {}
        # #58(slice A):DataClient 拥有周期发现(替代退役的 InstrumentRefresher)
        self._update_instruments_task: Optional[asyncio.Task] = None

    # ── 生命周期(live;共享 browser,但 start 防御性 idempotent)──────────
    async def _connect(self) -> None:
        # slice 10c smoke 修正:原设计依赖 factory 层先调 `start()`,实际未接;
        # `start()` 已加幂等(BrowserManager 内 `_context is not None` 早返),DataClient 自管更稳。
        # `get_page` 是只读;**首次连必须 `create_page`**(原代码 bug:用 get_page → None)。
        await self._browser_manager.start()
        self._page = await self._browser_manager.create_page("data")
        await self._page.goto(f"{self._config.base_url}/customer/inplay/highlights")
        self._ws_handler = OrbitExchWebSocketHandler(self._page)
        self._ws_handler.on_price_update(self._on_price_frame)
        await self._ws_handler.start()
        self._log.info(f"OrbitExchDataClient connected; pages={self._page is not None}")

        # #58(slice A):DataClient 拥有发现 —— 首次抓全 + 周期重抓(替代退役的 InstrumentRefresher)。
        # 直调 load_all_async(不走 initialize 的 load_all config 闸,Gap α);灌 cache 经 _handle_data→DataEngine。
        await self._instrument_provider.load_all_async()
        self._send_all_instruments_to_data_engine()
        if self._config.update_instruments_interval_mins:
            self._update_instruments_task = self.create_task(
                self._update_instruments(self._config.update_instruments_interval_mins),
            )

    async def _disconnect(self) -> None:
        if self._update_instruments_task:
            self._update_instruments_task.cancel()
            self._update_instruments_task = None
        if self._ws_handler is not None:
            await self._ws_handler.stop()
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

    # ── NT type-specific subscribe 钩子 ──────────────────────────────
    async def _subscribe_order_book_deltas(self, command) -> None:
        """注册 instrument → 路由表;WS 推到该 market 时 publish 对应 OrderBookDeltas。"""
        self._register_instrument_routing(command.instrument_id)

    async def _unsubscribe_order_book_deltas(self, command) -> None:
        self._unregister_instrument_routing(command.instrument_id)

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
        # slice 9(#49):透 marketDefinition.inPlay 到 instrument.info["in_play"]
        in_play = bool(parsed.get("in_play", False))
        ts = self._clock.timestamp_ns()
        for runner in parsed.get("runners", []):
            sel_id = str(runner.get("selection_id", ""))
            instrument_id = routing.get(sel_id)
            if instrument_id is None:
                continue
            write_inplay_to_instrument_info(self._cache, instrument_id, in_play)
            deltas = oe_runner_to_book_deltas(instrument_id, runner, ts)
            if deltas is not None:
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
