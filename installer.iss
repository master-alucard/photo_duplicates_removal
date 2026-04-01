; ── Inno Setup script for Image Deduper ──────────────────────────────────────
; Build with: ISCC.exe installer.iss
; Requires the PyInstaller output at dist\ImageDeduper\
; ─────────────────────────────────────────────────────────────────────────────

#define AppName      "Image Deduper"
#define AppVersion   "1.0.0"
#define AppPublisher "Katador.net"
#define AppURL       "https://github.com/master-alucard/photo_duplicates_removal"
#define AppEmail     "office@katador.net"
#define AppExeName   "ImageDeduper.exe"

[Setup]
AppId={{E3A1F2B4-7C9D-4E5F-A123-BC456DEF7890}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
AppContact={#AppEmail}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
; Install per-user so no admin rights are required
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=installer_output
OutputBaseFilename=ImageDeduper-Setup-{#AppVersion}
SetupIconFile=assets\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
DisableWelcomePage=no
DisableProgramGroupPage=auto
; Minimum Windows version: Windows 10
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; All bundled files from PyInstaller
Source: "dist\ImageDeduper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";         Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; \
    Description: "Launch {#AppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove settings and history left in AppData when the user uninstalls
Type: filesandordirs; Name: "{userappdata}\ImageDeduper"
