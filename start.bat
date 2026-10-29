@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto create_venv

rem Existing environment from an older version: make sure the acceleration
rem packages are present, otherwise the host silently falls back to slow mss.
".venv\Scripts\python.exe" -c "import dxcam, cv2, numpy, soundcard" >nul 2>nul
if not errorlevel 1 goto run
echo.
echo Обновляю зависимости: добавляю DXGI-захват и OpenCV...
echo.

goto install_deps

:create_venv
echo.
echo Первый запуск: создаю окружение и устанавливаю зависимости...
echo Это может занять пару минут.
echo.

where py >nul 2>nul
if errorlevel 1 goto venv_python
py -3 -m venv .venv
goto venv_done

:venv_python
python -m venv .venv

:venv_done
if errorlevel 1 goto fail

:install_deps
if exist "wheels" goto install_wheels
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
goto deps_done

:install_wheels
echo Устанавливаю из локального набора wheels...
".venv\Scripts\python.exe" -m pip install --no-index --find-links wheels --disable-pip-version-check -r requirements.txt
if not errorlevel 1 goto deps_done
echo Локальные wheels не подошли, скачиваю пакеты из интернета...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt

:deps_done
if errorlevel 1 goto fail

:run
if not "%~1"=="" goto cli
if exist ".venv\Scripts\pythonw.exe" goto gui_pythonw
".venv\Scripts\python.exe" -m remotepc --gui
if errorlevel 1 goto fail
exit /b 0

:gui_pythonw
".venv\Scripts\python.exe" -c "import tkinter" >nul 2>nul
if errorlevel 1 goto cli
start "" ".venv\Scripts\pythonw.exe" -m remotepc --gui
exit /b 0

:cli
".venv\Scripts\python.exe" -m remotepc %*
if errorlevel 1 goto fail
exit /b 0

:fail
echo.
echo Не удалось запустить RemotePC. Проверьте, что установлен Python 3.11+.
pause
exit /b 1
