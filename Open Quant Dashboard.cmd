@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch_dashboard.ps1"
if errorlevel 1 (
    echo.
    echo Dashboard failed to start. Review the error above and the logs in .runtime.
    pause
    exit /b 1
)

endlocal
