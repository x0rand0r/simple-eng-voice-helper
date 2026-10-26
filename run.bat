@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Souffleur

set "VENV=.venv"
set "VPY=%VENV%\Scripts\python.exe"

if exist "%VPY%" goto deps

echo [Souffleur] Первый запуск: создаю виртуальное окружение...
set "PY="
for %%V in (3.13 3.12 3.11 3.10 3.14) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo Не найден Python 3.10 или новее. Установите его с https://www.python.org/downloads/ и запустите run.bat снова.
  pause
  exit /b 1
)
%PY% -m venv "%VENV%" || goto fail

:deps
rem reinstall packages only when requirements.txt has changed
fc /b requirements.txt "%VENV%\requirements.installed" >nul 2>&1
if not errorlevel 1 goto run
echo [Souffleur] Устанавливаю зависимости, это займёт минуту...
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt || goto fail
copy /y requirements.txt "%VENV%\requirements.installed" >nul

:run
"%VPY%" -m app %*
if errorlevel 1 goto fail
exit /b 0

:fail
echo.
echo [Souffleur] Что-то пошло не так, подробности выше.
pause
exit /b 1
