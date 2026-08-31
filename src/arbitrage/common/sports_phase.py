"""跨数据源的比赛阶段状态。"""

from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import dataclass


PHASE_PRE = "PRE"
PHASE_IN_PLAY = "IN_PLAY"
PHASE_POST = "POST"

_PHASE_RANK = {
    PHASE_PRE: 0,
    PHASE_IN_PLAY: 1,
    PHASE_POST: 2,
}
_STORE_KEY_PREFIX = "sports:phase:"


@dataclass(frozen=True, slots=True)
class SportsPhaseState:
    game_id: int
    phase: str
    source: str
    ts_event: int


class SportsPhaseStore:
    """按 ``game_id`` 保存单调推进的 PRE / IN_PLAY / POST。"""

    def __init__(self, cache) -> None:
        self._cache = cache

    @staticmethod
    def _key(game_id) -> str:
        return f"{_STORE_KEY_PREFIX}{int(game_id)}"

    def get(self, game_id) -> SportsPhaseState | None:
        raw = self._cache.get(self._key(game_id))
        if not raw:
            return None
        values = json.loads(raw.decode("utf-8"))
        return SportsPhaseState(
            game_id=int(values["game_id"]),
            phase=str(values["phase"]),
            source=str(values["source"]),
            ts_event=int(values["ts_event"]),
        )

    def observe_in_play(
        self,
        game_id,
        in_play: bool,
        *,
        source: str,
        ts_event: int,
    ) -> bool:
        phase = PHASE_IN_PLAY if in_play else PHASE_PRE
        return self.advance(game_id, phase, source=source, ts_event=ts_event)

    def advance(self, game_id, phase: str, *, source: str, ts_event: int) -> bool:
        """只允许阶段向前推进，发生有效跃迁时返回 ``True``。"""
        if phase not in _PHASE_RANK:
            raise ValueError(f"Unsupported sports phase: {phase}")
        previous = self.get(game_id)
        if previous is not None and _PHASE_RANK[phase] <= _PHASE_RANK[previous.phase]:
            return False
        state = SportsPhaseState(
            game_id=int(game_id),
            phase=phase,
            source=str(source),
            ts_event=int(ts_event),
        )
        self._cache.add(self._key(game_id), json.dumps(asdict(state)).encode("utf-8"))
        return True

    def delete(self, game_id) -> None:
        self._cache.delete(self._key(game_id))
