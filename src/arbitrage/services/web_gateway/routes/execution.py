"""
订单执行 API 路由
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..state import app_state

router = APIRouter(prefix="/api/execution", tags=["execution"])
_log = logging.getLogger(__name__)


# =============================================================================
# 请求模型
# =============================================================================

class PlaceOrderRequest(BaseModel):
    """下单请求"""
    venue: str  # "polymarket" or "orbitexch"
    pair_id: str
    market_type: str  # "home", "draw", "away"
    side: str  # "BUY" or "SELL" (Polymarket), "BACK" or "LAY" (OrbitExch)
    price: float
    size: float

    # Polymarket 特有
    token_id: str = ""
    condition_id: str = ""

    # OrbitExch 特有
    market_id: str = ""
    selection_id: str = ""
    handicap: float = 0.0


class CancelOrderRequest(BaseModel):
    """撤单请求"""
    order_id: str


class TakeAtMarketRequest(BaseModel):
    """市价成交请求"""
    order_id: str


class ExecutionConfigRequest(BaseModel):
    """执行配置更新请求"""
    tracking_timeout_sec: float | None = None
    max_failure_retries: int | None = None
    discount: float | None = None
    take_off: float | None = None
    market_order_enabled: bool | None = None


# =============================================================================
# API 端点
# =============================================================================

@router.get("/status")
async def get_execution_status():
    """获取执行服务状态"""
    service = app_state.get_execution_service()

    if not service:
        return {
            "initialized": False,
            "enabled": False,
        }

    summary = service.get_orders_summary()

    return {
        "initialized": service._initialized,
        "enabled": service.config.enabled,
        **summary,
    }


@router.get("/config")
async def get_execution_config():
    """获取执行配置"""
    return app_state.get_execution_config()


@router.put("/config")
async def update_execution_config(request: ExecutionConfigRequest):
    """更新执行配置"""
    return app_state.update_execution_config(request.dict())


@router.post("/orders")
async def place_order(request: PlaceOrderRequest):
    """
    下单

    根据 venue 创建并执行订单。
    """
    from src.arbitrage.services.execution import (
        Order, Venue, OrderSide, OrderType
    )

    service = app_state.get_execution_service()

    if not service or not service._initialized:
        raise HTTPException(status_code=503, detail="Execution service not initialized")

    # 解析 venue
    try:
        venue = Venue(request.venue)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid venue: {request.venue}")

    # 解析 side
    side_mapping = {
        "BUY": OrderSide.BUY,
        "SELL": OrderSide.SELL,
        "BACK": OrderSide.BACK,
        "LAY": OrderSide.LAY,
    }
    side = side_mapping.get(request.side.upper())
    if not side:
        raise HTTPException(status_code=400, detail=f"Invalid side: {request.side}")

    # 创建订单
    order = Order(
        venue=venue,
        pair_id=request.pair_id,
        market_type=request.market_type,
        side=side,
        price=request.price,
        size=request.size,
        token_id=request.token_id,
        condition_id=request.condition_id,
        market_id=request.market_id,
        selection_id=request.selection_id,
        handicap=request.handicap,
        order_type=OrderType.GTC,
    )

    # 执行订单
    result = await service.execute_order(order)

    return result.to_dict()


@router.delete("/orders/{order_id}")
async def cancel_order(order_id: str):
    """撤销订单"""
    service = app_state.get_execution_service()

    if not service or not service._initialized:
        raise HTTPException(status_code=503, detail="Execution service not initialized")

    result = await service.cancel_order(order_id)

    return result.to_dict()


@router.get("/orders")
async def get_orders(
    venue: str | None = None,
    limit: int = 100,
):
    """
    获取活跃订单列表

    Args:
        venue: 可选，过滤平台
        limit: 返回数量限制
    """
    from src.arbitrage.services.execution import Venue

    service = app_state.get_execution_service()

    if not service:
        return {"orders": [], "total": 0}

    venue_filter = None
    if venue:
        try:
            venue_filter = Venue(venue)
        except ValueError:
            pass

    orders = service.get_active_orders(venue=venue_filter)

    return {
        "orders": orders[:limit],
        "total": len(orders),
    }


@router.get("/orders/active")
async def get_active_orders(venue: str | None = None):
    """获取活跃订单"""
    odds_service = app_state.get_odds_service()
    if not odds_service:
        return {"orders": [], "total": 0}

    venue_filter = venue if venue in {"polymarket", "orbitexch"} else None

    orders = []
    if venue_filter in (None, "polymarket"):
        for order in odds_service.get_polymarket_open_orders():
            size_remaining = float(order.original_size) - float(order.size_matched)
            if size_remaining <= 0:
                continue
            orders.append({
                "venue": "polymarket",
                "order_id": order.order_id,
                "status": order.status,
                "price": order.price,
                "size_matched": order.size_matched,
                "size_remaining": size_remaining,
                "token_id": order.asset_id,
                "condition_id": order.market,
            })

    if venue_filter in (None, "orbitexch"):
        for bet in odds_service.get_orbitexch_bets():
            size_remaining = float(bet.get("sizeRemaining", 0))
            if size_remaining <= 0:
                continue
            orders.append({
                "venue": "orbitexch",
                "order_id": str(bet.get("offerId", "")),
                "status": bet.get("status", ""),
                "request_price": float(bet.get("price", 0)),
                "average_price": float(bet.get("averagePrice", 0) or 0),
                "size_matched": float(bet.get("sizeMatched", 0)),
                "size_remaining": size_remaining,
                "market_id": bet.get("marketId", ""),
                "selection_id": bet.get("selectionId", ""),
                "side": bet.get("side", ""),
            })

    return {
        "orders": orders,
        "total": len(orders),
    }


@router.get("/orders/{order_id}")
async def get_order(order_id: str):
    """获取单个订单"""
    service = app_state.get_execution_service()

    if not service:
        raise HTTPException(status_code=404, detail="Order not found")

    for o in service.get_active_orders():
        if o["order_id"] == order_id:
            return o

    raise HTTPException(status_code=404, detail="Order not found")


@router.post("/cancel-all")
async def cancel_all_orders(venue: str | None = None):
    """撤销所有活跃订单"""
    from src.arbitrage.services.execution import Venue

    service = app_state.get_execution_service()

    if not service or not service._initialized:
        raise HTTPException(status_code=503, detail="Execution service not initialized")

    venue_filter = None
    if venue:
        try:
            venue_filter = Venue(venue)
        except ValueError:
            pass

    results = await service.cancel_all_orders(venue=venue_filter)

    return {
        "cancelled": len([r for r in results if r.success]),
        "failed": len([r for r in results if not r.success]),
        "results": [r.to_dict() for r in results],
    }


@router.post("/initialize")
async def initialize_service():
    """初始化执行服务"""
    service = app_state.get_execution_service()

    if not service:
        raise HTTPException(status_code=500, detail="Failed to create execution service")

    success = await service.initialize()

    return {
        "success": success,
        "initialized": service._initialized,
    }


@router.post("/orbitexch/cancel-all-unmatched")
async def cancel_all_orbitexch_unmatched():
    """
    撤销 OrbitExch 上所有未成交订单

    使用 OrbitExch 的 "Cancel All Unmatched" 功能批量撤单。
    此操作会撤销 OrbitExch 账户中所有未成交订单，包括非本系统下的订单。
    """
    service = app_state.get_execution_service()

    if not service or not service._initialized:
        raise HTTPException(status_code=503, detail="Execution service not initialized")

    result = await service.cancel_all_orbitexch_unmatched()

    return result.to_dict()
