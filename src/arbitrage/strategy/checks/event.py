"""按本轮策略评估的触发源过滤分支。"""

from __future__ import annotations

from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


_EVENT_NAMES = {
    "obd": frozenset({"OrderBookDeltas", "MarketOrderBookDeltas"}),
    "score": frozenset({"SportsGameUpdate"}),
}


class EventCheck(Check):
    """配置 event 时只接受对应触发源；省略时不做过滤。"""

    def __init__(self, event: str | None = None) -> None:
        if event is None:
            self._event_names = None
            return
        if not isinstance(event, str) or event.strip().lower() not in _EVENT_NAMES:
            raise ValueError("event: event must be one of obd/score")
        self._event_names = _EVENT_NAMES[event.strip().lower()]

    def passes(self, ctx: EvalContext) -> bool:
        return self._event_names is None or ctx.event_name in self._event_names
