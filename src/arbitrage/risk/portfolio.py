"""
ArbitragePortfolio —— NT Portfolio 子类,扩展 outcome exposure / outcome share 等持仓指标。

详细设计:`docs/arbitrage/architectures/risk/architecture.md §3.2 / §4.1 / §4.2`。
公式平移自旧 `services/risk/position.py`,但**腿来源改为从 NT Cache 的 Position 反推**
(不再自维护 _positions dict)。

**pair_id 来源(#34 修正)**:由 matching 算出,经 `PairRegistry` 暴露;本类经 `_resolve_pair_id`
读 registry(原"info["competition"] → pair_id"是错读,competition 是联赛名)。

instrument.info 契约(由 discovery 组件填充,本类只读;单一 seam 见 `_leg_from_position`):
- info["sport"] / info["competition"](联赛名)/ info["home_team"] / info["away_team"] / info["start_ts"] —— **matching 输入**
- info["selection_role"]("home"/"draw"/"away")—— outcome 指标计算用("market_type" 同义)
venue / 公式分支由 instrument 类型判定(BinaryOption=PM,BettingInstrument=OE),不靠字符串。

NT `Portfolio` 是 cdef class,子类**只能加纯 Python 方法**(不能加 cpdef/cdef)。
"""

from __future__ import annotations

from dataclasses import dataclass

from nautilus_trader.model.instruments import BettingInstrument
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.portfolio.portfolio import Portfolio

from src.arbitrage.common.pair_registry import PairRegistry


class _Leg:
    """从 NT Position 反推的单腿(outcome 指标计算用,size 已是 USD 口径)。"""

    __slots__ = ("venue", "market_type", "size", "price")

    def __init__(self, venue: str, market_type: str, size: float, price: float) -> None:
        self.venue = venue
        self.market_type = market_type
        self.size = size
        self.price = price

    def profit_if_wins(self) -> float:
        if self.venue == "polymarket":
            return self.size * (1.0 - self.price)
        return self.size * (self.price - 1.0)  # orbitexch

    def loss_if_loses(self) -> float:
        if self.venue == "polymarket":
            return self.size * self.price
        return self.size  # orbitexch

    def share_if_wins(self) -> float:
        if self.venue == "polymarket":
            return self.size
        return self.size * self.price  # orbitexch gross payout


@dataclass(frozen=True, slots=True)
class OutcomeExposure:
    """某个 outcome 发生时的绝对金额风险/收益。"""

    net_profit: float
    liability: float


class ArbitragePortfolio(Portfolio):
    """outcome exposure / share 领域指标。OE position size 已由 adapter 归一到 USD。"""

    def __init__(self, msgbus, cache, clock, config=None) -> None:
        super().__init__(msgbus=msgbus, cache=cache, clock=clock, config=config)
        # 基类 `_cache` 是私有 cdef(非 readonly),Python 子类方法读不到 → 自存一份引用。
        # kernel 原生构造时按此实参表调用(import 替换,见 bootstrap.py)。
        self._arb_cache = cache

    # ── 注入(launcher 在 kernel 原生构造后调用)─────────────────────
    def configure_arb(
        self,
        *,
        share: float = 100.0,
        pair_registry: PairRegistry | None = None,
    ) -> None:
        self._arb_share = share
        self._arb_pair_registry = pair_registry  # #34: matching 写,本类读;`_resolve_pair_id` 用

    # 兜底默认(configure_arb 未调用时)
    @property
    def _share(self) -> float:
        return getattr(self, "_arb_share", 100.0)

    @property
    def _pair_registry(self) -> PairRegistry | None:
        return getattr(self, "_arb_pair_registry", None)

    # ── per-pair 指标 ────────────────────────────────────────────────
    def outcome_exposures(self, pair_id: str, account_id=None) -> dict[str, OutcomeExposure]:
        """各 outcome 的绝对金额净利润与 liability。Risk 门控只读这个接口。"""
        legs = self._legs_for_pair(pair_id, account_id)
        return self._compute_outcome_exposures(legs, outcomes=self._outcomes_for_pair(pair_id, legs))

    def outcome_shares(self, pair_id: str, account_id=None) -> dict[str, float]:
        """各 outcome 当前持仓 share。Strategy share_limit action 用于计算剩余额度。"""
        legs = self._legs_for_pair(pair_id, account_id)
        outcomes = self._outcomes_for_pair(pair_id, legs)
        return {
            outcome: sum(leg.share_if_wins() for leg in legs if leg.market_type == outcome)
            for outcome in outcomes
        }

    def outcome_shares_for_venue(
        self, pair_id: str, venue: str, account_id=None
    ) -> dict[str, float]:
        """某 venue 各 outcome 的 share。PM 按单腿门控用。"""
        venue_lower = venue.lower()
        legs = [leg for leg in self._legs_for_pair(pair_id, account_id) if leg.venue == venue_lower]
        outcomes = self._outcomes_from_legs(legs) or {"home", "away"}
        return {
            outcome: sum(leg.share_if_wins() for leg in legs if leg.market_type == outcome)
            for outcome in outcomes
        }

    # ── 内部 ─────────────────────────────────────────────────────────
    def _compute_outcome_exposures(
        self,
        legs: list[_Leg],
        *,
        outcomes: set[str] | None = None,
    ) -> dict[str, OutcomeExposure]:
        if not legs:
            return {}
        outcomes = outcomes or self._outcomes_from_legs(legs)

        result: dict[str, OutcomeExposure] = {}
        for outcome in outcomes:
            profit = sum(leg.profit_if_wins() for leg in legs if leg.market_type == outcome)
            liability = sum(leg.loss_if_loses() for leg in legs if leg.market_type != outcome)
            result[outcome] = OutcomeExposure(net_profit=profit - liability, liability=liability)
        return result

    def _outcomes_for_pair(self, pair_id: str, legs: list[_Leg]) -> set[str]:
        outcomes = self._outcomes_from_registry(pair_id)
        if outcomes:
            return outcomes
        return self._outcomes_from_legs(legs)

    @staticmethod
    def _outcomes_from_legs(legs: list[_Leg]) -> set[str]:
        outcomes = {"home", "away"}
        if any(leg.market_type == "draw" for leg in legs):
            outcomes.add("draw")
        return outcomes

    def _outcomes_from_registry(self, pair_id: str) -> set[str]:
        registry = self._pair_registry
        if registry is None or not hasattr(registry, "instrument_ids_for_pair"):
            return set()
        outcomes: set[str] = set()
        for instrument_id in registry.instrument_ids_for_pair(pair_id):
            instrument = self._arb_cache.instrument(InstrumentId.from_str(str(instrument_id)))
            if instrument is None or not instrument.info:
                continue
            market_type = instrument.info.get("selection_role") or instrument.info.get("market_type")
            if market_type:
                outcomes.add(str(market_type))
        return outcomes

    def _active_pair_ids(self, account_id=None) -> set[str]:
        pair_ids: set[str] = set()
        for position in self._arb_cache.positions_open(account_id=account_id):
            pair_id = self._resolve_pair_id(position)
            if pair_id:
                pair_ids.add(pair_id)
        return pair_ids

    def _legs_for_pair(self, pair_id: str, account_id=None) -> list[_Leg]:
        legs: list[_Leg] = []
        for position in self._arb_cache.positions_open(account_id=account_id):
            if self._resolve_pair_id(position) != pair_id:
                continue
            leg = self._leg_from_position(position)
            if leg is not None:
                legs.append(leg)
        return legs

    # ── pair_id 来源(#34:由 matching 经 PairRegistry 提供;**不是** info["competition"])──
    def _resolve_pair_id(self, position) -> str | None:
        registry = self._pair_registry
        if registry is None:
            return None  # registry 未注入(测试/启动早期)→ 不参与 pair 聚合
        return registry.get(position.instrument_id)

    def _leg_from_position(self, position) -> _Leg | None:
        instrument = self._arb_cache.instrument(position.instrument_id)
        if instrument is None or not instrument.info:
            return None
        # Q9 标准 key:`selection_role`("home"/"draw"/"away");兼容旧 `market_type`
        market_type = instrument.info.get("selection_role") or instrument.info.get("market_type")
        if not market_type:
            return None
        if isinstance(instrument, BinaryOption):
            venue = "polymarket"
        elif isinstance(instrument, BettingInstrument):
            venue = "orbitexch"
        else:
            return None
        return _Leg(
            venue=venue,
            market_type=market_type,
            size=abs(position.quantity.as_double()),
            price=position.avg_px_open,
        )
