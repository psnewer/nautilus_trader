"""
Debug API 路由

提供 debug 模式配置、覆盖管理和测试数据管理接口。
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.arbitrage.services.debug import debug_manager, MockCategory

router = APIRouter(prefix="/api/debug", tags=["debug"])
_log = logging.getLogger(__name__)


# =============================================================================
# 请求模型
# =============================================================================

class SetOverrideRequest(BaseModel):
    """设置覆盖请求"""
    name: str
    enabled: bool = True
    value: Any = None


class AddMockDataRequest(BaseModel):
    """添加模拟数据请求"""
    id: str
    category: str
    name: str
    data: Any
    enabled: bool = True
    conditions: dict = {}
    priority: int = 0


class LoadConfigRequest(BaseModel):
    """加载配置请求"""
    path: str


# =============================================================================
# 状态和开关
# =============================================================================

@router.get("/status")
async def get_debug_status():
    """获取 debug 模式状态"""
    return debug_manager.get_status()


@router.post("/enable")
async def enable_debug():
    """启用 debug 模式"""
    debug_manager.enable()
    return {
        "success": True,
        "message": "Debug mode enabled",
        "status": debug_manager.get_status(),
    }


@router.post("/disable")
async def disable_debug():
    """禁用 debug 模式"""
    debug_manager.disable()
    return {
        "success": True,
        "message": "Debug mode disabled",
        "status": debug_manager.get_status(),
    }


# =============================================================================
# 覆盖管理
# =============================================================================

@router.get("/overrides")
async def list_overrides():
    """列出所有覆盖配置"""
    return {
        "overrides": debug_manager.config.to_dict()["overrides"],
    }


@router.post("/overrides")
async def set_override(request: SetOverrideRequest):
    """设置覆盖"""
    success = debug_manager.set_override(
        request.name,
        enabled=request.enabled,
        value=request.value,
    )
    if not success:
        raise HTTPException(status_code=400, detail=f"Unknown override: {request.name}")

    return {
        "success": True,
        "status": debug_manager.get_status(),
    }


# =============================================================================
# Mock 数据管理
# =============================================================================

@router.get("/mock-data")
async def list_mock_data():
    """列出所有模拟数据"""
    return {
        "mock_data": debug_manager.config.to_dict()["mock_data"],
    }


@router.post("/mock-data")
async def add_mock_data(request: AddMockDataRequest):
    """添加模拟数据"""
    try:
        category = MockCategory(request.category)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid category: {request.category}")

    debug_manager.add_mock(
        item_id=request.id,
        category=category,
        name=request.name,
        data=request.data,
        enabled=request.enabled,
        conditions=request.conditions,
    )

    return {
        "success": True,
        "status": debug_manager.get_status(),
    }


@router.get("/categories")
async def list_categories():
    """列出所有数据分类"""
    return {
        "categories": [cat.value for cat in MockCategory]
    }


# =============================================================================
# 配置文件
# =============================================================================

@router.post("/config/load")
async def load_config(request: LoadConfigRequest):
    """从文件加载配置"""
    success = debug_manager.load(request.path)
    if not success:
        raise HTTPException(status_code=400, detail=f"Failed to load config: {request.path}")

    return {
        "success": True,
        "path": request.path,
        "status": debug_manager.get_status(),
    }


@router.post("/config/save")
async def save_config(path: str = None):
    """保存配置到文件"""
    success = debug_manager.save(path)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to save config")

    return {
        "success": True,
        "path": path or debug_manager.config.config_file,
    }


@router.get("/config/export")
async def export_config():
    """导出配置"""
    return debug_manager.config.to_dict()
