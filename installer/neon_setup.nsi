; NEON Windows Installer (NSIS 3.x) — Windows 8.1, Windows 10 & Windows 11
Unicode True

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

!define APP_NAME "NEON"
!define APP_DISPLAY_NAME "NEON - LG webOS & Conf Console"
!define APP_PUBLISHER "NEON Community"
!define UNINST_REG_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\NEON_LG_Control"

!ifndef APP_VERSION
  !define APP_VERSION "2.0.0.0"
!endif
!ifndef APP_EXE
  !define APP_EXE "NeonLGControl81.exe"
!endif
!ifndef DIST_DIR
  !define DIST_DIR "..\dist\NeonLGControl81"
!endif
!ifndef ICON_FILE
  !define ICON_FILE "..\assets\neon.ico"
!endif
!ifndef OUT_FILE
  !define OUT_FILE "..\dist\NEON_Setup_Win81_10_11.exe"
!endif

Name "${APP_DISPLAY_NAME}"
OutFile "${OUT_FILE}"
InstallDir "$PROGRAMFILES\NEON"
InstallDirRegKey HKLM "Software\NEON" "InstallDir"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

VIProductVersion "${APP_VERSION}"
VIAddVersionKey "ProductName" "${APP_DISPLAY_NAME}"
VIAddVersionKey "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey "FileDescription" "NEON Windows Installer"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "LegalCopyright" "Open Source Community Tool"

; Modern UI 2 Branding & Icons
!define MUI_ABORTWARNING
!define MUI_ICON "${ICON_FILE}"
!define MUI_UNICON "${ICON_FILE}"
!define MUI_LANGDLL_ALLLANGUAGES
!define MUI_LANGDLL_REGISTRY_ROOT "HKLM"
!define MUI_LANGDLL_REGISTRY_KEY "Software\NEON"
!define MUI_LANGDLL_REGISTRY_VALUENAME "Installer Language"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"

; Installer Pages
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

; Uninstaller Pages
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

; Languages
!insertmacro MUI_LANGUAGE "Russian"
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_RESERVEFILE_LANGDLL

LangString DESC_DESKTOP_CREATED ${LANG_ENGLISH} "Desktop shortcut created: NEON"
LangString DESC_DESKTOP_CREATED ${LANG_RUSSIAN} "Ярлык на рабочем столе создан: NEON"

Function .onInit
  !insertmacro MUI_LANGDLL_DISPLAY
  ${If} ${RunningX64}
    SetRegView 64
    StrCpy $INSTDIR "$PROGRAMFILES64\NEON"
  ${Else}
    StrCpy $INSTDIR "$PROGRAMFILES\NEON"
  ${EndIf}
  ReadRegStr $0 HKLM "Software\NEON" "InstallDir"
  ${If} $0 != ""
    StrCpy $INSTDIR $0
  ${EndIf}
FunctionEnd

Function un.onInit
  !insertmacro MUI_UNGETLANGUAGE
  ${If} ${RunningX64}
    SetRegView 64
  ${EndIf}
FunctionEnd

Section "NEON Console" SecCore
  SectionIn RO

  ; Stop running instances cleanly before updating files
  nsExec::ExecToStack 'taskkill /F /IM NeonLGControl81.exe'
  Pop $0
  Pop $1
  nsExec::ExecToStack 'taskkill /F /IM NeonLGControl.exe'
  Pop $0
  Pop $1
  System::Call 'Kernel32::SetEnvironmentVariable(t "NEON_INSTDIR", t "$INSTDIR")'
  nsExec::ExecToStack 'powershell.exe -NoProfile -NonInteractive -Command "Get-Process -Name sing-box,xray -ErrorAction SilentlyContinue | Where-Object { $$_.Path -and $$env:NEON_INSTDIR -and $$_.Path.StartsWith($$env:NEON_INSTDIR, [System.StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force -ErrorAction SilentlyContinue"'
  Pop $0
  Pop $1
  Sleep 350

  SetShellVarContext all
  SetOutPath "$INSTDIR"
  File /r "${DIST_DIR}\*.*"

  ; Create Uninstaller
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; Desktop Shortcut (explicit executable & icon index 0)
  SetOutPath "$INSTDIR"
  CreateShortCut "$DESKTOP\NEON.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
  DetailPrint "$(DESC_DESKTOP_CREATED)"

  ; Start Menu Shortcuts
  CreateDirectory "$SMPROGRAMS\NEON"
  CreateShortCut "$SMPROGRAMS\NEON\NEON Console.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
  CreateShortCut "$SMPROGRAMS\NEON\Uninstall NEON.lnk" "$INSTDIR\Uninstall.exe" "" "$INSTDIR\Uninstall.exe" 0

  ; Save Install Metadata & Add/Remove Programs Registration
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0

  WriteRegStr HKLM "Software\NEON" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "${UNINST_REG_KEY}" "DisplayName" "${APP_DISPLAY_NAME}"
  WriteRegStr HKLM "${UNINST_REG_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "${UNINST_REG_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKLM "${UNINST_REG_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE},0"
  WriteRegStr HKLM "${UNINST_REG_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "${UNINST_REG_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKLM "${UNINST_REG_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKLM "${UNINST_REG_KEY}" "EstimatedSize" $0
  WriteRegDWORD HKLM "${UNINST_REG_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINST_REG_KEY}" "NoRepair" 1

  ; Refresh Windows Explorer Shell Icon Cache
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'
SectionEnd

Section "Uninstall"
  ; Terminate running NEON application if active
  nsExec::ExecToStack 'taskkill /F /IM NeonLGControl81.exe'
  Pop $0
  Pop $1
  nsExec::ExecToStack 'taskkill /F /IM NeonLGControl.exe'
  Pop $0
  Pop $1
  System::Call 'Kernel32::SetEnvironmentVariable(t "NEON_INSTDIR", t "$INSTDIR")'
  nsExec::ExecToStack 'powershell.exe -NoProfile -NonInteractive -Command "Get-Process -Name sing-box,xray -ErrorAction SilentlyContinue | Where-Object { $$_.Path -and $$env:NEON_INSTDIR -and $$_.Path.StartsWith($$env:NEON_INSTDIR, [System.StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force -ErrorAction SilentlyContinue"'
  Pop $0
  Pop $1
  Sleep 450

  ; Move CWD away from $INSTDIR so Windows allows complete folder deletion
  SetOutPath "$TEMP"

  ; Remove Desktop and Start Menu shortcuts (both All Users & Current User)
  SetShellVarContext all
  Delete "$DESKTOP\NEON.lnk"
  RMDir /r "$SMPROGRAMS\NEON"

  SetShellVarContext current
  Delete "$DESKTOP\NEON.lnk"
  RMDir /r "$SMPROGRAMS\NEON"

  ; Remove all NEON app settings, logs, and profile maps in %APPDATA% & %LOCALAPPDATA%
  RMDir /r "$APPDATA\NEON"
  RMDir /r "$LOCALAPPDATA\NEON"

  ; Also clean per-user %APPDATA%\NEON across user profiles in case elevated under a different admin account
  ReadEnvStr $2 "SystemDrive"
  ${If} $2 != ""
    FindFirst $3 $4 "$2\Users\*"
    ${DoWhile} $4 != ""
      ${If} $4 != "."
      ${AndIf} $4 != ".."
        ${If} ${FileExists} "$2\Users\$4\AppData\Roaming\NEON\*.*"
          RMDir /r "$2\Users\$4\AppData\Roaming\NEON"
        ${EndIf}
        ${If} ${FileExists} "$2\Users\$4\AppData\Local\NEON\*.*"
          RMDir /r "$2\Users\$4\AppData\Local\NEON"
        ${EndIf}
        RMDir "$2\Users\$4\AppData\Roaming\NEON"
        RMDir "$2\Users\$4\AppData\Local\NEON"
        Delete "$2\Users\$4\Desktop\NEON.lnk"
      ${EndIf}
      FindNext $3 $4
    ${Loop}
    FindClose $3
  ${EndIf}

  ; Remove installed files and folder completely
  Delete "$INSTDIR\Uninstall.exe"
  RMDir /r "$INSTDIR"

  ; Remove all Registry keys (native + 32-bit view fallback)
  DeleteRegKey HKLM "${UNINST_REG_KEY}"
  DeleteRegKey HKLM "Software\NEON"
  DeleteRegKey HKCU "Software\NEON"
  ${If} ${RunningX64}
    SetRegView 32
    DeleteRegKey HKLM "${UNINST_REG_KEY}"
    DeleteRegKey HKLM "Software\NEON"
    SetRegView 64
  ${EndIf}

  ; Refresh Windows Explorer Shell Cache
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'
SectionEnd
