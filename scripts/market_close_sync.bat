@echo off
REM Post-close data sync, driven by Windows Task Scheduler.
REM   %1 = extra args passed to the command (e.g. --verify)
REM Registered by scripts\register_sync_tasks.ps1 as three daily tasks:
REM   15:15 (full sync)  15:30 (--verify)  16:00 (--verify)
REM Output is appended to logs\market_close_sync.log so a failed night can be
REM read back the next morning.

setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"
if not exist "logs" mkdir "logs"

echo. >> "logs\market_close_sync.log"
echo ================ %DATE% %TIME% : market_close_sync %* ================ >> "logs\market_close_sync.log"
"%ROOT%\venv\Scripts\python.exe" manage.py market_close_sync %* >> "logs\market_close_sync.log" 2>&1
endlocal
