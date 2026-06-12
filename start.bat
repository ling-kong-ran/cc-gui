@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [CC Bridge] Checking Python...
python --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.10+
    pause
    exit /b 1
)

echo [CC Bridge] Starting server (port 17878, increment if occupied)...
echo [CC Bridge] Press Ctrl+C to stop
echo.
python -u server.py
pause
