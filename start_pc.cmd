@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (python tools\start_pc.py) else (py -3 tools\start_pc.py)
pause
