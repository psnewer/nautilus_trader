"""
配置管理 API 路由
"""

from typing import Any

from fastapi import APIRouter

from ..state import app_state

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
async def get_all_config():
    """获取所有配置"""
    return {
        "discovery": app_state.get_discovery_config(),
        "matching": app_state.get_matching_config(),
    }


@router.get("/discovery")
async def get_discovery_config():
    """获取市场发现配置"""
    return app_state.get_discovery_config()


@router.put("/discovery")
async def update_discovery_config(data: dict[str, Any]):
    """更新市场发现配置"""
    return app_state.update_discovery_config(data)


@router.get("/matching")
async def get_matching_config():
    """获取市场匹配配置"""
    return app_state.get_matching_config()


@router.put("/matching")
async def update_matching_config(data: dict[str, Any]):
    """更新市场匹配配置"""
    return app_state.update_matching_config(data)
