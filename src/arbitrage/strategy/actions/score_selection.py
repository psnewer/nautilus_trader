"""ScoreSelectionAction —— 按当前比分与订单方向筛选已选 candidate 的腿。"""

from __future__ import annotations

import logging
import re

from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)
_SCORE_PART = re.compile(r"^\s*(\d+)\s*-\s*(\d+)(?:\s*\((\d+)\s*-\s*(\d+)\))?\s*$")


class ScoreSelectionAction(Action):
    """按比分领先关系保留 BUY/SELL 腿。

    `win_or_draw=None` 时完全放通。显式启用后，True 保留非落后方 BUY 与落后方
    SELL；False 保留落后方 BUY 与非落后方 SELL。比分或主客方映射未知时 fail-closed。
    """

    def __init__(self, win_or_draw: bool | None = None, tie_break: bool = False) -> None:
        if win_or_draw is not None and not isinstance(win_or_draw, bool):
            raise ValueError(
                f"score_selection: win_or_draw must be a boolean, got {win_or_draw!r}",
            )
        if not isinstance(tie_break, bool):
            raise ValueError(
                f"score_selection: tie_break must be a boolean, got {tie_break!r}",
            )
        self._win_or_draw = win_or_draw
        self._tie_break = tie_break

    async def execute(self, ctx: EvalContext) -> None:
        if self._win_or_draw is None:
            return
        selected = ctx.scratch.get("selected_candidate")
        if not isinstance(selected, dict) or selected.get("cancel_pair_orders"):
            return
        legs = selected.get("legs")
        if not isinstance(legs, list) or not legs:
            return

        standings = _standings(ctx, tie_break=self._tie_break)
        pair_roles = _pair_roles(ctx)
        kept = []
        for leg in legs:
            side_role = _side_role(ctx, leg, pair_roles)
            standing = standings.get(side_role)
            side = str(leg.get("side") or "BUY").upper()
            keep = _should_keep(side, standing, self._win_or_draw)
            if keep:
                kept.append(leg)
                continue
            _LOG.info(
                f"ScoreSelection: pair={ctx.pair_id} drop leg={leg.get('instrument_id')} "
                f"side={side} side_role={side_role} standing={standing} "
                f"win_or_draw={self._win_or_draw} tie_break={self._tie_break}",
            )

        filtered = dict(selected)
        filtered["legs"] = kept
        ctx.scratch["selected_candidate"] = filtered
        ctx.scratch["legs"] = kept


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
        return {"home": "non_trailing", "away": "trailing"}
    if comparison < 0:
        return {"home": "trailing", "away": "non_trailing"}
    return {"home": "non_trailing", "away": "non_trailing"}


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
        instrument = ctx.cache.instrument(instrument_id)
        info = getattr(instrument, "info", None) or {}
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
    instrument = ctx.cache.instrument(instrument_id)
    info = getattr(instrument, "info", None) or {}
    role = str(info.get("selection_role") or "").lower()
    claim = str(info.get("claim") or leg.get("claim") or "").lower()
    if pair_roles == {"home", "away"} and role in pair_roles:
        return role
    if role in {"home", "away"} and claim in {"", "yes"}:
        return role
    if not role and claim in {"yes", "no"}:
        return "home" if claim == "yes" else "away"
    return None


def _should_keep(side: str, standing: str | None, win_or_draw: bool) -> bool:
    if standing is None or side not in {"BUY", "SELL"}:
        return False
    non_trailing = standing == "non_trailing"
    return (side == "BUY" and non_trailing == win_or_draw) or (
        side == "SELL" and non_trailing != win_or_draw
    )


def _optional_int(value: str | None) -> int | None:
    return int(value) if value is not None else None


def _unit_is_complete(unit: tuple[int, int, int | None, int | None]) -> bool:
    """识别网球已结束盘，避免新盘 0-0 到达前把刚结束的一盘误当当前局分。"""
    left, right, _, _ = unit
    high, low = max(left, right), min(left, right)
    return (high >= 6 and high - low >= 2) or (high == 7 and low in {5, 6})
