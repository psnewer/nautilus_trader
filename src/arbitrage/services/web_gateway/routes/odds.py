"""
赔率订阅 API 路由
"""

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException

from ..state import app_state

router = APIRouter(prefix="/api/odds", tags=["odds"])
_log = logging.getLogger(__name__)

# 订阅状态
_subscription_running = False


@router.get("/status")
async def get_subscription_status():
    """获取订阅状态"""
    service = app_state.get_odds_service()

    if not service:
        return {
            "running": False,
            "subscriptions_count": 0,
        }

    subscriptions = service.get_subscriptions()

    return {
        "running": service._running,
        "subscriptions_count": len(subscriptions),
    }


@router.get("/subscriptions")
async def get_subscriptions():
    """获取订阅列表"""
    service = app_state.get_odds_service()

    if not service:
        return {"subscriptions": []}

    subscriptions = service.get_subscriptions()

    return {
        "subscriptions": subscriptions,
        "total": len(subscriptions),
    }


@router.get("/latest")
async def get_latest_odds(pair_id: str | None = None):
    """
    获取最新赔率数据

    Args:
        pair_id: 可选，指定 pair_id 返回单个 pair 的赔率
    """
    service = app_state.get_odds_service()

    if not service:
        return {"odds": {}}

    odds = service.get_latest_odds(pair_id)

    return {
        "odds": odds,
        "pair_id": pair_id,
    }


async def _run_subscription():
    """执行赔率订阅（后台任务）"""
    global _subscription_running

    _subscription_running = True
    _log.info("Starting odds subscription...")

    try:
        # 获取完整的 MatchedPair 对象
        matched_pairs_full = app_state.matched_pairs_full

        if not matched_pairs_full:
            _log.warning("No matched pairs found. Run matching first.")
            return

        # 获取或创建服务
        service = app_state.get_odds_service()

        # 直接使用完整的 MatchedPair 对象订阅
        await service.subscribe_matched_pairs(matched_pairs_full)

        _log.info(f"Subscribed to {len(matched_pairs_full)} pairs")

    except Exception as e:
        _log.error(f"Odds subscription failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        _subscription_running = False


@router.post("/subscribe")
async def subscribe_odds(background_tasks: BackgroundTasks):
    """
    基于当前 matched_pairs 启动赔率订阅
    """
    global _subscription_running

    if _subscription_running:
        raise HTTPException(status_code=409, detail="Subscription already running")

    # 检查是否有匹配结果
    matched_pairs_full = app_state.matched_pairs_full
    if not matched_pairs_full:
        raise HTTPException(status_code=400, detail="No matched pairs found. Run matching first.")

    background_tasks.add_task(_run_subscription)

    return {"status": "started", "pairs_count": len(matched_pairs_full)}


@router.post("/unsubscribe")
async def unsubscribe_odds():
    """
    停止赔率订阅
    """
    service = app_state.get_odds_service()

    if not service:
        return {"status": "no service running"}

    await service.stop()

    return {"status": "stopped"}
