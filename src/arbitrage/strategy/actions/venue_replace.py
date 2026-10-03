"""VenueReplaceAction —— 将执行腿替换为 Polymarket 腿。"""

from __future__ import annotations

import logging
import math
from copy import deepcopy

from nautilus_trader.model.identifiers import InstrumentId
from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.common.venues import ORBITEXCH
from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import leg_economics
from src.arbitrage.common.venues import qty_from_share
from src.arbitrage.common.venues import venue_id_from_instrument_id
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.checks.quote_legs import quote_legs_by_outcome
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)
_LOWER_TIER_COMPETITION_MARKERS = ("CHALLENGER", "WTA", "UTR", "ITF")


class VenueReplaceAction(Action):
    """
    把非 PM 腿替换为同 pair、同 outcome 的 PM 路由腿。

    candidate 本质就是包了元数据的 legs 数组,`ctx.scratch["legs"]` 则是裸 legs 数组;两种
    表示的承重内容都是 legs,故 `legs` / `candidates` / candi_select 之后的
    `selected_candidate` 三种输入形态都支持。

    `pm_price`(默认真)决定替换后 PM 腿的下单价:
      - **不存在 / True**:用 PM 报价腿自身概率(= PM best_ask 隐含概率,PM 实时价);
      - **存在且 False**:沿用原腿的 committed `prob`(两 venue 共享 outcome 概率,不看 PM 实时价)。
    `tier_convert="pre"` 且 OE 原始 competition 属于 Challenger/WTA/UTR/ITF 时优先改为
    对手 outcome,并直接使用对手 PM 实时价;命中后不再检查其它转换条件。
    `tier_convert="post"` 则先完整执行既有转换逻辑,再将最终 PM 腿反转;
    若原始 outcome 的 start_price 严格小于 `post_ignore`,则跳过这次 post 反转。`convert=true` 时,已经是 PM
    的输入腿改为对手 outcome,并直接使用对手 PM 实时价;
    `pm_price` 对原生 PM 输入腿不起作用。非 PM 输入仍替换为同 outcome PM 腿。
    `convert` 未命中时,`attitude=true` 可在原 outcome PM bid <= start_price 时反转,
    `deviate_convert=true` 可在 bid >= `1.2xstart_price` 时反转;
    两者均仅在原 outcome 存在 start_price 且完整 PM ask 向量概率和位于 `[0.98,1.02]` 时生效。
    缺 start_price 只是不触发动态反转,不阻止后续默认同方向替换。
    PM 是 probability venue,qty=share(不随价缩放);price/prob/cost 按所选价重算。
    """

    def __init__(
        self,
        pm_price: bool | None = None,
        convert: bool | None = None,
        deviate_convert: bool | None = None,
        attitude: bool | None = None,
        tier_convert: str | None = None,
        post_ignore: float | None = None,
    ) -> None:
        if pm_price is not None and not isinstance(pm_price, bool):
            raise ValueError("pm_price must be a boolean")
        if convert is not None and not isinstance(convert, bool):
            raise ValueError("convert must be a boolean")
        if tier_convert is not None and (
            not isinstance(tier_convert, str) or tier_convert not in {"pre", "post"}
        ):
            raise ValueError("tier_convert must be 'pre' or 'post'")
        if deviate_convert is not None and not isinstance(deviate_convert, bool):
            raise ValueError("deviate_convert must be a boolean")
        if attitude is not None and not isinstance(attitude, bool):
            raise ValueError("attitude must be a boolean")
        try:
            post_ignore_value = None if post_ignore is None else float(post_ignore)
        except (TypeError, ValueError) as exc:
            raise ValueError("post_ignore must be a finite number") from exc
        if isinstance(post_ignore, bool) or (
            post_ignore_value is not None and not math.isfinite(post_ignore_value)
        ):
            raise ValueError("post_ignore must be a finite number")
        # 不存在或 True → 用 PM 实时价;仅显式 False 保留旧逻辑(用原 OE/SE prob)。
        self._use_pm_price = pm_price is None or pm_price
        self._convert = bool(convert)
        self._tier_convert = tier_convert
        self._post_ignore = post_ignore_value
        self._deviate_convert = bool(deviate_convert)
        self._attitude = bool(attitude)

    async def execute(self, ctx: EvalContext) -> None:
        if ctx.scratch.get("cancel_pair_orders"):
            return

        pm_legs = _polymarket_legs_by_outcome(ctx)
        lower_tier_competition = (
            _lower_tier_oe_competition(ctx) if self._tier_convert is not None else None
        )
        tier_convert_mode = self._tier_convert if lower_tier_competition is not None else None
        if tier_convert_mode is not None:
            _LOG.info(
                f"VenueReplace: pair={ctx.pair_id} tier_convert={tier_convert_mode} hit "
                f"oe_competition={lower_tier_competition!r}",
            )
        pm_bids, start_prices = (
            ({}, {})
            if tier_convert_mode == "pre"
            else _dynamic_inputs(
                ctx,
                pm_legs,
                self._deviate_convert,
                self._attitude,
                include_start=tier_convert_mode == "post" and self._post_ignore is not None,
            )
        )

        selected = ctx.scratch.get("selected_candidate")
        if isinstance(selected, dict):
            if selected.get("cancel_pair_orders"):
                return
            replaced = _replace_candidate(
                selected,
                pm_legs,
                ctx.pair_id,
                self._use_pm_price,
                self._convert,
                tier_convert_mode,
                self._post_ignore,
                self._deviate_convert,
                self._attitude,
                pm_bids,
                start_prices,
            )
            if replaced is None:
                ctx.scratch["selected_candidate"] = {}
                ctx.scratch["legs"] = []
                return
            ctx.scratch["selected_candidate"] = replaced
            ctx.scratch["legs"] = replaced["legs"]
            return

        if "candidates" in ctx.scratch:
            ctx.scratch["candidates"] = _replace_candidates(
                ctx.scratch.get("candidates"),
                pm_legs,
                ctx.pair_id,
                self._use_pm_price,
                self._convert,
                tier_convert_mode,
                self._post_ignore,
                self._deviate_convert,
                self._attitude,
                pm_bids,
                start_prices,
            )
            return

        legs = ctx.scratch.get("legs")
        if not legs:
            return
        replaced = _replace_legs(
            legs,
            pm_legs,
            ctx.pair_id,
            self._use_pm_price,
            self._convert,
            tier_convert_mode,
            self._post_ignore,
            self._deviate_convert,
            self._attitude,
            pm_bids,
            start_prices,
        )
        ctx.scratch["legs"] = replaced or []


def _polymarket_legs_by_outcome(ctx: EvalContext) -> dict[str, dict]:
    result = {}
    for outcome, legs in quote_legs_by_outcome(ctx).items():
        pm_legs = [leg for leg in legs if str(leg.get("venue", "")).upper() == POLYMARKET]
        if pm_legs:
            result[outcome] = min(
                pm_legs,
                key=lambda leg: (float(leg["prob"]), str(leg["instrument_id"])),
            )
    return result


def _lower_tier_oe_competition(ctx: EvalContext) -> str | None:
    """从 pair 的 OE BettingInstrument 读取 venue 原始 competition。"""
    if ctx.cache is None or ctx.pair_registry is None:
        return None
    for value in sorted(ctx.pair_registry.instrument_ids_for_pair(ctx.pair_id), key=str):
        if venue_id_from_instrument_id(value) != ORBITEXCH:
            continue
        instrument_id = (
            value if isinstance(value, InstrumentId) else InstrumentId.from_str(str(value))
        )
        instrument = ctx.cache.instrument(instrument_id)
        competition = str(getattr(instrument, "competition_name", "") or "").strip()
        normalized = competition.upper()
        if any(marker in normalized for marker in _LOWER_TIER_COMPETITION_MARKERS):
            return competition
    return None


def _polymarket_bids_by_outcome(
    ctx: EvalContext,
    pm_legs: dict[str, dict],
) -> dict[str, float]:
    result = {}
    for outcome, leg in pm_legs.items():
        try:
            instrument_id = InstrumentId.from_str(str(leg["instrument_id"]))
            book = ctx.cache.order_book(instrument_id)
            fn = getattr(book, "best_bid_price", None)
            value = fn() if callable(fn) else None
            bid = float(value) if value is not None else None
        except (KeyError, TypeError, ValueError):
            bid = None
        if bid is not None and 0 < bid < 1:
            result[outcome] = bid
    return result


def _dynamic_inputs(
    ctx: EvalContext,
    pm_legs: dict[str, dict],
    deviate_convert: bool,
    attitude: bool,
    *,
    include_start: bool = False,
) -> tuple[dict[str, float], dict[str, float]]:
    if not deviate_convert and not attitude:
        return {}, _start_prices(ctx) if include_start else {}
    return _polymarket_bids_by_outcome(ctx, pm_legs), _start_prices(ctx)


def _start_prices(ctx: EvalContext) -> dict[str, float]:
    state = PairPriceStore(ctx.cache).get(ctx.pair_id) if ctx.cache is not None else None
    return dict(state.start_price) if state is not None else {}


def _replace_candidate(
    candidate: dict,
    pm_legs: dict[str, dict],
    pair_id: str,
    use_pm_price: bool,
    convert: bool,
    tier_convert_mode: str | None,
    post_ignore: float | None,
    deviate_convert: bool,
    attitude: bool,
    pm_bids: dict[str, float],
    start_prices: dict[str, float],
) -> dict | None:
    if candidate.get("cancel_pair_orders"):
        return deepcopy(candidate)
    replaced_legs = _replace_legs(
        candidate.get("legs") or [],
        pm_legs,
        pair_id,
        use_pm_price,
        convert,
        tier_convert_mode,
        post_ignore,
        deviate_convert,
        attitude,
        pm_bids,
        start_prices,
    )
    if replaced_legs is None:
        return None
    replaced = deepcopy(candidate)
    replaced["legs"] = replaced_legs
    return replaced


def _replace_candidates(
    candidates,
    pm_legs: dict[str, dict],
    pair_id: str,
    use_pm_price: bool,
    convert: bool,
    tier_convert_mode: str | None,
    post_ignore: float | None,
    deviate_convert: bool,
    attitude: bool,
    pm_bids: dict[str, float],
    start_prices: dict[str, float],
) -> list[dict]:
    if not isinstance(candidates, list):
        _LOG.warning(f"VenueReplace: pair={pair_id} candidates is not list, clear")
        return []
    result = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        replaced = _replace_candidate(
            candidate,
            pm_legs,
            pair_id,
            use_pm_price,
            convert,
            tier_convert_mode,
            post_ignore,
            deviate_convert,
            attitude,
            pm_bids,
            start_prices,
        )
        if replaced is not None:
            result.append(replaced)
    return result


def _replace_legs(
    legs: list[dict],
    pm_legs: dict[str, dict],
    pair_id: str,
    use_pm_price: bool,
    convert: bool,
    tier_convert_mode: str | None,
    post_ignore: float | None,
    deviate_convert: bool,
    attitude: bool,
    pm_bids: dict[str, float],
    start_prices: dict[str, float],
) -> list[dict] | None:
    if not legs:
        return None
    result = []
    for leg in legs:
        venue = str(leg.get("venue", "")).upper()
        pre_tier_convert = tier_convert_mode == "pre"
        post_tier_convert = tier_convert_mode == "post"
        convert_target_leg = not pre_tier_convert and convert and venue == POLYMARKET
        source_outcome = str(leg.get("claim") or leg.get("role") or "").lower()
        post_ignored = post_tier_convert and _post_convert_ignored(
            source_outcome,
            start_prices,
            post_ignore,
        )
        dynamic_convert = (
            not pre_tier_convert
            and not convert_target_leg
            and _should_dynamic_convert(
                source_outcome,
                pm_legs,
                pm_bids,
                start_prices,
                deviate_convert=deviate_convert,
                attitude=attitude,
            )
        )
        flip_target = pre_tier_convert or convert_target_leg or dynamic_convert
        outcome = _opposite_outcome(source_outcome) if flip_target else source_outcome
        if post_tier_convert and not post_ignored:
            outcome = _opposite_outcome(outcome)
            flip_target = True
        elif post_ignored:
            _LOG.info(
                f"VenueReplace: pair={pair_id} leg={leg.get('instrument_id')} "
                f"post ignored outcome={source_outcome} "
                f"start_price={start_prices.get(source_outcome)} threshold={post_ignore}",
            )
        if venue == POLYMARKET and not flip_target:
            result.append(deepcopy(leg))
            continue

        share = _share_if_wins(leg)
        pm_leg = pm_legs.get(outcome)
        if outcome not in VALID_OUTCOMES or share is None or share <= 0 or pm_leg is None:
            _LOG.warning(
                f"VenueReplace: pair={pair_id} leg={leg.get('instrument_id')} "
                f"cannot replace outcome={outcome!r} share={share!r}, drop group",
            )
            return None

        replacement = deepcopy(pm_leg)
        # use_pm_price=True(默认):用 PM 报价腿概率(PM 实时 ask);False:用原腿共享 `prob`。
        # PM 是 probability venue,price 即概率;qty=share(不缩放),cost=share×prob。
        prob = _order_prob(leg, pm_leg, pair_id, use_pm_price or flip_target)
        qty = qty_from_share(POLYMARKET, share, prob)
        replacement["price"] = prob
        replacement["prob"] = prob
        replacement["qty"] = qty
        replacement["share_if_wins"] = share
        replacement["cost"] = leg_economics(POLYMARKET, prob, qty).loss_if_loses
        result.append(replacement)
    return result


def _post_convert_ignored(
    outcome: str,
    start_prices: dict[str, float],
    post_ignore: float | None,
) -> bool:
    if post_ignore is None or outcome not in VALID_OUTCOMES:
        return False
    try:
        start = float(start_prices[outcome])
    except (KeyError, TypeError, ValueError):
        return False
    return math.isfinite(start) and start < post_ignore


def _should_dynamic_convert(
    outcome: str,
    pm_legs: dict[str, dict],
    pm_bids: dict[str, float],
    start_prices: dict[str, float],
    *,
    deviate_convert: bool,
    attitude: bool,
) -> bool:
    if not deviate_convert and not attitude:
        return False
    asks = [_pm_quote_prob(pm_legs.get(role, {})) for role in VALID_OUTCOMES]
    if any(value is None for value in asks) or not 0.98 <= sum(asks) <= 1.02:
        return False
    if outcome not in VALID_OUTCOMES:
        return False
    bid = _positive_float(pm_bids.get(outcome))
    if bid is None:
        return False
    start = _positive_float(start_prices.get(outcome))
    if start is None:
        return False
    if attitude and bid <= start:
        return True
    return deviate_convert and bid >= 1.2 * start


def _opposite_outcome(outcome: str) -> str:
    return "no" if outcome == "yes" else "yes" if outcome == "no" else ""


def _share_if_wins(leg: dict) -> float | None:
    value = leg.get("share_if_wins")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _pm_quote_prob(pm_leg: dict) -> float | None:
    return _positive_float(pm_leg.get("prob")) or _positive_float(pm_leg.get("price"))


def _order_prob(
    leg: dict,
    pm_leg: dict,
    pair_id: str,
    use_pm_price: bool,
) -> float:
    """PM 下单价的隐含概率。

    use_pm_price=True(默认):用 PM 报价腿概率(PM 实时 ask);缺报价时告警回退 0。
    use_pm_price=False:用原腿共享 `prob`(两 venue 共享 outcome 概率);裸腿无 committed
    价格时回退 PM 报价腿概率并告警。
    """
    if use_pm_price:
        pm_prob = _pm_quote_prob(pm_leg)
        if pm_prob is not None:
            return pm_prob
        _LOG.warning(
            f"VenueReplace: pair={pair_id} leg={leg.get('instrument_id')} "
            "missing PM quote prob, fallback 0",
        )
        return 0.0
    prob = _positive_float(leg.get("prob"))
    if prob is not None:
        return prob
    fallback = _pm_quote_prob(pm_leg)
    _LOG.warning(
        f"VenueReplace: pair={pair_id} leg={leg.get('instrument_id')} "
        f"missing prob, fallback to PM quote prob={fallback}",
    )
    return fallback if fallback is not None else 0.0
