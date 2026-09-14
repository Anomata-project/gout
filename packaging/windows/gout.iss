; Inno Setup: gout's Windows installer (packaging/build.py setup, after build.py app).
; Installs for the current user, without an administrator prompt, and puts gout on that user's PATH.

#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId={{6B0F3C2A-9D41-4E0B-8A53-2F1E0C7D9A10}
AppName=gout
AppVersion={#Version}
AppVerName=gout {#Version}
AppPublisher=Anomata Project
AppPublisherURL=https://github.com/Anomata-project/gout
AppSupportURL=https://github.com/Anomata-project/gout/issues
DefaultDirName={localappdata}\Programs\gout
DisableDirPage=auto
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=gout-{#Version}-windows-x64-setup
SetupIconFile=gout.ico
UninstallDisplayIcon={app}\gout.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes

[Files]
Source: "..\..\dist\app\gout\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "gout.ico"; DestDir: "{app}"; Flags: ignoreversion

[Tasks]
Name: "desktopicon"; Description: "A gout icon on the desktop"; Flags: unchecked

[Icons]
Name: "{userprograms}\gout"; Filename: "{cmd}"; Parameters: "/k ""{app}\gout-start.cmd"""; WorkingDir: "{%USERPROFILE}"; IconFilename: "{app}\gout.ico"; Comment: "gout, a command-line DAW"
Name: "{userdesktop}\gout"; Filename: "{cmd}"; Parameters: "/k ""{app}\gout-start.cmd"""; WorkingDir: "{%USERPROFILE}"; IconFilename: "{app}\gout.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
Filename: "{cmd}"; Parameters: "/k ""{app}\gout-start.cmd"""; WorkingDir: "{%USERPROFILE}"; Description: "Open gout now"; Flags: postinstall nowait skipifsilent

[Code]
function NeedsAddPath(Dir: string): Boolean;
var
  Path: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Path) + ';') = 0;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Path, Dir: string;
  At: Integer;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path) then
    exit;
  Dir := ExpandConstant('{app}');
  At := Pos(';' + Uppercase(Dir), Uppercase(Path));
  if At > 0 then
  begin
    Delete(Path, At, Length(Dir) + 1);
    RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path);
  end;
end;
