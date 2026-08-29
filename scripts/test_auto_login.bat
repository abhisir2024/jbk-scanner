@echo off
title Fyers Auto Login Test
cd /d "E:\Fyers API"
echo ============================================
echo   Testing Fyers Auto Login (TOTP)
echo ============================================
echo.
echo Waiting 90s for rate limit to clear...
timeout /t 90 /nobreak >nul
echo.
python auth/auto_login.py --force
if %ERRORLEVEL% EQU 0 (
    echo.
    echo ✅ Auto-login test PASSED!
) else (
    echo.
    echo ❌ Auto-login test FAILED!
)
pause
