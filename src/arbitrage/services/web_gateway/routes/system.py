"""
系统配置 API 路由
"""

from typing import Any

from fastapi import APIRouter

from ..state import app_state

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/config")
async def get_system_config():
    """获取系统配置"""
    return app_state.get_system_config()


@router.put("/config")
async def update_system_config(data: dict[str, Any]):
    """更新系统配置"""
    return app_state.update_system_config(data)
