@echo off
title TTFM Auto-Trade Bot
echo ============================================
echo   TTFM Strategy Engine - Starting...
echo ============================================
echo.

:: 1. Start NestJS Bridge (MongoDB Backend)
start "TTFM Bridge (NestJS / MongoDB)" cmd /k "cd /d %~dp0bridge && npm run start:dev"

:: 2. Start Vibe-Trading Research Brain (AI Swarm)
start "Vibe-Trading Research (AI Swarm)" cmd /k "cd /d %~dp0vibe-trading && vibe-trading serve --port 8899"

:: Wait for services to boot
timeout /t 10 /nobreak > nul

:: 3. Start Python Strategy Engine (AI Powered)
start "TTFM Strategy (Python / Kronos AI)" cmd /k "cd /d %~dp0strategy && py main.py"

echo.
echo ============================================
echo   SYSTEMS ACTIVE
echo ============================================
echo.
echo   Bridge Backend:  http://localhost:4000
echo   Journal Stats:   http://localhost:4000/journal/stats
echo   AI Predictor:    Kronos Foundation Model (ACTIVE)
echo.
echo   Auth API Key:    test_key_123
echo.
echo NOTE: Ensure MetaTrader 5 is running and logged in!
echo.
echo Press any key to close this launcher window...
pause > nul
