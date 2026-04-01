; ── Inno Setup script for Image Deduper ──────────────────────────────────────
; Build with: ISCC.exe installer\installer.iss   (from repo root)
; Requires the PyInstaller output at dist\ImageDeduper\
; ─────────────────────────────────────────────────────────────────────────────

#define AppName      "Image Deduper"
#define AppVersion   "1.0.2"
#define AppPublisher "Katador.net"
#define AppURL       "https://github.com/master-alucard/photo_duplicates_removal"
#define AppEmail     "office@katador.net"
#define AppExeName   "ImageDeduper.exe"
#define AppDataName  "ImageDeduper"

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
; Install to Program Files — requires admin rights
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
; Admin required for Program Files
PrivilegesRequired=admin
OutputDir=installer_output
OutputBaseFilename=ImageDeduper-Setup-{#AppVersion}
SetupIconFile=..\assets\app.ico
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
Source: "..\dist\ImageDeduper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}";   Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; \
    Description: "Launch {#AppName}"; \
    Flags: nowait postinstall skipifsilent

; ── User data is intentionally NOT listed in [UninstallDelete] ───────────────
; Removal of %APPDATA%\ImageDeduper is handled in the [Code] section below,
; where we ask the user whether to keep or delete their settings.

[Code]

// ── Ask "keep or remove user data?" during uninstall ─────────────────────────
var
  DataPage: TInputOptionWizardPage;

procedure InitializeUninstallProgressForm();
begin
  // Nothing needed here — page is shown in InitializeUninstall
end;

function InitializeUninstall(): Boolean;
var
  AppDataPath: String;
begin
  Result := True;
  AppDataPath := ExpandConstant('{userappdata}\{#AppDataName}');

  // Only show the question if the folder actually exists
  if DirExists(AppDataPath) then
  begin
    case SuppressibleMsgBox(
      'Do you want to remove your personal settings and scan history?' + #13#10 + #13#10 +
      'Selecting "Yes" will permanently delete:' + #13#10 +
      '  • Settings and preferences' + #13#10 +
      '  • Scan history and logs' + #13#10 + #13#10 +
      'Selecting "No" will keep your data so it is available if you reinstall.',
      mbConfirmation, MB_YESNO, IDNO)
    of
      IDYES:
        begin
          DelTree(AppDataPath, True, True, True);
        end;
      // IDNO: do nothing — data is preserved
    end;
  end;
end;
