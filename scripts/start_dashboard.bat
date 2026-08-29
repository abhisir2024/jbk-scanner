@echo off
cd /d "E:\Fyers API"
start /b python dashboard.py > dashboard.log 2>&1
echo Dashboard started. PID: check dashboard.log
