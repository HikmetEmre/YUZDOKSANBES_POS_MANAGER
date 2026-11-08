; ============================================================
;  YUZDOKSANBES POS — Inno Setup 6 installer script
;  Built by build.bat:  ISCC /DAppVersion=<x.y.z> installer.iss
;
;  Program  → C:\Program Files\YUZDOKSANBES POS        (replaced on upgrade)
;  Data     → C:\ProgramData\YUZDOKSANBES              (NEVER deleted:
;             pos.db, backups, logs, secret_key.txt, credentials.json, logo.png)
;
;  Test-only command-line switches (used by the automated smoke test):
;    /DATADIR=<folder>   put the data folder somewhere else
;    /CHECKOUTPIN=…  /WAITERPIN=…   set PINs during a /VERYSILENT install
; ============================================================

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName    "YUZDOKSANBES POS"
#define AppExe     "YUZDOKSANBES_POS.exe"

[Setup]
; NEVER change AppId — it is how a newer installer finds and upgrades this one.
AppId={{1B3A9CDB-0CF0-413A-9FA8-A42E5C724690}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=YUZDOKSANBES
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=output
OutputBaseFilename=YUZDOKSANBES_POS_Kurulum_{#AppVersion}
SetupIconFile=..\logo.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; Admin (one UAC prompt) so it can add the firewall rules. The command-line
; override exists only for the automated per-user smoke test.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[CustomMessages]
turkish.AutoStart=Windows açılışında otomatik başlat (önerilir)
turkish.StartNow=POS'u şimdi başlat
turkish.DataFolder=Veri klasörü (yedekler, kayıtlar)

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart";   Description: "{cm:AutoStart}";        GroupDescription: "{cm:AdditionalIcons}"

[Dirs]
; Data folder: every Windows user on the café PC may write (the POS runs as
; whoever is logged in). Never removed by the uninstaller.
Name: "{code:DataDir}"; Permissions: users-modify; Flags: uninsneveruninstall

[Files]
Source: "dist\YUZDOKSANBES_POS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README_KURULUM.md"; DestDir: "{app}"; DestName: "KURULUM_REHBERI.md"; Flags: ignoreversion
; Report logo: copied once; the café's own logo.png in the data folder is never overwritten.
Source: "..\logo.png"; DestDir: "{code:DataDir}"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExe}"
Name: "{group}\{cm:DataFolder}"; Filename: "{code:DataDir}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{autostartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: autostart; Flags: runminimized

[Run]
; Firewall: phones on the café WiFi must reach the POS (TCP 80, fallback 8000)
; and find siparis.local (mDNS, UDP 5353). Old rules are removed first so an
; upgrade never piles up duplicates. Admin installs only.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; Flags: runhidden; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppName}"" dir=in action=allow program=""{app}\{#AppExe}"" protocol=TCP localport=80,8000 profile=any enable=yes"; Flags: runhidden; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppName}"" dir=in action=allow program=""{app}\{#AppExe}"" protocol=UDP localport=5353 profile=any enable=yes"; Flags: runhidden; Check: IsAdminInstallMode
Filename: "{app}\{#AppExe}"; Description: "{cm:StartNow}"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; Flags: runhidden; RunOnceId: "DelFirewall"; Check: IsAdminInstallMode

[Code]
var
  PinPage: TInputQueryWizardPage;

function DataDir(Param: String): String;
begin
  Result := ExpandConstant('{param:DATADIR|{commonappdata}\YUZDOKSANBES}');
end;

function CredentialsFile(): String;
begin
  Result := AddBackslash(DataDir('')) + 'credentials.json';
end;

function IsAlnum(const S: String): Boolean;
var
  I: Integer;
begin
  // Only English letters and digits: they type the same on every phone and
  // keyboard (no ı/ş/ğ surprises) and need no escaping in credentials.json.
  Result := Length(S) > 0;
  for I := 1 to Length(S) do
    if not (((S[I] >= '0') and (S[I] <= '9')) or
            ((S[I] >= 'A') and (S[I] <= 'Z')) or
            ((S[I] >= 'a') and (S[I] <= 'z'))) then
    begin
      Result := False;
      Exit;
    end;
end;

function ValidPin(const S: String): Boolean;
begin
  // 4–16 letters and/or digits (case-sensitive); the old built-in placeholders are refused.
  Result := IsAlnum(S) and (Length(S) >= 4) and (Length(S) <= 16)
            and (S <> '0000') and (S <> '1111');
end;

procedure InitializeWizard();
begin
  PinPage := CreateInputQueryPage(wpSelectTasks,
    'Giriş şifreleri',
    'Kasa ve garson ekranlarına giriş için iki ayrı şifre belirleyin.',
    'Şifre 4–16 karakter olmalı: harf ve/veya rakam (örn. kasa2026). Türkçe harf (ı, ş, ğ…) ve boşluk kullanmayın.' + #13#10 +
    'Büyük/küçük harf fark eder. İki şifre birbirinden farklı olmalı.' + #13#10 +
    'Kasa şifresi ödeme, iptal ve yönetim panelini açar — yalnızca kasaya verin.' + #13#10 +
    'Garson şifresi yalnızca sipariş almaya yarar.');
  PinPage.Add('Kasa (yönetici) şifresi:', True);
  PinPage.Add('Garson şifresi:', True);
  // EVERY install (new, reinstall after uninstall, upgrade) asks for the
  // passwords; what is typed here replaces the old ones. The data folder (and
  // its credentials.json) survives an uninstall, so say so when it exists.
  if FileExists(CredentialsFile()) then
    PinPage.SubCaptionLabel.Caption := PinPage.SubCaptionLabel.Caption + #13#10#13#10 +
      'Bu bilgisayarda daha önce şifre belirlenmiş. Burada yazdığınız şifreler eskilerinin yerine geçer; ' +
      'eski şifreler ve açık oturumlar artık çalışmaz.';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  // Silent installs never stop here: their PINs come from /CHECKOUTPIN and
  // /WAITERPIN (or, without them, the app starts with its loud placeholder warning).
  if (CurPageID = PinPage.ID) and not WizardSilent() then
  begin
    if not ValidPin(PinPage.Values[0]) or not ValidPin(PinPage.Values[1]) then
    begin
      MsgBox('Her iki şifre de 4–16 karakter olmalı ve yalnızca İngilizce harf (A–Z, a–z) ile rakam içermeli.' + #13#10 +
             'Türkçe harf (ı, ş, ğ, ü, ö, ç) ve boşluk kullanılamaz; 0000 ve 1111 kabul edilmez.', mbError, MB_OK);
      Result := False;
    end
    else if PinPage.Values[0] = PinPage.Values[1] then
    begin
      MsgBox('Kasa şifresi ile Garson şifresi farklı olmalı.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure StopRunningPos();
var
  Code: Integer;
begin
  // An older version may be running (e.g. auto-started at login): stop it so
  // its files can be replaced. Sales data is in the database, nothing is lost.
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningPos();
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  C, W: String;
begin
  if CurStep = ssPostInstall then
  begin
    // The passwords typed in THIS install always win (overwrite the file).
    // Silent installs: from /CHECKOUTPIN + /WAITERPIN; without them the
    // existing file is kept (unattended update).
    if WizardSilent() then
    begin
      C := ExpandConstant('{param:CHECKOUTPIN|}');
      W := ExpandConstant('{param:WAITERPIN|}');
    end
    else
    begin
      C := PinPage.Values[0];
      W := PinPage.Values[1];
    end;
    if ValidPin(C) and ValidPin(W) and (C <> W) then
    begin
      if SaveStringToFile(CredentialsFile(),
           '{"checkout_pin": "' + C + '", "waiter_pin": "' + W + '"}', False) then
        Log('Passwords written to ' + CredentialsFile())
      else
        MsgBox('Şifreler kaydedilemedi: ' + CredentialsFile(), mbError, MB_OK);
    end
    else
      Log('No new passwords given - existing credentials.json (if any) kept');
  end;
end;

function InitializeUninstall(): Boolean;
begin
  StopRunningPos();
  Result := True;
end;
