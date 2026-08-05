"""
OrbitExchExecutionClient —— OE 自写执行客户端(§3.2)。

详细设计:`docs/arbitrage/architectures/execution/architecture.md §3.2 / §4.5`。

= 自写 `LiveExecutionClient` + `ArbExecutionSessionMixin`(session / 超时 / 同步)。
订单 IO 包同目录 `executor.place_order/cancel_order`(Playwright);账户状态走 `general` 频道
`BALANCE` 帧 → `generate_account_state`(WS 已含挂单占用,Q17)。

**位置(refactor.md #33 校准)**:OE 是自写适配器,所有 venue-coupled 件
(browser_manager/executor/scraper/data/websocket_handler/message_parser/providers)均住
`nautilus_trader/adapters/orbitexch/`(P9 唯一例外);本文件与它们同目录。

**OE 执行端存活**:本客户端持 execution page;reconcile reports 先用 general WS
存活锚判定快照新鲜,必要时 reload execution page 并等待 `CURRENT_BETS` 重推。

**离线可测**:`oe_balance_to_account_balances` 纯映射、`_on_general_frame` 路由、`_modify_order`
拒绝、`_submit_order` session 门控(注入 fake `_place_via_executor`)。
**live 待验**:真 browser/executor 构造、NT Order→executor 冻结请求翻译、`CURRENT_BETS` 单 bet
item→`generate_order_*`/report(item schema 待 populated 抓帧)、reports。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import AccountBalance
from nautilus_trader.model.objects import Money

from nautilus_trader.adapters.orbitexch.executor import OrbitExchOrderRequest
from nautilus_trader.adapters.orbitexch.message_parser import OrbitExchMessageParser

from src.arbitrage.common.control import TOPIC_ARBITRAGE_PARAMS
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.execution.market_price import worst_decimal_lay_price
from src.arbitrage.execution.session import ArbExecutionSessionMixin
from src.arbitrage.execution.session import cancel_session_started

ORBITEXCH = "ORBITEXCH"

# #105 §4.3bis(4):exec 页 WS 存活锚 idle_timeout(live 实测心跳 ≈35s,300s 保守;config-wire 待 cutover)
_EXEC_WS_IDLE_TIMEOUT_SECS = 300.0
_CONNECT_READY_TIMEOUT_SECS = 30.0
_ORDER_IO_TIMEOUT_SECS = 5.0


def oe_balance_to_account_balances(balance: float, currency=USD) -> list:
    """OE `BALANCE` 帧 → AccountBalance(纯映射,可单测)。

    WS 余额已含挂单占用(Q17)→ 即可用:total = free = balance,locked = 0
    (RiskEngine OE 分支直接信 free,不再减)。"""
    money = Money(balance, currency)
    return [AccountBalance(money, Money(0, currency), money)]


def _resolve_future(fut) -> None:
    if fut is not None and not fut.done():
        fut.set_result(None)


def _oe_result_is_transport_unknown(result) -> bool:
    response = result.get("venue_response") if isinstance(result, dict) else None
    return isinstance(response, dict) and bool(response.get("_transport_error"))


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


def nt_order_to_executor_order(nt_order, inst) -> OrbitExchOrderRequest | None:
    """NT `Order` + OE `BettingInstrument` → executor 无状态请求。

    - `market_id`/`selection_id`/`handicap` 取自 OE instrument 属性(缺 → None,调用方判失败)
    - side:NT `BUY`→`BACK` / `SELL`→`LAY`(OE)
    - `price`=NT order price(已是 OE 赔率格式)/ `size`=NT qty / `order_type`=GTC(POC)
    可单测(无 Playwright);真 `executor.place_order` 由 `_place_via_executor` 调(仅非 skip)。
    """
    from nautilus_trader.model.enums import OrderSide as _NTOrderSide

    market_id = str(getattr(inst, "market_id", "") or "")
    selection_id = str(getattr(inst, "selection_id", "") or "")
    if not market_id or not selection_id:
        return None
    side = "BACK" if nt_order.side == _NTOrderSide.BUY else "LAY"
    return OrbitExchOrderRequest(
        client_order_id=str(nt_order.client_order_id),
        market_id=market_id,
        selection_id=selection_id,
        handicap=_coerce_handicap(getattr(inst, "selection_handicap", None)),
        side=side,
        price=float(nt_order.price),
        size=float(nt_order.quantity),
        order_type="GTC",
    )


def current_bets_to_fills(bets) -> list:
    """OE `CURRENT_BETS` 快照(**非增量**)→ 累计成交意图列表(纯函数,可单测,无 NT 依赖)。

    设计见 `architectures/execution/architecture.md §3.2`。字段语义复用 odds_client(单一真理源):
    - join key:`offerId`(== NT order 的 `venue_order_id`,executor 下单时从响应 `offerIds` 写入)。
    - `sizeMatched` 是**累积**已成交量;调用方用它判断全成,并结合 NT order 已成交量推出本次
      `last_qty`。
    - `averagePrice` = 成交均价(unmatched 时 0)。

    仅当 `sizeMatched > 0 且 averagePrice > 0` 才产出一条累计成交意图;调用方据此发
    `generate_order_filled`。返回 `list[dict]`:`{"offer_id","avg_price","size_matched"}`。

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
        avg_price = float(bet.get("averagePrice", 0) or 0)
        if size_matched > 0 and avg_price > 0:
            fills.append({
                "offer_id": offer_id,
                "avg_price": avg_price,
                "size_matched": size_matched,
            })
    return fills


_FX_AMOUNT_KEYS = {
    "liability",
    "netprofit",
    "profitnet",
    "profitgross",
    "profit",
    "pnl",
}


def normalize_current_bets_to_usd(bets, fx: float) -> list:
    """OE CURRENT_BETS 的金额/size 字段从 GBP 归一到 USD,adapter 外部统一 USD 口径。"""
    if fx <= 0:
        fx = 1.0
    return [_normalize_bet_amounts_to_usd(bet, fx) for bet in (bets or [])]


def _normalize_bet_amounts_to_usd(bet: dict, fx: float) -> dict:
    normalized = dict(bet)
    for key, value in bet.items():
        if not _is_fx_amount_key(key):
            continue
        amount = _try_float(value)
        if amount is None:
            continue
        normalized[key] = amount * fx
    return normalized


def _is_fx_amount_key(key: str) -> bool:
    key_lower = str(key).lower()
    return key_lower.startswith("size") or key_lower in _FX_AMOUNT_KEYS


def _try_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def current_bets_to_positions(bets) -> list:
    """OE `CURRENT_BETS` 快照 → 每 selection 净持仓(纯函数,可单测,无 NT 依赖;#105 §4.3bis(2))。

    BACK=long / LAY=short;`net = ΣBACK_matched − ΣLAY_matched`(matched = `sizeMatched`);
    `avg_px = 主方向(net 符号侧)成交量加权 averagePrice`(反方向当平仓只减 qty、不进 avg_px,
    对齐 NT "avg_px_open 只算开仓侧",不依赖 fill 时序);`net==0` 跳过(FLAT);matched=0 不计。

    返回 `list[dict]`:`{"market_id","selection_id","side"(LONG/SHORT),"qty","avg_px"}`。
    """
    acc: dict = {}   # (market_id, selection_id) -> {long_q, long_w, short_q, short_w}
    for bet in (bets or []):
        prog = bet_order_progress(bet)
        if prog is None:
            continue
        matched = float(prog["filled_qty"])      # = sizeMatched(累积成交量)
        if matched <= 0:
            continue
        avg = float(prog["avg_px"])
        a = acc.setdefault(
            (prog["market_id"], prog["selection_id"]),
            {"long_q": 0.0, "long_w": 0.0, "short_q": 0.0, "short_w": 0.0},
        )
        if prog["side"] == "BACK":
            a["long_q"] += matched
            a["long_w"] += matched * avg
        elif prog["side"] == "LAY":
            a["short_q"] += matched
            a["short_w"] += matched * avg
    out = []
    for (market_id, selection_id), a in acc.items():
        net = a["long_q"] - a["short_q"]
        if abs(net) < 1e-9:
            continue                              # FLAT(完全对冲)
        if net > 0:
            side, avg_px = "LONG", (a["long_w"] / a["long_q"] if a["long_q"] > 0 else 0.0)
        else:
            side, avg_px = "SHORT", (a["short_w"] / a["short_q"] if a["short_q"] > 0 else 0.0)
        out.append({
            "market_id": market_id, "selection_id": selection_id,
            "side": side, "qty": abs(net), "avg_px": avg_px,
        })
    return out


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
        venue_liveness: VenueExecutionLiveness,
        session_timeout_secs: float = 30.0,
        fx: float = 1.0,
        market_order_enabled: bool = False,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(ORBITEXCH),
            venue=Venue(ORBITEXCH),
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            base_currency=USD,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._init_arb_session(
            session_timeout_secs=session_timeout_secs,
        )
        self._venue_liveness = venue_liveness
        self._set_account_id(AccountId(f"{ORBITEXCH}-001"))
        self._browser_manager = browser_manager
        self._config = config
        self._parser = OrbitExchMessageParser()
        self._executor = None
        self._fx = float(fx) if fx > 0 else 1.0
        self._market_order_enabled = bool(market_order_enabled)
        self._page = None
        self._ws_handler = None
        self._bet_fill_seq: dict = {}   # offerId → 成交序号(trade_id 唯一)
        self._current_bets: dict = {}   # offerId → 最新 bet(快照,供 reconcile reports)
        # #105:页锁 —— 串行所有"碰 OE execution 页"的操作(place / cancel / 未来 reload),
        # 因 NT 不串行化命令(均 create_task);同页并发 placeBets 会丢回执(synchronization §8.1/§8.2)。
        self._page_lock = asyncio.Lock()
        self._order_io_timeout_secs = _ORDER_IO_TIMEOUT_SECS
        # #105 A1 §4.3bis(4):exec 页 WS 存活锚 —— general WS 任一帧(含 SockJS 心跳)刷新;
        # `_exec_ws_fresh()` 据此判 idle(reconcile/venue-liveness 用,A2+ 接入)。0 = 还没收过帧。
        self._last_frame_ns = 0
        self._exec_ws_idle_timeout_ns = secs_to_nanos(_EXEC_WS_IDLE_TIMEOUT_SECS)
        self._first_frame_fut = None
        self._balance_ready_fut = None
        self._current_bets_ready_fut = None
        self._connect_ready_timeout_secs = _CONNECT_READY_TIMEOUT_SECS
        self._balance_reported = False
        # #105 A2 §4.3bis(3):reload-then-report 机制(单 flight + 存活闸)。reconcile 成功信号 =
        # reload 后 CURRENT_BETS 重推(刷 `_last_current_bets_ns`);order/position reports 共用该快照。
        self._last_current_bets_ns = 0
        self._reload_inflight = None
        # 从 reload 发起起算，导航与 CURRENT_BETS 重推共用页面加载总预算。
        self._reload_bets_wait_ns = secs_to_nanos(self._config.page_timeout / 1000.0)
        # #255:reload 后首帧 = 静默对账帧(不自派生 fill / 不推报告 / 不标 alive);
        # 报告与 alive 由触发 reload 的调用方负责(QueryOrder 推送 / NT 拉取返回)。
        self._reload_frame_pending = False
        # ack via CURRENT_BETS(不再由 place 回执 ack):offerId -> client_order_id,下单后
        # 暂存;首次在任意一帧 CURRENT_BETS(含 reload 静默帧)中出现该 offerId 即证明订单
        # 已被 venue 接收,弹出并 ack(不看是否已成交)。见 `_on_current_bets`。
        self._pending_accept: dict[str, ClientOrderId] = {}
        self._msgbus.subscribe(topic=TOPIC_ARBITRAGE_PARAMS, handler=self._on_set_arbitrage_params_cmd)

    def _current_fx(self) -> float:
        return self._fx if self._fx > 0 else 1.0

    def _on_set_arbitrage_params_cmd(self, cmd) -> None:
        fx = getattr(cmd, "fx", None)
        if fx is None:
            return
        fx = float(fx)
        if fx <= 0:
            self._log.warning(f"Ignore invalid OE fx={fx}")
            return
        self._fx = fx

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
        self._page.set_default_timeout(self._config.page_timeout)
        # #67:**先挂 WS 监听,再导航**。`page.on('websocket')` 只捕获注册之后页面新建的 WS;general
        # WS 在登录导航 `/customer/` 期间建立,若先 goto/login 再 start 会错过它 → 收不到 BALANCE/
        # CURRENT_BETS(老 odds_client 注释明示"必须在 goto 前挂拦截,否则错过 WS 创建")。
        self._ws_handler = OrbitExchWebSocketHandler(self._page, logger=self._log)
        self._ws_handler.on_order_update(self._on_general_frame)  # general 频道:余额 + current_bets
        self._ws_handler.on_frame(self._mark_exec_frame)          # #105 A1:每帧(含心跳)刷存活锚
        self._ws_handler.on_disconnect(self._mark_exec_stale)     # close:orders → 下次 reconcile 触发 reload-then-report
        await self._ws_handler.start()
        # general WS 可能在首次导航/登录期间立即推送业务帧，必须先建立 waiter，
        # 否则状态虽已更新，登录后新建的 future 仍会白等完整 timeout。
        loop = asyncio.get_running_loop()
        self._balance_ready_fut = loop.create_future()
        self._current_bets_ready_fut = loop.create_future()
        await self._page.goto(
            self._config.base_url,
            wait_until="domcontentloaded",
            timeout=self._config.page_timeout,
        )
        if "/customer/" not in (self._page.url or ""):  # 持久化 profile 未登录 → 填表单
            await self._login()
        await self._wait_for_initial_business_state()
        self._executor = OrbitExchExecutor(
            config=ExecutionConfig(market_order_enabled=self._market_order_enabled),
            fx_getter=self._current_fx,
        )
        self._executor.set_page("default", self._page)
        if not self._balance_reported:
            # 初始 account state(让账户注册;真实余额由 WS BALANCE 帧 `_on_general_frame` 更新)
            self.generate_account_state(
                balances=oe_balance_to_account_balances(0.0),
                margins=[],
                reported=True,
                ts_event=self._clock.timestamp_ns(),
            )
        self._log.info("OrbitExchExecutionClient connected (live)")

    async def _wait_for_initial_business_state(self) -> None:
        """登录后最多等待 OE execution 页两个业务真值:BALANCE + CURRENT_BETS。

        waiter 在首次页面导航前建立，登录期间到达的业务帧也能完成它。
        超时不阻断连接:余额缺失时 `_connect` 仍发 0 兜底;CURRENT_BETS 后续由 reconcile reload 自愈。
        """
        futures = [f for f in (self._balance_ready_fut, self._current_bets_ready_fut) if f is not None]
        if not futures:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*futures), timeout=self._connect_ready_timeout_secs)
        except asyncio.TimeoutError:
            missing = []
            if not self._balance_reported:
                missing.append("BALANCE")
            if self._last_current_bets_ns <= 0:
                missing.append("CURRENT_BETS")
            self._log.warning(f"OE connect ready timed out waiting for {', '.join(missing)}")
        finally:
            self._balance_ready_fut = None
            self._current_bets_ready_fut = None

    async def _login(self) -> None:
        """OE 登录(平移自 `scraper.py:login`):填 username/password → 点 Log In → 等 `/customer/`
        → **关登录后弹窗**(否则弹层盖住页面,general WS 不推 BALANCE/CURRENT_BETS)。"""
        cfg = self._config
        await self._page.goto(cfg.base_url, wait_until="domcontentloaded", timeout=cfg.page_timeout)
        await self._page.wait_for_selector('input[name="username"]', timeout=10000)
        await self._page.fill('input[name="username"]', cfg.username)
        await self._page.fill('input[name="password"]', cfg.password)
        await self._page.click('button[type="submit"]:has-text("Log In")')
        await self._page.wait_for_url("**/customer/**", timeout=15000)
        self._log.info("OrbitExch login successful")
        await self._dismiss_post_login_popup()

    async def _dismiss_post_login_popup(self) -> None:
        """关登录后弹窗:等待弹窗出现 → 点主页面区域关闭。
        无弹窗 / 出错都不致命(吞掉),不阻断连接。"""
        try:
            popup = self._page.locator('div[class*="_postLoginPopup_"]').first
            if callable(popup):
                popup = popup()
            await popup.wait_for(state="visible", timeout=7000)
            await self._page.mouse.click(24, 160)
            self._log.info("OrbitExch post-login popup dismissed")
        except Exception as e:
            self._log.debug(f"OrbitExch no post-login popup (error: {e})")

    async def _disconnect(self) -> None:
        if self._ws_handler is not None:
            await self._ws_handler.stop()
        # #62:共享 BrowserManager 不在此 close(data 也不关;避免关掉共享登录浏览器)。

    async def _submit_order(self, command) -> None:
        # #261:session 已由 mixin 的同步 `submit_order` 建立(派生态不能有空窗),此处不再建。
        order = command.order
        try:
            result = await self._place_via_executor(order)
            if result is None or not result.get("success"):
                if result is not None and _oe_result_is_transport_unknown(result):
                    self._log.warning(
                        "OE submit result unknown; retaining SUBMITTED order for NT inflight reconcile "
                        f"client_order_id={order.client_order_id}: {result.get('message')}",
                    )
                    return
                reason = (
                    result.get("message")
                    if result is not None
                    else "no executor result"
                ) or "submit failed"
                self.generate_order_rejected(
                    strategy_id=order.strategy_id, instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id, reason=reason,
                    ts_event=self._clock.timestamp_ns(),
                )
                return
            # ack 不在此处触发(不再是回执即 ack):暂存 offerId -> client_order_id,
            # 等下一条 CURRENT_BETS 帧(不论是否已成交)首次带出这个 offerId 时才 ack,
            # 见 `_on_current_bets`。
            venue_order_id = str(
                result.get("venue_order_id") or order.client_order_id.value,
            )
            self._pending_accept[venue_order_id] = order.client_order_id
        except Exception as e:
            self._log.warning(
                f"OE submit result unknown; retaining SUBMITTED order for NT inflight reconcile "
                f"client_order_id={order.client_order_id}: {e!r}",
            )

    async def _place_via_executor(self, nt_order):
        """NT Order → 无状态 executor request → `executor.place_order`(Playwright)。
        Gap C(#63):仅**非 skip** 模式触达(SkipExecution `_submit_order` 在 skip 时 mock 不 super)。
        `_executor`/`_page` 由 `_connect` 设(待真接线)。"""
        inst = self._cache.instrument(nt_order.instrument_id)
        if inst is None:
            self._log.error(f"OE place: instrument {nt_order.instrument_id} not in cache")
            return None
        request = nt_order_to_executor_order(nt_order, inst)
        if request is None:
            self._log.error(f"OE place: {nt_order.instrument_id} missing market_id/selection_id")
            return None
        if self._executor is None:
            self._log.error("OE place: executor 未初始化(_connect 未接线 / 非 live)")
            return None
        if self._market_order_enabled and request.side == "LAY":
            planned_price = request.price
            request = replace(
                request,
                price=worst_decimal_lay_price(
                    self._cache.order_book(nt_order.instrument_id),
                    ORBITEXCH,
                    planned_price,
                ),
            )
            self._log.info(
                f"OE marketable LAY price: planned={planned_price} submit={request.price}",
            )
        self.generate_order_submitted(
            strategy_id=nt_order.strategy_id,
            instrument_id=nt_order.instrument_id,
            client_order_id=nt_order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )
        return await asyncio.wait_for(
            self._run_page_write(lambda: self._executor.place_order(request, self._page)),
            timeout=self._order_io_timeout_secs,
        )

    async def _cancel_order(self, command) -> None:
        await self._cancel_one(
            command.strategy_id, command.instrument_id,
            command.client_order_id, command.venue_order_id,
            session_started=cancel_session_started(command),
        )

    async def _cancel_one(
        self,
        strategy_id,
        instrument_id,
        client_order_id,
        venue_order_id,
        *,
        session_started: bool = False,
    ) -> None:
        """Gap C(#63):NT cancel → executor.cancel_order(需 venue_order_id)→ NT canceled/rejected 事件。
        仅非 skip 触达;/live-test 验。"""
        nt_order = self._cache.order(client_order_id)
        voi = venue_order_id or (nt_order.venue_order_id if nt_order is not None else None)
        now = self._clock.timestamp_ns()
        if nt_order is not None and not session_started and not self._begin_cancel_session(nt_order):
            return
        if self._executor is None or voi is None:
            self.generate_order_cancel_rejected(
                strategy_id, instrument_id, client_order_id, voi,
                "no executor / venue_order_id", now,
            )
            return
        inst = self._cache.instrument(instrument_id)
        bet = self._current_bets.get(str(voi))
        market_id = str(
            getattr(inst, "market_id", "") or (bet or {}).get("marketId", ""),
        )
        try:
            result = await asyncio.wait_for(
                self._run_page_write(
                    lambda: self._executor.cancel_order(market_id, str(voi), self._page),
                ),
                timeout=self._order_io_timeout_secs,
            )
        except Exception as e:  # noqa: BLE001 — 结果未知,保留原订单状态并等待 NT inflight reconcile
            self._log.warning(
                "OE cancel result unknown; retaining order state for NT inflight reconcile "
                f"client_order_id={client_order_id}, venue_order_id={voi}: {e!r}",
            )
            return
        if result is not None and result.get("success"):
            self._log.info(
                f"OE cancel request accepted: client_order_id={client_order_id}, "
                f"venue_order_id={voi}; awaiting CURRENT_BETS confirmation",
            )
            self._ack_cancel_session(client_order_id, voi)
        else:
            if result is not None and _oe_result_is_transport_unknown(result):
                self._log.warning(
                    "OE cancel result unknown; retaining order state for NT inflight reconcile "
                    f"client_order_id={client_order_id}, venue_order_id={voi}: "
                    f"{result.get('message')}",
                )
                return
            reason = (result.get("message") if result is not None else None) or "cancel failed"
            self.generate_order_cancel_rejected(strategy_id, instrument_id, client_order_id, voi, reason, now)

    async def _query_order(self, command) -> None:
        """卡在飞时强制 reload，复用既有 reload 成功判定(同 #255)。
        #256:不再对账——不主动构造/推送报告,reload 成功后的状态同步交给 WS 监听在
        后续自然帧里做(与常规 WS-stale 场景一致);ack 也已不靠这里兜底(见 `_on_current_bets`
        的 `_pending_accept`)。"""
        # in-flight 查询暂不参与 venue liveness；保留调用位置供以后按需恢复。
        # self._venue_liveness.mark_order_dead(ORBITEXCH)
        # self._venue_liveness.mark_position_dead(ORBITEXCH)
        if not await self._ensure_exec_snapshot_fresh(force=True):
            return
        # self._venue_liveness.mark_order_alive(ORBITEXCH)
        # self._venue_liveness.mark_position_alive(ORBITEXCH)

    async def _run_page_write(self, operation):
        async with self._page_lock:
            return await operation()

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
        self._log.info(f"OE cancel_all_unmatched: success={result.get('success')}")

    async def _cancel_residual_one(self, order) -> None:
        """Gap C(#63):撤一条残单 —— 复用 `_cancel_one`(order 是 NT Order)。#105:循环 + exec_count
        跟踪由 base `_cancel_residual_orders`/`_tracked_residual_cancel` 统一,本方法只负责"撤一单"。
        注:补偿撤单的**触发策略**(单腿失败 → 撤另一腿)见 [[bug_compensating_cancel_missing]]。"""
        await self._cancel_one(
            order.strategy_id, order.instrument_id,
            order.client_order_id, order.venue_order_id,
            session_started=True,
        )

    def _mark_exec_frame(self) -> None:
        """#105 A1:exec 页 general WS 任一帧(含 SockJS 心跳)→ 刷存活锚 `_last_frame_ns`。"""
        self._last_frame_ns = self._clock.timestamp_ns()
        if self._first_frame_fut is not None and not self._first_frame_fut.done():
            self._first_frame_fut.set_result(None)

    def _mark_exec_stale(self, reason: str) -> None:
        """#105/#109 对齐:general WS close 只置 stale;恢复仍由 reports/reconcile 出口裁决。"""
        if reason == "close:orders" or reason == "liveness_timeout":
            self._last_frame_ns = 0

    def _exec_ws_fresh(self) -> bool:
        """#105 §4.3bis(4):exec 页 WS 是否新鲜(idle_timeout 内有帧)。
        `_last_frame_ns==0`(尚未收过任何帧)视为不新鲜。供 reconcile 存活闸 / venue-liveness 用(A2+ 接入)。"""
        if self._last_frame_ns == 0:
            return False
        return (self._clock.timestamp_ns() - self._last_frame_ns) <= self._exec_ws_idle_timeout_ns

    async def _reload_exec_page(self) -> bool:
        """#105 §4.3bis:reload exec 页(走页锁)→ 等 CURRENT_BETS 重推。
        返回 True=拿到真实 response(reconcile 成功),False=reload 失败 / CURRENT_BETS 超时未重推(venue dead)。"""
        if self._page is None:
            return False
        # #255:reload 皆对账 —— 重推的首帧必须静默(报告消费者=触发方);失败时标记不清,
        # 顺延到下一自然帧(该帧成交由再下一帧的累计差分自愈)。
        self._reload_frame_pending = True
        reload_ts = self._clock.timestamp_ns()
        try:
            async with self._page_lock:   # 与 place/cancel 同锁串行(#105 §8.2)
                await self._page.reload(wait_until="domcontentloaded", timeout=self._config.page_timeout)
        except Exception as e:  # noqa: BLE001 — reload 失败 = reconcile 失败,交调用方判 dead
            self._log.warning(f"OE exec page reload failed: {e!r}")
            return False
        # 等 reload 后新 CURRENT_BETS(`_on_current_bets` 刷 `_last_current_bets_ns`);锁外等,不挡 place/cancel
        while self._last_current_bets_ns <= reload_ts:
            if self._clock.timestamp_ns() - reload_ts > self._reload_bets_wait_ns:
                self._log.warning("OE exec reload: CURRENT_BETS 未在超时内重推 → reconcile 失败")
                return False
            await asyncio.sleep(0.1)
        return True

    async def _ensure_exec_snapshot_fresh(self, *, force: bool = False) -> bool:
        """#105 §4.3bis(3):确保 `_current_bets` 新鲜(reconcile)。WS 活(`_exec_ws_fresh`)→ True 不 reload;
        否则 single-flight reload 一次(并发调用共享同一 future)。返回 True=快照可信,False=reconcile 失败(venue dead)。"""
        if not force and self._last_current_bets_ns > 0 and self._exec_ws_fresh():
            return True
        if self._reload_inflight is not None:
            return await self._reload_inflight
        self._reload_inflight = asyncio.ensure_future(self._reload_exec_page())
        try:
            return await self._reload_inflight
        finally:
            self._reload_inflight = None

    def _on_general_frame(self, message) -> None:
        parsed = self._parser.parse_general_frame(message)
        if parsed is None:
            return
        if parsed["type"] == "balance":
            balance = parsed["balance"]
            if balance is not None:
                self.generate_account_state(
                    balances=oe_balance_to_account_balances(balance * self._current_fx()),
                    margins=[],
                    reported=True,
                    ts_event=self._clock.timestamp_ns(),
                )
                self._balance_reported = True
                _resolve_future(self._balance_ready_fut)
        elif parsed["type"] == "current_bets":
            self._on_current_bets(parsed["bets"])

    def _on_current_bets(self, bets) -> None:
        """CURRENT_BETS 快照 → NT 成交事件(`generate_order_filled`)。

        `current_bets_to_fills` 读取累计成交;每条按 `offerId == venue_order_id` 反查 NT order →
        发 `generate_order_filled`(last_qty=sizeMatched, last_px=averagePrice)。accepted 已由 `_submit_order`
        同步生成、撤单由 `_cancel_*` 生成,这里只补成交。**matched 帧填充值已 live 验**
        (#82,offerId=222016509:sizeMatched/averagePrice)。
        `liquidity_side=MAKER` 无条件硬编码:**已评估无害**——OE 是博彩交易所、CURRENT_BETS 无 maker/taker
        字段(maker/taker 是 PM CLOB 概念),且 OE fill `commission=0`、套利 outcome 指标在
        strategy/risk/portfolio 层算、不读此字段 → 该侧纯名义,留 MAKER 即可(refactor.md #82/#83)。"""
        from nautilus_trader.model.enums import LiquiditySide
        from nautilus_trader.model.identifiers import TradeId
        from nautilus_trader.model.identifiers import VenueOrderId
        from nautilus_trader.model.objects import Money

        usd_bets = normalize_current_bets_to_usd(bets, self._current_fx())

        # 快照(非增量)→ 缓存供 reconcile reports;键 = offerId。缓存值为 USD 口径。
        self._current_bets = {str(b.get("offerId", "")): b for b in usd_bets if b.get("offerId")}
        self._emit_cancel_events_from_current_bets()

        # ack via CURRENT_BETS:offerId 首次出现在任意一帧(含 reload 静默帧)即证明订单
        # 已被 venue 接收,不看是否已成交;与 fill 派生是独立的一次性状态跃迁,不受
        # #255 静默帧规则约束(不会双计)。`newly_acked` 供本帧 fill 派生兜底解析
        # client_order_id(NT 引擎对刚 ack 的事件是异步 apply,cache 索引还没跟上)。
        newly_acked: dict[str, ClientOrderId] = {}
        for offer_id in list(self._pending_accept):
            if offer_id not in self._current_bets:
                continue
            coid = self._pending_accept.pop(offer_id)
            nt_order = self._cache.order(coid)
            if nt_order is None:
                continue
            self.generate_order_accepted(
                strategy_id=nt_order.strategy_id,
                instrument_id=nt_order.instrument_id,
                client_order_id=coid,
                venue_order_id=VenueOrderId(offer_id),
                ts_event=self._clock.timestamp_ns(),
            )
            newly_acked[offer_id] = coid

        # #255:reload 后首帧是静默对账帧 —— 不自派生 fill,成交由触发 reload 的调用方经
        # 报告对账(inferred fill)补齐;fill 事件异步 apply,同帧双路对同一快照派生必读到
        # 滞后 filled_qty → 每笔部分成交双计 + 终态 overfill(2026-07-18 实盘)。
        # 常规帧只走事件路径,不推报告。
        reload_frame = self._reload_frame_pending
        if reload_frame:
            self._reload_frame_pending = False
        else:
            # `sizeMatched` 是 OE 原始 GBP 累计成交量。这里不维护 prevMatched;生成 NT fill 时用
            # 当前 NT order.filled_qty 推出本次 last_qty,避免累计快照被重复累加。reconcile 快照仍使用上方
            # usd_bets。
            for fill in current_bets_to_fills(bets):
                offer_id = fill["offer_id"]
                voi = VenueOrderId(offer_id)
                client_order_id = self._cache.client_order_id(voi) or newly_acked.get(offer_id)
                nt_order = self._cache.order(client_order_id) if client_order_id is not None else None
                if nt_order is None:
                    self._log.warning(f"CURRENT_BETS 成交 offerId={offer_id} 无对应 NT order;跳过")
                    continue
                inst = self._cache.instrument(nt_order.instrument_id)
                matched_qty = fill["size_matched"] * self._current_fx()
                already_filled = nt_order.filled_qty.as_double()
                remaining_qty = nt_order.quantity.as_double() - already_filled
                last_qty = min(matched_qty - already_filled, remaining_qty)
                if last_qty <= 0:
                    continue
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
                    last_qty=inst.make_qty(last_qty),
                    last_px=inst.make_price(fill["avg_price"]),
                    quote_currency=USD,
                    commission=Money(0, USD),
                    liquidity_side=LiquiditySide.MAKER,
                    ts_event=self._clock.timestamp_ns(),
                )

        self._last_current_bets_ns = self._clock.timestamp_ns()
        _resolve_future(self._current_bets_ready_fut)
        # 静默帧不标 alive:恢复语境下 alive 必须晚于报告进入 ExecEngine(#244 顺序),由触发方标。
        if not reload_frame:
            self._venue_liveness.mark_order_alive(ORBITEXCH)
            self._venue_liveness.mark_position_alive(ORBITEXCH)


    @staticmethod
    def _bet_is_cancelled(bet: dict) -> bool:
        state = bet.get("offerState") or bet.get("offer_state") or bet.get("state") or bet.get("status")
        return str(state or "").upper() in {"CANCELLED", "CANCELED"}

    def _emit_cancel_events_from_current_bets(self) -> None:
        """新 CURRENT_BETS 快照确认撤单完成:订单消失或 offerState=CANCELLED。"""
        from nautilus_trader.model.identifiers import VenueOrderId

        for client_order_id, sess in self._active_cancel_sessions_snapshot():
            venue_order_id = sess.get("venue_order_id")
            nt_order = self._cache.order(client_order_id)
            if venue_order_id is None and nt_order is not None:
                venue_order_id = nt_order.venue_order_id
            if venue_order_id is None:
                venue_order_id = self._cache.venue_order_id(client_order_id)
            if venue_order_id is None:
                continue
            bet = self._current_bets.get(str(venue_order_id))
            if bet is not None and not self._bet_is_cancelled(bet):
                continue
            if nt_order is None:
                continue
            self.generate_order_canceled(
                strategy_id=nt_order.strategy_id,
                instrument_id=nt_order.instrument_id,
                client_order_id=client_order_id,
                venue_order_id=VenueOrderId(str(venue_order_id)),
                ts_event=self._clock.timestamp_ns(),
            )

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
        仅为能反查到 NT order 的 bet 构建。"""
        snapshot = self._capture_reconciliation_state_snapshot(kind="order")
        if not await self._ensure_exec_snapshot_fresh() or self._last_current_bets_ns <= 0:
            # #259:快照不可信 → 抛,不返空。返 [] 会被 NT 读成「查询成功、venue 无挂单」——
            # `live/execution_engine.py:875-881` 只把**异常**计入 `failed_venues`,返空使
            # `_did_position_status_query_fail` 恒 False、`:900-910` 跳过保护失效,连续对账遂用
            # `_create_flat_position_report`(qty=0)当目标 + 默认 `generate_missing_orders=True`
            # 合成成交,抹平真实状态的账面记录。`mark_*_dead` 只写我们自己的 VenueExecutionLiveness,
            # NT 看不见,不能替代异常。(#122 当初让 PM「对齐 OE」,实为参照系本身偏离 NT 约定。)
            raise RuntimeError(
                "OE order status reports unavailable: exec snapshot not fresh "
                "(page reload failed / CURRENT_BETS not repushed)",
            )
        reports = []
        for bet in self._current_bets.values():
            report = self._build_order_report(bet)
            if report is not None:
                reports.append(report)
        return self._guard_reconciliation_reports("order", reports, snapshot)

    async def generate_order_status_report(self, command):
        """单订单 reconcile:按 command 的 `venue_order_id` / `client_order_id` 在快照里定位。

        #319:单数=NT inflight-check / QueryOrder 解析专用路径,**不附 snapshot**、豁免 staleness 闸
        (理由见 PM `generate_order_status_report`)。
        """
        if not await self._ensure_exec_snapshot_fresh() or self._last_current_bets_ns <= 0:
            # #259:**查询失败**(快照不可信)→ 抛;下方「快照里找不到该单」仍返 None(NT 契约合法值)。
            raise RuntimeError(
                "OE order status report unavailable: exec snapshot not fresh "
                "(page reload failed / CURRENT_BETS not repushed)",
            )
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
        """OE 持仓 reconcile(#105 §4.3bis(2)):从 CURRENT_BETS 快照聚合净持仓
        (BACK=long/LAY=short,net=ΣBACK−ΣLAY,avg_px=主方向成交量加权)。matched=0 / 净 0 跳过;
        instrument 经 market_id+selection_id 反查,反查不到跳过。
        **注**:NT Portfolio 平时由 order fills 自行派生持仓;本 report 仅供 reconcile 对账用。"""
        snapshot = self._capture_reconciliation_state_snapshot(kind="position")
        if not await self._ensure_exec_snapshot_fresh() or self._last_current_bets_ns <= 0:
            # #259:同上——返空会让 NT 拿真实持仓去对齐一个假的「空仓」报告。
            raise RuntimeError(
                "OE position status reports unavailable: exec snapshot not fresh "
                "(page reload failed / CURRENT_BETS not repushed)",
            )

        reports = self._build_position_status_reports_from_current_bets()
        return self._guard_reconciliation_reports("position", reports, snapshot)

    def _build_position_status_reports_from_current_bets(self) -> list:
        """从当前完整 CURRENT_BETS 快照构造仓位报告，不修改 liveness。"""
        from decimal import Decimal

        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.execution.reports import PositionStatusReport
        from nautilus_trader.model.enums import PositionSide

        reports = []
        now = self._clock.timestamp_ns()
        for pos in current_bets_to_positions(self._current_bets.values()):
            inst = self._resolve_oe_instrument(pos["market_id"], pos["selection_id"])
            if inst is None:
                self._log.warning(
                    f"position reconcile: 反查不到 instrument(market={pos['market_id']},"
                    f"selection={pos['selection_id']});跳过")
                continue
            reports.append(PositionStatusReport(
                account_id=self.account_id,
                instrument_id=inst.id,
                position_side=PositionSide.LONG if pos["side"] == "LONG" else PositionSide.SHORT,
                quantity=inst.make_qty(pos["qty"]),
                report_id=UUID4(),
                ts_last=now,
                ts_init=now,
                avg_px_open=Decimal(str(pos["avg_px"])) if pos["avg_px"] > 0 else None,
            ))
        return reports
