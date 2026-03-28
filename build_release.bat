@echo off
setlocal

cd /d "%~dp0"

set "PYINSTALLER=pyinstaller"
where pyinstaller >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    set "PYINSTALLER=py -3 -m PyInstaller"
)

%PYINSTALLER% --noconfirm --clean --windowed ^
  --name CodexTaskWatcher ^
  --add-data "assets;assets" ^
  watch_codex_idle.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Build failed.
    exit /b %ERRORLEVEL%
)

if exist dist\CodexTaskWatcher.zip del /f /q dist\CodexTaskWatcher.zip >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\CodexTaskWatcher\*' -DestinationPath 'dist\CodexTaskWatcher.zip' -Force"

echo.
echo Build complete:
echo   dist\CodexTaskWatcher\CodexTaskWatcher.exe
echo   dist\CodexTaskWatcher.zip

endlocal
