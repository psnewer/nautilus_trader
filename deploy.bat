@echo off
REM ============================================================
REM Deploy Config System and Web Panel
REM For Nautilus Trader Project - Windows Version
REM ============================================================

echo.
echo ============================================================
echo Nautilus Trader - Config System Deployment
echo ============================================================
echo.

REM Check if in project root
if not exist "pyproject.toml" if not exist "setup.py" (
    echo [WARNING] Not in Nautilus Trader project root
    echo Please run this script in project root directory
    pause
    exit /b 1
)

REM 1. Create directory structure
echo [1/7] Creating directory structure...

if not exist config mkdir config
if not exist actors mkdir actors
if not exist services mkdir services
if not exist services\market_discovery mkdir services\market_discovery
if not exist services\market_matching mkdir services\market_matching
if not exist web_panel mkdir web_panel
if not exist web_panel\api mkdir web_panel\api
if not exist web_panel\static mkdir web_panel\static
if not exist web_panel\static\css mkdir web_panel\static\css
if not exist web_panel\static\js mkdir web_panel\static\js
if not exist web_panel\templates mkdir web_panel\templates
if not exist output mkdir output

REM Create __init__.py files
type nul > config\__init__.py
type nul > actors\__init__.py
type nul > services\__init__.py
type nul > services\market_discovery\__init__.py
type nul > services\market_matching\__init__.py
type nul > web_panel\__init__.py
type nul > web_panel\api\__init__.py

echo   [OK] Directory structure created
echo.

REM 2. Create config/manager.py
echo [2/7] Creating ConfigManager...

echo from pathlib import Path > config\manager.py
echo from typing import Any, Optional >> config\manager.py
echo import yaml >> config\manager.py
echo from .schemas import SystemConfig >> config\manager.py
echo. >> config\manager.py
echo. >> config\manager.py
echo class ConfigManager: >> config\manager.py
echo     _instance = None >> config\manager.py
echo     _config = None >> config\manager.py
echo. >> config\manager.py
echo     def __new__(cls): >> config\manager.py
echo         if cls._instance is None: >> config\manager.py
echo             cls._instance = super().__new__(cls) >> config\manager.py
echo         return cls._instance >> config\manager.py
echo. >> config\manager.py
echo     def __init__(self): >> config\manager.py
echo         if self._config is None: >> config\manager.py
echo             self.load_config() >> config\manager.py
echo. >> config\manager.py
echo     def load_config(self, config_file=None): >> config\manager.py
echo         if config_file is None: >> config\manager.py
echo             config_file = Path(__file__).parent / "config.yaml" >> config\manager.py
echo             if not config_file.exists(): >> config\manager.py
echo                 config_file = Path(__file__).parent / "defaults.yaml" >> config\manager.py
echo         self._config = SystemConfig.from_yaml_file(str(config_file)) >> config\manager.py
echo. >> config\manager.py
echo     def save_config(self, config_file=None): >> config\manager.py
echo         if config_file is None: >> config\manager.py
echo             config_file = Path(__file__).parent / "config.yaml" >> config\manager.py
echo         self._config.save_to_file(str(config_file)) >> config\manager.py
echo. >> config\manager.py
echo     def update_config(self, path, value): >> config\manager.py
echo         parts = path.split('.') >> config\manager.py
echo         obj = self._config >> config\manager.py
echo         for part in parts[:-1]: >> config\manager.py
echo             obj = getattr(obj, part) >> config\manager.py
echo         setattr(obj, parts[-1], value) >> config\manager.py
echo. >> config\manager.py
echo     def get_config(self, path=None): >> config\manager.py
echo         if path is None: >> config\manager.py
echo             return self._config >> config\manager.py
echo         parts = path.split('.') >> config\manager.py
echo         obj = self._config >> config\manager.py
echo         for part in parts: >> config\manager.py
echo             obj = getattr(obj, part) >> config\manager.py
echo         return obj >> config\manager.py
echo. >> config\manager.py
echo     @property >> config\manager.py
echo     def market_discovery(self): >> config\manager.py
echo         return self._config.market_discovery >> config\manager.py
echo. >> config\manager.py
echo     @property >> config\manager.py
echo     def market_matching(self): >> config\manager.py
echo         return self._config.market_matching >> config\manager.py
echo. >> config\manager.py
echo     @property >> config\manager.py
echo     def web_panel(self): >> config\manager.py
echo         return self._config.web_panel >> config\manager.py
echo. >> config\manager.py
echo. >> config\manager.py
echo config_manager = ConfigManager() >> config\manager.py

echo   [OK] ConfigManager created
echo.

REM 3. Create config/__init__.py
echo [3/7] Creating config/__init__.py...

echo from .manager import config_manager > config\__init__.py
echo from .schemas import SystemConfig >> config\__init__.py
echo. >> config\__init__.py
echo __all__ = ['config_manager', 'SystemConfig'] >> config\__init__.py

echo   [OK] config/__init__.py created
echo.

REM 4. Create web_panel/app.py
echo [4/7] Creating Web Panel app...

echo from fastapi import FastAPI, Request > web_panel\app.py
echo from fastapi.responses import HTMLResponse, JSONResponse >> web_panel\app.py
echo from fastapi.staticfiles import StaticFiles >> web_panel\app.py
echo from fastapi.templating import Jinja2Templates >> web_panel\app.py
echo from pathlib import Path >> web_panel\app.py
echo import sys >> web_panel\app.py
echo. >> web_panel\app.py
echo project_root = Path(__file__).parent.parent >> web_panel\app.py
echo sys.path.insert(0, str(project_root)) >> web_panel\app.py
echo. >> web_panel\app.py
echo from config.manager import config_manager >> web_panel\app.py
echo. >> web_panel\app.py
echo app = FastAPI(title="Nautilus Config Panel") >> web_panel\app.py
echo. >> web_panel\app.py
echo base_dir = Path(__file__).parent >> web_panel\app.py
echo app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static") >> web_panel\app.py
echo templates = Jinja2Templates(directory=str(base_dir / "templates")) >> web_panel\app.py
echo. >> web_panel\app.py
echo @app.get("/", response_class=HTMLResponse) >> web_panel\app.py
echo async def index(request: Request): >> web_panel\app.py
echo     return templates.TemplateResponse("index.html", {"request": request}) >> web_panel\app.py
echo. >> web_panel\app.py
echo @app.get("/config", response_class=HTMLResponse) >> web_panel\app.py
echo async def config_page(request: Request): >> web_panel\app.py
echo     config = config_manager.get_config() >> web_panel\app.py
echo     return templates.TemplateResponse("config.html", {"request": request, "config": config.to_dict()}) >> web_panel\app.py
echo. >> web_panel\app.py
echo @app.get("/api/config") >> web_panel\app.py
echo async def get_config(): >> web_panel\app.py
echo     config = config_manager.get_config() >> web_panel\app.py
echo     return config.to_dict() >> web_panel\app.py
echo. >> web_panel\app.py
echo @app.post("/api/config/update") >> web_panel\app.py
echo async def update_config(data: dict): >> web_panel\app.py
echo     path = data.get("path") >> web_panel\app.py
echo     value = data.get("value") >> web_panel\app.py
echo     if not path: >> web_panel\app.py
echo         return JSONResponse({"error": "path required"}, status_code=400) >> web_panel\app.py
echo     try: >> web_panel\app.py
echo         config_manager.update_config(path, value) >> web_panel\app.py
echo         config_manager.save_config() >> web_panel\app.py
echo         return {"success": True, "message": "Config updated"} >> web_panel\app.py
echo     except Exception as e: >> web_panel\app.py
echo         return JSONResponse({"error": str(e)}, status_code=500) >> web_panel\app.py
echo. >> web_panel\app.py
echo if __name__ == "__main__": >> web_panel\app.py
echo     import uvicorn >> web_panel\app.py
echo     uvicorn.run(app, host="0.0.0.0", port=8000) >> web_panel\app.py

echo   [OK] Web Panel app created
echo.

REM 5. Create simple HTML template
echo [5/7] Creating HTML templates...

echo ^<!DOCTYPE html^> > web_panel\templates\index.html
echo ^<html^>^<head^>^<title^>Nautilus Trader^</title^>^</head^> >> web_panel\templates\index.html
echo ^<body^>^<h1^>Nautilus Trader Config Panel^</h1^> >> web_panel\templates\index.html
echo ^<a href="/config"^>Go to Config^</a^>^</body^>^</html^> >> web_panel\templates\index.html

echo ^<!DOCTYPE html^> > web_panel\templates\config.html
echo ^<html^>^<head^>^<title^>Config^</title^>^</head^> >> web_panel\templates\config.html
echo ^<body^>^<h1^>Configuration^</h1^> >> web_panel\templates\config.html
echo ^<p^>Config system is ready. Use API to update config.^</p^> >> web_panel\templates\config.html
echo ^<pre^>API Endpoint: POST /api/config/update^</pre^>^</body^>^</html^> >> web_panel\templates\config.html

echo   [OK] HTML templates created
echo.

REM 6. Create run script
echo [6/7] Creating run script...

echo import uvicorn > run_web_panel.py
echo. >> run_web_panel.py
echo if __name__ == "__main__": >> run_web_panel.py
echo     uvicorn.run("web_panel.app:app", host="0.0.0.0", port=8000, reload=True) >> run_web_panel.py

echo   [OK] Run script created
echo.

REM 7. Update .gitignore
echo [7/7] Updating .gitignore...

echo. >> .gitignore
echo # Config System >> .gitignore
echo config/config.yaml >> .gitignore
echo output/ >> .gitignore

echo   [OK] .gitignore updated
echo.

REM Done
echo ============================================================
echo [SUCCESS] Deployment completed!
echo ============================================================
echo.
echo Next steps:
echo   1. Copy config files:
echo      copy config_schemas.py config\schemas.py
echo      copy config.yaml config\defaults.yaml
echo      copy event_preprocessor.py services\market_matching\
echo.
echo   2. Run web panel:
echo      python run_web_panel.py
echo.
echo   3. Open in browser:
echo      http://localhost:8000/config
echo.
pause
