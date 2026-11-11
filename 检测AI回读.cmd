@echo off
setlocal
if not defined ACCIO_NODE_BIN goto noauth
if not exist "%ACCIO_NODE_BIN%" goto noauth
"%ACCIO_NODE_BIN%" "%~dp0scripts\check-ai-recovery.mjs" %*
exit /b %errorlevel%
:noauth
echo Run this command from the current authorized Accio task terminal.
echo No AI request was started. Do not copy tokens into files or chat.
exit /b 2
