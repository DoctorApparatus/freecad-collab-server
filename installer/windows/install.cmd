@echo off
rem Double-click to open the FreeCAD collab installer menu.
rem With arguments, they are passed to install.ps1 (see install.ps1 -? for options).
setlocal
if "%~1"=="" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Pause
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
)
exit /b %errorlevel%
