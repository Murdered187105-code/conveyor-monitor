@echo off
chcp 65001 >nul
cd /d "%~dp0"
python conveyor_monitor_v5.py
if errorlevel 1 pause
