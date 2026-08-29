@echo off
title Single-Order Punch Tracker (>=200 lots, +/-5% band)
cd /d "E:\Fyers API"
echo ============================================================
echo   Single-Order Punch Tracker
echo   All 211 F&O stocks | strikes within +/-6%% of spot
echo   Punch: >=200 lots within +/-5%% of current price
echo   Current expiry only | skips expiry day + day before
echo   Simple: direction (bullish/bearish) + lots + moneyness
echo ============================================================
echo.
python -u -m scanner.big_money_ws --stocks 0 --window 60 --band 6 --atm-band 5 --min-lots 200
pause