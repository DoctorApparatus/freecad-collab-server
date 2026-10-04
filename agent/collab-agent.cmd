@echo off
rem Start the AI participant on Windows.
rem   collab-agent.cmd [path\to\agent.env]
rem Set COLLAB_FREECADCMD to FreeCADCmd.exe if it is not found automatically.
setlocal
set "COLLAB_AGENT_HOME=%~dp0"
if not "%~1"=="" set "COLLAB_AGENT_ENV_FILE=%~f1"
set PYTHONUNBUFFERED=1
if defined COLLAB_FREECADCMD goto run
for /d %%D in ("%ProgramFiles%\FreeCAD*") do if exist "%%D\bin\FreeCADCmd.exe" set "COLLAB_FREECADCMD=%%D\bin\FreeCADCmd.exe"
if not defined COLLAB_FREECADCMD (
    echo error: FreeCADCmd.exe not found. Install FreeCAD or set COLLAB_FREECADCMD.
    exit /b 1
)
:run
"%COLLAB_FREECADCMD%" "%~dp0run_agent.py"
exit /b %errorlevel%
