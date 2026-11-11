@echo off
setlocal
set "WB_REL="
if exist "%~dp0..\artifacts\latest-release.path" set /p WB_REL=<"%~dp0..\artifacts\latest-release.path"
if defined WB_REL if exist "%~dp0..\%WB_REL%" goto packaged
set "WB_NODE="
for %%N in (node.exe) do set "WB_NODE=%%~$PATH:N"
if defined WB_NODE goto source
for /d %%D in ("%APPDATA%\Accio\pre-install\*") do if exist "%%~fD\node\node.exe" set "WB_NODE=%%~fD\node\node.exe"
if defined WB_NODE goto source
echo Node.js is missing. Use the complete release folder or install Node.js 22.
pause
exit /b 2
:packaged
set "ELECTRON_RUN_AS_NODE="
start "" "%~dp0..\%WB_REL%" %*
exit /b 0
:source
"%WB_NODE%" "%~dp0start.mjs" %*
if errorlevel 1 pause
exit /b %errorlevel%
