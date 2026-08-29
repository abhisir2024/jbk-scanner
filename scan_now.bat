@echo off
title Fyers Scanner - One Click Scan
cd /d "E:\Fyers API"
echo ============================================
echo   Scanning all F&O stocks + indices...
echo   This takes a few minutes.
echo ============================================
echo.
python scan.py
echo.
pause
