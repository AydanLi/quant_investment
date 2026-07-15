@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch_robinhood_mirror.ps1"
if errorlevel 1 (
    echo.
    echo Robinhood Mirror failed to start. Review logs in .runtime.
    pause
    exit /b 1
)
endlocal
