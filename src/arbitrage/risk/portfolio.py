"""
ArbitragePortfolio —— NT Portfolio 子类,扩展 outcome exposure / outcome share 等持仓指标。

详细设计:`docs/arbitrage/architectures/risk/architecture.md §3.2 / §4.1 / §4.2`。
公式使用当前 outcome exposure 契约，**腿来源从 NT Cache 的 Position 反推**
(不再自维护 _positions dict)。

**pair_id 来源(#34 修正)**:由 matching 算出,经 `PairRegistry` 暴露;本类经 `_resolve_pair_id`
读 registry(原"info["competition"] → pair_id"是错读,competition 是联赛名)。

instrument.info 契约(由 discovery 组件填充,本类只读;单一 seam 见 `_leg_from_position`):
- info["sport"] / info["competition"](联赛名)/ info["home_team"] / info["away_team"] / info["start_ts"] —— **matching 输入**
- info["selection_role"]("home"/"draw"/"away")—— outcome 指标计算用("market_type" 同义)
venue / 公式分支由 Venue Registry 的 odds_model 判定,具体 venue 仍取 `instrument.id.venue`。

NT `Portfolio` 是 cdef class,子类**只能加纯 Python 方法**(不能加 cpdef/cdef)。
"""

from __future__ import annotations

from dataclasses import dataclass

from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import BettingInstrument
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.portfolio.portfolio import Portfolio
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.realized_pnl import RealizedPnlLedger
from src.arbitrage.common.venues import PositionOutcomeInvariantError
from src.arbitrage.common.venues import is_dust_position
from src.arbitrage.common.venues import leg_economics
from src.arbitrage.common.venues import outcome_for_position
from src.arbitrage.common.venues import venue_id_from_instrument_id


class _Leg:
    """从 NT Position 反推的单腿(outcome 指标计算用,size 已是 USD 口径)。"""

    __slots__ = ("venue", "market_type", "size", "price", "is_lay")

    def __init__(
        self,
        venue: str,
        market_type: str,
        size: float,
        price: float,
        is_lay: bool = False,
    ) -> None:
        self.venue = venue
        self.market_type = market_type
        self.size = size
        self.price = price
        self.is_lay = is_lay

    def profit_if_wins(self) -> float:
        return leg_economics(
            self.venue,
            self.price,
            self.size,
            is_lay=self.is_lay,
        ).profit_if_wins

    def loss_if_loses(self) -> float:
        return leg_economics(
            self.venue,
            self.price,
            self.size,
            is_lay=self.is_lay,
        ).loss_if_loses

    def share_if_wins(self) -> float:
        return leg_economics(
            self.venue,
            self.price,
            self.size,
            is_lay=self.is_lay,
        ).share_if_wins


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
        realized_pnl_ledger: RealizedPnlLedger | None = None,
    ) -> None:
        self._arb_share = share
        self._arb_pair_registry = pair_registry  # #34: matching 写,本类读;`_resolve_pair_id` 用
        self._arb_realized_pnl_ledger = realized_pnl_ledger

    # 兜底默认(configure_arb 未调用时)
    @property
    def _share(self) -> float:
        return getattr(self, "_arb_share", 100.0)

    @property
    def _pair_registry(self) -> PairRegistry | None:
        return getattr(self, "_arb_pair_registry", None)

    @property
    def _realized_pnl_ledger(self) -> RealizedPnlLedger | None:
        return getattr(self, "_arb_realized_pnl_ledger", None)

    # ── per-pair 指标 ────────────────────────────────────────────────
    def outcome_exposures(self, pair_id: str, account_id=None) -> dict[str, OutcomeExposure]:
        """各 outcome 的绝对金额净利润与 liability。Risk 门控只读这个接口。"""
        legs = self._legs_for_pair(pair_id, account_id)
        outcomes = self._outcomes_for_pair(pair_id, legs)
        exposures = self._compute_outcome_exposures(legs, outcomes=outcomes)
        realized_pnl = self._realized_pnl_for_pair(pair_id, account_id)
        if not exposures or abs(realized_pnl) <= 1e-12:
            return exposures
        return {
            outcome: OutcomeExposure(
                net_profit=exposure.net_profit + realized_pnl,
                liability=exposure.liability,
            )
            for outcome, exposure in exposures.items()
        }

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
        all_legs = self._legs_for_pair(pair_id, account_id)
        legs = [leg for leg in all_legs if leg.venue == venue_lower]
        outcomes = self._outcomes_for_pair(pair_id, all_legs)
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
        if not legs and not outcomes:
            return {}
        outcomes = outcomes or self._outcomes_from_legs(legs)

        result: dict[str, OutcomeExposure] = {}
        for outcome in outcomes:
            profit = sum(leg.profit_if_wins() for leg in legs if leg.market_type == outcome)
            liability = sum(leg.loss_if_loses() for leg in legs if leg.market_type != outcome)
            result[outcome] = OutcomeExposure(net_profit=profit - liability, liability=liability)
        return result

    def _realized_pnl_for_pair(self, pair_id: str, account_id=None) -> float:
        registry = self._pair_registry
        if registry is None:
            return 0.0
        ledger = self._realized_pnl_ledger
        total = 0.0
        for raw_instrument_id in registry.instrument_ids_for_pair(pair_id):
            instrument_id = InstrumentId.from_str(str(raw_instrument_id))
            pnl = self.realized_pnl(instrument_id, account_id)
            if pnl is not None:
                total += pnl.as_double()
            if ledger is not None:
                total += ledger.instrument_adjustment(instrument_id, account_id)
        return total

    def _outcomes_for_pair(self, pair_id: str, legs: list[_Leg]) -> set[str]:
        outcomes = self._outcomes_from_registry(pair_id)
        if outcomes:
            return outcomes
        return self._outcomes_from_legs(legs)

    @staticmethod
    def _outcomes_from_legs(legs: list[_Leg]) -> set[str]:
        if not legs:
            return set()
        outcomes = {leg.market_type for leg in legs}
        if not outcomes.issubset({"yes", "no"}):
            raise PositionOutcomeInvariantError(
                f"portfolio legs must use yes/no outcomes: outcomes={sorted(outcomes)}",
            )
        return {"yes", "no"}

    def _outcomes_from_registry(self, pair_id: str) -> set[str]:
        registry = self._pair_registry
        if registry is None or not hasattr(registry, "instrument_ids_for_pair"):
            return set()
        outcomes: set[str] = set()
        for instrument_id in registry.instrument_ids_for_pair(pair_id):
            instrument = self._arb_cache.instrument(InstrumentId.from_str(str(instrument_id)))
            if instrument is None or not instrument.info:
                continue
            claim = instrument.info.get("claim")
            if not claim:
                raise PositionOutcomeInvariantError(
                    f"registered instrument missing claim: instrument_id={instrument_id}",
                )
            outcomes.add(str(claim).lower())
        if outcomes and outcomes != {"yes", "no"}:
            raise PositionOutcomeInvariantError(
                f"pair outcomes must be yes/no: pair_id={pair_id}, outcomes={sorted(outcomes)}",
            )
        return outcomes

    def _active_pair_ids(self, account_id=None) -> set[str]:
        pair_ids: set[str] = set()
        for position in self._arb_cache.positions_open(account_id=account_id):
            pair_id = self._resolve_pair_id(position)
            if pair_id:
                pair_ids.add(pair_id)
        return pair_ids

    def _legs_for_pair(self, pair_id: str, account_id=None) -> list[_Leg]:
        positions = [
            position
            for position in self._arb_cache.positions_open(account_id=account_id)
            if self._resolve_pair_id(position) == pair_id
        ]
        outcomes = self._outcomes_from_registry(pair_id) or self._outcomes_from_positions(positions)
        legs: list[_Leg] = []
        for position in positions:
            leg = self._leg_from_position(position, outcomes=outcomes)
            if leg is not None:
                legs.append(leg)
        return legs

    def _outcomes_from_positions(self, positions) -> set[str]:
        outcomes: set[str] = set()
        for position in positions:
            instrument = self._arb_cache.instrument(position.instrument_id)
            info = getattr(instrument, "info", None)
            claim = (info or {}).get("claim")
            if not claim:
                raise PositionOutcomeInvariantError(
                    f"position instrument missing claim: instrument_id={position.instrument_id}",
                )
            outcomes.add(str(claim).lower())
        return {"yes", "no"} if outcomes else set()

    # ── pair_id 来源(#34:由 matching 经 PairRegistry 提供;**不是** info["competition"])──
    def _resolve_pair_id(self, position) -> str | None:
        registry = self._pair_registry
        if registry is None:
            return None  # registry 未注入(测试/启动早期)→ 不参与 pair 聚合
        return registry.get(position.instrument_id)

    def _leg_from_position(self, position, *, outcomes: set[str] | None = None) -> _Leg | None:
        instrument = self._arb_cache.instrument(position.instrument_id)
        if instrument is None or not instrument.info:
            return None
        claim = instrument.info.get("claim")
        if not claim:
            raise PositionOutcomeInvariantError(
                f"position instrument missing claim: instrument_id={position.instrument_id}",
            )
        if not isinstance(instrument, (BinaryOption, BettingInstrument)):
            return None
        venue_id = venue_id_from_instrument_id(instrument.id)
        if not venue_id:
            return None
        venue = venue_id.lower()
        position_side = getattr(position, "side", None)
        if outcomes is None:
            outcomes = {"yes", "no"}
        market_type = outcome_for_position(
            venue_id,
            outcomes,
            selection_role=None,
            claim=claim,
            position_side=position_side,
            size=abs(position.quantity.as_double()),
        )
        if market_type is None:
            # dust(venue 撮合误差,±dust 净仓)→ 忽略;真正无法映射 → fail-closed。
            if is_dust_position(position):
                return None
            raise PositionOutcomeInvariantError(
                f"position cannot map to pair outcome: instrument_id={position.instrument_id}",
            )
        return _Leg(
            venue=venue,
            market_type=market_type,
            size=abs(position.quantity.as_double()),
            price=position.avg_px_open,
            is_lay=position_side == PositionSide.SHORT,
        )
