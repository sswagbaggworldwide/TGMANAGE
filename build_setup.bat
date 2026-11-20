@echo off
cd /d "%~dp0"
set "OPENTELE_NO_FETCH=1"
if not exist .venv\Scripts\python.exe py -3.10 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -r converter_requirements.txt -r requirements-build.txt
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m unittest discover -s tests -q
if errorlevel 1 goto fail
.venv\Scripts\python.exe packaging\build_release.py %*
if errorlevel 1 goto fail
echo Installer: release\setup.exe
exit /b 0
:fail
echo Build failed. Read the error above.
pause
exit /b 1
