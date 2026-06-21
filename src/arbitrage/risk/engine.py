"""
ArbitrageLiveRiskEngine —— NT LiveRiskEngine 子类(实盘环境 kernel 用的是 LiveRiskEngine,
非基类 RiskEngine)。在 submit 管道上透明拦截:NT 父类自动检查(price/quantity/GTD +
notional/submit_rate/TradingState/native 余额)+ 应用层余额检查 + 单场止盈/止损硬停。

详细设计:`docs/arbitrage/architectures/risk/architecture.md §3.1 / §4`。

要点:
- `_check_order` 是 NT cpdef,Python 子类覆盖会被父类 `_handle_submit_order` 调到(cpdef 语义)。
- 自定义拒绝**必须自己 emit denied 事件**(父类 `_handle_submit_order` 见 False 仅 return,
  指望 _check_order 已调 `_deny_order`),否则 Strategy.on_order_denied 不触发。
- `CancelOrder` 走另一条命令通路,不经 _check_order,故补偿撤单永远放行。
- `arb:intent=recovery` 的补救下单仍走 NT 基础检查 + 余额检查,但跳过 profit gates
  (match_tp/match_sl),避免“别开新仓”硬停挡住降风险补救。
- 门限读 **live** Cache(非 Strategy 快照)。tp/sl 经 self._portfolio(实为
  ArbitragePortfolio)pull outcome_exposures。
"""

from __future__ import annotations

from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.live.risk_engine import LiveRiskEngine
from nautilus_trader.model.instruments import BettingInstrument
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import LimitOrder

from src.arbitrage.common.opportunity import RISK_LEG_DENIED_TOPIC
from src.arbitrage.common.opportunity import meta_from_order
from src.arbitrage.common.opportunity import order_intent
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.risk.config import ArbRiskParams


class ArbitrageLiveRiskEngine(LiveRiskEngine):

    # ── 注入(launcher 在 kernel 原生构造后调用)─────────────────────
    def configure_arb(
        self,
        params: ArbRiskParams,
        *,
        venue_liveness: VenueExecutionLiveness | None = None,
    ) -> None:
        self._arb_params = params
        self._arb_venue_liveness = venue_liveness

    @property
    def _params(self) -> ArbRiskParams:
        return getattr(self, "_arb_params", None) or ArbRiskParams()

    # ── Submit 入口:share limit + adjusted size gate──────────────────
    def _handle_submit_order(self, command) -> None:
        adjusted = self._adjust_submit_order_for_share_limit(command)
        if adjusted is None:
            return
        super()._handle_submit_order(adjusted)

    def _adjust_submit_order_for_share_limit(self, command):
        params = self._params
        if not isinstance(command, SubmitOrder) or params.max_leg_share is None:
            return command
        if params.max_leg_share <= 0:
            self._deny_order(command.order, f"max_leg_share gate: invalid max={params.max_leg_share:.4f}")
            return None

        order = command.order
        if not isinstance(order, LimitOrder):
            return command

        instrument = self._cache.instrument(order.instrument_id)
        if instrument is None:
            return command

        pair_id = self._pair_id_for_order(order)
        if pair_id is None:
            return command

        requested_share = self._order_share_if_wins(instrument, order)
        if requested_share <= 0:
            self._deny_order(order, f"max_leg_share gate: non-positive requested_share={requested_share:.4f}")
            return None

        roles = self._expected_outcome_roles(order)
        if not roles:
            role = self._outcome_role_from_instrument(instrument)
            roles = {role} if role else set()
        if not roles:
            return command

        current_shares = self._portfolio.outcome_shares(pair_id, order.account_id)
        remaining_by_role = {
            role: params.max_leg_share - float(current_shares.get(role, 0.0))
            for role in roles
        }
        min_remaining = min(remaining_by_role.values())
        if min_remaining <= 0:
            self._deny_order(
                order,
                f"max_leg_share gate: pair={pair_id} remaining={min_remaining:.4f} max={params.max_leg_share:.4f}",
            )
            return None

        scale = min(1.0, min_remaining / requested_share)
        adjusted_size = order.quantity.as_double() * scale
        adjusted_qty = Quantity(adjusted_size, precision=instrument.size_precision)
        if adjusted_qty.as_double() <= 0:
            self._deny_order(
                order,
                f"max_leg_share gate: adjusted_size={adjusted_size:.8f} precision={instrument.size_precision}",
            )
            return None
        if scale >= 1.0:
            return command

        adjusted_order = self._copy_limit_order_with_quantity(
            order=order,
            quantity=adjusted_qty,
        )
        return SubmitOrder(
            trader_id=command.trader_id,
            strategy_id=command.strategy_id,
            order=adjusted_order,
            command_id=command.id,
            ts_init=command.ts_init,
            position_id=command.position_id,
            client_id=command.client_id,
            params=command.params,
            correlation_id=command.correlation_id,
        )

    def _order_share_if_wins(self, instrument, order) -> float:
        size = order.quantity.as_double()
        if isinstance(instrument, BinaryOption):
            return size
        if isinstance(instrument, BettingInstrument):
            return size * float(order.price) * self._params.fx
        return size

    def _expected_outcome_roles(self, order) -> set[str]:
        meta = meta_from_order(order)
        if meta is None:
            return set()
        return {
            role
            for leg_key in meta.expected_legs
            for role in [self._outcome_role_from_leg_key(leg_key)]
            if role is not None
        }

    @staticmethod
    def _outcome_role_from_leg_key(leg_key: str) -> str | None:
        parts = str(leg_key).split(":")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
        return None

    @staticmethod
    def _outcome_role_from_instrument(instrument) -> str | None:
        info = getattr(instrument, "info", None) or {}
        role = info.get("selection_role") or info.get("market_type")
        return str(role) if role else None

    @staticmethod
    def _copy_limit_order_with_quantity(*, order, quantity):
        display_qty = order.display_qty
        if display_qty is not None and display_qty > quantity:
            display_qty = quantity
        return LimitOrder(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            order_side=order.side,
            quantity=quantity,
            price=order.price,
            init_id=order.init_id,
            ts_init=order.ts_init,
            time_in_force=order.time_in_force,
            expire_time_ns=order.expire_time_ns,
            post_only=order.is_post_only,
            reduce_only=order.is_reduce_only,
            quote_quantity=order.is_quote_quantity,
            display_qty=display_qty,
            emulation_trigger=order.emulation_trigger,
            trigger_instrument_id=order.trigger_instrument_id,
            contingency_type=order.contingency_type,
            order_list_id=order.order_list_id,
            linked_order_ids=order.linked_order_ids,
            parent_order_id=order.parent_order_id,
            exec_algorithm_id=order.exec_algorithm_id,
            exec_algorithm_params=order.exec_algorithm_params,
            exec_spawn_id=order.exec_spawn_id,
            tags=order.tags,
        )

    # ── NT 拦截 hook(覆盖 cpdef,签名须与父类一致:instrument, order)──
    def _check_order(self, instrument, order) -> bool:
        if not super()._check_order(instrument, order):  # NT: price/quantity/GTD
            return False
        if not self._check_required_venues_alive(order):
            return False
        if not self._check_balance(instrument, order):
            return False
        if not self._check_profit_gates(order):
            return False
        return True

    def _deny_order(self, order, reason: str) -> None:
        super()._deny_order(order, reason)
        meta = meta_from_order(order)
        if meta is None:
            return
        self._msgbus.publish(
            topic=RISK_LEG_DENIED_TOPIC,
            msg={
                "opportunity_id": meta.opportunity_id,
                "pair_id": meta.pair_id,
                "leg_key": meta.leg_key,
                "client_order_id": str(order.client_order_id),
                "reason": str(reason),
            },
        )

    # ── 应用层:余额(venue 非对称,Q17)────────────────────────────
    def _check_balance(self, instrument, order) -> bool:
        if not order.has_price:  # property(Python 侧;`has_price_c` 是 cdef 仅 Cython 可调)
            return True  # 无价单(市价)交给 NT 父类的 native 余额检查
        account = self._cache.account_for_venue(order.instrument_id.venue)
        if account is None:
            return True  # 账户未就绪,不在此处拦(NT 会处理)
        currency = instrument.quote_currency
        cost = self._order_cost(instrument, order)

        if order.instrument_id.venue.value == "POLYMARKET":
            # PM 上报 reported=True/locked=0/free=total → 不能信 cache free,自扣在途挂单
            total = account.balance_total(currency)
            if total is None:
                return True
            available = total.as_double() - self._pm_open_notional(order.instrument_id.venue, currency)
        else:
            # OE:WS 余额帧已含挂单占用,直接信 free
            free = account.balance_free(currency)
            if free is None:
                return True
            available = free.as_double()

        if cost > available:
            self._deny_order(
                order=order,
                reason=f"Insufficient balance: cost={cost:.4f} > available={available:.4f} {currency}",
            )
            return False
        return True

    def _order_cost(self, instrument, order) -> float:
        """新单的潜在占用(= 输掉时的 liability,与 outcome exposure 口径对齐)。"""
        size = order.leaves_qty.as_double()
        if isinstance(instrument, BinaryOption):
            return size * float(order.price)          # PM: size * price
        return size * self._params.fx                 # OE: size * fx

    def _pm_open_notional(self, venue, currency) -> float:
        total = 0.0
        for o in self._cache.orders_open(venue=venue):
            if o.has_price:
                total += o.leaves_qty.as_double() * float(o.price)
        return total

    # ── 应用层:venue execution liveness(跨 venue 同机会 fail-closed)────
    def _check_required_venues_alive(self, order) -> bool:
        liveness = getattr(self, "_arb_venue_liveness", None)
        if liveness is None:
            return True
        required = self._required_venues(order)
        missing = sorted(venue for venue in required if not liveness.venue_alive(venue))
        if missing:
            self._deny_order(
                order=order,
                reason=f"venue liveness gate: not alive={','.join(missing)}",
            )
            return False
        return True

    def _required_venues(self, order) -> set[str]:
        meta = meta_from_order(order)
        if meta is None:
            return {str(order.instrument_id.venue.value).upper()}

        venues = {
            venue
            for leg_key in meta.expected_legs
            for venue in [self._venue_from_leg_key(leg_key)]
            if venue is not None
        }
        if venues:
            return venues
        return {str(order.instrument_id.venue.value).upper()}

    @staticmethod
    def _venue_from_leg_key(leg_key: str) -> str | None:
        prefix = str(leg_key).split(":", 1)[0].lower()
        if prefix in {"pm", "polymarket"}:
            return "POLYMARKET"
        if prefix in {"oe", "orbitexch"}:
            return "ORBITEXCH"
        return None

    # ── 应用层:单场止盈/止损硬停(Q16 修订)────────────────────────────
    def _check_profit_gates(self, order) -> bool:
        if order_intent(order) == "recovery":
            return True

        pf = self._portfolio  # 实为 ArbitragePortfolio(import 替换后 kernel 原生构造)
        pair_id = self._pair_id_for_order(order)
        params = self._params

        if pair_id is None:
            return True

        exposures = pf.outcome_exposures(pair_id, order.account_id)
        if not exposures:
            return True

        profits = [exposure.net_profit for exposure in exposures.values()]
        tp_amount = params.share * params.match_tp
        sl_amount = params.share * params.match_sl

        # 1. match_tp:所有 outcome 绝对利润都超过目标 share*tp → 已赚够别加
        if all(profit > tp_amount for profit in profits):
            self._deny_order(order=order, reason=f"match_tp gate: pair={pair_id} all_profit>{tp_amount:.4f}")
            return False
        # 2. match_sl:所有 outcome 绝对利润都跌破 share*sl → 该场恶化别加
        if all(profit < sl_amount for profit in profits):
            self._deny_order(order=order, reason=f"match_sl gate: pair={pair_id} all_profit<{sl_amount:.4f}")
            return False
        return True

    def _pair_id_for_order(self, order) -> str | None:
        # #34:pair_id 经 ArbitragePortfolio 的 PairRegistry 读(matching 唯一写者)
        pf = self._portfolio
        registry = getattr(pf, "_pair_registry", None)
        if registry is None:
            return None
        return registry.get(order.instrument_id)
