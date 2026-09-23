@echo off
REM Double-click to start the NEPSE platform.
REM Always uses the project's venv Python, so no activation is needed,
REM and stops any old server on port 8501 first so two never stack.
REM
REM Binds 0.0.0.0 (all addresses) so it keeps working even when the router
REM hands this PC a different IP. Open it from this PC at
REM http://127.0.0.1:8501/ , or from another device using this PC's current
REM IP (run `ipconfig` to see it), e.g. http://192.168.1.25:8501/ .
cd /d "%~dp0"

for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8501" ^| findstr LISTENING') do (
    echo Stopping old server, PID %%p
    taskkill /PID %%p /F >nul 2>&1
)

echo Starting server on port 8501 (open http://127.0.0.1:8501/)
"%~dp0venv\Scripts\python.exe" manage.py runserver 0.0.0.0:8501
pause
