"""VenueExecutionLiveness —— execution reconcile / stuck-flight 的 venue 存活真值。

该状态只表达“执行端能否可信对账”,不表达是否有持仓、是否有挂单,也不在每次下单前翻 false。
Risk 在提交前读取它做 fail-closed 门控;启动/周期 reconciliation 在远端 order / position
查询正常返回后分别标记 alive,在查询抛异常时标记 dead。report 的本地应用结果不参与判定。
"""

from __future__ import annotations

from collections.abc import Iterable


class VenueExecutionLiveness:
    """单进程共享的 venue execution liveness 表。

    PM 的 order / position 对账来源不同,所以拆成两位;venue alive 是派生值:
    `order_alive && position_alive`。未知 venue 默认 false,避免启动后未对账就交易。
    """

    def __init__(self, venues: Iterable[object] = ()) -> None:
        self._order_alive: dict[str, bool] = {}
        self._position_alive: dict[str, bool] = {}
        for venue in venues:
            key = self._key(venue)
            self._order_alive[key] = False
            self._position_alive[key] = False

    def mark_order_alive(self, venue: object) -> None:
        self._order_alive[self._key(venue)] = True

    def mark_order_dead(self, venue: object) -> None:
        self._order_alive[self._key(venue)] = False

    def mark_position_alive(self, venue: object) -> None:
        self._position_alive[self._key(venue)] = True

    def mark_position_dead(self, venue: object) -> None:
        self._position_alive[self._key(venue)] = False

    def order_alive(self, venue: object) -> bool:
        return self._order_alive.get(self._key(venue), False)

    def position_alive(self, venue: object) -> bool:
        return self._position_alive.get(self._key(venue), False)

    def venue_alive(self, venue: object) -> bool:
        key = self._key(venue)
        return self._order_alive.get(key, False) and self._position_alive.get(key, False)

    def all_alive(self, venues: Iterable[object]) -> bool:
        return all(self.venue_alive(venue) for venue in venues)

    def snapshot(self) -> dict[str, dict[str, bool]]:
        venues = set(self._order_alive) | set(self._position_alive)
        return {
            venue: {
                "order_alive": self._order_alive.get(venue, False),
                "position_alive": self._position_alive.get(venue, False),
                "venue_alive": self.venue_alive(venue),
            }
            for venue in sorted(venues)
        }

    @staticmethod
    def _key(venue: object) -> str:
        value = getattr(venue, "value", venue)
        return str(value).upper()
