@echo off
setlocal
if not defined ACCIO_NODE_BIN goto noauth
if not exist "%ACCIO_NODE_BIN%" goto noauth
"%ACCIO_NODE_BIN%" "%~dp0scripts\connect-live.mjs" %*
set "WB_EXIT=%errorlevel%"
if not "%WB_EXIT%"=="0" pause
exit /b %WB_EXIT%
:noauth
echo Open this file from the current authorized Accio terminal.
echo Do not copy tokens into this file or the chat.
pause
exit /b 2
