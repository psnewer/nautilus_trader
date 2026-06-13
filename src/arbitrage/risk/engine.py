"""
ArbitrageLiveRiskEngine —— NT LiveRiskEngine 子类(实盘环境 kernel 用的是 LiveRiskEngine,
非基类 RiskEngine)。在 submit 管道上透明拦截:NT 父类自动检查(price/quantity/GTD +
notional/submit_rate/TradingState/native 余额)+ 应用层余额检查 + 组合级硬停。

详细设计:`docs/arbitrage/architectures/risk/architecture.md §3.1 / §4`。

要点:
- `_check_order` 是 NT cpdef,Python 子类覆盖会被父类 `_handle_submit_order` 调到(cpdef 语义)。
- 自定义拒绝**必须自己 emit denied 事件**(父类 `_handle_submit_order` 见 False 仅 return,
  指望 _check_order 已调 `_deny_order`),否则 Strategy.on_order_denied 不触发。
- `CancelOrder` 走另一条命令通路,不经 _check_order,故补偿撤单永远放行。
- `arb:intent=recovery` 的补救下单仍走 NT 基础检查 + 余额检查,但跳过 rebate gates
  (match_tp/match_sl/global_sl),避免“别开新仓”硬停挡住降风险补救。
- 门限读 **live** Cache(非 Strategy 快照)。tp/sl/global 经 self._portfolio(实为
  ArbitragePortfolio)pull way_rebate。
"""

from __future__ import annotations

from nautilus_trader.live.risk_engine import LiveRiskEngine
from nautilus_trader.model.instruments import BinaryOption

from src.arbitrage.risk.config import ArbRiskParams


class ArbitrageLiveRiskEngine(LiveRiskEngine):

    # ── 注入(launcher 在 kernel 原生构造后调用)─────────────────────
    def configure_arb(self, params: ArbRiskParams) -> None:
        self._arb_params = params

    @property
    def _params(self) -> ArbRiskParams:
        return getattr(self, "_arb_params", None) or ArbRiskParams()

    # ── NT 拦截 hook(覆盖 cpdef,签名须与父类一致:instrument, order)──
    def _check_order(self, instrument, order) -> bool:
        if not super()._check_order(instrument, order):  # NT: price/quantity/GTD
            return False
        if not self._check_balance(instrument, order):
            return False
        if not self._check_rebate_gates(order):
            return False
        return True

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
        """新单的潜在占用(= 输掉时的 liability,与 way_rebate.loss_if_loses 对齐)。"""
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

    # ── 应用层:组合级硬停三门限(Q16)────────────────────────────────
    def _check_rebate_gates(self, order) -> bool:
        if _order_intent(order) == "recovery":
            return True

        pf = self._portfolio  # 实为 ArbitragePortfolio(import 替换后 kernel 原生构造)
        pair_id = self._pair_id_for_order(order)
        params = self._params

        if pair_id is not None:
            rebate = pf.way_rebate(pair_id, order.account_id)
            # 1. match_tp:任一方向已 ≥ tp → 已赚够别加
            if rebate and max(rebate.values()) >= params.match_tp:
                self._deny_order(order=order, reason=f"match_tp gate: pair={pair_id} rebate≥{params.match_tp}")
                return False
            # 2. match_sl:该 pair min < sl → 该场恶化别加
            min_rebate = min(rebate.values()) if rebate else None
            if min_rebate is not None and min_rebate < params.match_sl:
                self._deny_order(order=order, reason=f"match_sl gate: pair={pair_id} min={min_rebate:.4f}<{params.match_sl}")
                return False

        # 3. global_sl + settled gate:None(任一 active pair 一腿未结算)→ fail-closed deny
        global_sum = pf.global_min_rebate_sum(order.account_id)
        if global_sum is None:
            self._deny_order(order=order, reason="settled gate: global_min_rebate_sum is None (unsettled leg) → fail-closed")
            return False
        if global_sum < params.global_sl:
            self._deny_order(order=order, reason=f"global_sl gate: sum={global_sum:.4f}<{params.global_sl}")
            return False
        return True

    def _pair_id_for_order(self, order) -> str | None:
        # #34:pair_id 经 ArbitragePortfolio 的 PairRegistry 读(matching 唯一写者)
        pf = self._portfolio
        registry = getattr(pf, "_pair_registry", None)
        if registry is None:
            return None
        return registry.get(order.instrument_id)


def _order_intent(order) -> str:
    """从 NT order tags 读取套利域意图。默认 `arbitrage` 保持旧行为。"""
    for tag in getattr(order, "tags", None) or []:
        if isinstance(tag, str) and tag.startswith("arb:intent="):
            return tag.split("=", 1)[1]
    return "arbitrage"
