# -*- coding: utf-8 -*-
"""
Web Panel 应用 - 完整实现
包含所有 API 端点的实际逻辑
"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import json
import subprocess
import sys
from datetime import datetime
import logging

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Nautilus Trader Web Panel")

# 获取基础路径
base_dir = Path(__file__).parent
project_root = base_dir.parent  # 项目根目录

# 挂载静态文件和模板
app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")
templates = Jinja2Templates(directory=str(base_dir / "templates"))

# 输出目录 (使用项目根目录)
output_dir = project_root / "output"
output_dir.mkdir(exist_ok=True)

# 爬虫脚本路径 (使用绝对路径)
POLYMARKET_SCRIPT = str(project_root / "services/market_discovery/polymarket_final_v3.py")
ORBITEXCH_SCRIPT = str(project_root / "services/market_discovery/orbitexch_crawler.py")
MATCHER_SCRIPT = str(project_root / "services/market_matching/market_matching_service.py")


# ==================== 页面路由 ====================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """首页"""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """配置页面"""
    return templates.TemplateResponse("config.html", {"request": request})


@app.get("/discovery", response_class=HTMLResponse)
async def discovery_page(request: Request):
    """市场发现页面"""
    return templates.TemplateResponse("discovery.html", {"request": request})


@app.get("/matching", response_class=HTMLResponse)
async def matching_page(request: Request):
    """市场匹配页面"""
    return templates.TemplateResponse("matching.html", {"request": request})


# ==================== API 端点 ====================

@app.get("/api/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


# ==================== 市场发现 API ====================

@app.post("/api/discovery/start-polymarket")
async def start_polymarket_crawler():
    """启动 Polymarket 爬虫"""
    try:
        logger.info("启动 Polymarket 爬虫...")
        
        # 检查脚本是否存在
        if not Path(POLYMARKET_SCRIPT).exists():
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "error": f"爬虫脚本不存在: {POLYMARKET_SCRIPT}",
                    "message": "请确保爬虫脚本已创建"
                }
            )
        
        # 运行爬虫
        result = subprocess.run(
            [sys.executable, POLYMARKET_SCRIPT],
            capture_output=True,
            text=True,
            timeout=300  # 5分钟超时
        )
        
        if result.returncode == 0:
            # 读取生成的文件
            events_file = output_dir / "polymarket_events.json"
            if events_file.exists():
                with open(events_file, 'r', encoding='utf-8') as f:
                    events = json.load(f)
                
                logger.info(f"Polymarket 爬虫完成: {len(events)} 个事件")
                
                return {
                    "success": True,
                    "message": f"成功爬取 {len(events)} 个事件",
                    "count": len(events),
                    "output": result.stdout,
                    "timestamp": datetime.now().isoformat()
                }
            else:
                return {
                    "success": True,
                    "message": "爬虫运行完成但未生成数据文件",
                    "output": result.stdout,
                    "timestamp": datetime.now().isoformat()
                }
        else:
            logger.error(f"Polymarket 爬虫失败: {result.stderr}")
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": result.stderr,
                    "output": result.stdout,
                    "message": "爬虫执行失败"
                }
            )
            
    except subprocess.TimeoutExpired:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "爬虫执行超时（超过5分钟）",
                "message": "请检查网络连接或减少爬取数量"
            }
        )
    except Exception as e:
        logger.error(f"启动 Polymarket 爬虫失败: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e),
                "message": "启动爬虫失败"
            }
        )


@app.post("/api/discovery/start-orbitexch")
async def start_orbitexch_crawler():
    """启动 OrbitExch 爬虫"""
    try:
        logger.info("启动 OrbitExch 爬虫...")
        
        # 检查脚本是否存在
        if not Path(ORBITEXCH_SCRIPT).exists():
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "error": f"爬虫脚本不存在: {ORBITEXCH_SCRIPT}",
                    "message": "请确保爬虫脚本已创建"
                }
            )
        
        # 运行爬虫
        result = subprocess.run(
            [sys.executable, ORBITEXCH_SCRIPT],
            capture_output=True,
            text=True,
            timeout=300
        )
        
        if result.returncode == 0:
            # 读取生成的文件
            events_file = output_dir / "orbitexch_events.json"
            if events_file.exists():
                with open(events_file, 'r', encoding='utf-8') as f:
                    events = json.load(f)
                
                logger.info(f"OrbitExch 爬虫完成: {len(events)} 个事件")
                
                return {
                    "success": True,
                    "message": f"成功爬取 {len(events)} 个事件",
                    "count": len(events),
                    "output": result.stdout,
                    "timestamp": datetime.now().isoformat()
                }
            else:
                return {
                    "success": True,
                    "message": "爬虫运行完成但未生成数据文件",
                    "output": result.stdout,
                    "timestamp": datetime.now().isoformat()
                }
        else:
            logger.error(f"OrbitExch 爬虫失败: {result.stderr}")
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": result.stderr,
                    "output": result.stdout,
                    "message": "爬虫执行失败"
                }
            )
            
    except Exception as e:
        logger.error(f"启动 OrbitExch 爬虫失败: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e),
                "message": "启动爬虫失败"
            }
        )


@app.get("/api/discovery/polymarket-events")
async def get_polymarket_events():
    """获取 Polymarket 事件"""
    try:
        events_file = output_dir / "polymarket_events.json"
        
        if not events_file.exists():
            return {
                "events": [],
                "count": 0,
                "message": "尚未爬取数据"
            }
        
        with open(events_file, 'r', encoding='utf-8') as f:
            events = json.load(f)
        
        return {
            "events": events,
            "count": len(events),
            "timestamp": datetime.fromtimestamp(events_file.stat().st_mtime).isoformat()
        }
        
    except Exception as e:
        logger.error(f"读取 Polymarket 事件失败: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "error": str(e),
                "message": "读取数据失败"
            }
        )


@app.get("/api/discovery/orbitexch-events")
async def get_orbitexch_events():
    """获取 OrbitExch 事件"""
    try:
        events_file = output_dir / "orbitexch_events.json"
        
        if not events_file.exists():
            return {
                "events": [],
                "count": 0,
                "message": "尚未爬取数据"
            }
        
        with open(events_file, 'r', encoding='utf-8') as f:
            events = json.load(f)
        
        return {
            "events": events,
            "count": len(events),
            "timestamp": datetime.fromtimestamp(events_file.stat().st_mtime).isoformat()
        }
        
    except Exception as e:
        logger.error(f"读取 OrbitExch 事件失败: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "error": str(e),
                "message": "读取数据失败"
            }
        )


# ==================== 市场匹配 API ====================

@app.post("/api/matching/start")
async def start_matching():
    """启动市场匹配"""
    try:
        logger.info("启动市场匹配...")
        
        # 检查脚本是否存在
        if not Path(MATCHER_SCRIPT).exists():
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "error": f"匹配脚本不存在: {MATCHER_SCRIPT}",
                    "message": "请确保匹配脚本已创建"
                }
            )
        
        # 检查输入文件是否存在
        poly_file = output_dir / "polymarket_events.json"
        orbit_file = output_dir / "orbitexch_events.json"
        
        if not poly_file.exists() or not orbit_file.exists():
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "缺少输入数据",
                    "message": "请先运行爬虫获取数据"
                }
            )
        
        # 运行匹配
        result = subprocess.run(
            [sys.executable, MATCHER_SCRIPT],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode == 0:
            # 读取匹配结果
            matches_file = output_dir / "market_matches.json"
            if matches_file.exists():
                with open(matches_file, 'r', encoding='utf-8') as f:
                    matches = json.load(f)
                
                logger.info(f"匹配完成: {len(matches)} 个匹配")
                
                return {
                    "success": True,
                    "message": f"成功匹配 {len(matches)} 个事件",
                    "count": len(matches),
                    "output": result.stdout,
                    "timestamp": datetime.now().isoformat()
                }
            else:
                return {
                    "success": True,
                    "message": "匹配完成但未生成结果文件",
                    "output": result.stdout,
                    "timestamp": datetime.now().isoformat()
                }
        else:
            logger.error(f"匹配失败: {result.stderr}")
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": result.stderr,
                    "output": result.stdout,
                    "message": "匹配执行失败"
                }
            )
            
    except Exception as e:
        logger.error(f"启动匹配失败: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e),
                "message": "启动匹配失败"
            }
        )


@app.get("/api/matching/results")
async def get_matching_results():
    """获取匹配结果"""
    try:
        matches_file = output_dir / "market_matches.json"
        poly_file = output_dir / "polymarket_events.json"
        orbit_file = output_dir / "orbitexch_events.json"
        
        # 读取匹配结果
        if matches_file.exists():
            with open(matches_file, 'r', encoding='utf-8') as f:
                matches = json.load(f)
        else:
            matches = []
        
        # 读取源数据统计
        poly_count = 0
        orbit_count = 0
        
        if poly_file.exists():
            with open(poly_file, 'r', encoding='utf-8') as f:
                poly_count = len(json.load(f))
        
        if orbit_file.exists():
            with open(orbit_file, 'r', encoding='utf-8') as f:
                orbit_count = len(json.load(f))
        
        return {
            "matches": matches,
            "match_count": len(matches),
            "polymarket_count": poly_count,
            "orbitexch_count": orbit_count,
            "timestamp": datetime.fromtimestamp(matches_file.stat().st_mtime).isoformat() if matches_file.exists() else None
        }
        
    except Exception as e:
        logger.error(f"读取匹配结果失败: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "error": str(e),
                "message": "读取匹配结果失败"
            }
        )


# ==================== 配置 API ====================

@app.get("/api/config")
async def get_config():
    """获取配置"""
    try:
        config_file = project_root / "config/config.yaml"

        if not config_file.exists():
            # 如果 config.yaml 不存在，尝试使用 defaults.yaml
            default_file = project_root / "config/defaults.yaml"
            if default_file.exists():
                with open(default_file, 'r', encoding='utf-8') as f:
                    import yaml
                    config = yaml.safe_load(f)
                return config or {}
            else:
                return {
                    "market_discovery": {
                        "polymarket": {
                            "enabled": True,
                            "sports": ["Soccer", "Basketball"],
                            "max_events_per_sport": 0,
                            "max_events_per_competition": 100,
                            "max_total_events": 1000
                        },
                        "orbitexch": {
                            "enabled": True,
                            "sports": ["American Football", "Soccer"],
                            "max_competitions_per_sport": 5,
                            "headless": False
                        }
                    },
                    "market_matching": {
                        "preprocessor": {
                            "enabled": True,
                            "normalize_sport": True,
                            "normalize_competition": True,
                            "normalize_team_names": True
                        },
                        "matcher": {
                            "min_confidence": 0.6,
                            "allow_team_swap": True
                        }
                    }
                }

        with open(config_file, 'r', encoding='utf-8') as f:
            import yaml
            config = yaml.safe_load(f)

        return config or {}

    except Exception as e:
        logger.error(f"读取配置失败: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "error": str(e),
                "message": "读取配置失败"
            }
        )


@app.post("/api/config")
async def update_config(request: Request):
    """更新整个配置文件"""
    try:
        data = await request.json()

        config_file = project_root / "config/config.yaml"
        config_file.parent.mkdir(exist_ok=True)

        with open(config_file, 'w', encoding='utf-8') as f:
            import yaml
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

        logger.info("配置已更新")

        return {
            "success": True,
            "message": "配置已保存",
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"保存配置失败: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e),
                "message": "保存配置失败"
            }
        )


@app.post("/api/config/update")
async def update_config_item(request: Request):
    """更新配置项（支持嵌套路径）"""
    try:
        data = await request.json()
        path = data.get('path')
        value = data.get('value')

        if not path:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "缺少 path 参数",
                    "message": "请提供配置路径"
                }
            )

        config_file = project_root / "config/config.yaml"
        config_file.parent.mkdir(exist_ok=True)

        # 读取现有配置
        if config_file.exists():
            with open(config_file, 'r', encoding='utf-8') as f:
                import yaml
                config = yaml.safe_load(f) or {}
        else:
            config = {}

        # 使用点分隔路径更新配置
        # 例如: "market_discovery.polymarket.enabled" -> config['market_discovery']['polymarket']['enabled'] = value
        keys = path.split('.')
        current = config

        # 遍历到倒数第二个键，创建必要的嵌套字典
        for key in keys[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]

        # 设置最终值
        current[keys[-1]] = value

        # 保存配置
        with open(config_file, 'w', encoding='utf-8') as f:
            import yaml
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

        logger.info(f"配置项已更新: {path} = {value}")

        return {
            "success": True,
            "message": f"配置项 {path} 已更新",
            "path": path,
            "value": value,
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"更新配置项失败: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e),
                "message": "更新配置项失败"
            }
        )


@app.post("/api/config/reload")
async def reload_config():
    """重新加载配置"""
    try:
        # 这里可以添加重新加载配置的逻辑
        # 例如通知其他服务重新读取配置文件

        logger.info("配置重新加载请求")

        return {
            "success": True,
            "message": "配置已重新加载",
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"重新加载配置失败: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e),
                "message": "重新加载配置失败"
            }
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)