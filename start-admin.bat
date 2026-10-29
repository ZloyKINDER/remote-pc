@echo off
setlocal
net session >nul 2>&1
if %errorlevel%==0 goto :run
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b
:run
call "%~dp0start.bat" %*
