@echo off
REM Double-click to export the database for moving to another PC.
REM Writes a compressed .sql.gz file to the folder you type below.
cd /d "%~dp0"

echo.
echo  NEPSE platform - database export
echo  ---------------------------------
echo  Plug in your USB / external drive first.
echo.
set /p TARGET=Folder to save the backup into (example E:\nepse_backup):
if "%TARGET%"=="" set TARGET=%~dp0backup

echo.
echo  1 = SLIM  (about 300-600 MB, floorsheet data re-synced on the new PC)  [recommended]
echo  2 = FULL  (about 20 GB, needs an external drive)
echo.
set /p MODE=Choose 1 or 2:

if "%MODE%"=="2" (
    "%~dp0venv\Scripts\python.exe" scripts\db_export.py --out "%TARGET%" --full
) else (
    "%~dp0venv\Scripts\python.exe" scripts\db_export.py --out "%TARGET%"
)

echo.
pause
