"""连续触发门使用的 pair 级追加式运行时历史。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass


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
