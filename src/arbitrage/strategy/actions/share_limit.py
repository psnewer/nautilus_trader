"""
ShareLimitModification —— 在 strategy action 链中执行 share limit 缩放。

两种输入:
- 单一机会:读取 `ctx.scratch["legs"]`,按 leg 自带 `share_if_wins/qty` 计算目标 share,
  直接写回调整后的 `qty/share_if_wins/cost`。
- candidate 数组:读取 `ctx.scratch["candidates"]`,对每个 candidate 独立计算 scale,
  输出调整后的 candidate 数组,供后续 `CandiSelectAction` 选择。

可选 `current_position_gate=True` 时,先按 Portfolio 跨 venue 聚合的当前持仓 outcome 筛腿。
可选 `current_order_gate={"enable": true}` 时,只要当前 pair 存在任意 open order 就清空输出；
`ignore_start_price=true` 时忽略该 pair 首张且唯一曾提交的 open order。

复用原公式,按 Venue Registry odds_model 分支:
  - probability venue: remaining = max - current[role]（单腿独立检查）
  - decimal odds venue: remaining = max - merged[role]（merge后一边为0）
"""

from __future__ import annotations

import logging
from copy import deepcopy

from src.arbitrage.common.venues import PositionOutcomeInvariantError
from src.arbitrage.common.venues import is_decimal_odds_venue
from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class ShareLimitModification(Action):
    """按当前仓位 outcome 过滤,并按 share limit 调整 legs 或 candidates。"""

    def __init__(
        self,
        max_leg_share: float | None = None,
        current_position_gate: bool = False,
        current_order_gate: dict | None = None,
    ) -> None:
        if not isinstance(current_position_gate, bool):
            raise ValueError("share_limit: current_position_gate must be a boolean")
        order_gate = _normalize_current_order_gate(current_order_gate)
        self._max_leg_share = float(max_leg_share) if max_leg_share is not None else None
        self._current_position_gate = current_position_gate
        self._current_order_gate = order_gate["enable"]
        self._ignore_start_price_orders = order_gate["ignore_start_price"]

    async def execute(self, ctx: EvalContext) -> None:
        if self._current_order_gate and not self._apply_current_order_gate(ctx):
            return
        if self._current_position_gate and not self._apply_current_position_gate(ctx):
            return

        max_leg_share = self._configured_max_leg_share(ctx)
        if max_leg_share is None:
            return
        if self._adjust_candidates(ctx):
            return

        legs = ctx.scratch.get("legs", [])
        if not legs:
            return

        portfolio = ctx.portfolio
        if portfolio is None:
            _LOG.warning(f"ShareLimitModification: pair={ctx.pair_id} no portfolio, skip")
            return

        requested_by_leg = []
        remaining_values = []
        scale = 1.0
        for leg in legs:
            venue = str(leg.get("venue", "")).upper()
            role = str(leg.get("role", ""))
            if not venue or not role:
                continue
            requested_share = _leg_share_if_wins(leg, venue)
            if requested_share is None or requested_share <= 0:
                _LOG.warning(
                    f"ShareLimitModification: pair={ctx.pair_id} leg={leg.get('instrument_id')} "
                    "missing qty/share_if_wins, clear legs",
                )
                ctx.scratch["legs"] = []
                return
            if is_decimal_odds_venue(venue):
                try:
                    remaining = self._decimal_remaining(portfolio, ctx.pair_id, venue, role, max_leg_share)
                except PositionOutcomeInvariantError as e:
                    _LOG.error(f"ShareLimitModification: pair={ctx.pair_id} portfolio invariant: {e}")
                    ctx.scratch["legs"] = []
                    return
            else:
                try:
                    remaining = self._probability_remaining(portfolio, ctx.pair_id, venue, role, max_leg_share)
                except PositionOutcomeInvariantError as e:
                    _LOG.error(f"ShareLimitModification: pair={ctx.pair_id} portfolio invariant: {e}")
                    ctx.scratch["legs"] = []
                    return
            if remaining <= 0:
                _LOG.info(
                    f"ShareLimitModification: pair={ctx.pair_id} remaining={remaining:.4f} "
                    f"<= 0, clear legs"
                )
                ctx.scratch["legs"] = []
                return
            scale = min(scale, remaining / requested_share)
            remaining_values.append(remaining)
            requested_by_leg.append((leg, requested_share))

        if not requested_by_leg:
            return

        ctx.scratch["legs"] = _adjust_legs(requested_by_leg, scale)
        ctx.scratch["share_limit_scale"] = scale
        ctx.scratch["adjusted_share"] = min(s for _, s in requested_by_leg) * scale
        _LOG.debug(
            f"ShareLimitModification: pair={ctx.pair_id} scale={scale:.4f} "
            f"adjusted_share={ctx.scratch['adjusted_share']:.4f}"
        )

    def _apply_current_order_gate(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            _LOG.warning(
                f"ShareLimitModification[current_order_gate]: pair={ctx.pair_id} "
                "missing cache/pair_registry, clear output",
            )
            self._clear_all_outputs(ctx)
            return False

        try:
            instrument_ids = pair_instrument_ids(ctx)
            submitted_orders = [
                order
                for instrument_id in instrument_ids
                for order in (ctx.cache.orders(instrument_id=instrument_id) or ())
                if _was_submitted(order)
            ]
            for instrument_id in instrument_ids:
                open_orders = ctx.cache.orders_open(instrument_id=instrument_id) or ()
                blocking = next(
                    (
                        order
                        for order in open_orders
                        if not self._is_ignored_start_price_order(order, submitted_orders)
                    ),
                    None,
                )
                if blocking is not None:
                    _LOG.info(
                        f"ShareLimitModification[current_order_gate]: pair={ctx.pair_id} "
                        f"open order exists on instrument={instrument_id}, clear output",
                    )
                    self._clear_all_outputs(ctx)
                    return False
        except (AttributeError, TypeError, ValueError) as exc:
            _LOG.error(
                f"ShareLimitModification[current_order_gate]: pair={ctx.pair_id} "
                f"cannot read open orders: {exc}",
            )
            self._clear_all_outputs(ctx)
            return False
        return True

    def _is_ignored_start_price_order(self, order, submitted_orders) -> bool:
        if not self._ignore_start_price_orders:
            return False
        if not _was_submitted(order):
            return False
        return not any(not _same_order(order, other) for other in submitted_orders)

    @staticmethod
    def _clear_all_outputs(ctx: EvalContext) -> None:
        ctx.scratch["legs"] = []
        if "candidates" in ctx.scratch:
            ctx.scratch["candidates"] = []
        if "selected_candidate" in ctx.scratch:
            ctx.scratch["selected_candidate"] = {}

    def _apply_current_position_gate(self, ctx: EvalContext) -> bool:
        portfolio = ctx.portfolio
        if portfolio is None:
            _LOG.warning(
                f"ShareLimitModification[current_position_gate]: pair={ctx.pair_id} "
                "no portfolio, clear output",
            )
            self._clear_output(ctx)
            return False

        try:
            shares = portfolio.outcome_shares(ctx.pair_id, None)
            current_outcomes = {
                str(outcome).lower()
                for outcome, share in shares.items()
                if float(share) > 0.0
            }
        except (AttributeError, TypeError, ValueError, PositionOutcomeInvariantError) as exc:
            _LOG.error(
                f"ShareLimitModification[current_position_gate]: pair={ctx.pair_id} "
                f"cannot read current positions: {exc}",
            )
            self._clear_output(ctx)
            return False

        if not current_outcomes:
            return True

        if "candidates" in ctx.scratch:
            candidates = ctx.scratch.get("candidates")
            if not isinstance(candidates, list):
                _LOG.warning(
                    f"ShareLimitModification[current_position_gate]: pair={ctx.pair_id} "
                    "candidates is not list, clear candidates",
                )
                ctx.scratch["candidates"] = []
                return False

            filtered_candidates = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                legs = self._filter_current_outcome_legs(
                    candidate.get("legs") or [],
                    current_outcomes,
                )
                if not legs:
                    continue
                filtered = dict(candidate)
                filtered["legs"] = legs
                filtered_candidates.append(filtered)
            ctx.scratch["candidates"] = filtered_candidates
            return True

        legs = ctx.scratch.get("legs") or []
        ctx.scratch["legs"] = self._filter_current_outcome_legs(legs, current_outcomes)
        return True

    def _filter_current_outcome_legs(
        self,
        legs: list[dict],
        current_outcomes: set[str],
    ) -> list[dict]:
        return [
            leg
            for leg in legs
            if str(leg.get("claim") or leg.get("role") or "").lower() in current_outcomes
        ]

    @staticmethod
    def _clear_output(ctx: EvalContext) -> None:
        if "candidates" in ctx.scratch:
            ctx.scratch["candidates"] = []
        else:
            ctx.scratch["legs"] = []

    def _adjust_candidates(self, ctx: EvalContext) -> bool:
        if "candidates" not in ctx.scratch:
            return False

        max_leg_share = self._configured_max_leg_share(ctx)
        if max_leg_share is None:
            return True
        candidates = ctx.scratch.get("candidates") or []
        if not isinstance(candidates, list):
            _LOG.warning(f"ShareLimitModification: pair={ctx.pair_id} candidates is not list, skip")
            return True

        portfolio = ctx.portfolio
        if portfolio is None:
            _LOG.warning(f"ShareLimitModification: pair={ctx.pair_id} no portfolio, clear candidates")
            ctx.scratch["candidates"] = []
            return True

        adjusted_candidates = []
        for idx, candidate in enumerate(candidates):
            try:
                adjusted = self._adjust_candidate(portfolio, ctx.pair_id, candidate, idx, max_leg_share)
            except PositionOutcomeInvariantError as e:
                _LOG.error(f"ShareLimitModification: pair={ctx.pair_id} portfolio invariant: {e}")
                ctx.scratch["candidates"] = []
                return True
            if adjusted is not None:
                adjusted_candidates.append(adjusted)

        ctx.scratch["candidates"] = adjusted_candidates
        _LOG.debug(
            f"ShareLimitModification: pair={ctx.pair_id} candidates={len(candidates)} "
            f"adjusted={len(adjusted_candidates)}"
        )
        return True

    def _adjust_candidate(
        self,
        portfolio,
        pair_id: str,
        candidate: dict,
        idx: int,
        max_leg_share: float,
    ) -> dict | None:
        legs = candidate.get("legs") or []
        if not legs:
            return None

        base_share = _candidate_base_share(candidate)
        requested_by_leg = []
        remaining_values = []
        scale = 1.0
        # #260:丢弃 candidate 必须留痕 —— 单机会路径对相同条件有 warning(见 `execute` 内
        # "missing qty/share_if_wins, clear legs"),这里原本两处静默 `return None`,
        # 导致候选凭空消失、`CandiSelectAction` 从缩小的集合里选而无从追查。
        for leg in legs:
            venue = str(leg.get("venue", "")).upper()
            role = str(leg.get("role", ""))
            if not venue or not role:
                _LOG.warning(
                    f"ShareLimitModification: pair={pair_id} candidate={idx} "
                    f"leg={leg.get('instrument_id')} missing venue/role, drop candidate",
                )
                return None

            requested_share = _leg_share_if_wins(leg, venue)
            if requested_share is None or requested_share <= 0:
                _LOG.warning(
                    f"ShareLimitModification: pair={pair_id} candidate={idx} "
                    f"leg={leg.get('instrument_id')} missing qty/share_if_wins, drop candidate",
                )
                return None

            if is_decimal_odds_venue(venue):
                remaining = self._decimal_remaining(portfolio, pair_id, venue, role, max_leg_share)
            else:
                remaining = self._probability_remaining(portfolio, pair_id, venue, role, max_leg_share)
            if remaining <= 0:
                # 额度用满是**常态**(单机会路径同条件打 info)。candidate 路径每轮可能命中多次,
                # 故降为 DEBUG:留痕可查,但不刷屏。
                _LOG.debug(
                    f"ShareLimitModification: pair={pair_id} candidate={idx} "
                    f"venue={venue} role={role} remaining={remaining:.4f} <= 0, drop candidate",
                )
                return None
            scale = min(scale, remaining / requested_share)
            remaining_values.append(remaining)
            requested_by_leg.append((leg, requested_share))

        if scale <= 0:
            return None

        adjusted = deepcopy(candidate)
        adjusted_legs = _adjust_legs(requested_by_leg, scale)

        adjusted["legs"] = adjusted_legs
        adjusted["share_limit_scale"] = scale
        adjusted["adjusted_share"] = base_share * scale if base_share > 0 else min(
            leg["share_if_wins"] for leg in adjusted_legs
        )
        adjusted.setdefault("candidate_index", idx)
        return adjusted

    def _probability_remaining(self, portfolio, pair_id: str, venue: str, role: str, max_leg_share: float) -> float:
        """Probability venue:单腿独立检查。"""
        shares = portfolio.outcome_shares_for_venue(pair_id, venue.lower(), None)
        current = shares.get(role, 0.0)
        return max_leg_share - current

    def _decimal_remaining(self, portfolio, pair_id: str, venue: str, role: str, max_leg_share: float) -> float:
        """Decimal odds venue:按二元 outcome 净额计算 remaining。

        OE/SE 平台自动对冲，margin 按净敞口计算；outcome 名称统一为 yes/no，
        但算法只依赖“恰好两个互斥 outcome”，不硬编码名称。
        """
        shares = portfolio.outcome_shares_for_venue(pair_id, venue.lower(), None)
        outcomes = tuple(shares)
        if role not in shares or len(outcomes) != 2:
            return max_leg_share - shares.get(role, 0.0)
        opposite = next(outcome for outcome in outcomes if outcome != role)
        merged = max(0.0, shares.get(role, 0.0) - shares.get(opposite, 0.0))
        return max_leg_share - merged

    def _configured_max_leg_share(self, ctx: EvalContext) -> float | None:
        if self._max_leg_share is not None:
            return self._max_leg_share
        value = (ctx.strategy_defaults or {}).get("max_leg_share")
        return float(value) if value is not None else None


def _normalize_current_order_gate(raw: dict | None) -> dict[str, bool]:
    if raw is None:
        return {"enable": False, "ignore_start_price": False}
    if not isinstance(raw, dict):
        raise ValueError("share_limit: current_order_gate must be an object")
    unknown = set(raw) - {"enable", "ignore_start_price"}
    if unknown:
        raise ValueError(
            f"share_limit: current_order_gate has unknown fields: {sorted(unknown)}",
        )
    enable = raw.get("enable", False)
    ignore_start_price = raw.get("ignore_start_price", False)
    if not isinstance(enable, bool):
        raise ValueError("share_limit: current_order_gate.enable must be a boolean")
    if not isinstance(ignore_start_price, bool):
        raise ValueError(
            "share_limit: current_order_gate.ignore_start_price must be a boolean",
        )
    return {
        "enable": enable,
        "ignore_start_price": ignore_start_price,
    }


def _was_submitted(order) -> bool:
    return int(getattr(order, "ts_submitted", 0) or 0) > 0


def _same_order(left, right) -> bool:
    left_id = str(getattr(left, "client_order_id", "") or "")
    right_id = str(getattr(right, "client_order_id", "") or "")
    if left_id and right_id:
        return left_id == right_id
    return left is right


def _candidate_base_share(candidate: dict) -> float:
    for key in ("base_share", "share", "target_share"):
        value = candidate.get(key)
        if value is not None:
            return float(value)
    shares = [
        float(leg["share_if_wins"])
        for leg in candidate.get("legs", [])
        if leg.get("share_if_wins") is not None
    ]
    return min(shares) if shares else 0.0


def _leg_share_if_wins(leg: dict, venue: str) -> float | None:
    if leg.get("share_if_wins") is not None:
        return float(leg["share_if_wins"])
    if leg.get("qty") is not None:
        qty = float(leg["qty"])
        if is_decimal_odds_venue(venue):
            return qty * float(leg.get("price", 0.0))
        return qty
    return None


def _adjust_legs(requested_by_leg: list[tuple[dict, float]], scale: float) -> list[dict]:
    adjusted_legs = []
    for leg, requested_share in requested_by_leg:
        venue = str(leg.get("venue", "")).upper()
        price = float(leg.get("price", 0.0))
        new_leg = deepcopy(leg)
        new_share = requested_share * scale
        new_leg["share_if_wins"] = new_share
        if "qty" in new_leg:
            new_leg["qty"] = float(new_leg["qty"]) * scale
        elif is_decimal_odds_venue(venue):
            new_leg["qty"] = new_share / price if price > 0 else 0.0
        else:
            new_leg["qty"] = new_share
        if "cost" in new_leg:
            new_leg["cost"] = float(new_leg["cost"]) * scale
        return_leg = new_leg
        adjusted_legs.append(return_leg)
    return adjusted_legs
