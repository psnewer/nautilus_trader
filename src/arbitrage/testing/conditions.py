"""
退出条件原语

可组合的条件，对 LogEvent 流求值。
- LogMatch: 单行日志匹配（logger/level/contains/pattern）
- AllOf: 全部子条件满足（顺序无关）
- AnyOf: 任一子条件满足
- Sequence: 顺序匹配，前一个满足后才考虑下一个
- Negate: 反向（用于"不应出现"）

每个条件维护自己的内部状态，调用 feed(event) 喂入一条事件，调用 met() 查询是否满足。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .monitor import LogEvent


@dataclass
class ConditionResult:
    """条件评估结果（用于报告）"""

    name: str
    met: bool
    matched_events: list[LogEvent] = field(default_factory=list)
    children: list["ConditionResult"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "met": self.met,
            "matched_events": [
                {
                    "logger": e.logger,
                    "level": e.level,
                    "message": e.message,
                    "timestamp": e.timestamp,
                }
                for e in self.matched_events
            ],
            "children": [c.to_dict() for c in self.children],
        }


class Condition(ABC):
    """退出条件基类"""

    @abstractmethod
    def feed(self, event: LogEvent) -> None:
        """喂入一条事件"""

    @abstractmethod
    def met(self) -> bool:
        """是否已满足"""

    @abstractmethod
    def result(self) -> ConditionResult:
        """生成结果用于报告"""

    def reset(self) -> None:
        """重置内部状态（默认无操作；有状态的子类需覆盖）"""


class LogMatch(Condition):
    """
    匹配一条日志。

    Args:
        logger: 限定 logger 名（精确匹配）。None 表示任意 logger。
        level: 限定级别 ("INFO" / "WARNING" / "ERROR" / "DEBUG")。None 表示任意。
        contains: 子串匹配。
        pattern: 正则匹配（与 contains 二选一或组合）。
        name: 可选标识，便于报告查看。
    """

    def __init__(
        self,
        *,
        logger: str | None = None,
        level: str | None = None,
        contains: str | None = None,
        pattern: str | None = None,
        name: str | None = None,
    ) -> None:
        self._logger = logger
        self._level = level.upper() if level else None
        self._contains = contains
        self._pattern = re.compile(pattern) if pattern else None
        self._name = name or self._auto_name()
        self._matched: LogEvent | None = None

    def _auto_name(self) -> str:
        bits = []
        if self._logger:
            bits.append(f"logger={self._logger}")
        if self._level:
            bits.append(f"level={self._level}")
        if self._contains:
            bits.append(f"contains={self._contains!r}")
        if self._pattern:
            bits.append(f"pattern={self._pattern.pattern!r}")
        return f"LogMatch({', '.join(bits) or '*'})"

    def feed(self, event: LogEvent) -> None:
        if self._matched is not None:
            return
        if self._logger is not None and event.logger != self._logger:
            return
        if self._level is not None and event.level != self._level:
            return
        if self._contains is not None and self._contains not in event.message:
            return
        if self._pattern is not None and not self._pattern.search(event.message):
            return
        self._matched = event

    def met(self) -> bool:
        return self._matched is not None

    def result(self) -> ConditionResult:
        return ConditionResult(
            name=self._name,
            met=self.met(),
            matched_events=[self._matched] if self._matched else [],
        )

    def reset(self) -> None:
        self._matched = None


class _Composite(Condition):
    """组合条件基类"""

    def __init__(self, *children: Condition, name: str | None = None) -> None:
        if not children:
            raise ValueError("composite condition requires at least one child")
        self._children: list[Condition] = list(children)
        self._name = name or self.__class__.__name__

    def reset(self) -> None:
        for c in self._children:
            c.reset()


class AllOf(_Composite):
    """所有子条件都满足（顺序无关）"""

    def feed(self, event: LogEvent) -> None:
        for c in self._children:
            c.feed(event)

    def met(self) -> bool:
        return all(c.met() for c in self._children)

    def result(self) -> ConditionResult:
        children = [c.result() for c in self._children]
        return ConditionResult(
            name=self._name,
            met=self.met(),
            children=children,
        )


class AnyOf(_Composite):
    """任一子条件满足"""

    def feed(self, event: LogEvent) -> None:
        for c in self._children:
            if c.met():
                continue
            c.feed(event)

    def met(self) -> bool:
        return any(c.met() for c in self._children)

    def result(self) -> ConditionResult:
        children = [c.result() for c in self._children]
        return ConditionResult(
            name=self._name,
            met=self.met(),
            children=children,
        )


class Sequence(_Composite):
    """
    顺序条件: 子条件按顺序匹配，前一个满足后才开始评估下一个。

    每条事件只会喂给"当前未满足"的那一个子条件，
    避免后面的条件提前匹配到早期事件。
    """

    def __init__(self, *children: Condition, name: str | None = None) -> None:
        super().__init__(*children, name=name)
        self._cursor = 0

    def feed(self, event: LogEvent) -> None:
        if self._cursor >= len(self._children):
            return
        current = self._children[self._cursor]
        current.feed(event)
        # 推进游标
        while self._cursor < len(self._children) and self._children[self._cursor].met():
            self._cursor += 1

    def met(self) -> bool:
        return self._cursor >= len(self._children)

    def result(self) -> ConditionResult:
        children = [c.result() for c in self._children]
        return ConditionResult(
            name=self._name,
            met=self.met(),
            children=children,
        )

    def reset(self) -> None:
        super().reset()
        self._cursor = 0


class Negate(Condition):
    """
    反向条件: 包装的子条件未满足时为 True。

    一般用于失败兜底，如 Negate(LogMatch(level="ERROR"))。
    注意 Negate 本身永远"不会自然满足"（因为只要还没看到匹配就为 True），
    所以它通常配合 Sequence/超时使用，而不是单独用作 success。
    """

    def __init__(self, child: Condition, *, name: str | None = None) -> None:
        self._child = child
        self._name = name or f"Negate({child.__class__.__name__})"

    def feed(self, event: LogEvent) -> None:
        self._child.feed(event)

    def met(self) -> bool:
        return not self._child.met()

    def result(self) -> ConditionResult:
        return ConditionResult(
            name=self._name,
            met=self.met(),
            children=[self._child.result()],
        )

    def reset(self) -> None:
        self._child.reset()
