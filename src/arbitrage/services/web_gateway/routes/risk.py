"""
风控服务 API 路由
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..state import app_state


router = APIRouter(prefix="/api/risk", tags=["risk"])


class RiskConfigUpdate(BaseModel):
    """风控配置更新"""
    enabled: bool = True
    execution_enabled: bool = True
    match_sl: float = -0.10
    global_sl: float = -0.50
    match_tp: float = 0.10
    match_overrides: dict[str, float] = {}
    health_check_interval_sec: float = 120.0


class MatchOverride(BaseModel):
    """单场止损覆盖"""
    sl: float


@router.get("/status")
async def get_risk_status():
    """获取风控状态"""
    risk_service = app_state.get_risk_service()
    return risk_service.get_global_status()


@router.get("/config")
async def get_risk_config():
    """获取风控配置"""
    risk_service = app_state.get_risk_service()
    return risk_service.config.to_dict()


@router.put("/config")
async def update_risk_config(config: RiskConfigUpdate):
    """更新风控配置"""
    result = app_state.update_risk_config(config.model_dump())
    return {"status": "ok", "config": result}


@router.get("/positions")
async def get_positions():
    """获取所有持仓"""
    risk_service = app_state.get_risk_service()
    return risk_service.get_positions_summary()


@router.get("/positions/{pair_id}")
async def get_position(pair_id: str):
    """获取指定比赛的持仓"""
    risk_service = app_state.get_risk_service()
    position = risk_service.get_position(pair_id)

    if not position:
        raise HTTPException(status_code=404, detail="Position not found")

    return position.to_dict()


@router.get("/way-rebate")
async def get_all_way_rebates():
    """获取所有比赛的持仓返水率"""
    risk_service = app_state.get_risk_service()
    return risk_service.get_all_way_rebates()


@router.get("/way-rebate/{pair_id}")
async def get_way_rebate(pair_id: str):
    """获取指定比赛的持仓返水率"""
    risk_service = app_state.get_risk_service()
    way_rebate = risk_service.get_way_rebate(pair_id)
    return {"pair_id": pair_id, "way_rebate": way_rebate}


@router.get("/check/{pair_id}")
async def check_risk(pair_id: str):
    """检查指定比赛是否允许下注"""
    risk_service = app_state.get_risk_service()
    result = risk_service.check_risk(pair_id)
    return result.to_dict()


@router.post("/close-match/{pair_id}")
async def close_match(pair_id: str):
    """标记比赛已结束"""
    risk_service = app_state.get_risk_service()
    risk_service.close_match(pair_id)
    return {"status": "ok", "pair_id": pair_id, "closed": True}


@router.post("/clear")
async def clear_positions():
    """清空所有持仓（谨慎使用）"""
    risk_service = app_state.get_risk_service()
    risk_service.clear_positions()
    return {"status": "ok", "message": "All positions cleared"}


@router.put("/match-override/{pair_id}")
async def set_match_override(pair_id: str, override: MatchOverride):
    """设置单场比赛的止损覆盖"""
    risk_service = app_state.get_risk_service()
    config = risk_service.config
    config.match_overrides[pair_id] = override.sl
    app_state._save_config()

    return {"status": "ok", "pair_id": pair_id, "sl": override.sl}


@router.delete("/match-override/{pair_id}")
async def delete_match_override(pair_id: str):
    """删除单场比赛的止损覆盖"""
    risk_service = app_state.get_risk_service()
    config = risk_service.config

    if pair_id in config.match_overrides:
        del config.match_overrides[pair_id]
        app_state._save_config()

    return {"status": "ok", "pair_id": pair_id}
