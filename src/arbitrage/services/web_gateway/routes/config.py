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
        "odds": app_state.get_odds_config(),
        "arbitrage": app_state.get_arbitrage_config(),
        "execution": app_state.get_execution_config(),
        "system": app_state.get_system_config(),
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


@router.get("/odds")
async def get_odds_config():
    """获取赔率订阅配置"""
    return app_state.get_odds_config()


@router.put("/odds")
async def update_odds_config(data: dict[str, Any]):
    """更新赔率订阅配置"""
    return app_state.update_odds_config(data)


@router.get("/arbitrage")
async def get_arbitrage_config():
    """获取套利配置"""
    return app_state.get_arbitrage_config()


@router.put("/arbitrage")
async def update_arbitrage_config(data: dict[str, Any]):
    """更新套利配置"""
    return app_state.update_arbitrage_config(data)
