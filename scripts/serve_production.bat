@echo off
REM ===========================================================================
REM  Start the NEPSE desk on Waitress (production WSGI server).
REM
REM  Replaces "manage.py runserver", which Django explicitly documents as NOT
REM  security-audited and unfit for production traffic: single process, no
REM  request timeouts, no concurrency limit, and it dies to one stuck client.
REM
REM  Run this INSTEAD of runserver on the machine behind the Cloudflare tunnel.
REM ===========================================================================

setlocal
cd /d "%~dp0.."

echo.
echo === Stopping any old runserver / waitress processes on 8501 ==============
REM runserver spawns a parent + autoreloader child, so several PIDs pile up.
REM Stale ones keep serving hours-old code from memory and are a real trap.
for /f "tokens=2" %%p in ('tasklist /fi "imagename eq python.exe" /fo list ^| find "PID:"') do (
    wmic process where "ProcessId=%%p" get CommandLine 2>nul | find "8501" >nul && (
        echo   killing PID %%p
        taskkill /PID %%p /F >nul 2>&1
    )
)

echo.
echo === Refreshing hashed static assets ======================================
REM Required: with DJANGO_DEBUG=0 the app uses CompressedManifestStaticFilesStorage,
REM which raises on every {%% static %%} lookup if staticfiles.json is missing.
call venv\Scripts\python.exe manage.py collectstatic --noinput
if errorlevel 1 (
    echo.
    echo   collectstatic FAILED - not starting the server, every page would 500.
    exit /b 1
)

echo.
echo === Sanity check ========================================================
call venv\Scripts\python.exe manage.py check --deploy

echo.
echo === Starting Waitress on 192.168.1.31:8501 ==============================
echo   Ctrl+C to stop. Leave this window open.
echo.
REM --threads=8 : the desk is IO-bound on MySQL, so threads beat processes here,
REM               and LocMem cache stays shared (a second process would not see it).
REM --channel-timeout : drop clients that stop reading, so one stalled browser
REM               cannot hold a thread forever.
call venv\Scripts\waitress-serve.exe ^
    --listen=192.168.1.31:8501 ^
    --threads=8 ^
    --channel-timeout=120 ^
    nepse_project.wsgi:application

endlocal
