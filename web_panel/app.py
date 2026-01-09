"""Web Panel - FastAPI Application with Discovery and Matching pages"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import sys
import traceback
import json

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    from config.manager import config_manager
except ImportError as e:
    print(f"Error importing config_manager: {e}")
    config_manager = None

app = FastAPI(title="Nautilus Config Panel")

base_dir = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")
templates = Jinja2Templates(directory=str(base_dir / "templates"))


# ============================================================
# Page Routes
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Home page"""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """Config page"""
    try:
        if config_manager is None:
            return HTMLResponse(
                content="<h1>Config Error</h1><p>Config manager not initialized.</p>",
                status_code=500
            )
        
        config = config_manager.get_config()
        config_dict = config.to_dict()
        
        return templates.TemplateResponse("config.html", {
            "request": request,
            "config": config_dict
        })
    except Exception as e:
        error_msg = f"Error loading config: {str(e)}\n{traceback.format_exc()}"
        print(error_msg)
        return HTMLResponse(
            content=f"<h1>Config Error</h1><pre>{error_msg}</pre>",
            status_code=500
        )


@app.get("/discovery", response_class=HTMLResponse)
async def discovery_page(request: Request):
    """Market Discovery page"""
    return templates.TemplateResponse("discovery.html", {"request": request})


@app.get("/matching", response_class=HTMLResponse)
async def matching_page(request: Request):
    """Market Matching page"""
    return templates.TemplateResponse("matching.html", {"request": request})


# ============================================================
# Config API
# ============================================================

@app.get("/api/config")
async def get_config():
    """Get complete config as JSON"""
    try:
        if config_manager is None:
            return JSONResponse(
                {"error": "Config manager not initialized"},
                status_code=500
            )
        
        config = config_manager.get_config()
        return JSONResponse(config.to_dict())
    except Exception as e:
        return JSONResponse(
            {"error": str(e), "traceback": traceback.format_exc()},
            status_code=500
        )


@app.post("/api/config/update")
async def update_config(data: dict):
    """Update config item"""
    try:
        if config_manager is None:
            return JSONResponse(
                {"error": "Config manager not initialized"},
                status_code=500
            )
        
        path = data.get("path")
        value = data.get("value")
        
        if not path:
            return JSONResponse(
                {"error": "path is required"},
                status_code=400
            )
        
        config_manager.update_config(path, value)
        config_manager.save_config()
        
        return JSONResponse({
            "success": True,
            "message": f"Config updated: {path}"
        })
    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=500
        )


@app.post("/api/config/save")
async def save_config():
    """Save config to file"""
    try:
        if config_manager is None:
            return JSONResponse(
                {"error": "Config manager not initialized"},
                status_code=500
            )
        
        config_manager.save_config()
        return JSONResponse({
            "success": True,
            "message": "Config saved"
        })
    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=500
        )


@app.post("/api/config/reload")
async def reload_config():
    """Reload config from file"""
    try:
        if config_manager is None:
            return JSONResponse(
                {"error": "Config manager not initialized"},
                status_code=500
            )
        
        config_manager.load_config()
        return JSONResponse({
            "success": True,
            "message": "Config reloaded"
        })
    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=500
        )


# ============================================================
# Discovery API
# ============================================================

@app.post("/api/discovery/start-polymarket")
async def start_polymarket():
    """Start Polymarket crawler"""
    # TODO: Implement crawler start logic
    return JSONResponse({
        "success": True,
        "message": "Polymarket crawler started (simulation)"
    })


@app.post("/api/discovery/start-orbitexch")
async def start_orbitexch():
    """Start OrbitExch crawler"""
    # TODO: Implement crawler start logic
    return JSONResponse({
        "success": True,
        "message": "OrbitExch crawler started (simulation)"
    })


@app.get("/api/discovery/polymarket-events")
async def get_polymarket_events():
    """Get Polymarket events"""
    try:
        output_dir = project_root / "output"
        polymarket_file = output_dir / "polymarket_events.json"
        
        if polymarket_file.exists():
            with open(polymarket_file, 'r', encoding='utf-8') as f:
                events = json.load(f)
            return JSONResponse(events)
        else:
            return JSONResponse([])
    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=500
        )


@app.get("/api/discovery/orbitexch-events")
async def get_orbitexch_events():
    """Get OrbitExch events"""
    try:
        output_dir = project_root / "output"
        orbitexch_file = output_dir / "orbitexch_events.json"
        
        if orbitexch_file.exists():
            with open(orbitexch_file, 'r', encoding='utf-8') as f:
                events = json.load(f)
            return JSONResponse(events)
        else:
            return JSONResponse([])
    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=500
        )


# ============================================================
# Matching API
# ============================================================

@app.post("/api/matching/start")
async def start_matching():
    """Start market matching"""
    # TODO: Implement matching logic
    return JSONResponse({
        "success": True,
        "match_count": 0,
        "message": "Matching started (simulation)"
    })


@app.get("/api/matching/results")
async def get_matching_results():
    """Get matching results"""
    try:
        output_dir = project_root / "output"
        matches_file = output_dir / "market_matches.json"
        
        # Load match results
        matches = []
        if matches_file.exists():
            with open(matches_file, 'r', encoding='utf-8') as f:
                matches = json.load(f)
        
        # Load event counts
        poly_file = output_dir / "polymarket_events.json"
        orbit_file = output_dir / "orbitexch_events.json"
        
        poly_count = 0
        orbit_count = 0
        
        if poly_file.exists():
            with open(poly_file, 'r', encoding='utf-8') as f:
                poly_count = len(json.load(f))
        
        if orbit_file.exists():
            with open(orbit_file, 'r', encoding='utf-8') as f:
                orbit_count = len(json.load(f))
        
        return JSONResponse({
            "matches": matches,
            "match_count": len(matches),
            "polymarket_count": poly_count,
            "orbitexch_count": orbit_count
        })
    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=500
        )


# ============================================================
# Health Check
# ============================================================

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return JSONResponse({
        "status": "ok",
        "config_manager_loaded": config_manager is not None
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
