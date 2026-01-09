@echo off
REM Diagnose and fix config system

echo ============================================================
echo Nautilus Trader - Config System Diagnostic
echo ============================================================
echo.

echo [Step 1] Checking required files...
echo.

set ERROR=0

if not exist "config\schemas.py" (
    echo   [ERROR] config\schemas.py NOT FOUND!
    echo   Please copy config_schemas.py to config\schemas.py
    set ERROR=1
) else (
    echo   [OK] config\schemas.py exists
)

if not exist "config\defaults.yaml" (
    echo   [ERROR] config\defaults.yaml NOT FOUND!
    echo   Please copy config.yaml to config\defaults.yaml
    set ERROR=1
) else (
    echo   [OK] config\defaults.yaml exists
)

if not exist "config\manager.py" (
    echo   [ERROR] config\manager.py NOT FOUND!
    echo   Please run deploy.bat first
    set ERROR=1
) else (
    echo   [OK] config\manager.py exists
)

echo.

if %ERROR%==1 (
    echo ============================================================
    echo [ERROR] Missing required files!
    echo ============================================================
    echo.
    echo Please run these commands:
    echo   1. copy config_schemas.py config\schemas.py
    echo   2. copy config.yaml config\defaults.yaml
    echo.
    pause
    exit /b 1
)

echo [Step 2] Testing config module...
echo.

python -c "from config.manager import config_manager; print('Config manager loaded successfully')" 2>nul
if %errorlevel% neq 0 (
    echo   [ERROR] Failed to import config_manager
    echo.
    echo   Running detailed diagnostic...
    python -c "from config.manager import config_manager"
    echo.
    pause
    exit /b 1
) else (
    echo   [OK] Config manager imports successfully
)

echo.

echo [Step 3] Testing config loading...
echo.

python -c "from config.manager import config_manager; c = config_manager.get_config(); print('Config loaded:', type(c))" 2>nul
if %errorlevel% neq 0 (
    echo   [ERROR] Failed to load config
    echo.
    echo   Running detailed diagnostic...
    python -c "from config.manager import config_manager; c = config_manager.get_config()"
    echo.
    pause
    exit /b 1
) else (
    echo   [OK] Config loads successfully
)

echo.

echo [Step 4] Updating web_panel/app.py...
echo.

if exist "app_fixed.py" (
    copy /Y app_fixed.py web_panel\app.py
    echo   [OK] web_panel/app.py updated
) else (
    echo   [WARNING] app_fixed.py not found, skipping update
)

echo.

echo ============================================================
echo [SUCCESS] All checks passed!
echo ============================================================
echo.
echo Next steps:
echo   1. Restart web panel: python run_web_panel.py
echo   2. Open browser: http://localhost:8000/config
echo   3. Test API: http://localhost:8000/api/health
echo.
pause
