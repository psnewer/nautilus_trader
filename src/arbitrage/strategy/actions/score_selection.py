"""ScoreSelectionAction —— 按当前比分与订单方向筛选执行腿。"""

from __future__ import annotations

import logging
import re

from src.arbitrage.strategy.checks.quote_legs import instrument_info
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)
_SCORE_PART = re.compile(r"^\s*(\d+)\s*-\s*(\d+)(?:\s*\((\d+)\s*-\s*(\d+)\))?\s*$")
_VALID_STANDINGS = frozenset({"win", "draw", "lose"})


class ScoreSelectionAction(Action):
    """按订单押注方向当前的比赛状态保留 BUY/SELL 腿。

    `standing=None` 时完全放通；显式配置时接受以 ``|`` 分隔的 ``win/draw/lose``
    组合。SELL 的 win/lose 与标的参赛方相反，draw 不变。比分或主客方映射未知时
    fail-closed。
    """

    def __init__(self, standing: str | None = None, tie_break: bool = False) -> None:
        if not isinstance(tie_break, bool):
            raise ValueError(
                f"score_selection: tie_break must be a boolean, got {tie_break!r}",
            )
        self._standings = _parse_standings(standing)
        self._tie_break = tie_break

    async def execute(self, ctx: EvalContext) -> None:
        if self._standings is None:
            return
        selected = ctx.scratch.get("selected_candidate")
        if isinstance(selected, dict):
            if selected.get("cancel_pair_orders"):
                return
            legs = selected.get("legs")
            if not isinstance(legs, list) or not legs:
                return
            kept = self._filter_legs(ctx, legs)
            filtered = dict(selected)
            filtered["legs"] = kept
            ctx.scratch["selected_candidate"] = filtered
            ctx.scratch["legs"] = kept
            return

        if "candidates" in ctx.scratch:
            candidates = ctx.scratch.get("candidates")
            if not isinstance(candidates, list):
                ctx.scratch["candidates"] = []
                return
            standings = self._get_standings(ctx)
            pair_roles = _pair_roles(ctx)
            filtered_candidates = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                if candidate.get("cancel_pair_orders"):
                    filtered_candidates.append(candidate)
                    continue
                legs = candidate.get("legs")
                if not isinstance(legs, list) or not legs:
                    continue
                kept = self._filter_legs(
                    ctx,
                    legs,
                    standings=standings,
                    pair_roles=pair_roles,
                )
                if not kept:
                    continue
                filtered = dict(candidate)
                filtered["legs"] = kept
                filtered_candidates.append(filtered)
            ctx.scratch["candidates"] = filtered_candidates
            return

        legs = ctx.scratch.get("legs")
        if not isinstance(legs, list) or not legs:
            return
        ctx.scratch["legs"] = self._filter_legs(ctx, legs)

    def _filter_legs(
        self,
        ctx: EvalContext,
        legs: list[dict],
        *,
        standings: dict[str, str] | None = None,
        pair_roles: set[str] | None = None,
    ) -> list[dict]:
        standings = standings if standings is not None else self._get_standings(ctx)
        pair_roles = pair_roles if pair_roles is not None else _pair_roles(ctx)
        kept = []
        for leg in legs:
            side_role = _side_role(ctx, leg, pair_roles)
            standing = standings.get(side_role)
            side = str(leg.get("side") or "BUY").upper()
            keep = _should_keep(side, standing, self._standings)
            if keep:
                kept.append(leg)
                continue
            _LOG.info(
                f"ScoreSelection: pair={ctx.pair_id} drop leg={leg.get('instrument_id')} "
                f"side={side} side_role={side_role} standing={standing} "
                f"allowed_standings={sorted(self._standings)} tie_break={self._tie_break}",
            )
        return kept

    def _get_standings(self, ctx: EvalContext) -> dict[str, str]:
        return _standings(ctx, tie_break=self._tie_break)


def _standings(ctx: EvalContext, *, tie_break: bool) -> dict[str, str]:
    if ctx.sports_store is None or ctx.pair_registry is None:
        return {}
    game_id = ctx.pair_registry.game_id_for_pair(ctx.pair_id)
    if game_id is None:
        return {}
    state = ctx.sports_store.get(game_id)
    comparison = _compare_score(
        getattr(state, "score", "") if state is not None else "",
        tie_break=tie_break,
    )
    if comparison is None:
        return {}
    if comparison > 0:
        return {"home": "win", "away": "lose"}
    if comparison < 0:
        return {"home": "lose", "away": "win"}
    return {"home": "draw", "away": "draw"}


def _compare_score(score: str, *, tie_break: bool = False) -> int | None:
    """返回主方相对客方的比赛级领先关系：1 / 0 / -1。"""
    parts = [part.strip() for part in str(score or "").split(",") if part.strip()]
    parsed = []
    for part in parts:
        match = _SCORE_PART.match(part)
        if match is None:
            return None
        left, right, tie_left, tie_right = match.groups()
        parsed.append((int(left), int(right), _optional_int(tie_left), _optional_int(tie_right)))
    if not parsed:
        return None

    # 6-6 起已进入抢七。未启用抢七比较时，不能把未知抢七态误判成普通平分。
    if parsed[-1][0] == parsed[-1][1] == 6 and not tie_break:
        return None

    completed = parsed if len(parsed) > 1 and _unit_is_complete(parsed[-1]) else parsed[:-1]
    home_units = sum(left > right for left, right, _, _ in completed)
    away_units = sum(left < right for left, right, _, _ in completed)
    if home_units != away_units:
        return 1 if home_units > away_units else -1

    if len(completed) == len(parsed):
        return 0

    left, right, tie_left, tie_right = parsed[-1]
    if left != right:
        return 1 if left > right else -1
    if tie_break and tie_left is not None and tie_right is not None and tie_left != tie_right:
        return 1 if tie_left > tie_right else -1
    return 0


def _pair_roles(ctx: EvalContext) -> set[str]:
    if ctx.cache is None or ctx.pair_registry is None:
        return set()
    roles = set()
    for instrument_id in ctx.pair_registry.instrument_ids_for_pair(ctx.pair_id):
        info = instrument_info(ctx, instrument_id)
        role = str(info.get("selection_role") or "").lower()
        if role in {"home", "away"}:
            roles.add(role)
    return roles


def _side_role(ctx: EvalContext, leg: dict, pair_roles: set[str]) -> str | None:
    if ctx.cache is None:
        return None
    instrument_id = leg.get("instrument_id")
    if not instrument_id:
        return None
    info = instrument_info(ctx, instrument_id)
    role = str(info.get("selection_role") or "").lower()
    claim = str(info.get("claim") or leg.get("claim") or "").lower()
    if pair_roles == {"home", "away"} and role in pair_roles:
        return role
    if role in {"home", "away"} and claim in {"", "yes"}:
        return role
    if not role and claim in {"yes", "no"}:
        return "home" if claim == "yes" else "away"
    return None


def _should_keep(
    side: str,
    standing: str | None,
    allowed_standings: frozenset[str],
) -> bool:
    if standing is None or side not in {"BUY", "SELL"}:
        return False
    order_standing = standing
    if side == "SELL":
        order_standing = {"win": "lose", "draw": "draw", "lose": "win"}[standing]
    return order_standing in allowed_standings


def _parse_standings(value: str | None) -> frozenset[str] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"score_selection: standing must be a string, got {value!r}",
        )
    parts = [part.strip().lower() for part in value.split("|")]
    if not parts or any(not part for part in parts):
        raise ValueError(
            "score_selection: standing must contain win, draw, or lose separated by '|'",
        )
    invalid = set(parts) - _VALID_STANDINGS
    if invalid:
        raise ValueError(
            f"score_selection: invalid standing values {sorted(invalid)!r}",
        )
    return frozenset(parts)


def _optional_int(value: str | None) -> int | None:
    return int(value) if value is not None else None


def _unit_is_complete(unit: tuple[int, int, int | None, int | None]) -> bool:
    """识别网球已结束盘，避免新盘 0-0 到达前把刚结束的一盘误当当前局分。"""
    left, right, _, _ = unit
    high, low = max(left, right), min(left, right)
    return (high >= 6 and high - low >= 2) or (high == 7 and low in {5, 6})
