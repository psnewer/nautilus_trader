"""in_game 持仓的稀疏 pair 级运行时状态。"""

from __future__ import annotations

import re


_SET_RE = re.compile(r"S([1-9]\d*)", re.IGNORECASE)


class InGamePositionRuntimeStore:
    """仅在需要覆盖配置阈值时，为对应 pair 分配状态。"""

    def __init__(self) -> None:
        self._min_set_by_pair: dict[str, int] = {}

    def min_set(self, pair_id: str) -> int | None:
        return self._min_set_by_pair.get(pair_id)

    def set_min_set(self, pair_id: str, min_set: int) -> None:
        if isinstance(min_set, bool) or not isinstance(min_set, int) or min_set <= 0:
            raise ValueError("in_game position min_set must be a positive integer")
        self._min_set_by_pair[pair_id] = min_set

    def delete_pair(self, pair_id: str) -> None:
        self._min_set_by_pair.pop(pair_id, None)


def current_set_number(*, pair_id: str, pair_registry, sports_store) -> int:
    """读取 pair 当前盘数；状态不完整时 fail-close 为 0。"""
    if pair_registry is None or sports_store is None:
        return 0
    game_id = pair_registry.game_id_for_pair(pair_id)
    state = sports_store.get(game_id) if game_id is not None else None
    match = _SET_RE.fullmatch(str(getattr(state, "period", "") or ""))
    return int(match.group(1)) if match else 0
