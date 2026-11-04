"""ServeGateAction —— 按首局发球方与当前局数奇偶筛选发球/接发腿。"""

from __future__ import annotations

import logging

from src.arbitrage.strategy.actions.score_selection import _SCORE_PART
from src.arbitrage.strategy.actions.score_selection import _pair_roles
from src.arbitrage.strategy.actions.score_selection import _side_role
from src.arbitrage.strategy.actions.score_selection import _unit_is_complete
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class ServeGateAction(Action):
    """默认只保留当前发球方, ``serve=false`` 时只保留当前接发方。"""

    def __init__(self, serve: bool = True) -> None:
        if not isinstance(serve, bool):
            raise ValueError("serve_gate: serve must be a boolean")
        self._serve = serve

    async def execute(self, ctx: EvalContext) -> None:  # noqa: C901 - 三种 scratch 形态
        server_role = _current_server_role(ctx)
        eligible_role = server_role if self._serve else _opposite(server_role)
        pair_roles = _pair_roles(ctx)

        selected = ctx.scratch.get("selected_candidate")
        if isinstance(selected, dict):
            if selected.get("cancel_pair_orders"):
                return
            legs = selected.get("legs")
            if not isinstance(legs, list) or not legs:
                return
            kept = _filter_legs(ctx, legs, pair_roles, eligible_role, server_role, self._serve)
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
                kept = _filter_legs(
                    ctx,
                    legs,
                    pair_roles,
                    eligible_role,
                    server_role,
                    self._serve,
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
        ctx.scratch["legs"] = _filter_legs(
            ctx,
            legs,
            pair_roles,
            eligible_role,
            server_role,
            self._serve,
        )


def _filter_legs(
    ctx: EvalContext,
    legs: list[dict],
    pair_roles: set[str],
    eligible_role: str | None,
    server_role: str | None,
    serve: bool,
) -> list[dict]:
    kept = []
    for leg in legs:
        role = _side_role(ctx, leg, pair_roles)
        if eligible_role is not None and role == eligible_role:
            kept.append(leg)
            continue
        _LOG.info(
            f"ServeGate: pair={ctx.pair_id} drop leg={leg.get('instrument_id')} "
            f"role={role} server_role={server_role} serve={serve}",
        )
    return kept


def _current_server_role(ctx: EvalContext) -> str | None:
    if ctx.sports_store is None or ctx.pair_registry is None:
        return None
    if _pair_roles(ctx) != {"home", "away"}:
        return None
    game_id = ctx.pair_registry.game_id_for_pair(ctx.pair_id)
    state = ctx.sports_store.get(game_id) if game_id is not None else None
    if state is None or bool(getattr(state, "ended", False)):
        return None
    first_serve = str(getattr(state, "first_serve", "") or "").lower()
    if first_serve not in {"home", "away"}:
        return None
    score = _parse_score(getattr(state, "score", ""))
    if score is None:
        return None
    current_unit = score[-1]
    if (
        current_unit[2] is not None
        or current_unit[3] is not None
        or current_unit[:2] == (6, 6)
    ):
        return None
    if any(not _unit_is_complete(unit) for unit in score[:-1]):
        return None
    completed_games = sum(left + right for left, right, _, _ in score)
    return first_serve if completed_games % 2 == 0 else _opposite(first_serve)


def _parse_score(score) -> tuple[tuple[int, int, int | None, int | None], ...] | None:
    parts = [part.strip() for part in str(score or "").split(",") if part.strip()]
    parsed = []
    for part in parts:
        match = _SCORE_PART.match(part)
        if match is None:
            return None
        left, right, tie_left, tie_right = match.groups()
        parsed.append(
            (
                int(left),
                int(right),
                int(tie_left) if tie_left is not None else None,
                int(tie_right) if tie_right is not None else None,
            ),
        )
    return tuple(parsed) if parsed else None


def _opposite(role: str | None) -> str | None:
    if role == "home":
        return "away"
    if role == "away":
        return "home"
    return None
