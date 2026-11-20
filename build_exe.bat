@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -3.10 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -r converter_requirements.txt -r requirements-build.txt
if errorlevel 1 goto fail
.venv\Scripts\python.exe packaging\build_release.py --exe-only %*
if errorlevel 1 goto fail
exit /b 0
:fail
pause
exit /b 1
