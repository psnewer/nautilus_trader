"""
执行会话 (Execution Session)

管理一次订单执行的完整生命周期。
"""

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SessionPhase(Enum):
    """会话阶段"""
    PLANNING = "planning"      # 订单规划
    OPERATING = "operating"    # 订单操作
    TRACKING = "tracking"      # 订单追踪
    COMPLETED = "completed"    # 执行完成
    FAILED = "failed"          # 执行失败


class SessionEndReason(Enum):
    """会话结束原因"""
    TARGET_MET = "target_met"           # 目标达成
    ZERO_FILL = "zero_fill"             # 全部未成交（最小损失）
    MAX_FAILURE_RETRIES = "max_failure_retries"  # 达到失败重试次数上限
    MARKET_CLOSED = "market_closed"     # 市场关闭
    MANUAL_STOP = "manual_stop"         # 手动停止
    ERROR = "error"                     # 错误


@dataclass
class OutcomeShares:
    """各方向的 share 数量"""
    home: float = 0.0
    draw: float = 0.0
    away: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {"home": self.home, "draw": self.draw, "away": self.away}

    @classmethod
    def from_dict(cls, data: dict) -> "OutcomeShares":
        return cls(
            home=data.get("home", 0.0),
            draw=data.get("draw", 0.0),
            away=data.get("away", 0.0),
        )

    def total(self) -> float:
        return self.home + self.draw + self.away

    def is_zero(self) -> bool:
        return self.home == 0 and self.draw == 0 and self.away == 0

    def __getitem__(self, key: str) -> float:
        return getattr(self, key, 0.0)

    def __setitem__(self, key: str, value: float) -> None:
        setattr(self, key, value)


@dataclass
class OutcomeProbabilities:
    """各方向的概率 (0-1)"""
    home: float = 0.0
    draw: float = 0.0
    away: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {"home": self.home, "draw": self.draw, "away": self.away}

    @classmethod
    def from_dict(cls, data: dict) -> "OutcomeProbabilities":
        return cls(
            home=data.get("home", 0.0),
            draw=data.get("draw", 0.0),
            away=data.get("away", 0.0),
        )

    def outcomes(self) -> list[str]:
        """返回有效的结果列表（概率 > 0）"""
        result = []
        if self.home > 0:
            result.append("home")
        if self.draw > 0:
            result.append("draw")
        if self.away > 0:
            result.append("away")
        return result

    def __getitem__(self, key: str) -> float:
        return getattr(self, key, 0.0)

    def __setitem__(self, key: str, value: float) -> None:
        setattr(self, key, value)


@dataclass
class ExecutionSession:
    """
    执行会话

    管理一次订单执行的完整生命周期，包括：
    - 初始目标设定
    - 成交追踪
    - 补救计算
    - 重试管理
    """
    session_id: str
    pair_id: str
    opportunity_id: str

    # 目标和成交
    initial_target: OutcomeShares           # 初始目标 share
    adjusted_target: OutcomeShares          # 调整后目标（补救后）
    filled: OutcomeShares                   # 已成交 share
    initial_probabilities: OutcomeProbabilities  # 初始下单时的概率

    # 状态
    phase: SessionPhase = SessionPhase.PLANNING
    end_reason: SessionEndReason | None = None

    # 重试
    retry_count: int = 0
    failure_count: int = 0
    max_failure_retries: int = 3

    # 当前操作
    pending_operations: list = field(default_factory=list)
    operation_results: list = field(default_factory=list)

    # 历史记录
    history: list[dict] = field(default_factory=list)

    # 时间戳
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def create(
        cls,
        pair_id: str,
        opportunity_id: str,
        target_shares: dict[str, float],
        probabilities: dict[str, float],
        max_failure_retries: int = 3,
    ) -> "ExecutionSession":
        """
        创建新会话

        Args:
            pair_id: 比赛 ID
            opportunity_id: 机会 ID
            target_shares: 目标 share {home, draw, away}
            probabilities: 概率 {home, draw, away}
            max_failure_retries: 最大失败重试次数
        """
        initial_target = OutcomeShares.from_dict(target_shares)
        return cls(
            session_id=f"session-{uuid.uuid4().hex[:8]}",
            pair_id=pair_id,
            opportunity_id=opportunity_id,
            initial_target=initial_target,
            adjusted_target=OutcomeShares.from_dict(target_shares),  # 初始时等于目标
            filled=OutcomeShares(),
            initial_probabilities=OutcomeProbabilities.from_dict(probabilities),
            max_failure_retries=max_failure_retries,
        )

    def update_phase(self, phase: SessionPhase) -> None:
        """更新阶段"""
        old_phase = self.phase
        self.phase = phase
        self.updated_at = time.time()
        self.history.append({
            "event": "phase_change",
            "from": old_phase.value,
            "to": phase.value,
            "timestamp": self.updated_at,
        })

    def update_filled(self, filled: dict[str, float]) -> None:
        """更新已成交"""
        self.filled = OutcomeShares.from_dict(filled)
        self.updated_at = time.time()
        self.history.append({
            "event": "filled_update",
            "filled": self.filled.to_dict(),
            "timestamp": self.updated_at,
        })

    def update_target(self, target: dict[str, float]) -> None:
        """更新调整后目标"""
        self.adjusted_target = OutcomeShares.from_dict(target)
        self.updated_at = time.time()
        self.history.append({
            "event": "target_update",
            "target": self.adjusted_target.to_dict(),
            "timestamp": self.updated_at,
        })

    def increment_retry(self) -> None:
        """
        增加重试次数
        """
        self.retry_count += 1
        self.updated_at = time.time()
        self.history.append({
            "event": "retry",
            "count": self.retry_count,
            "timestamp": self.updated_at,
        })

    def complete(self, reason: SessionEndReason) -> None:
        """完成会话"""
        self.phase = SessionPhase.COMPLETED
        self.end_reason = reason
        self.updated_at = time.time()
        self.history.append({
            "event": "completed",
            "reason": reason.value,
            "timestamp": self.updated_at,
        })

    def fail(self, reason: SessionEndReason) -> None:
        """失败会话"""
        self.phase = SessionPhase.FAILED
        self.end_reason = reason
        self.updated_at = time.time()
        self.history.append({
            "event": "failed",
            "reason": reason.value,
            "timestamp": self.updated_at,
        })

    def is_target_met(self) -> bool:
        """检查目标是否达成"""
        target = self.adjusted_target
        filled = self.filled
        # 允许小误差
        epsilon = 0.01
        return (
            abs(filled.home - target.home) < epsilon and
            abs(filled.draw - target.draw) < epsilon and
            abs(filled.away - target.away) < epsilon
        )

    def is_zero_fill(self) -> bool:
        """检查是否全部未成交"""
        return self.filled.is_zero()

    def needs_recovery(self) -> bool:
        """检查是否需要补救"""
        if self.is_target_met():
            return False
        if self.is_zero_fill():
            return False
        # 部分成交，需要补救
        return True

    def increment_failure(self) -> bool:
        """
        增加失败次数

        Returns:
            是否还可以继续失败重试
        """
        self.failure_count += 1
        self.updated_at = time.time()
        self.history.append({
            "event": "failure_retry",
            "count": self.failure_count,
            "timestamp": self.updated_at,
        })
        return self.failure_count < self.max_failure_retries

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "session_id": self.session_id,
            "pair_id": self.pair_id,
            "opportunity_id": self.opportunity_id,
            "initial_target": self.initial_target.to_dict(),
            "adjusted_target": self.adjusted_target.to_dict(),
            "filled": self.filled.to_dict(),
            "initial_probabilities": self.initial_probabilities.to_dict(),
            "phase": self.phase.value,
            "end_reason": self.end_reason.value if self.end_reason else None,
            "retry_count": self.retry_count,
            "failure_count": self.failure_count,
            "max_failure_retries": self.max_failure_retries,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history_count": len(self.history),
        }
