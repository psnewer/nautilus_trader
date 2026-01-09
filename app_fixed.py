"""Web Panel - FastAPI Application"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import sys
import traceback

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    from config.manager import config_manager
except ImportError as e:
    print(f"Error importing config_manager: {e}")
    print("Make sure config/schemas.py exists and is valid")
    config_manager = None

app = FastAPI(title="Nautilus Config Panel")

base_dir = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")
templates = Jinja2Templates(directory=str(base_dir / "templates"))


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
                content="<h1>Config Error</h1><p>Config manager not initialized. "
                       "Please check config/schemas.py exists.</p>",
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
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        print(error_msg)
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
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        print(error_msg)
        return JSONResponse(
            {"error": str(e), "traceback": traceback.format_exc()},
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
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        print(error_msg)
        return JSONResponse(
            {"error": str(e), "traceback": traceback.format_exc()},
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
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        print(error_msg)
        return JSONResponse(
            {"error": str(e), "traceback": traceback.format_exc()},
            status_code=500
        )


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
