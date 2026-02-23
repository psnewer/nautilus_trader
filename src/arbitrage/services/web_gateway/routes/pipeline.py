"""
管道控制 API 路由

Start/Stop 全流程管道（Discovery → Matching → OddsSubscription）
"""

import asyncio
import logging
import os
import signal

from fastapi import APIRouter, HTTPException

from ..state import app_state

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])
_log = logging.getLogger(__name__)


@router.post("/start")
async def start_pipeline():
    """启动全流程管道（手动触发）"""
    if app_state.pipeline_active:
        raise HTTPException(status_code=409, detail="Pipeline already running")

    # 检查是否有手动任务在运行
    from .discovery import _discovery_running
    from .matching import _matching_running
    if _discovery_running:
        raise HTTPException(status_code=409, detail="Discovery is already running")
    if _matching_running:
        raise HTTPException(status_code=409, detail="Matching is already running")

    app_state.start_pipeline()

    # 后台触发 Discovery
    from .discovery import _run_discovery
    asyncio.create_task(_run_discovery())

    return {"status": "started", "phase": app_state.pipeline_phase}


@router.post("/stop")
async def stop_pipeline():
    """停止管道并关闭程序"""
    app_state.stop_scheduler()
    app_state.stop_pipeline()

    # 延迟 0.5s 后发送 SIGTERM 关闭 uvicorn
    loop = asyncio.get_event_loop()
    loop.call_later(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM))

    return {"status": "stopping"}


@router.get("/status")
async def get_pipeline_status():
    """获取管道状态"""
    return {
        "active": app_state.pipeline_active,
        "phase": app_state.pipeline_phase,
    }
