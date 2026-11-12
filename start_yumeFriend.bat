@echo off
rem yumeFriend launcher (double-click me).
rem NOTE: keep this file pure ASCII -- cmd.exe reads .bat with the console code
rem page (GBK here), so UTF-8 Chinese inside a .bat gets mangled.
rem All Chinese output lives in scripts\start_desktop.ps1 (UTF-8 with BOM).
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_desktop.ps1"
if errorlevel 1 (
    echo.
    echo Failed to start. Please send the error above to me.
    pause
)
