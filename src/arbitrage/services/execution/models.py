"""
订单执行模型

定义订单、执行结果等数据结构。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import time
import uuid


class Venue(Enum):
    """交易平台"""
    POLYMARKET = "polymarket"
    ORBITEXCH = "orbitexch"


class OrderSide(Enum):
    """订单方向"""
    BUY = "BUY"
    SELL = "SELL"
    BACK = "BACK"  # OrbitExch: 买入
    LAY = "LAY"    # OrbitExch: 卖出


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "pending"        # 待发送
    SUBMITTED = "submitted"    # 已提交
    LIVE = "live"              # 挂单中
    PARTIALLY_FILLED = "partially_filled"  # 部分成交
    FILLED = "filled"          # 完全成交
    CANCELLED = "cancelled"    # 已撤销
    REJECTED = "rejected"      # 被拒绝
    FAILED = "failed"          # 执行失败


class OrderType(Enum):
    """订单类型"""
    GTC = "GTC"  # Good-Til-Cancelled (POC)
    FOK = "FOK"  # Fill-Or-Kill (市价立即成交或取消)
    FAK = "FAK"  # Fill-And-Kill (部分成交，剩余取消)


@dataclass
class Order:
    """
    订单模型

    统一的订单表示，支持 Polymarket 和 OrbitExch。
    """
    # 基本信息
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    venue: Venue = Venue.POLYMARKET

    # 市场信息
    pair_id: str = ""           # 内部匹配 ID
    market_type: str = ""       # "home", "draw", "away"

    # Polymarket 特有
    token_id: str = ""          # Polymarket token ID
    condition_id: str = ""      # Polymarket condition ID

    # OrbitExch 特有
    market_id: str = ""         # OrbitExch market ID
    selection_id: str = ""      # OrbitExch selection ID
    handicap: float = 0.0       # OrbitExch handicap

    # 订单参数
    side: OrderSide = OrderSide.BUY
    price: float = 0.0          # 价格 (Polymarket: 0-1, OrbitExch: 赔率)
    size: float = 0.0           # 数量 (USDC)
    order_type: OrderType = OrderType.GTC

    # 状态
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0    # 已成交数量
    avg_fill_price: float = 0.0 # 平均成交价格

    # 平台返回的 ID
    venue_order_id: str = ""    # 平台订单 ID

    # 时间戳
    created_at: float = field(default_factory=time.time)
    submitted_at: float = 0.0
    updated_at: float = field(default_factory=time.time)

    # 错误信息
    error_message: str = ""

    # 元数据
    metadata: dict = field(default_factory=dict)

    @property
    def remaining_size(self) -> float:
        """未成交数量"""
        return self.size - self.filled_size

    @property
    def is_active(self) -> bool:
        """是否为活跃订单"""
        return self.status in (
            OrderStatus.SUBMITTED,
            OrderStatus.LIVE,
            OrderStatus.PARTIALLY_FILLED,
        )

    @property
    def is_done(self) -> bool:
        """是否已完成"""
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "order_id": self.order_id,
            "venue": self.venue.value,
            "pair_id": self.pair_id,
            "market_type": self.market_type,
            "token_id": self.token_id,
            "condition_id": self.condition_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "handicap": self.handicap,
            "side": self.side.value,
            "price": self.price,
            "size": self.size,
            "order_type": self.order_type.value,
            "status": self.status.value,
            "filled_size": self.filled_size,
            "avg_fill_price": self.avg_fill_price,
            "remaining_size": self.remaining_size,
            "venue_order_id": self.venue_order_id,
            "created_at": self.created_at,
            "submitted_at": self.submitted_at,
            "updated_at": self.updated_at,
            "error_message": self.error_message,
            "metadata": self.metadata,
        }


@dataclass
class ExecutionResult:
    """
    执行结果

    订单执行后的返回结果。
    """
    success: bool
    order: Order
    message: str = ""
    venue_response: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "order": self.order.to_dict(),
            "message": self.message,
            "venue_response": self.venue_response,
        }


@dataclass
class CancelResult:
    """
    撤单结果
    """
    success: bool
    order_id: str
    venue_order_id: str = ""
    message: str = ""
    venue_response: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "order_id": self.order_id,
            "venue_order_id": self.venue_order_id,
            "message": self.message,
            "venue_response": self.venue_response,
        }
