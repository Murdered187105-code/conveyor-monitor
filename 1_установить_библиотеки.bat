@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Установка библиотек...
python -m pip install -r requirements.txt
echo.
echo Готово. Теперь установите драйвер Interception (см. README.txt).
pause
