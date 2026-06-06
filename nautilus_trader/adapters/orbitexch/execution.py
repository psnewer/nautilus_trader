"""
OrbitExchExecutionClient —— OE 自写执行客户端(§3.2)。

详细设计:`docs/arbitrage/architectures/execution/architecture.md §3.2 / §4.5`。

= 自写 `LiveExecutionClient` + `ArbExecutionSessionMixin`(session / leg_settled / 超时 / 同步)。
订单 IO 包同目录 `executor.place_order/cancel_order`(Playwright);账户状态走 `general` 频道
`BALANCE` 帧 → `generate_account_state`(WS 已含挂单占用,Q17)。

**位置(refactor.md #33 校准)**:OE 是自写适配器,所有 venue-coupled 件
(browser_manager/executor/scraper/data/websocket_handler/message_parser/providers)均住
`nautilus_trader/adapters/orbitexch/`(P9 唯一例外);本文件与它们同目录。

**OE 健康检查不在本客户端**:页面 reload 机制宿主是 `OrbitExchDataClient`(它持 page),
本客户端只做订单 + 账户状态(§4.3)。

**离线可测**:`oe_balance_to_account_balances` 纯映射、`_on_general_frame` 路由、`_modify_order`
拒绝、`_submit_order` session 门控(注入 fake `_place_via_executor`)。
**live 待验**:真 browser/executor 构造、NT Order→executor 旧 Order 翻译、`CURRENT_BETS` 单 bet
item→`generate_order_*`/report(item schema 待 populated 抓帧)、reports。
"""

from __future__ import annotations

from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.currencies import GBP
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import AccountBalance
from nautilus_trader.model.objects import Money

from nautilus_trader.adapters.orbitexch.message_parser import OrbitExchMessageParser

from src.arbitrage.common.leg_settled import LegSettledRegistry
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.execution.session import ArbExecutionSessionMixin

ORBITEXCH = "ORBITEXCH"


def oe_balance_to_account_balances(balance: float, currency=GBP) -> list:
    """OE `BALANCE` 帧 → AccountBalance(纯映射,可单测)。

    WS 余额已含挂单占用(Q17)→ 即可用:total = free = balance,locked = 0
    (RiskEngine OE 分支直接信 free,不再减)。"""
    money = Money(balance, currency)
    return [AccountBalance(money, Money(0, currency), money)]


def _coerce_handicap(value) -> float:
    """OE `selection_handicap` → float;`null_handicap`(NT sentinel -9999999.0)/NaN/None → 0.0
    (match-odds 无 handicap;executor `bet_uuid` 用 `int(handicap)`)。"""
    from nautilus_trader.model.instruments.betting import null_handicap

    try:
        h = float(value)
    except (TypeError, ValueError):
        return 0.0
    if h != h or h == float(null_handicap()):   # NaN 或 NT 无-handicap sentinel
        return 0.0
    return h


def nt_order_to_legacy_order(nt_order, inst):
    """Gap C(#63)纯映射:NT `Order` + OE `BettingInstrument` → executor 旧 `Order`。

    - `market_id`/`selection_id`/`handicap` 取自 OE instrument 属性(缺 → None,调用方判失败)
    - side:NT `BUY`→`BACK` / `SELL`→`LAY`(OE)
    - `price`=NT order price(已是 OE 赔率格式)/ `size`=NT qty / `order_type`=GTC(POC)
    可单测(无 Playwright);真 `executor.place_order` 由 `_place_via_executor` 调(仅非 skip)。
    """
    from nautilus_trader.model.enums import OrderSide as _NTOrderSide

    from src.arbitrage.common.order_models import Order as _Order
    from src.arbitrage.common.order_models import OrderSide as _OrderSide
    from src.arbitrage.common.order_models import OrderType as _OrderType
    from src.arbitrage.common.order_models import Venue as _Venue

    market_id = str(getattr(inst, "market_id", "") or "")
    selection_id = str(getattr(inst, "selection_id", "") or "")
    if not market_id or not selection_id:
        return None
    side = _OrderSide.BACK if nt_order.side == _NTOrderSide.BUY else _OrderSide.LAY
    return _Order(
        venue=_Venue.ORBITEXCH,
        market_id=market_id,
        selection_id=selection_id,
        handicap=_coerce_handicap(getattr(inst, "selection_handicap", None)),
        side=side,
        price=float(nt_order.price),
        size=float(nt_order.quantity),
        order_type=_OrderType.GTC,
    )


def current_bets_to_fills(bets, prev_matched: dict) -> list:
    """OE `CURRENT_BETS` 快照(**非增量**)→ 新增成交意图列表(纯函数,可单测,无 NT 依赖)。

    设计见 `architectures/execution/architecture.md §3.2`。字段语义复用 odds_client(单一真理源):
    - join key:`offerId`(== NT order 的 `venue_order_id`,executor 下单时从响应 `offerIds` 写入)。
    - `sizeMatched` 是**累积**已成交量 → 本次新增 `delta = sizeMatched − prev_matched[offerId]`。
    - `averagePrice` = 成交均价(unmatched 时 0)。

    仅当 `delta > 0 且 averagePrice > 0` 才产出一条成交意图;调用方据此发 `generate_order_filled`
    并把 `size_matched` 写回 `prev_matched`。返回 `list[dict]`:
    `{"offer_id","delta_qty","avg_price","size_matched"}`。

    **实测确认**(2026-06-06 抓帧):unmatched item = `{offerId, selectionId, averagePrice:0,
    profitNet, liability}`(+ 派生用的 `marketId`/`sizeRemaining`/`sizeMatched`)。**matched 态填充值
    待真成交确认**([[gap_c_oe_exec_live_validated]])。
    """
    fills = []
    for bet in bets:
        offer_id = str(bet.get("offerId", "") or "")
        if not offer_id:
            continue
        size_matched = float(bet.get("sizeMatched", 0) or 0)
        prev = float(prev_matched.get(offer_id, 0.0))
        delta = size_matched - prev
        avg_price = float(bet.get("averagePrice", 0) or 0)
        if delta > 0 and avg_price > 0:
            fills.append({
                "offer_id": offer_id,
                "delta_qty": delta,
                "avg_price": avg_price,
                "size_matched": size_matched,
            })
    return fills


def bet_order_progress(bet) -> dict | None:
    """OE `CURRENT_BETS` 单 bet → 订单进度(纯函数,可单测,无 NT 依赖)。供 reconcile reports 用。

    **bet 真实 schema**(权威来源 = 老 `orchestrator`/`tracker`/`odds_client` 实读字段,非精选 debug log):
    `offerId`/`marketId`/`selectionId`/`side`(BACK/LAY)/`sizePlaced`(原始量)/`sizeMatched`/
    `sizeRemaining`/`averagePrice`/`price`/`placedDate`/`profitNet`/`liability`。

    全部从 bet 自身派生(**含 `side`/`sizePlaced`,无需反查 NT order** → 外部/重启单也能 reconcile):
    - `original_qty`:优先 `sizePlaced`,缺则 `sizeMatched+sizeRemaining` 兜底
    - `status`:remaining>0&matched>0→`"partially_filled"` / 仅 remaining→`"accepted"` /
      仅 matched→`"filled"` / 都 0→`"unknown"`(调用方跳过)

    缺 offerId → None。
    """
    offer_id = str(bet.get("offerId", "") or "")
    if not offer_id:
        return None
    size_matched = float(bet.get("sizeMatched", 0) or 0)
    size_remaining = float(bet.get("sizeRemaining", 0) or 0)
    size_placed = bet.get("sizePlaced")
    original_qty = (
        float(size_placed) if size_placed not in (None, "")
        else size_matched + size_remaining
    )
    if size_remaining > 0 and size_matched > 0:
        status = "partially_filled"
    elif size_remaining > 0:
        status = "accepted"
    elif size_matched > 0:
        status = "filled"
    else:
        status = "unknown"
    return {
        "offer_id": offer_id,
        "market_id": str(bet.get("marketId", "") or ""),
        "selection_id": str(bet.get("selectionId", "") or ""),
        "side": str(bet.get("side", "") or "").upper(),   # "BACK"/"LAY"/""
        "original_qty": original_qty,
        "filled_qty": size_matched,
        "avg_px": float(bet.get("averagePrice", 0) or 0),
        "price": float(bet.get("price", 0) or 0),
        "status": status,
    }


class OrbitExchExecutionClient(ArbExecutionSessionMixin, LiveExecutionClient):
    def __init__(
        self,
        loop,
        browser_manager,
        msgbus,
        cache,
        clock,
        instrument_provider,
        config,
        *,
        leg_settled: LegSettledRegistry,
        pair_registry: PairRegistry | None = None,
        session_timeout_secs: float = 30.0,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(ORBITEXCH),
            venue=Venue(ORBITEXCH),
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            base_currency=GBP,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._init_arb_session(
            leg_settled=leg_settled,
            session_timeout_secs=session_timeout_secs,
            pair_registry=pair_registry,
        )
        self._set_account_id(AccountId(f"{ORBITEXCH}-001"))
        self._browser_manager = browser_manager
        self._config = config
        self._parser = OrbitExchMessageParser()
        self._executor = None
        self._page = None
        self._ws_handler = None
        self._bet_matched: dict = {}    # offerId → 累积 sizeMatched(CURRENT_BETS 快照算 delta)
        self._bet_fill_seq: dict = {}   # offerId → 成交序号(trade_id 唯一)
        self._current_bets: dict = {}   # offerId → 最新 bet(快照,供 reconcile reports)

    async def _connect(self) -> None:
        """Gap C(#63):真接线 —— 共享 BM 取 `"execution"` page(#62:data/exec 同登录 context)→
        登录(持久化 profile 已登录则跳过)→ 构 `OrbitExchExecutor` → general WS(余额/订单)→ 初始
        account state。**仅非 skip 触达**(SkipExecution `_connect` no-op)。**/live-test 验**(真登录,
        无法单测);订单回执(WS 帧 → `generate_order_*`)待 live 抓 frame schema 补全(`_on_general_frame`)。"""
        from nautilus_trader.adapters.orbitexch.executor import OrbitExchExecutor
        from nautilus_trader.adapters.orbitexch.websocket_handler import OrbitExchWebSocketHandler

        from src.arbitrage.common.execution_config import ExecutionConfig

        await self._browser_manager.start()  # 幂等(#62 共享单例;data 或 exec 谁先连谁起)
        self._page = await self._browser_manager.create_page("execution")
        await self._page.goto(self._config.base_url, wait_until="domcontentloaded")
        if "/customer/" not in (self._page.url or ""):  # 持久化 profile 未登录 → 填表单
            await self._login()
        self._executor = OrbitExchExecutor(config=ExecutionConfig())
        self._executor.set_page("default", self._page)
        self._ws_handler = OrbitExchWebSocketHandler(self._page)
        self._ws_handler.on_order_update(self._on_general_frame)  # general 频道:余额 + current_bets
        await self._ws_handler.start()
        # 初始 account state(让账户注册;真实余额由 WS BALANCE 帧 `_on_general_frame` 更新)
        self.generate_account_state(
            balances=oe_balance_to_account_balances(0.0),
            margins=[],
            reported=True,
            ts_event=self._clock.timestamp_ns(),
        )
        self._log.info("OrbitExchExecutionClient connected (live)")

    async def _login(self) -> None:
        """OE 登录(平移自 `scraper.py:login`):填 username/password → 点 Log In → 等 `/customer/`。"""
        cfg = self._config
        await self._page.goto(cfg.base_url, wait_until="networkidle")
        await self._page.wait_for_selector('input[name="username"]', timeout=10000)
        await self._page.fill('input[name="username"]', cfg.username)
        await self._page.fill('input[name="password"]', cfg.password)
        await self._page.click('button[type="submit"]:has-text("Log In")')
        await self._page.wait_for_url("**/customer/**", timeout=15000)
        self._log.info("OrbitExch login successful")

    async def _disconnect(self) -> None:
        if self._ws_handler is not None:
            await self._ws_handler.stop()
        # #62:共享 BrowserManager 不在此 close(data 也不关;避免关掉共享登录浏览器)。

    async def _submit_order(self, command) -> None:
        if not self._begin_session(command):
            return
        order = command.order
        result = await self._place_via_executor(order)
        if result is None or not result.success:
            reason = (result.message if result is not None else "no executor result") or "submit failed"
            self.generate_order_rejected(
                strategy_id=order.strategy_id, instrument_id=order.instrument_id,
                client_order_id=order.client_order_id, reason=reason,
                ts_event=self._clock.timestamp_ns(),
            )
            return
        from nautilus_trader.model.identifiers import VenueOrderId
        self.generate_order_accepted(
            strategy_id=order.strategy_id, instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=VenueOrderId(result.order.venue_order_id or order.client_order_id.value),
            ts_event=self._clock.timestamp_ns(),
        )

    async def _place_via_executor(self, nt_order):
        """NT Order → executor 旧 Order(`nt_order_to_legacy_order`)→ `executor.place_order`(Playwright)。
        Gap C(#63):仅**非 skip** 模式触达(SkipExecution `_submit_order` 在 skip 时 mock 不 super)。
        `_executor`/`_page` 由 `_connect` 设(待真接线)。"""
        inst = self._cache.instrument(nt_order.instrument_id)
        if inst is None:
            self._log.error(f"OE place: instrument {nt_order.instrument_id} not in cache")
            return None
        legacy = nt_order_to_legacy_order(nt_order, inst)
        if legacy is None:
            self._log.error(f"OE place: {nt_order.instrument_id} missing market_id/selection_id")
            return None
        if self._executor is None:
            self._log.error("OE place: executor 未初始化(_connect 未接线 / 非 live)")
            return None
        return await self._executor.place_order(legacy, self._page)

    async def _cancel_order(self, command) -> None:
        await self._cancel_one(
            command.strategy_id, command.instrument_id,
            command.client_order_id, command.venue_order_id,
        )

    async def _cancel_one(self, strategy_id, instrument_id, client_order_id, venue_order_id) -> None:
        """Gap C(#63):NT cancel → executor.cancel_order(需 venue_order_id)→ NT canceled/rejected 事件。
        仅非 skip 触达;/live-test 验。"""
        from src.arbitrage.common.order_models import Order as _Order
        from src.arbitrage.common.order_models import Venue as _Venue

        nt_order = self._cache.order(client_order_id)
        voi = venue_order_id or (nt_order.venue_order_id if nt_order is not None else None)
        now = self._clock.timestamp_ns()
        if self._executor is None or voi is None:
            self.generate_order_cancel_rejected(
                strategy_id, instrument_id, client_order_id, voi,
                "no executor / venue_order_id", now,
            )
            return
        legacy = _Order(venue=_Venue.ORBITEXCH, venue_order_id=str(voi))
        result = await self._executor.cancel_order(legacy, self._page)
        if result is not None and getattr(result, "success", False):
            self.generate_order_canceled(strategy_id, instrument_id, client_order_id, voi, now)
        else:
            reason = (getattr(result, "message", None) if result is not None else None) or "cancel failed"
            self.generate_order_cancel_rejected(strategy_id, instrument_id, client_order_id, voi, reason, now)

    async def _modify_order(self, command) -> None:
        order = self._cache.order(command.client_order_id)
        venue_order_id = command.venue_order_id or (order.venue_order_id if order is not None else None)
        self.generate_order_modify_rejected(
            strategy_id=command.strategy_id, instrument_id=command.instrument_id,
            client_order_id=command.client_order_id, venue_order_id=venue_order_id,
            reason="OrbitExch does not support order modification",
            ts_event=self._clock.timestamp_ns(),
        )

    async def _cancel_all_orders(self, command) -> None:
        """Gap C(#63):executor.cancel_all_unmatched(整页撤未成交);per-order canceled 事件由 WS
        回执补(待 frame schema)。无 executor → no-op。"""
        if self._executor is None:
            return
        result = await self._executor.cancel_all_unmatched(self._page)
        self._log.info(f"OE cancel_all_unmatched: success={getattr(result, 'success', None)}")

    def _cancel_residual_orders(self, instrument_id, residual: list) -> None:
        for order in residual:
            self._loop.create_task(self._cancel_residual_one(order))

    async def _cancel_residual_one(self, order) -> None:
        """Gap C(#63):补偿/残单撤 —— 复用 `_cancel_one`(order 是 NT Order)。
        注:补偿撤单的**触发策略**(单腿失败 → 撤另一腿)见 [[bug_compensating_cancel_missing]],本方法只负责"撤一单"。"""
        await self._cancel_one(
            order.strategy_id, order.instrument_id,
            order.client_order_id, order.venue_order_id,
        )

    def _on_general_frame(self, message) -> None:
        parsed = self._parser.parse_general_frame(message)
        if parsed is None:
            return
        if parsed["type"] == "balance":
            balance = parsed["balance"]
            if balance is not None:
                self.generate_account_state(
                    balances=oe_balance_to_account_balances(balance),
                    margins=[],
                    reported=True,
                    ts_event=self._clock.timestamp_ns(),
                )
        elif parsed["type"] == "current_bets":
            self._on_current_bets(parsed["bets"])

    def _on_current_bets(self, bets) -> None:
        """CURRENT_BETS 快照 → NT 成交事件(`generate_order_filled`)。

        `current_bets_to_fills` 算新增成交;每条按 `offerId == venue_order_id` 反查 NT order →
        发 `generate_order_filled`(last_qty=delta, last_px=averagePrice)。accepted 已由 `_submit_order`
        同步生成、撤单由 `_cancel_*` 生成,这里只补成交。leg_settled 由 mixin 的 `_send_order_event`
        漏斗自动标记。**live 待验**:matched 帧填充值 + liquidity=MAKER(mean_rebate 挂单吃返水)假设。"""
        from nautilus_trader.model.enums import LiquiditySide
        from nautilus_trader.model.identifiers import TradeId
        from nautilus_trader.model.identifiers import VenueOrderId
        from nautilus_trader.model.objects import Money

        # 快照(非增量)→ 缓存供 reconcile reports;键 = offerId
        self._current_bets = {str(b.get("offerId", "")): b for b in (bets or []) if b.get("offerId")}

        for fill in current_bets_to_fills(bets, self._bet_matched):
            offer_id = fill["offer_id"]
            voi = VenueOrderId(offer_id)
            client_order_id = self._cache.client_order_id(voi)
            nt_order = self._cache.order(client_order_id) if client_order_id is not None else None
            if nt_order is None:
                self._log.warning(f"CURRENT_BETS 成交 offerId={offer_id} 无对应 NT order;跳过")
                self._bet_matched[offer_id] = fill["size_matched"]
                continue
            inst = self._cache.instrument(nt_order.instrument_id)
            seq = self._bet_fill_seq.get(offer_id, 0) + 1
            self._bet_fill_seq[offer_id] = seq
            self.generate_order_filled(
                strategy_id=nt_order.strategy_id,
                instrument_id=nt_order.instrument_id,
                client_order_id=client_order_id,
                venue_order_id=voi,
                venue_position_id=None,
                trade_id=TradeId(f"OE-{offer_id}-{seq}"),
                order_side=nt_order.side,
                order_type=nt_order.order_type,
                last_qty=inst.make_qty(fill["delta_qty"]),
                last_px=inst.make_price(fill["avg_price"]),
                quote_currency=GBP,
                commission=Money(0, GBP),
                liquidity_side=LiquiditySide.MAKER,
                ts_event=self._clock.timestamp_ns(),
            )
            self._bet_matched[offer_id] = fill["size_matched"]

    def _resolve_oe_instrument(self, market_id: str, selection_id: str):
        """按 `market_id`+`selection_id` 在 cache 反查 OE instrument(外部/重启单无 NT order 时用)。"""
        if not market_id or not selection_id:
            return None
        for inst in self._cache.instruments(Venue(ORBITEXCH)):
            if (str(getattr(inst, "market_id", "")) == market_id
                    and str(getattr(inst, "selection_id", "")) == selection_id):
                return inst
        return None

    def _build_order_report(self, bet):
        """单 bet → `OrderStatusReport`(reconcile)。优先用 **bet 自带 `side`/`sizePlaced`/`price`**
        (支持外部/重启单);instrument_id 经 NT order 或 `market_id`+`selection_id` 反查;数量/价 NT
        order 优先、缺则用 bet。side/instrument 都拿不到 或 status unknown → 跳过返 None。"""
        from decimal import Decimal

        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.execution.reports import OrderStatusReport
        from nautilus_trader.model.enums import OrderSide
        from nautilus_trader.model.enums import OrderStatus
        from nautilus_trader.model.enums import OrderType
        from nautilus_trader.model.enums import TimeInForce
        from nautilus_trader.model.identifiers import VenueOrderId

        prog = bet_order_progress(bet)
        if prog is None or prog["status"] == "unknown":
            return None
        voi = VenueOrderId(prog["offer_id"])
        client_order_id = self._cache.client_order_id(voi)
        nt_order = self._cache.order(client_order_id) if client_order_id is not None else None

        # order_side:bet 自带 side(BACK→BUY/LAY→SELL)优先,缺则取 NT order
        if prog["side"] == "BACK":
            order_side = OrderSide.BUY
        elif prog["side"] == "LAY":
            order_side = OrderSide.SELL
        elif nt_order is not None:
            order_side = nt_order.side
        else:
            self._log.warning(f"reconcile: offerId={prog['offer_id']} 无 side 且无 NT order;跳过")
            return None

        # instrument_id:NT order 优先,否则 market_id+selection_id 反查(外部/重启单)
        if nt_order is not None:
            instrument_id = nt_order.instrument_id
            inst = self._cache.instrument(instrument_id)
        else:
            inst = self._resolve_oe_instrument(prog["market_id"], prog["selection_id"])
            if inst is None:
                self._log.warning(
                    f"reconcile: offerId={prog['offer_id']} 反查不到 instrument"
                    f"(market={prog['market_id']},selection={prog['selection_id']});跳过")
                return None
            instrument_id = inst.id

        status_map = {
            "accepted": OrderStatus.ACCEPTED,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
            "filled": OrderStatus.FILLED,
        }
        now = self._clock.timestamp_ns()
        quantity = nt_order.quantity if nt_order is not None else inst.make_qty(prog["original_qty"])
        if nt_order is not None:
            price = nt_order.price
        else:
            price = inst.make_price(prog["price"]) if prog["price"] > 0 else None
        return OrderStatusReport(
            account_id=self.account_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=voi,
            order_side=order_side,
            order_type=nt_order.order_type if nt_order is not None else OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            order_status=status_map[prog["status"]],
            quantity=quantity,
            filled_qty=inst.make_qty(prog["filled_qty"]),
            report_id=UUID4(),
            ts_accepted=nt_order.ts_init if nt_order is not None else now,
            ts_last=now,
            ts_init=now,
            price=price,
            avg_px=Decimal(str(prog["avg_px"])) if prog["avg_px"] > 0 else None,
        )

    async def generate_order_status_reports(self, command) -> list:
        """reconcile:缓存的 CURRENT_BETS 快照(`_on_current_bets` 维护)→ `OrderStatusReport` 列表。
        仅为能反查到 NT order 的 bet 构建。宿主健康检查应先 reload 页面刷新 CURRENT_BETS 再调(另落)。"""
        reports = []
        for bet in self._current_bets.values():
            report = self._build_order_report(bet)
            if report is not None:
                reports.append(report)
        return reports

    async def generate_order_status_report(self, command):
        """单订单 reconcile:按 command 的 `venue_order_id` / `client_order_id` 在快照里定位。"""
        target_voi = str(command.venue_order_id) if command.venue_order_id is not None else None
        target_coid = command.client_order_id
        for offer_id, bet in self._current_bets.items():
            if target_voi is not None and offer_id != target_voi:
                continue
            report = self._build_order_report(bet)
            if report is None:
                continue
            if target_coid is not None and report.client_order_id != target_coid:
                continue
            return report
        return None

    async def generate_fill_reports(self, command) -> list:
        """OE WS 不回放历史成交(CURRENT_BETS 只给累积 sizeMatched,非逐笔)→ 无法构 FillReport;
        实时成交经 `_on_current_bets`→`generate_order_filled` 已发。返 []。"""
        return []

    async def generate_position_status_reports(self, command) -> list:
        """OE 持仓 reconcile 延后:NT Portfolio 由 order fills 自行派生持仓;OE 账户经 BALANCE 帧
        对账(Q17,WS 余额已含挂单占用)。betting back/lay→position_side 语义 + matched 样本待真成交。返 []。"""
        return []
