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


@router.get("/config")
async def get_odds_config():
    """获取赔率订阅配置"""
    return app_state.get_odds_config()


@router.put("/config")
async def update_odds_config(data: dict[str, Any]):
    """更新赔率订阅配置"""
    return app_state.update_odds_config(data)


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
    subscriptions_count = len(subscriptions)

    # 判断运行状态：有活跃订阅即认为运行中
    is_running = subscriptions_count > 0

    return {
        "running": is_running,
        "subscriptions_count": subscriptions_count,
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
        return {"odds": {}, "pairs_info": {}}

    odds = service.get_latest_odds(pair_id)

    # 获取比赛状态（in-play / pre-match）
    match_statuses = service.get_all_match_statuses()

    # 获取 pairs 信息（包含队名、比赛状态等）
    pairs_info = {}
    for pair_result in app_state.matched_pairs:
        is_live = match_statuses.get(pair_result.pair_id)
        pairs_info[pair_result.pair_id] = {
            "sport": pair_result.sport,
            "competition": pair_result.competition,
            "polymarket_home": pair_result.polymarket_home,
            "polymarket_away": pair_result.polymarket_away,
            "orbitexch_home": pair_result.orbitexch_home,
            "orbitexch_away": pair_result.orbitexch_away,
            "is_live": is_live,
            "status": "IN-PLAY" if is_live else ("PRE-MATCH" if is_live is False else "UNKNOWN"),
        }

    return {
        "odds": odds,
        "pairs_info": pairs_info,
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

        # 确保策略服务已注册
        app_state.ensure_strategy_registered()

        # 确保执行服务已注册（接收策略机会回调）
        await app_state.ensure_execution_registered()

        # 获取或创建服务
        service = app_state.get_odds_service()

        # 在订阅前先设置 RiskService 引用（确保余额回调能立即生效）
        app_state.ensure_risk_registered()

        # 直接使用完整的 MatchedPair 对象订阅
        await service.subscribe_matched_pairs(matched_pairs_full)

        _log.info(f"Subscribed to {len(matched_pairs_full)} pairs")

        # 注册比赛到策略服务（包含比赛状态：in-play 或 pre-match）
        app_state.register_matches_to_strategy()
        _log.info("Registered matches to strategy service with status")

        # 加载历史持仓到风控服务
        position_counts = await app_state.load_risk_historical_positions()
        _log.info(f"Loaded historical positions: {position_counts}")

        # 更新执行服务的 OrbitExch 页面引用（订阅后页面才可用）
        app_state.update_execution_pages()
        _log.info("Updated execution service with OrbitExch pages")

        # 启动健康检查循环（确保 OrbitExch pages 已设置）
        risk_service = app_state.get_risk_service()
        risk_service.start_health_check_loop()
        _log.info("Health check loop started")

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


@router.get("/debug/orbitexch-selections")
async def get_orbitexch_selections():
    """
    获取 OrbitExch selection 映射信息（用于调试）

    返回已注册的 market_id:selection_id -> pair_id/market_type 映射
    """
    service = app_state.get_odds_service()

    if not service or not service._orbitexch_client:
        return {"selections": {}, "message": "OrbitExch client not running"}

    selections = service._orbitexch_client.get_all_registered_selections()

    return {
        "selections": selections,
        "total": len(selections),
    }


@router.get("/debug/websocket-analysis")
async def get_websocket_analysis():
    """
    获取 WebSocket 消息分析（用于调试）

    返回捕获的 WebSocket 发送和接收的消息，帮助分析订阅机制
    """
    service = app_state.get_odds_service()

    if not service or not service._orbitexch_client:
        return {"error": "OrbitExch client not running"}

    analysis = await service._orbitexch_client.get_websocket_analysis()

    return analysis


@router.get("/debug/polymarket-tokens")
async def get_polymarket_tokens():
    """
    获取 Polymarket 已订阅的 tokens（用于调试）
    """
    service = app_state.get_odds_service()

    if not service or not service._polymarket_client:
        return {"error": "Polymarket client not running"}

    subscribed_tokens = service._polymarket_client._subscribed_tokens
    latest_odds = service._polymarket_client._latest_odds

    # Group by event_id
    events_summary = {}
    for token_id, info in subscribed_tokens.items():
        event_id = info.get("event_id", "unknown")
        if event_id not in events_summary:
            events_summary[event_id] = {"tokens": [], "has_data": False}
        events_summary[event_id]["tokens"].append({
            "token_id": token_id[:30] + "...",
            "market_type": info.get("market_type"),
            "outcome": info.get("outcome"),
            "has_data": token_id in latest_odds,
        })
        if token_id in latest_odds:
            events_summary[event_id]["has_data"] = True

    # Events with data vs without
    events_with_data = [eid for eid, info in events_summary.items() if info["has_data"]]
    events_without_data = [eid for eid, info in events_summary.items() if not info["has_data"]]

    return {
        "events_summary": events_summary,
        "events_with_data": events_with_data,
        "events_without_data": events_without_data,
        "total_subscribed": len(subscribed_tokens),
        "tokens_with_data": len(latest_odds),
    }


@router.get("/debug/raw-odds")
async def get_raw_odds():
    """
    获取原始赔率数据（用于调试）

    包括所有缓存的赔率数据和未匹配的数据
    """
    service = app_state.get_odds_service()

    if not service:
        return {"odds": {}}

    # OrbitExch 已注册的 selections 和未匹配的 selections
    orbitexch_registered = {}
    orbitexch_unmatched = {}
    orbitexch_pair_info = {}
    if service._orbitexch_client:
        orbitexch_registered = service._orbitexch_client.get_all_registered_selections()
        orbitexch_unmatched = service._orbitexch_client.get_unmatched_selections()
        orbitexch_pair_info = service._orbitexch_client.get_pair_info()

    return {
        "latest_odds": service._latest_odds,
        "subscribed_pairs": {
            pair_id: {
                "polymarket_event_id": pair.polymarket_event_id,
                "orbitexch_home": pair.orbitexch_home_team,
                "orbitexch_away": pair.orbitexch_away_team,
                "orbitexch_data": pair.orbitexch_data,
            }
            for pair_id, pair in service._subscribed_pairs.items()
        },
        "orbitexch_pair_info": orbitexch_pair_info,
        "orbitexch_registered_selections": orbitexch_registered,
        "orbitexch_unmatched_selections": orbitexch_unmatched,
    }
