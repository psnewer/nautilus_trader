"""LatestScoreAction —— 只保留最近一次可判定比分增量所属参赛方的腿。"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

from src.arbitrage.strategy.actions.score_selection import _SCORE_PART
from src.arbitrage.strategy.actions.score_selection import ScoreSelectionAction
from src.arbitrage.strategy.condition import EvalContext


_NO_SCORE = "no_score"
_UNKNOWN = "unknown"


class LatestScoreAction(ScoreSelectionAction):
    """
    按 Store 中连续比分快照推断最后得分方,并按目标 instrument 筛腿。

    首帧没有前态、比分格式非法、发生双方同时增加或回退等无法唯一归因的变化时
    fail-closed。盘间仅追加 ``0-0`` 的结构变化不覆盖上一位真实得分方;抢七括号小分
    也属于比分变化。
    """

    _log_name = "LatestScore"

    def __init__(self) -> None:
        super().__init__(standing="win")

    def _get_standings(self, ctx: EvalContext) -> dict[str, str]:
        role = _latest_scoring_role(ctx)
        if role == "home":
            return {"home": "win", "away": "lose"}
        if role == "away":
            return {"home": "lose", "away": "win"}
        return {}


def _latest_scoring_role(ctx: EvalContext) -> str | None:
    if ctx.sports_store is None or ctx.pair_registry is None:
        return None
    game_id = ctx.pair_registry.game_id_for_pair(ctx.pair_id)
    if game_id is None:
        return None
    history_reader = getattr(ctx.sports_store, "score_history", None)
    if not callable(history_reader):
        return None
    history = history_reader(game_id)
    for previous, current in reversed(tuple(pairwise(history))):
        result = _score_transition_role(previous.score, current.score)
        if result in {"home", "away"}:
            return result
        if result == _UNKNOWN:
            return None
    return None


def _score_transition_role(previous: str, current: str) -> str:
    before = _score_totals(previous)
    after = _score_totals(current)
    if before is None or after is None:
        return _UNKNOWN

    home_games = after[0] - before[0]
    away_games = after[1] - before[1]
    if home_games > 0 and away_games == 0:
        return "home"
    if away_games > 0 and home_games == 0:
        return "away"
    if home_games != 0 or away_games != 0:
        return _UNKNOWN

    home_tiebreak = after[2] - before[2]
    away_tiebreak = after[3] - before[3]
    if home_tiebreak > 0 and away_tiebreak == 0:
        return "home"
    if away_tiebreak > 0 and home_tiebreak == 0:
        return "away"
    if home_tiebreak == 0 and away_tiebreak == 0:
        return _NO_SCORE
    return _UNKNOWN


def _score_totals(score: str) -> tuple[int, int, int, int] | None:
    parts = [part.strip() for part in str(score or "").split(",") if part.strip()]
    if not parts:
        return None
    parsed: list[Sequence[str | None]] = []
    for part in parts:
        match = _SCORE_PART.match(part)
        if match is None:
            return None
        parsed.append(match.groups())
    return (
        sum(int(part[0]) for part in parsed),
        sum(int(part[1]) for part in parsed),
        sum(int(part[2]) for part in parsed if part[2] is not None),
        sum(int(part[3]) for part in parsed if part[3] is not None),
    )
