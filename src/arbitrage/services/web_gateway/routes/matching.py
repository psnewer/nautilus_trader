"""
市场匹配 API 路由
"""

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from ..state import app_state, MatchedPairResult
from ..messages import MatchingCompleteMessage
from ..topics import MATCHING_COMPLETE_TOPIC

router = APIRouter(prefix="/api/matching", tags=["matching"])
_log = logging.getLogger(__name__)

# 运行状态
_matching_running = False


class AliasRequest(BaseModel):
    """别名请求"""
    alias: str
    standard: str


@router.get("/config")
async def get_matching_config():
    """获取市场匹配配置"""
    return app_state.get_matching_config()


@router.put("/config")
async def update_matching_config(data: dict[str, Any]):
    """更新市场匹配配置"""
    return app_state.update_matching_config(data)


@router.post("/config/sport-aliases")
async def add_sport_alias(body: AliasRequest):
    """添加 sport 别名"""
    return app_state.add_sport_alias(body.alias, body.standard)


@router.post("/config/competition-aliases")
async def add_competition_alias(body: AliasRequest):
    """添加 competition 别名"""
    return app_state.add_competition_alias(body.alias, body.standard)


@router.get("/results")
async def get_matching_results():
    """获取市场匹配结果"""
    return app_state.get_matching_results()


@router.get("/status")
async def get_matching_status():
    """获取匹配任务状态"""
    return {
        "running": _matching_running,
        "pairs_count": len(app_state._matched_pairs),
    }


async def _run_matching():
    """执行市场匹配（后台任务）"""
    global _matching_running

    _matching_running = True
    _log.info("Starting market matching...")

    try:
        from src.arbitrage.services.market_matching.service import MarketMatchingService

        # 获取已发现的事件
        poly_events = app_state.polymarket_events
        orbit_events = app_state.orbitexch_events

        if not poly_events and not orbit_events:
            _log.warning("No events to match. Run discovery first.")
            if app_state.pipeline_active:
                app_state.stop_pipeline()
            return

        # 创建匹配服务
        service = MarketMatchingService(app_state.matching_config_obj)

        # 需要将 DiscoveryResult 转换为 scraper 的 MatchEvent 格式
        # 这里创建简单的适配类
        class MockPolyEvent:
            def __init__(self, r):
                self.sport = r.sport
                self.competition = r.competition
                self.home_team = r.home_team
                self.away_team = r.away_team
                self.event_id = r.event_id
                self.title = ""
                self.tags = []

        class MockOrbitEvent:
            def __init__(self, r):
                self.sport = r.sport
                self.competition = r.competition
                self.home_team = r.home_team
                self.away_team = r.away_team
                self.sport_id = r.extra.get("sport_id", "")
                self.competition_id = r.extra.get("competition_id", "")
                # Market ID (unique per match) and Selection IDs for odds matching
                self.market_id = r.extra.get("market_id", "")
                self.home_selection_id = r.extra.get("home_selection_id", "")
                self.draw_selection_id = r.extra.get("draw_selection_id", "")
                self.away_selection_id = r.extra.get("away_selection_id", "")

        poly_mock = [MockPolyEvent(e) for e in poly_events]
        orbit_mock = [MockOrbitEvent(e) for e in orbit_events]

        # 执行匹配
        matched = service.match_markets(poly_mock, orbit_mock)

        # 保存完整的 MatchedPair 对象（用于赔率订阅）
        app_state.set_matched_pairs_full(matched)

        # 转换为 MatchedPairResult（用于 UI 显示）
        results = [
            MatchedPairResult(
                pair_id=p.pair_id,
                sport=p.sport,
                competition=p.competition,
                polymarket_home=p.polymarket_home_team,
                polymarket_away=p.polymarket_away_team,
                polymarket_event_id=p.polymarket_event_id,
                orbitexch_home=p.orbitexch_home_team,
                orbitexch_away=p.orbitexch_away_team,
                similarity=p.total_similarity,
                confidence=p.confidence,
                extra=p.orbitexch_data.copy() if hasattr(p, "orbitexch_data") else {},
            )
            for p in matched
        ]

        app_state.set_matched_pairs(results)
        _log.info(f"Matched {len(results)} pairs")

        # 发布 Matching 完成消息
        msgbus = app_state.get_msgbus()
        msg = MatchingCompleteMessage(pairs_count=len(results))
        msgbus.publish(MATCHING_COMPLETE_TOPIC, msg)
        _log.info(f"Published MatchingCompleteMessage: pairs={msg.pairs_count}")

    except Exception as e:
        _log.error(f"Matching failed: {e}")
        import traceback
        traceback.print_exc()
        # 管道模式下重置状态，防止卡死
        if app_state.pipeline_active:
            app_state.stop_pipeline()
    finally:
        _matching_running = False


@router.post("/run")
async def run_matching(background_tasks: BackgroundTasks):
    """触发市场匹配"""
    global _matching_running

    if _matching_running:
        raise HTTPException(status_code=409, detail="Matching already running")

    background_tasks.add_task(_run_matching)

    return {"status": "started"}
