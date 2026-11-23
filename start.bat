@echo off
setlocal EnableExtensions DisableDelayedExpansion

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "exitCode=%errorlevel%"
if not "%exitCode%"=="0" (
    echo.
    echo CreatorRadar exited with code %exitCode%.
    pause
)
exit /b %exitCode%
