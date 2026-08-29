@echo off
title Big Money Single-Order Watch
cd /d "E:\Fyers API"
echo Starting Big Money 15-min single-order watch...
echo Saves live bursts to data\big_money_live.json for the dashboard.
echo Press Ctrl+C to stop
echo.
python -m scanner.big_money_watch --interval 15
pause
