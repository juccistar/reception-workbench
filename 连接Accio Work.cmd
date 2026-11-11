@echo off
call "%~dp0scripts\launch.cmd" --connect-from-accio %*
exit /b %errorlevel%
