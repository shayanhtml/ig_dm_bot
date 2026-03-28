@echo off
TITLE Instagram Model DM Bot
echo ============================================================
echo         Instagram Model DM Bot - Startup Script
echo ============================================================
echo.

:: Ensure we are in the correct directory
cd /d "%~dp0"

:: Check if python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in your system PATH.
    echo Please install Python 3.9+ from python.org
    pause
    exit /b 1
)

:: Start the server
echo [INFO] Starting the bot server...
echo.
python server.py

:: Pause if the server crashes so the user can see the error
pause
