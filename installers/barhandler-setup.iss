; Inno Setup script — proper Windows installer for barhandler-manager.
;
; Wraps the headless bhm.exe (PyInstaller build) into a standard installer:
;   * installs to %LocalAppData%\Programs\BarhandlerManager (no admin/UAC)
;   * BEFORE installing, wipes any previous install — the Python one at
;     %USERPROFILE%\.barhandler-manager (+ its "BarhandlerManager" scheduled
;     task) AND a prior setup — so it always installs clean/fresh
;   * autostarts at logon (Startup shortcut), Start-menu "Dashboard" shortcut
;   * starts it right after install
;   * proper Uninstall entry in Programs & Features that stops + removes it
;
; Built in CI on windows-latest:
;   ISCC.exe /DMyAppVersion=X.Y.Z /DExePath=dist\bhm.exe installers\barhandler-setup.iss
;   -> dist\barhandler-setup.exe

#define MyAppName "BarHandler Manager"
#define MyAppPublisher "goodpesik"
#define MyAppExeName "bhm.exe"
#define MyAppUrl "http://localhost:9999/"
#define TaskName "BarhandlerManager"

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef ExePath
  #define ExePath "dist\bhm.exe"
#endif

[Setup]
; Stable AppId so Inno recognises & replaces a previous setup install.
AppId={{7C2B6E9A-4D3F-4E21-9B7A-0A9F2C1D8E64}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppSupportURL=https://github.com/goodpesik/barhandler-manager
DefaultDirName={localappdata}\Programs\BarhandlerManager
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=barhandler-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no
; SetupIconFile=installers\bhm.ico   ; add an icon later if we ship one

[Languages]
Name: "uk"; MessagesFile: "compiler:Languages\Ukrainian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#ExePath}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Start-menu shortcut that opens the dashboard in the browser.
Name: "{autoprograms}\{#MyAppName} (Dashboard)"; Filename: "{#MyAppUrl}"
; Autostart at logon — a Startup-folder shortcut to the headless exe.
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
; Launch now (headless, no window) so the operator doesn't have to log off/on.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait runhidden
; Offer to open the dashboard at the end.
Filename: "{#MyAppUrl}"; Description: "Відкрити дашборд / Open dashboard"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c taskkill /F /IM {#MyAppExeName}"; Flags: runhidden; RunOnceId: "killexe"

[Code]
{ Wipe any previous barhandler-manager before installing — the Python install
  and its scheduled task, plus a lingering exe — so we always start clean. }
procedure CleanPrevious;
var
  Rc: Integer;
begin
  { Stop + delete the old "BarhandlerManager" scheduled task (Python install). }
  Exec('schtasks.exe', '/End /TN "{#TaskName}"', '', SW_HIDE, ewWaitUntilTerminated, Rc);
  Exec('schtasks.exe', '/Delete /TN "{#TaskName}" /F', '', SW_HIDE, ewWaitUntilTerminated, Rc);

  { Kill a running exe (our unique name — safe). }
  Exec('cmd.exe', '/c taskkill /F /IM {#MyAppExeName}', '', SW_HIDE, ewWaitUntilTerminated, Rc);

  { Kill the OLD python-based manager, targeted by its command line so we
    don't touch unrelated python processes. }
  Exec('powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | ' +
    'Where-Object { $_.CommandLine -like ''*\.barhandler-manager\main.py*'' } | ' +
    'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
    '', SW_HIDE, ewWaitUntilTerminated, Rc);

  { Remove the old Python install directory entirely (user: clean everything). }
  DelTree(ExpandConstant('{userprofile}\.barhandler-manager'), True, True, True);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  CleanPrevious;
  Result := '';
end;
