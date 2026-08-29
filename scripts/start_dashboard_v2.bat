@echo off
cd /d "E:\Fyers API"
echo Starting Fyers Scanner Dashboard v2...
echo Open http://127.0.0.1:5001 in your browser
echo Press Ctrl+C to stop
echo.
python dashboard.py --port 5001
pause
