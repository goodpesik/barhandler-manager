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

#define MyAppName "Handler Device Manager"
#define MyAppPublisher "goodpesik"
#define MyAppExeName "bhm.exe"
; BH-161 — реліз публікує ще й standalone device-handler.exe (з v0.4.0 це нове
; ім'я того самого бінарника). Інсталятор ставить його як bhm.exe, але в полі
; є машини, де людина запустила завантажений device-handler.exe напряму — і
; тоді таке ім'я тримає порт 9999 та файл. Убивати треба ОБА, інакше установка
; проходить «успішно», а працює далі стара копія.
#define MyAppExeAltName "device-handler.exe"
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
Filename: "{cmd}"; Parameters: "/c taskkill /F /IM {#MyAppExeAltName}"; Flags: runhidden; RunOnceId: "killexealt"

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

  { Kill a running exe (our unique names — safe). Both names are ours: the
    installer lays the binary down as bhm.exe, but the release also ships it
    standalone as device-handler.exe, and that one is what a person who
    downloaded the exe directly is running. }
  Exec('cmd.exe', '/c taskkill /F /IM {#MyAppExeName}', '', SW_HIDE, ewWaitUntilTerminated, Rc);
  Exec('cmd.exe', '/c taskkill /F /IM {#MyAppExeAltName}', '', SW_HIDE, ewWaitUntilTerminated, Rc);

  { Kill the OLD python-based manager, targeted by its command line so we
    don't touch unrelated python processes. }
  Exec('powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | ' +
    'Where-Object { $_.CommandLine -like ''*\.barhandler-manager\main.py*'' } | ' +
    'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
    '', SW_HIDE, ewWaitUntilTerminated, Rc);

  // Remove the old Python install directory entirely (user: clean
  // everything). Inno has no userprofile constant, so expand the
  // USERPROFILE environment variable instead.
  DelTree(ExpandConstant('{%USERPROFILE}\.barhandler-manager'), True, True, True);
end;

{ BH-164 — запитати МЕНЕДЖЕР, чи можна його вбивати, ПЕРЕД тим як убивати.

  Перевірка в самому `/system/update` цього не закриває: там її роблять у мить
  натискання кнопки, а сюди людина доходить тоді, коли пройде майстер — через
  хвилину або через двадцять. За цей час цілком може початись оплата карткою, і
  `taskkill /F` нижче зніме менеджер посеред SSI-обміну: картку списано, а каса
  результату не дізнається ніколи.

  Повертає текст причини, якщо зайнято, і порожній рядок, якщо вільно АБО якщо
  спитати не вдалось. Саме так: недоступний менеджер (не запущений, стара версія
  без цього маршруту, зайнятий порт) НЕ має блокувати установку — інакше ми
  зробимо оновлення неможливим там, де воно найпотрібніше.

  PowerShell, а не WinHttp: у Inno немає HTTP-клієнта, а PowerShell на будь-якій
  підтримуваній вінді є. `-UseBasicParsing` — щоб не залежати від Internet
  Explorer, `-TimeoutSec 3` — щоб не тримати майстер, коли менеджер не відповідає. }
function ManagerBusyReason: String;
var
  Rc: Integer;
  TmpFile: String;
  Lines: TArrayOfString;
begin
  Result := '';
  TmpFile := ExpandConstant('{tmp}\bhm-busy.txt');
  { Пишемо файл через WriteAllText із UTF8Encoding($false), а НЕ через
    `Set-Content -Encoding UTF8`. Знайдено другим колом ревʼю: класичний
    powershell.exe 5.1 (а саме він тут і викликається — не pwsh 7) на
    `-Encoding UTF8` додає BOM, і `UTF8NoBOM` у ньому не існує. BOM поїхав би
    першим символом у Lines[0], тобто рівно в той діалог, де людина найбільше
    потребує чіткого тексту. BOM усе одно знімаємо нижче — але краще його тут
    не створювати, ніж покладатись на те, що Inno його зʼїсть. }
  if not Exec('powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference = ''Stop''; ' +
    'try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 ' +
    '-Uri ''http://127.0.0.1:9999/busy''; ' +
    '$j = $r.Content | ConvertFrom-Json; ' +
    'if ($j.busy) { [System.IO.File]::WriteAllText(' +
    '''' + TmpFile + ''', [string]$j.message, ' +
    '(New-Object System.Text.UTF8Encoding($false))) } } catch { }"',
    '', SW_HIDE, ewWaitUntilTerminated, Rc) then
    Exit;
  { Файл створюється ЛИШЕ коли менеджер сказав «зайнято». Немає файлу —
    вільно, не запущений або не вміє відповідати: ставимо далі. }
  if not FileExists(TmpFile) then
    Exit;
  if LoadStringsFromFile(TmpFile, Lines) and (GetArrayLength(Lines) > 0) then
    Result := Lines[0];
  DeleteFile(TmpFile);
  { Запобіжник на BOM: якщо він усе-таки приїхав, прибираємо, щоб перед текстом
    не стояв квадратик. }
  while (Length(Result) > 0) and (Ord(Result[1]) = 65279) do
    Result := Copy(Result, 2, Length(Result) - 1);
  if Result = '' then
    Result := 'Менеджер зараз зайнятий. Спробуйте за хвилину.';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  BusyReason: String;
begin
  BusyReason := ManagerBusyReason;
  if BusyReason <> '' then
  begin
    { Непорожній Result перериває установку й показує цей текст людині.
      Нічого ще не змінено: CleanPrevious не викликаний, менеджер живий. }
    Result := BusyReason;
    Exit;
  end;
  CleanPrevious;
  Result := '';
end;

{ BH-164, знайдено другим колом ревʼю: ВИДАЛЕННЯ робить той самий
  `taskkill /F` (секція [UninstallRun]), а гейт стояв лише на установці.
  Видалення посеред оплати карткою навіть гірше за оновлення: менеджер після
  нього не повернеться сам, тобто незавершену транзакцію вже нічим не добити.

  `False` тут скасовує видалення, нічого не зачепивши. }
function InitializeUninstall: Boolean;
var
  BusyReason: String;
begin
  BusyReason := ManagerBusyReason;
  if BusyReason = '' then
  begin
    Result := True;
    Exit;
  end;
  MsgBox(BusyReason, mbError, MB_OK);
  Result := False;
end;
