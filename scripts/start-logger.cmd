@echo off
REM Start the ACR telemetry logger.
REM
REM Safe to run at any time, including at login before the game exists: the
REM logger waits for Assetto Corsa Rally, records every stage run, and keeps
REM waiting when the game closes. It never needs to be restarted by hand.

cd /d "%~dp0.."
if not exist "runs" mkdir "runs"

set "UV=uv"
where uv >nul 2>&1 || set "UV=%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe"

echo [%date% %time%] logger starting >> "runs\logger.log"
"%UV%" run acr-telemetry log --hz 100 >> "runs\logger.log" 2>&1
echo [%date% %time%] logger exited with %errorlevel% >> "runs\logger.log"
