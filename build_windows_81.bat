@echo off
setlocal
cd /d "%~dp0"
py -3.8 -m pip install --upgrade "pip<25"
py -3.8 -m pip install -r requirements-legacy.txt
py -3.8 scripts\prepare_vless.py --win7
if errorlevel 1 exit /b 1
py -3.8 -m PyInstaller --noconfirm --clean --windowed --name NeonLGControl81 --icon "assets\neon.ico" --version-file "assets\version_info.txt" --add-data "change_region.sh;." --add-data "assets;assets" --add-data "samsung-belet/BeletSamsungClient-unsigned.wgt;samsung-belet" --add-data "lg-belet;lg-belet" --add-data "belet_samsung_native;belet_samsung_native" --add-data "belet_vidaa_client;belet_vidaa_client" --exclude-module PyQt5.QtWebEngineWidgets --exclude-module PyQt5.QtWebEngineCore --exclude-module PyQt5.QtQml --exclude-module PyQt5.QtQuick --exclude-module PyQt5.QtMultimedia --exclude-module PyQt5.QtBluetooth --exclude-module PyQt5.QtDesigner --exclude-module tkinter --uac-admin app_legacy.py
if errorlevel 1 exit /b 1

set MAKENSIS=
where makensis >nul 2>nul && set MAKENSIS=makensis
if "%MAKENSIS%"=="" if exist "%ProgramFiles(x86)%\NSIS\makensis.exe" set "MAKENSIS=%ProgramFiles(x86)%\NSIS\makensis.exe"
if "%MAKENSIS%"=="" if exist "%ProgramFiles%\NSIS\makensis.exe" set "MAKENSIS=%ProgramFiles%\NSIS\makensis.exe"

if not "%MAKENSIS%"=="" (
    echo Building Windows 8.1/10/11 installer with NSIS...
    "%MAKENSIS%" /V3 installer\neon_setup.nsi
    if errorlevel 1 exit /b 1
    echo Installer ready: dist\NEON_Setup_Win81_10_11.exe
) else (
    echo [!] NSIS makensis.exe not found; portable app built at dist\NeonLGControl81\NeonLGControl81.exe
)

echo.
echo Build complete!
pause
