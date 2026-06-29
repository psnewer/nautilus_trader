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

from dataclasses import replace

from nautilus_trader.live.risk_engine import LiveRiskEngine
from nautilus_trader.model.enums import TradingState
from nautilus_trader.model.instruments import BinaryOption

from src.arbitrage.common.opportunity import RISK_LEG_DENIED_TOPIC
from src.arbitrage.common.opportunity import meta_from_order
from src.arbitrage.common.control import TOPIC_ARBITRAGE_PARAMS
from src.arbitrage.common.control import TOPIC_RISK_PARAMS
from src.arbitrage.common.control import TOPIC_TRADING_STATE
from src.arbitrage.common.control import SetArbitrageParamsCommand
from src.arbitrage.common.control import SetRiskParamsCommand
from src.arbitrage.common.control import SetTradingStateCommand
from src.arbitrage.common.opportunity import order_intent
from src.arbitrage.common.params import ArbitrageParams
from src.arbitrage.common.utils import orbitexch_odds_to_probability
from src.arbitrage.common.utils import polymarket_price_to_probability
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.risk.config import ArbRiskParams


class ArbitrageLiveRiskEngine(LiveRiskEngine):

    # ── 注入(launcher 在 kernel 原生构造后调用)─────────────────────
    def configure_arb(
        self,
        params: ArbRiskParams,
        *,
        venue_liveness: VenueExecutionLiveness | None = None,
        arbitrage_params: ArbitrageParams | None = None,
    ) -> None:
        self._arb_params = params
        self._arb_arbitrage_params = arbitrage_params or ArbitrageParams()
        self._arb_venue_liveness = venue_liveness
        # 控制台命令订阅(方案乙;web §8.3)——幂等:configure_arb 可能被多次调用。
        if self._msgbus is not None and not getattr(self, "_arb_control_subscribed", False):
            self._msgbus.subscribe(topic=TOPIC_TRADING_STATE, handler=self._on_set_trading_state_cmd)
            self._msgbus.subscribe(topic=TOPIC_RISK_PARAMS, handler=self._on_set_risk_params_cmd)
            self._msgbus.subscribe(topic=TOPIC_ARBITRAGE_PARAMS, handler=self._on_set_arbitrage_params_cmd)
            self._arb_control_subscribed = True

    # ── 控制台命令 handler(web publish → 本引擎 apply)─────────────────
    def _on_set_trading_state_cmd(self, cmd) -> None:
        if not isinstance(cmd, SetTradingStateCommand):
            return
        state = {"ACTIVE": TradingState.ACTIVE, "HALTED": TradingState.HALTED}.get(cmd.state)
        if state is None:
            self._log.error(f"invalid trading_state command: {cmd.state!r}")
            return
        self.set_trading_state(state)  # NT 原生:发 TradingStateChanged + 后续 HALTED 拦 submit

    def _on_set_risk_params_cmd(self, cmd) -> None:
        if not isinstance(cmd, SetRiskParamsCommand):
            return
        overrides = {
            k: v
            for k, v in (
                ("match_tp", cmd.match_tp),
                ("match_sl", cmd.match_sl),
                ("min_probability", cmd.min_probability),
                ("max_probability", cmd.max_probability),
            )
            if v is not None
        }
        if not overrides:
            return
        next_params = replace(self._params, **overrides)
        if not self._valid_probability_bounds(next_params):
            self._log.error(
                "invalid risk params hot-update: "
                f"min_probability={next_params.min_probability}, max_probability={next_params.max_probability}"
            )
            return
        self._arb_params = next_params
        self._log.info(f"risk params hot-updated: {overrides}")

    def _on_set_arbitrage_params_cmd(self, cmd) -> None:
        if not isinstance(cmd, SetArbitrageParamsCommand):
            return
        overrides = {
            k: v
            for k, v in (
                ("share", cmd.share),
                ("max_leg_share", cmd.max_leg_share),
                ("fx", cmd.fx),
            )
            if v is not None
        }
        if not overrides:
            return
        self._arb_arbitrage_params = replace(self._arbitrage_params, **overrides)
        self._log.info(f"arbitrage params hot-updated: {overrides}")

    @property
    def _params(self) -> ArbRiskParams:
        return getattr(self, "_arb_params", None) or ArbRiskParams()

    @property
    def _arbitrage_params(self) -> ArbitrageParams:
        return getattr(self, "_arb_arbitrage_params", None) or ArbitrageParams()

    # ── NT 拦截 hook(覆盖 cpdef,签名须与父类一致:instrument, order)──
    def _check_order(self, instrument, order) -> bool:
        if not super()._check_order(instrument, order):  # NT: price/quantity/GTD
            return False
        if not self._check_probability_gate(instrument, order):
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
        return size * self._arbitrage_params.fx       # OE: size * fx

    def _pm_open_notional(self, venue, currency) -> float:
        total = 0.0
        for o in self._cache.orders_open(venue=venue):
            if o.has_price:
                total += o.leaves_qty.as_double() * float(o.price)
        return total

    # ── 应用层:赔率/概率门控───────────────────────────────────────
    def _check_probability_gate(self, instrument, order) -> bool:
        """PM price 即概率;OE 十进制赔率换算为隐含概率 1/price。"""
        if not order.has_price:
            return True

        params = self._params
        if not self._valid_probability_bounds(params):
            self._deny_order(
                order=order,
                reason=(
                    "probability gate config invalid: "
                    f"min={params.min_probability:.4f}, max={params.max_probability:.4f}"
                ),
            )
            return False

        if isinstance(instrument, BinaryOption):
            probability = polymarket_price_to_probability(order.price)
        else:
            probability = orbitexch_odds_to_probability(order.price)

        if probability < params.min_probability or probability > params.max_probability:
            self._deny_order(
                order=order,
                reason=(
                    "probability gate: "
                    f"probability={probability:.4f} outside "
                    f"[{params.min_probability:.4f}, {params.max_probability:.4f}]"
                ),
            )
            return False
        return True

    @staticmethod
    def _valid_probability_bounds(params: ArbRiskParams) -> bool:
        return 0.0 <= params.min_probability < params.max_probability <= 1.0

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
        share = self._arbitrage_params.share
        tp_amount = share * params.match_tp
        sl_amount = share * params.match_sl

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
