@echo off
title Fyers Scanner - Auto Mode
cd /d "E:\Fyers API"

echo ============================================
echo   Fyers Scanner - Auto Mode
echo ============================================
echo.
echo Starting web dashboard at http://127.0.0.1:5001
echo Scanner will auto-run every 5 minutes
echo Press Ctrl+C to stop
echo.

start "" python dashboard.py
timeout /t 3 /nobreak >nul

:loop
echo [%date% %time%] Running scan...
python auto_scan.py
echo [%date% %time%] Next scan in 5 minutes...
timeout /t 300 /nobreak >nul
goto loop
