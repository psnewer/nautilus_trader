"""
市场发现 API 路由
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from ..state import app_state, DiscoveryResult
from ..messages import DiscoveryCompleteMessage
from ..topics import DISCOVERY_COMPLETE_TOPIC

router = APIRouter(prefix="/api/discovery", tags=["discovery"])
_log = logging.getLogger(__name__)

# 运行状态
_discovery_running = False
_discovery_task = None


class DiscoverRequest(BaseModel):
    """发现请求"""
    venue: str | None = None  # None 表示所有平台


class VenueSportsUpdate(BaseModel):
    """更新平台 sports 配置"""
    sports: list[dict[str, Any]]


@router.get("/config")
async def get_discovery_config():
    """获取市场发现配置"""
    return app_state.get_discovery_config()


@router.put("/config/venues/{venue}/sports")
async def update_venue_sports(venue: str, body: VenueSportsUpdate):
    """更新指定平台的 sports 配置"""
    try:
        return app_state.update_venue_sports(venue, body.sports)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/results")
async def get_discovery_results():
    """获取市场发现结果"""
    return app_state.get_discovery_results()


@router.get("/status")
async def get_discovery_status():
    """获取发现任务状态"""
    return {
        "running": _discovery_running,
        "polymarket_count": len(app_state.polymarket_events),
        "orbitexch_count": len(app_state.orbitexch_events),
    }


async def _run_discovery(venue: str | None = None):
    """执行市场发现（后台任务）"""
    global _discovery_running

    _discovery_running = True
    _log.info(f"Starting discovery for venue: {venue or 'all'}")

    _has_failure = False

    try:
        config = app_state.discovery_config_obj
        _log.debug(f"Discovery config loaded, checking Polymarket...")
        _log.debug(f"venue={venue}, venue is None={venue is None}, venue=='polymarket'={venue=='polymarket'}")

        # Polymarket 发现
        if venue is None or venue == "polymarket":
            _log.debug(f"Entered Polymarket block")
            _log.debug(f"Polymarket enabled: {config.venues.polymarket.enabled}")
            _log.debug(f"Polymarket sports: {len(config.venues.polymarket.sports)}")
            if config.venues.polymarket.enabled:
                if not config.venues.polymarket.sports:
                    _log.warning("Polymarket: No sports configured, skipping discovery")
                else:
                    try:
                        _log.info(f"Polymarket: Starting discovery for {len(config.venues.polymarket.sports)} sports")
                        await _discover_polymarket(config)
                    except Exception as e:
                        _log.error(f"Polymarket discovery failed: {e}")
                        import traceback
                        traceback.print_exc()
                        _has_failure = True
            else:
                _log.info("Polymarket discovery is disabled")

        # OrbitExch 发现
        if venue is None or venue == "orbitexch":
            if config.venues.orbitexch.enabled:
                if not config.venues.orbitexch.sports:
                    _log.warning("OrbitExch: No sports configured, skipping discovery")
                else:
                    try:
                        _log.info(f"OrbitExch: Starting discovery for {len(config.venues.orbitexch.sports)} sports")
                        await _discover_orbitexch(config)
                    except Exception as e:
                        _log.error(f"OrbitExch discovery failed: {e}")
                        import traceback
                        traceback.print_exc()
                        _has_failure = True
            else:
                _log.info("OrbitExch discovery is disabled")

        # 任一平台失败 → 不发布消息，防止部分结果污染下游级联
        if _has_failure:
            _log.warning(
                "Discovery aborted: one or more venues failed, "
                "not publishing DiscoveryCompleteMessage to protect downstream services"
            )
            if app_state.pipeline_active:
                # 区分首次 discovery 和定时刷新：
                # - 首次 (phase="discovery"): 下游服务尚未启动，重置为 idle
                # - 定时刷新 (之前已 "running"): 下游服务仍在运行，恢复为 running
                prev_phase = app_state.pipeline_phase
                if prev_phase == "discovery":
                    app_state._pipeline_phase = "idle"
                    app_state._pipeline_active = False
                    _log.warning("Pipeline reset to idle due to initial discovery failure")
                else:
                    app_state._pipeline_phase = "running"
                    _log.warning(
                        "Periodic discovery failed, pipeline restored to 'running' "
                        "(existing services unaffected)"
                    )
            return

        _log.info("Discovery task completed (all venues succeeded)")

        # 发布 Discovery 完成消息
        msgbus = app_state.get_msgbus()
        msg = DiscoveryCompleteMessage(
            polymarket_count=len(app_state.polymarket_events),
            orbitexch_count=len(app_state.orbitexch_events),
        )
        msgbus.publish(DISCOVERY_COMPLETE_TOPIC, msg)
        _log.info(f"Published DiscoveryCompleteMessage: poly={msg.polymarket_count}, orbit={msg.orbitexch_count}")

    except Exception as e:
        _log.error(f"Discovery task failed: {e}")
        import traceback
        traceback.print_exc()
        if app_state.pipeline_active:
            app_state.stop_pipeline()
    finally:
        _discovery_running = False


async def _discover_polymarket(config):
    """执行 Polymarket 发现"""
    from nautilus_trader.adapters.polymarket.scraper import (
        PolymarketScraper,
    )

    _log.info("Discovering Polymarket events...")

    scraper = PolymarketScraper(config.venues.polymarket)

    # 获取目标 sports 和 competitions
    target_sports = [s.sport for s in config.venues.polymarket.sports]
    target_competitions = []
    for s in config.venues.polymarket.sports:
        target_competitions.extend(s.competitions)

    events = await scraper.discover_events(
        target_sports=target_sports if target_sports else None,
        target_competitions=target_competitions if target_competitions else None,
    )

    # 转换为 DiscoveryResult
    results = [
        DiscoveryResult(
            venue="polymarket",
            sport=e.sport,
            competition=e.competition,
            home_team=e.home_team,
            away_team=e.away_team,
            event_id=e.event_id,
        )
        for e in events
    ]

    app_state.set_polymarket_events(results)
    _log.info(f"Discovered {len(results)} Polymarket events")


async def _discover_orbitexch(config):
    """执行 OrbitExch 发现"""
    from nautilus_trader.adapters.orbitexch.discovery_scraper import (
        OrbitExchScraper,
    )

    _log.info("Discovering OrbitExch events...")

    scraper = OrbitExchScraper(config.venues.orbitexch)

    try:
        events = await scraper.discover_events(config.venues.orbitexch.sports)

        # 转换为 DiscoveryResult（保存 sport_id, competition_id 和 selection_ids 到 extra）
        results = [
            DiscoveryResult(
                venue="orbitexch",
                sport=e.sport,
                competition=e.competition,
                home_team=e.home_team,
                away_team=e.away_team,
                event_id="",
                extra={
                    "sport_id": e.sport_id,
                    "competition_id": e.competition_id,
                    "market_id": e.market_id,
                    "home_selection_id": e.home_selection_id,
                    "draw_selection_id": e.draw_selection_id,
                    "away_selection_id": e.away_selection_id,
                },
            )
            for e in events
        ]

        app_state.set_orbitexch_events(results)
        _log.info(f"Discovered {len(results)} OrbitExch events")

    finally:
        await scraper.close_browser()


@router.post("/run")
async def run_discovery(
    request: DiscoverRequest,
    background_tasks: BackgroundTasks,
):
    """触发市场发现"""
    global _discovery_running

    if _discovery_running:
        raise HTTPException(status_code=409, detail="Discovery already running")

    background_tasks.add_task(_run_discovery, request.venue)

    return {
        "status": "started",
        "venue": request.venue or "all",
    }


@router.post("/stop")
async def stop_discovery():
    """停止市场发现"""
    global _discovery_running

    if not _discovery_running:
        return {"status": "not_running"}

    # 注意：实际停止需要更复杂的实现
    _discovery_running = False
    return {"status": "stopping"}
