"""
ArbPolymarketExecutionClient —— PM 执行客户端薄子类(Q18c 宿主层)。

详细设计:`docs/arbitrage/architectures/execution/architecture.md §3.1 / §4.3 / §4.6`。

= 上游 `PolymarketExecutionClient`(订单 IO / 账户状态 / reports,直接复用)
  + `ArbExecutionSessionMixin`(session / leg_settled / 超时 / execution.* 同步)
  + `HealthCheckLoop`(健康检查节奏,PM 间隔可配)
  + `PolymarketSettlement`(健康检查 tick 内 merge/redeem)。

**位置(refactor.md #33 校准)**:本类是 PM venue-coupled 代码 → 住 PM adapter 目录(P9 唯一
例外:venue 适配器放 `nautilus_trader/adapters/<venue>/`)。**与上游 `execution.py` 同目录但不
同文件**(`arb_execution.py`)避免 upstream merge 冲突;import 上游类直接子类化。

**集成验证靠实盘**:构造真 client 需 `ClobClient`/`ws_auth`/Data API,离线 unit 测不到;
本文件可单测的是模块级纯映射 `pm_position_to_settlement`。其余接线经 /live-test 验。
"""

from __future__ import annotations

from typing import Awaitable
from typing import Callable

from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET
from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelOrder

from src.arbitrage.common.leg_settled import LegSettledRegistry
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.execution.health_check import HealthCheckLoop
from src.arbitrage.execution.session import ArbExecutionSessionMixin
from src.arbitrage.settlement.settlement import PolymarketSettlement
from src.arbitrage.settlement.settlement import SettlementPosition


def pm_position_to_settlement(p) -> SettlementPosition:
    """PM Data API 持仓(`PolymarketPosition`)→ settlement 视图(纯映射,可单测)。"""
    return SettlementPosition(
        condition_id=p.condition_id,
        size=p.size,
        neg_risk=p.neg_risk,
        redeemable=p.redeemable,
    )


class ArbPolymarketExecutionClient(ArbExecutionSessionMixin, PolymarketExecutionClient):
    def __init__(
        self,
        loop,
        http_client,
        msgbus,
        cache,
        clock,
        instrument_provider,
        ws_auth,
        config,
        name=None,
        *,
        leg_settled: LegSettledRegistry,
        pair_registry: PairRegistry | None = None,
        pair_inflight=None,  # PairInFlightGate(§6.10 §7);与 strategy 共享一份
        settlement: PolymarketSettlement | None = None,
        positions_fetcher: Callable[[], Awaitable[list]] | None = None,
        session_timeout_secs: float = 30.0,
        health_interval_secs: float = 30.0,
    ) -> None:
        super().__init__(
            loop, http_client, msgbus, cache, clock,
            instrument_provider, ws_auth, config, name,
        )
        self._init_arb_session(
            leg_settled=leg_settled,
            session_timeout_secs=session_timeout_secs,
            pair_registry=pair_registry,
            pair_inflight=pair_inflight,
        )
        self._settlement = settlement
        self._positions_fetcher = positions_fetcher  # async () -> list[PolymarketPosition](Data API /positions,launcher 注入)
        self._health_interval_secs = health_interval_secs
        self._health = HealthCheckLoop(
            name=f"health_check:{POLYMARKET}",
            clock=self._clock,
            msgbus=self._msgbus,
            loop=self._loop,
            log=self._log,
            interval_secs_provider=lambda: self._health_interval_secs,
            is_execution_active=lambda: self._execution_active,
            run_check=self._run_health_check,
        )

    async def _connect(self) -> None:
        await super()._connect()
        self._health.start()

    async def _disconnect(self) -> None:
        self._health.stop()
        await super()._disconnect()

    async def _submit_order(self, command) -> None:
        if not self._begin_session(command):
            return
        try:
            await super()._submit_order(command)
        except Exception as e:
            order = command.order
            self._log.error(
                f"Polymarket submit failed before venue acknowledgement "
                f"client_order_id={order.client_order_id}: {e!r}",
            )
            self.generate_order_denied(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=f"PM submit exception before venue acknowledgement: {e!r}",
                ts_event=self._clock.timestamp_ns(),
            )
            self._end_session(order.client_order_id)

    async def _cancel_residual_one(self, order) -> None:
        """#105:撤一条残单 —— 构 `CancelOrder` 走 `_cancel_order`。循环 + exec_count 跟踪由 base
        `_cancel_residual_orders`/`_tracked_residual_cancel` 统一(撤单纳入 exec_count,exec_count→0 才清 in-flight)。"""
        cmd = CancelOrder(
            trader_id=self.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id,
            command_id=UUID4(),
            ts_init=self._clock.timestamp_ns(),
        )
        await self._cancel_order(cmd)

    async def _run_health_check(self) -> None:
        if self._positions_fetcher is None:
            return
        positions = await self._positions_fetcher() or []

        if self._settlement is not None:
            await self._settlement.run([pm_position_to_settlement(p) for p in positions])

        for p in positions:
            instrument_id = getattr(p, "instrument_id", None)
            if instrument_id is None:
                continue
            pair_id = self._pair_id_for(instrument_id)
            if pair_id is not None:
                self._leg_settled.mark(pair_id, instrument_id)
