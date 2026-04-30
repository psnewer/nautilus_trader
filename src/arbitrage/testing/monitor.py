"""
日志监听器

注册一个 logging.Handler 到 root logger，捕获所有日志，转成 LogEvent 推入异步队列。
配合 conditions.py 的 ExitCondition 求值。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LogEvent:
    """一条捕获的日志记录"""

    logger: str
    level: str
    level_no: int
    message: str
    timestamp: float
    raw: logging.LogRecord | None = field(default=None, repr=False)

    @classmethod
    def from_record(cls, record: logging.LogRecord) -> "LogEvent":
        return cls(
            logger=record.name,
            level=record.levelname,
            level_no=record.levelno,
            message=record.getMessage(),
            timestamp=record.created,
            raw=record,
        )


class _QueueHandler(logging.Handler):
    """logging.Handler 把 LogRecord 投到 asyncio.Queue（线程安全）"""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        super().__init__()
        self._loop = loop
        self._queue = queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            event = LogEvent.from_record(record)
        except Exception:
            return
        try:
            self._loop.call_soon_threadsafe(self._safe_put, event)
        except RuntimeError:
            # loop closed; drop
            pass

    def _safe_put(self, event: LogEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


class LogMonitor:
    """
    日志监听器

    生命周期:
        async with LogMonitor() as mon:
            async for event in mon:
                ...

    或显式控制:
        mon = LogMonitor()
        mon.start()
        ...
        mon.stop()
    """

    def __init__(
        self,
        level: int = logging.DEBUG,
        loggers: list[str] | None = None,
        max_queue: int = 10000,
    ) -> None:
        self._level = level
        self._loggers = loggers or [""]  # 默认 root
        self._max_queue = max_queue
        self._queue: asyncio.Queue[LogEvent] | None = None
        self._handler: _QueueHandler | None = None
        self._attached: list[logging.Logger] = []
        self._captured: list[LogEvent] = []
        self._started_at: float = 0.0

    @property
    def captured(self) -> list[LogEvent]:
        """所有已经从队列拿出的事件（用于报告）"""
        return self._captured

    @property
    def started_at(self) -> float:
        return self._started_at

    def start(self) -> None:
        if self._handler is not None:
            return
        loop = asyncio.get_event_loop()
        self._queue = asyncio.Queue(maxsize=self._max_queue)
        self._handler = _QueueHandler(loop, self._queue)
        self._handler.setLevel(self._level)
        for name in self._loggers:
            log = logging.getLogger(name)
            log.addHandler(self._handler)
            # 确保级别不被 root 默认 WARNING 卡掉
            if log.level == logging.NOTSET or log.level > self._level:
                log.setLevel(self._level)
            self._attached.append(log)
        self._started_at = time.time()

    def stop(self) -> None:
        if self._handler is None:
            return
        for log in self._attached:
            log.removeHandler(self._handler)
        self._attached.clear()
        self._handler = None

    async def __aenter__(self) -> "LogMonitor":
        self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.stop()

    async def next_event(self, timeout: float | None = None) -> LogEvent | None:
        """
        从队列拿一条事件。超时返回 None。已捕获的事件会保留到 captured 列表用于报告。
        """
        if self._queue is None:
            raise RuntimeError("LogMonitor not started")
        try:
            if timeout is None:
                event = await self._queue.get()
            else:
                event = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        self._captured.append(event)
        return event
