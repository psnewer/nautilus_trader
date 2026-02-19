"""
订单执行服务

提供 Polymarket 和 OrbitExch 的订单执行功能。

功能:
1. 订单执行 (下单)
2. 订单撤销
3. 未成交部分按市价执行
4. 订单追踪和补救

架构:
- ExecutionService: 主服务，协调执行器
- ExecutionOrchestrator: 执行编排器，管理完整执行流程
- PolymarketExecutor: Polymarket 执行器 (使用 py-clob-client)
- OrbitExchExecutor: OrbitExch 执行器 (使用 Playwright)
- ExecutionSession: 执行会话，管理单次执行生命周期
- ExecutionPlanner: 执行规划器，规划订单操作
- OrderTracker: 订单追踪器，追踪订单状态
- RecoveryCalculator: 补救计算器，计算平均返水补救目标
"""

from .config import ExecutionConfig
from .models import (
    Venue,
    OrderSide,
    OrderStatus,
    OrderType,
    Order,
    ExecutionResult,
    CancelResult,
)
from .service import ExecutionService
from .polymarket_executor import PolymarketExecutor
from .orbitexch_executor import OrbitExchExecutor
from .session import (
    ExecutionSession,
    SessionPhase,
    SessionEndReason,
    OutcomeShares,
    OutcomeProbabilities,
)
from .planner import (
    ExecutionPlanner,
    ExecutionPlan,
    OrderOperation,
    OperationType,
    OperationVenue,
)
from .tracker import (
    OrderTracker,
    TrackingStatus,
    TrackingResult,
    BatchTrackingResult,
)
from .recovery import RecoveryCalculator, RecoveryResult
from .orchestrator import ExecutionOrchestrator

__all__ = [
    # Config
    "ExecutionConfig",
    # Models
    "Venue",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Order",
    "ExecutionResult",
    "CancelResult",
    # Service
    "ExecutionService",
    # Orchestrator
    "ExecutionOrchestrator",
    # Session
    "ExecutionSession",
    "SessionPhase",
    "SessionEndReason",
    "OutcomeShares",
    "OutcomeProbabilities",
    # Planner
    "ExecutionPlanner",
    "ExecutionPlan",
    "OrderOperation",
    "OperationType",
    "OperationVenue",
    # Tracker
    "OrderTracker",
    "TrackingStatus",
    "TrackingResult",
    "BatchTrackingResult",
    # Recovery
    "RecoveryCalculator",
    "RecoveryResult",
    # Executors
    "PolymarketExecutor",
    "OrbitExchExecutor",
]
