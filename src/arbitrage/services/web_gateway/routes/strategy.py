"""
策略服务 API 路由

支持插拔式的 Signal 和 Strategy 配置管理。
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..state import app_state

router = APIRouter(prefix="/api/strategy", tags=["strategy"])
_log = logging.getLogger(__name__)


# =============================================================================
# 请求模型
# =============================================================================

class SignalDefinitionUpdate(BaseModel):
    """Signal 定义更新"""
    params: dict[str, Any] = {}
    description: str = ""


class StrategyDefinitionUpdate(BaseModel):
    """Strategy 定义更新"""
    signals: list[str] = []
    description: str = ""


class CompetitionConfigUpdate(BaseModel):
    """Competition 配置更新"""
    competition: str
    strategies: list[str]


class MatchConfigUpdate(BaseModel):
    """Match 配置更新"""
    competition: str
    home: str
    away: str
    strategies: list[str] = []
    signal_overrides: dict[str, dict[str, Any]] = {}


class FullConfigUpdate(BaseModel):
    """完整配置更新"""
    enabled: bool = True
    default_strategies: list[str] = ["default"]
    signals: dict[str, SignalDefinitionUpdate] = {}
    strategies: dict[str, StrategyDefinitionUpdate] = {}
    competition_config: dict[str, list[str]] = {}
    match_config: list[MatchConfigUpdate] = []


# =============================================================================
# Signal & Strategy 定义 API
# =============================================================================

@router.get("/config")
async def get_strategy_config():
    """获取完整策略配置"""
    return app_state.get_strategy_config()


@router.get("/arbitrage-config")
async def get_arbitrage_config():
    """获取套利配置"""
    return app_state.get_arbitrage_config()


@router.put("/arbitrage-config")
async def update_arbitrage_config(data: dict[str, Any]):
    """更新套利配置"""
    return app_state.update_arbitrage_config(data)


@router.put("/config")
async def update_strategy_config(body: FullConfigUpdate):
    """更新完整策略配置"""
    try:
        return app_state.update_strategy_config(body.model_dump())
    except Exception as e:
        _log.error(f"Failed to update strategy config: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/signals")
async def get_signal_definitions():
    """获取所有 Signal 定义"""
    config = app_state.strategy_config_obj
    return {
        name: sig.to_dict()
        for name, sig in config.signals.items()
    }


@router.put("/signals/{signal_name}")
async def update_signal_definition(signal_name: str, body: SignalDefinitionUpdate):
    """更新或添加 Signal 定义"""
    try:
        config = app_state.strategy_config_obj
        config.add_signal(signal_name, body.params, body.description)
        app_state.update_strategy_config(config.to_dict())
        return {"signal_name": signal_name, **body.model_dump()}
    except Exception as e:
        _log.error(f"Failed to update signal {signal_name}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/strategies")
async def get_strategy_definitions():
    """获取所有 Strategy 定义"""
    config = app_state.strategy_config_obj
    return {
        name: strat.to_dict()
        for name, strat in config.strategies.items()
    }


@router.put("/strategies/{strategy_name}")
async def update_strategy_definition(strategy_name: str, body: StrategyDefinitionUpdate):
    """更新或添加 Strategy 定义"""
    try:
        config = app_state.strategy_config_obj
        config.add_strategy(strategy_name, body.signals, body.description)
        app_state.update_strategy_config(config.to_dict())
        return {"strategy_name": strategy_name, **body.model_dump()}
    except Exception as e:
        _log.error(f"Failed to update strategy {strategy_name}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# Competition Config API
# =============================================================================

@router.get("/competition-config")
async def get_competition_config():
    """获取所有 Competition 配置"""
    config = app_state.strategy_config_obj
    return {
        "default_strategies": config.default_strategies,
        "competition_config": config.competition_config,
    }


@router.put("/competition-config/{competition}")
async def set_competition_strategies(competition: str, body: CompetitionConfigUpdate):
    """设置 Competition 策略配置"""
    try:
        config = app_state.strategy_config_obj
        config.set_competition_strategies(competition, body.strategies)
        app_state.update_strategy_config(config.to_dict())
        return {"competition": competition, "strategies": body.strategies}
    except Exception as e:
        _log.error(f"Failed to set competition config: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/competition-config/{competition}")
async def remove_competition_config(competition: str):
    """移除 Competition 配置（将使用默认策略）"""
    config = app_state.strategy_config_obj
    if competition in config.competition_config:
        del config.competition_config[competition]
        app_state.update_strategy_config(config.to_dict())
        return {"message": f"Removed config for {competition}"}
    raise HTTPException(status_code=404, detail=f"No config found for {competition}")


# =============================================================================
# Match Config API
# =============================================================================

@router.get("/match-config")
async def get_match_config():
    """获取所有 Match 配置"""
    config = app_state.strategy_config_obj
    return [m.to_dict() for m in config.match_config]


@router.post("/match-config")
async def add_match_config(body: MatchConfigUpdate):
    """添加或更新 Match 配置"""
    try:
        from src.arbitrage.services.strategy.config import MatchConfig

        config = app_state.strategy_config_obj
        match_cfg = MatchConfig(
            competition=body.competition,
            home=body.home,
            away=body.away,
            strategies=body.strategies,
            signal_overrides=body.signal_overrides,
        )
        config.add_match_config(match_cfg)
        app_state.update_strategy_config(config.to_dict())
        return match_cfg.to_dict()
    except Exception as e:
        _log.error(f"Failed to add match config: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/match-config")
async def remove_match_config(competition: str, home: str, away: str):
    """移除 Match 配置"""
    config = app_state.strategy_config_obj
    removed = config.remove_match_config(competition, home, away)
    if removed:
        app_state.update_strategy_config(config.to_dict())
        return {"message": f"Removed match config for {home} vs {away}"}
    raise HTTPException(status_code=404, detail="Match config not found")


# =============================================================================
# 状态 API
# =============================================================================

@router.get("/status")
async def get_strategy_status():
    """获取策略服务状态"""
    try:
        service = app_state.get_strategy_service()
        config = app_state.strategy_config_obj

        return {
            "enabled": config.enabled,
            "default_strategies": config.default_strategies,
            "signals_count": len(config.signals),
            "strategies_count": len(config.strategies),
            "competition_config_count": len(config.competition_config),
            "match_config_count": len(config.match_config),
            "cached_matches": len(service._match_contexts),
            "cached_signals": len(service._signal_results),
            "opportunities_count": len(service._opportunities),
        }
    except Exception as e:
        _log.error(f"Failed to get strategy status: {e}")
        return {"enabled": False, "error": str(e)}


@router.get("/results")
async def get_all_results():
    """获取所有信号和策略结果"""
    try:
        service = app_state.get_strategy_service()
        return {
            "signals": service.get_signals(),
            "strategies": service.get_strategy_results(),
        }
    except Exception as e:
        _log.error(f"Failed to get results: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/results/{pair_id}")
async def get_pair_results(pair_id: str):
    """获取指定比赛的信号和策略结果"""
    try:
        service = app_state.get_strategy_service()
        return {
            "pair_id": pair_id,
            "signals": service.get_signals(pair_id),
            "strategies": service.get_strategy_results(pair_id),
        }
    except Exception as e:
        _log.error(f"Failed to get results for {pair_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/opportunities")
async def get_opportunities(limit: int = 50, status: str | None = None):
    """获取检测到的套利机会"""
    try:
        service = app_state.get_strategy_service()
        opportunities = service.get_opportunities(limit=limit, status=status)
        return {"opportunities": opportunities, "total": len(opportunities)}
    except Exception as e:
        _log.error(f"Failed to get opportunities: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/arbitrage-directions")
async def get_all_arbitrage_directions():
    """获取所有比赛的套利方向"""
    try:
        service = app_state.get_strategy_service()
        return service.get_arbitrage_directions()
    except Exception as e:
        _log.error(f"Failed to get arbitrage directions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/arbitrage-directions/{pair_id}")
async def get_pair_arbitrage_directions(pair_id: str):
    """获取指定比赛的套利方向"""
    try:
        service = app_state.get_strategy_service()
        return service.get_arbitrage_directions(pair_id)
    except Exception as e:
        _log.error(f"Failed to get arbitrage directions for {pair_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# 调试 API
# =============================================================================

@router.post("/debug/clear-cache")
async def clear_cache():
    """清除策略服务缓存"""
    try:
        service = app_state.get_strategy_service()
        service.clear_cache()
        return {"message": "Cache cleared"}
    except Exception as e:
        _log.error(f"Failed to clear cache: {e}")
        raise HTTPException(status_code=500, detail=str(e))
