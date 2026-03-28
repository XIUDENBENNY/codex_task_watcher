@echo off
setlocal

cd /d "%~dp0"

echo Starting Codex task watcher...
set "PY_CMD="
set "DEFAULT_ARGS=--debug"

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 set "PY_CMD=py -3"

if not defined PY_CMD (
    where python >nul 2>nul
    if %ERRORLEVEL% EQU 0 set "PY_CMD=python"
)

if not defined PY_CMD (
    echo Python launcher not found. Install Python or add it to PATH.
    pause
    exit /b 1
)

echo %* | findstr /I /C:"--tray" >nul
if %ERRORLEVEL% EQU 0 set "DEFAULT_ARGS="

%PY_CMD% -u "%~dp0watch_codex_idle.py" %DEFAULT_ARGS% %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Codex watcher exited with error code %EXIT_CODE%.
    pause
)

endlocal
