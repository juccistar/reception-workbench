@echo off
setlocal
if not defined ACCIO_NODE_BIN goto noauth
if not exist "%ACCIO_NODE_BIN%" goto noauth
"%ACCIO_NODE_BIN%" "%~dp0scripts\connect-live.mjs" %*
exit /b %errorlevel%
:noauth
>&2 echo {"status":"blocked","code":"authorized_environment_missing"}
exit /b 2
