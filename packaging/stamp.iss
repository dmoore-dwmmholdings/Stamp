; Inno Setup script for Stamp.
;
; Build the application first, then compile this:
;
;   python -m uv run pyinstaller packaging/stamp.spec --noconfirm
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\stamp.iss
;
; The result is packaging\dist\Stamp-<version>-Setup.exe, which is one file to
; give to a tester.  It installs for the current user only, thus it asks for no
; administrator password.

#define AppName "Stamp"
#define AppVersion "0.2.0"
#define AppPublisher "DWMM Holdings"
#define AppExeName "Stamp.exe"
#define SourceDir "..\build\dist\Stamp"

[Setup]
AppId={{8F3C6A21-7B54-4E9D-9C2A-1D6E5B0A7F31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename={#AppName}-{#AppVersion}-Setup
SetupIconFile=assets\stamp.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
; Per-user, so a tester needs no administrator password.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Put an icon on the desktop"; GroupDescription: "More shortcuts:"
Name: "associate"; Description: "Open .stamp project files with Stamp"; GroupDescription: "File types:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; A .stamp project opens with Stamp when the user asked for that.
Root: HKA; Subkey: "Software\Classes\.stamp"; ValueType: string; ValueName: ""; ValueData: "Stamp.Project"; Flags: uninsdeletevalue; Tasks: associate
Root: HKA; Subkey: "Software\Classes\Stamp.Project"; ValueType: string; ValueName: ""; ValueData: "Stamp project"; Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\Stamp.Project\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"; Tasks: associate
Root: HKA; Subkey: "Software\Classes\Stamp.Project\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: associate

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent

; The log directory is deliberately NOT deleted here.  It holds the crash reports
; and the bug reports, and a tester who removes Stamp may still have one to send.
; An empty directory is tidied, a directory with reports in it stays.
[UninstallDelete]
Type: dirifempty; Name: "{localappdata}\Stamp\logs"
Type: dirifempty; Name: "{localappdata}\Stamp"
