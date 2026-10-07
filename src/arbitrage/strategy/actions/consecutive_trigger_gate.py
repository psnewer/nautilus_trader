"""按原始腿方向与变化后的比分过滤连续触发。"""

from __future__ import annotations

import logging
import math

from src.arbitrage.strategy.actions.score_selection import _SCORE_PART
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.consecutive_triggers import TriggerLeg
from src.arbitrage.strategy.consecutive_triggers import TriggerObservation


_LOG = logging.getLogger(__name__)
_OBD_EVENTS = frozenset({"OrderBookDeltas", "MarketOrderBookDeltas"})


class ConsecutiveTriggerGateAction(Action):
    """当前触发与最近连续历史同方向、且累计足够多次比分变化时放行。"""

    def __init__(self, history_key: str, required_hits: int = 2) -> None:
        if not isinstance(history_key, str) or not history_key.strip():
            raise ValueError("consecutive_trigger_gate: history_key must be a non-empty string")
        if (
            isinstance(required_hits, bool)
            or not isinstance(required_hits, int)
            or required_hits < 2
        ):
            raise ValueError("consecutive_trigger_gate: required_hits must be an integer >= 2")
        self._history_key = history_key.strip()
        self._required_hits = required_hits

    async def execute(self, ctx: EvalContext) -> None:
        if ctx.event_name not in _OBD_EVENTS:
            _clear_outputs(ctx)
            return
        observation = _observation(ctx)
        store = ctx.consecutive_trigger_store
        if observation is None or store is None:
            _clear_outputs(ctx)
            return

        recent = store.iter_recent(ctx.pair_id, self._history_key)
        passed, count = _matches_recent_sequence(
            recent,
            observation,
            required_hits=self._required_hits,
        )
        # 判定读取旧历史；无论是否放行，本次有效触发都在判定后追加且永不覆盖。
        store.append(ctx.pair_id, self._history_key, observation)
        _LOG.info(
            f"ConsecutiveTriggerGate: pair={ctx.pair_id} history={self._history_key} "
            f"outcome={observation.outcome} score={observation.score_raw!r} "
            f"count={count} required={self._required_hits} passed={passed}",
        )
        if not passed:
            _clear_outputs(ctx)


def _observation(ctx: EvalContext) -> TriggerObservation | None:
    legs, candidates = _current_legs_and_candidates(ctx)
    if not legs:
        return None
    outcomes = {_outcome(leg) for leg in legs}
    if not outcomes.issubset(VALID_OUTCOMES):
        return None
    if len(outcomes) != 1:
        return None
    score_raw = _score(ctx)
    score_key = _score_key(score_raw)
    if score_key is None:
        return None
    outcome = next(iter(outcomes))
    return TriggerObservation(
        ts_ns=int(ctx.ts_now_ns or 0),
        event_name=str(ctx.event_name or ""),
        score_raw=score_raw,
        score_key=score_key,
        outcome=outcome,
        legs=tuple(_snapshot_leg(leg) for leg in legs),
        candidate_ids=tuple(
            str(candidate.get("candidate_id"))
            for candidate in candidates
            if candidate.get("candidate_id") is not None
        ),
        rates=tuple(
            value
            for candidate in candidates
            if (value := _finite_number(candidate.get("rate"))) is not None
        ),
    )


def _current_legs_and_candidates(ctx: EvalContext) -> tuple[list[dict], list[dict]]:
    selected = ctx.scratch.get("selected_candidate")
    if isinstance(selected, dict):
        if selected.get("cancel_pair_orders"):
            return [], []
        return _dict_legs(selected.get("legs")), [selected]
    if "candidates" in ctx.scratch:
        candidates = [
            value
            for value in (ctx.scratch.get("candidates") or [])
            if isinstance(value, dict) and not value.get("cancel_pair_orders")
        ]
        legs = [
            leg
            for candidate in candidates
            for leg in _dict_legs(candidate.get("legs"))
        ]
        return legs, candidates
    return _dict_legs(ctx.scratch.get("legs")), []


def _dict_legs(value) -> list[dict]:
    return [leg for leg in value if isinstance(leg, dict)] if isinstance(value, list) else []


def _score(ctx: EvalContext) -> str:
    if ctx.sports_store is None or ctx.pair_registry is None:
        return ""
    game_id = ctx.pair_registry.game_id_for_pair(ctx.pair_id)
    state = ctx.sports_store.get(game_id) if game_id is not None else None
    return str(getattr(state, "score", "") or "").strip()


def _score_key(score: str) -> tuple[tuple[int, int, int | None, int | None], ...] | None:
    parts = [part.strip() for part in score.split(",") if part.strip()]
    result = []
    for part in parts:
        match = _SCORE_PART.match(part)
        if match is None:
            return None
        left, right, tie_left, tie_right = match.groups()
        result.append((int(left), int(right), _optional_int(tie_left), _optional_int(tie_right)))
    return tuple(result) or None


def _optional_int(value: str | None) -> int | None:
    return int(value) if value is not None else None


def _matches_recent_sequence(recent, current, *, required_hits: int) -> tuple[bool, int]:
    count = 1
    last_score = current.score_key
    for previous in recent:
        if previous.outcome != current.outcome:
            break
        if previous.score_key == last_score:
            continue
        count += 1
        last_score = previous.score_key
        if count >= required_hits:
            return True, count
    return False, count


def _snapshot_leg(leg: dict) -> TriggerLeg:
    return TriggerLeg(
        instrument_id=str(leg.get("instrument_id") or ""),
        venue=str(leg.get("venue") or ""),
        side=str(leg.get("side") or "BUY").upper(),
        outcome=_outcome(leg),
        price=_finite_number(leg.get("price")),
        probability=_finite_number(leg.get("prob")),
    )


def _outcome(leg: dict) -> str:
    return str(leg.get("claim") or leg.get("role") or "").strip().lower()


def _finite_number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clear_outputs(ctx: EvalContext) -> None:
    if "selected_candidate" in ctx.scratch:
        selected = ctx.scratch.get("selected_candidate")
        if isinstance(selected, dict) and selected.get("cancel_pair_orders"):
            return
        ctx.scratch["selected_candidate"] = {}
    if "candidates" in ctx.scratch:
        ctx.scratch["candidates"] = []
    if "legs" in ctx.scratch:
        ctx.scratch["legs"] = []
