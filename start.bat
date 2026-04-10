@echo off
title TTFM Auto-Trade Bot
echo ============================================
echo   TTFM Strategy Engine - Starting...
echo ============================================
echo.

:: 1. Start NestJS Bridge
start "TTFM Bridge (NestJS)" cmd /k "cd /d %~dp0bridge && npm run start:dev"

:: Wait for NestJS to boot before starting Python
timeout /t 6 /nobreak > nul

:: 2. Start Python Strategy Engine
start "TTFM Strategy (Python)" cmd /k "cd /d %~dp0strategy && py main.py"

echo.
echo Both processes are now running in separate windows.
echo.
echo   Bridge:          http://localhost:4000
echo   Journal Stats:   http://localhost:4000/journal/stats
echo   Signal Log:      http://localhost:4000/journal/signals
echo   Filter Impact:   http://localhost:4000/journal/filter-impact
echo.
echo   Header required: x-api-key: ttfm_local_key_change_this
echo.
echo Press any key to close this launcher window...
pause > nul
