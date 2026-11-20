@echo off
setlocal
cd /d "%~dp0"
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
py scripts\prepare_vless.py
if errorlevel 1 exit /b 1
py -m PyInstaller --noconfirm --clean --windowed --name NeonLGControl --icon "assets\neon.ico" --version-file "assets\version_info.txt" --add-data "change_region.sh;." --add-data "ui.html;." --add-data "assets;assets" --add-data "samsung-belet/BeletSamsungClient-unsigned.wgt;samsung-belet" --add-data "lg-belet;lg-belet" --uac-admin app.py
if errorlevel 1 exit /b 1
echo.
echo Build complete: dist\NeonLGControl\NeonLGControl.exe
echo Keep the whole dist folder together when distributing.
pause
