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

    async def _connect(self) -> None:
        raise NotImplementedError("OE _connect: live 接线(browser/executor/general WS),/live-test 验")

    async def _disconnect(self) -> None:
        if self._browser_manager is not None:
            await self._browser_manager.close()

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
        """SEAM(live):NT Order → executor 旧 Order(market_id/selection_id 取自 instrument.info)
        → `executor.place_order(old_order, self._page)` → ExecutionResult。"""
        raise NotImplementedError("OE _place_via_executor: NT→executor 翻译 + Playwright 下单,/live-test 验")

    async def _cancel_order(self, command) -> None:
        raise NotImplementedError("OE _cancel_order: executor.cancel_order,/live-test 验")

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
        raise NotImplementedError("OE _cancel_all_orders: executor.cancel_all_unmatched,/live-test 验")

    def _cancel_residual_orders(self, instrument_id, residual: list) -> None:
        for order in residual:
            self._loop.create_task(self._cancel_residual_one(order))

    async def _cancel_residual_one(self, order) -> None:
        raise NotImplementedError("OE _cancel_residual_one: executor.cancel_order,/live-test 验")

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
        if bets:
            self._log.debug(f"CURRENT_BETS {len(bets)} item(s); bet→event 映射待 item schema")

    async def generate_order_status_reports(self, command) -> list:
        return []

    async def generate_order_status_report(self, command):
        return None

    async def generate_fill_reports(self, command) -> list:
        return []

    async def generate_position_status_reports(self, command) -> list:
        return []
