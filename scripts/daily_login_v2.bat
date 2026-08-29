@echo off
title Fyers Auto Login
cd /d "E:\Fyers API"
echo ============================================
echo   Fyers Auto Login (TOTP)
echo   No browser needed - fully automatic
echo ============================================
echo.
python auto_login.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Login failed! Falling back to manual mode...
    python auth/daily_login.py
)
pause
