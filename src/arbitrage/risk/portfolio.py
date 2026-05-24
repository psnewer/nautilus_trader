"""
ArbitragePortfolio —— NT Portfolio 子类,扩展领域指标 way_rebate(pull-based 纯函数)。

详细设计:`docs/arbitrage/architectures/risk/architecture.md §3.2 / §4.1 / §4.2`。
公式平移自旧 `services/risk/position.py`,但**腿来源改为从 NT Cache 的 Position 反推**
(不再自维护 _positions dict)。

instrument.info 契约(由 discovery 组件填充,本类只读,单一 seam 见 `_resolve_pair_id` /
`_leg_from_position`):
- info["competition"]  → pair_id(比赛级聚合键)
- info["market_type"]  → "home" / "draw" / "away"
venue / 公式分支由 instrument 类型判定(BinaryOption=PM,BettingInstrument=OE),不靠字符串。

NT `Portfolio` 是 cdef class,子类**只能加纯 Python 方法**(不能加 cpdef/cdef)。
"""

from __future__ import annotations

from nautilus_trader.model.instruments import BettingInstrument
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.portfolio.portfolio import Portfolio

from src.arbitrage.common.leg_settled import LegSettledRegistry


class _Leg:
    """从 NT Position 反推的单腿(way_rebate 计算用)。"""

    __slots__ = ("venue", "market_type", "size", "price", "fx")

    def __init__(self, venue: str, market_type: str, size: float, price: float, fx: float) -> None:
        self.venue = venue
        self.market_type = market_type
        self.size = size
        self.price = price
        self.fx = fx

    def profit_if_wins(self) -> float:
        if self.venue == "polymarket":
            return self.size * (1.0 - self.price)
        return self.size * (self.price - 1.0) * self.fx  # orbitexch

    def loss_if_loses(self) -> float:
        if self.venue == "polymarket":
            return self.size * self.price
        return self.size * self.fx  # orbitexch


class ArbitragePortfolio(Portfolio):
    """way_rebate 等领域指标。share/fx/leg_settled 由 launcher 经 `configure_arb` 注入
    (NT 构造时实参表固定,见 bootstrap.py)。"""

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
        fx: float = 1.0,
        leg_settled: LegSettledRegistry | None = None,
    ) -> None:
        self._arb_share = share
        self._arb_fx = fx
        self._arb_leg_settled = leg_settled

    # 兜底默认(configure_arb 未调用时)
    @property
    def _share(self) -> float:
        return getattr(self, "_arb_share", 100.0)

    @property
    def _fx(self) -> float:
        return getattr(self, "_arb_fx", 1.0)

    @property
    def _settled(self) -> LegSettledRegistry | None:
        return getattr(self, "_arb_leg_settled", None)

    # ── per-pair 指标 ────────────────────────────────────────────────
    def way_rebate(self, pair_id: str, account_id=None) -> dict[str, float]:
        """各方向持仓返水率。settled gate(§4.2):该 pair 任一腿 false → 返回 {}(fail-closed)。"""
        if self._settled is not None and self._settled.any_unsettled(pair_id):
            return {}
        return self._compute_way_rebate(self._legs_for_pair(pair_id, account_id))

    def min_way_rebate(self, pair_id: str, account_id=None) -> float | None:
        rebate = self.way_rebate(pair_id, account_id)
        if not rebate:
            return None
        return min(rebate.values())

    def way_rebates_by_venue(self, pair_id: str, account_id=None) -> dict[str, dict[str, float]]:
        if self._settled is not None and self._settled.any_unsettled(pair_id):
            return {}
        legs = self._legs_for_pair(pair_id, account_id)
        result: dict[str, dict[str, float]] = {}
        for venue in {leg.venue for leg in legs}:
            result[venue] = self._compute_way_rebate([leg for leg in legs if leg.venue == venue])
        return result

    # ── 全账户聚合(只遍历有 open position 的 active pair)──────────────
    def global_min_rebate_sum(self, account_id=None) -> float | None:
        """∑ 各 active pair 的 min_way_rebate。任一 active pair 一腿 false → None(fail-closed)。"""
        total = 0.0
        for pair_id in self._active_pair_ids(account_id):
            if self._settled is not None and self._settled.any_unsettled(pair_id):
                return None
            m = self.min_way_rebate(pair_id, account_id)
            if m is not None:
                total += m
        return total

    # ── 内部 ─────────────────────────────────────────────────────────
    def _compute_way_rebate(self, legs: list[_Leg]) -> dict[str, float]:
        if not legs:
            return {}
        outcomes = {"home", "away"}
        if any(leg.market_type == "draw" for leg in legs):
            outcomes.add("draw")
        share = self._share
        result: dict[str, float] = {}
        for outcome in outcomes:
            net = 0.0
            for leg in legs:
                if leg.market_type == outcome:
                    net += leg.profit_if_wins()
                else:
                    net -= leg.loss_if_loses()
            result[outcome] = net / share if share > 0 else 0.0
        return result

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

    # ── instrument.info 读取 seam(契约由 discovery 满足)──────────────
    def _resolve_pair_id(self, position) -> str | None:
        instrument = self._arb_cache.instrument(position.instrument_id)
        if instrument is None or not instrument.info:
            return None
        return instrument.info.get("competition")

    def _leg_from_position(self, position) -> _Leg | None:
        instrument = self._arb_cache.instrument(position.instrument_id)
        if instrument is None or not instrument.info:
            return None
        market_type = instrument.info.get("market_type")
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
            fx=self._fx if venue == "orbitexch" else 1.0,
        )
