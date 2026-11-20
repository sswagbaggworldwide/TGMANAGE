@echo off
cd /d "%~dp0"
set "OPENTELE_NO_FETCH=1"
if not exist .venv\Scripts\python.exe py -3.10 -m venv .venv
.venv\Scripts\python.exe -c "from importlib.metadata import version; from PIL import Image, ImageTk; import cryptg, telethon, cryptography, PyQt5, openpyxl, opentele2; assert version('opentele2') == '2.0.0'; assert version('telethon') == '1.45.0'" >nul 2>nul
if errorlevel 1 (
    .venv\Scripts\python.exe -m pip install -r requirements.txt -r converter_requirements.txt
    if errorlevel 1 goto fail
)
.venv\Scripts\python.exe app.py
exit /b 0
:fail
pause
