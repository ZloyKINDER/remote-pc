@echo off
setlocal
net session >nul 2>&1
if %errorlevel%==0 goto :run
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b
:run
echo Открываю входящий TCP-порт 8765 в брандмауэре Windows...
netsh advfirewall firewall delete rule name="RemotePC" >nul 2>&1
netsh advfirewall firewall add rule name="RemotePC" dir=in action=allow protocol=TCP localport=8765 profile=any
if errorlevel 1 (
  echo Не удалось добавить правило.
) else (
  echo Готово. Если порт в приложении другой, измените 8765 в этом файле.
)
pause
