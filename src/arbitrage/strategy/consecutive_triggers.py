"""连续触发门使用的 pair 级追加式运行时历史。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from src.arbitrage.strategy.actions.score_selection import _SCORE_PART


@dataclass(frozen=True, slots=True)
class TriggerLeg:
    instrument_id: str
    venue: str
    side: str
    outcome: str
    price: float | None
    probability: float | None


@dataclass(frozen=True, slots=True)
class TriggerObservation:
    ts_ns: int
    event_name: str
    score_raw: str
    score_key: tuple[tuple[int, int, int | None, int | None], ...]
    outcome: str
    legs: tuple[TriggerLeg, ...]
    candidate_ids: tuple[str, ...]
    rates: tuple[float, ...]
    passed: bool = False


class ConsecutiveTriggerStore:
    """保存活动 pair 的全部有效触发；只在比赛生命周期结束时整 pair 清理。"""

    def __init__(self) -> None:
        self._history: dict[tuple[str, str], list[TriggerObservation]] = {}

    def append(self, pair_id: str, history_key: str, value: TriggerObservation) -> None:
        self._history.setdefault((pair_id, history_key), []).append(value)

    def latest(self, pair_id: str, history_key: str) -> TriggerObservation | None:
        values = self._history.get((pair_id, history_key), ())
        return values[-1] if values else None

    def history(self, pair_id: str, history_key: str) -> tuple[TriggerObservation, ...]:
        return tuple(self._history.get((pair_id, history_key), ()))

    def iter_recent(self, pair_id: str, history_key: str) -> Iterator[TriggerObservation]:
        """从最近记录向前读；调用方必须在迭代结束后才能 append。"""
        return reversed(self._history.get((pair_id, history_key), ()))

    def delete_pair(self, pair_id: str) -> None:
        for key in [key for key in self._history if key[0] == pair_id]:
            del self._history[key]


def score_key(score: str) -> tuple[tuple[int, int, int | None, int | None], ...] | None:
    """解析完整比分，保留各盘常规局与可选抢七小分。"""
    parts = [part.strip() for part in str(score or "").split(",") if part.strip()]
    result = []
    for part in parts:
        match = _SCORE_PART.match(part)
        if match is None:
            return None
        left, right, tie_left, tie_right = match.groups()
        result.append((int(left), int(right), _optional_int(tie_left), _optional_int(tie_right)))
    return tuple(result) or None


def tie_break_in_progress(value) -> bool:
    left, right, _, _ = value[-1]
    return left == right == 6


def same_set(previous_score, current_score) -> bool:
    return len(previous_score) == len(current_score) and previous_score[:-1] == current_score[:-1]


def elapsed_games_same_set(previous_score, current_score) -> int | None:
    """返回同一盘内推进的常规局数；跨盘或倒退返回 None。"""
    if not same_set(previous_score, current_score):
        return None
    previous_left, previous_right, _, _ = previous_score[-1]
    current_left, current_right, _, _ = current_score[-1]
    elapsed = (
        regular_games_in_set(current_left, current_right)
        - regular_games_in_set(previous_left, previous_right)
    )
    return elapsed if elapsed >= 0 else None


def regular_games_in_set(left: int, right: int) -> int:
    games = left + right
    return games - 1 if (left, right) in {(7, 6), (6, 7)} else games


def _optional_int(value: str | None) -> int | None:
    return int(value) if value is not None else None
