@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [CCB GUI] Checking Python...
python --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.10+
    pause
    exit /b 1
)

echo [CCB GUI] Starting server (random port)...
echo [CCB GUI] Press Ctrl+C to stop
echo.
python -u server.py
pause
