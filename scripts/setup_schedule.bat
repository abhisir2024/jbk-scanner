@echo off
schtasks /create /tn "Fyers Scanner - Open" /tr "E:\Fyers API\scripts\auto_scan.bat" /sc daily /st 09:25 /f
schtasks /create /tn "Fyers Scanner - MidMorning" /tr "E:\Fyers API\scripts\auto_scan.bat" /sc daily /st 10:30 /f
schtasks /create /tn "Fyers Scanner - Midday" /tr "E:\Fyers API\scripts\auto_scan.bat" /sc daily /st 12:00 /f
schtasks /create /tn "Fyers Scanner - Afternoon" /tr "E:\Fyers API\scripts\auto_scan.bat" /sc daily /st 13:30 /f
schtasks /create /tn "Fyers Scanner - PreClose" /tr "E:\Fyers API\scripts\auto_scan.bat" /sc daily /st 15:00 /f
echo.
echo All 5 scanner tasks created!
schtasks /query /tn "Fyers Scanner*" /fo table
pause
