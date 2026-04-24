; ── Inno Setup script for Image Deduper ──────────────────────────────────────
; Build with: ISCC.exe installer\installer.iss   (from repo root)
; Requires the PyInstaller output at dist\ImageDeduper\
; ─────────────────────────────────────────────────────────────────────────────

#define AppName      "Image Deduper"
#define AppVersion   "1.1.13"
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
; App icon at install root — shortcuts reference this directly so Windows Start menu
; and taskbar always display the full multi-resolution .ico instead of extracting
; from the EXE (which can silently fall back to a generic icon on some Windows builds)
Source: "..\assets\app.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\app.ico"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}";   Filename: "{app}\{#AppExeName}"; Tasks: desktopicon; IconFilename: "{app}\app.ico"

[Run]
Filename: "{app}\{#AppExeName}"; \
    Description: "Launch {#AppName}"; \
    Flags: nowait postinstall skipifsilent

; ── User data is intentionally NOT listed in [UninstallDelete] ───────────────
; Settings/history and the hash Library live in separate AppData locations and
; are handled individually in the [Code] section below so the user can choose
; to keep or delete each one independently.

[Code]

// ── Ask about each data store separately during uninstall ─────────────────────
//
//  1. Settings & scan history  →  %APPDATA%\ImageDeduper\
//  2. Hash Library             →  %APPDATA%\Katador\ImageDeduper\library\
//
// Both questions default to "No" (keep data) so a quick Enter-through of the
// uninstaller never accidentally deletes user data.

function InitializeUninstall(): Boolean;
var
  SettingsPath: String;
  LibraryPath:  String;
begin
  Result := True;

  SettingsPath := ExpandConstant('{userappdata}\{#AppDataName}');
  LibraryPath  := ExpandConstant('{userappdata}\Katador\{#AppDataName}\library');

  // ── Question 1: settings & scan history ──────────────────────────────────
  if DirExists(SettingsPath) then
  begin
    case SuppressibleMsgBox(
      'Do you want to remove your settings and scan history?' + #13#10 + #13#10 +
      'Selecting "Yes" will permanently delete:' + #13#10 +
      '  • Settings and preferences' + #13#10 +
      '  • Scan history and logs' + #13#10 + #13#10 +
      'Selecting "No" will keep your data so it is available if you reinstall.',
      mbConfirmation, MB_YESNO, IDNO)
    of
      IDYES: DelTree(SettingsPath, True, True, True);
      // IDNO: preserved
    end;
  end;

  // ── Question 2: hash Library ──────────────────────────────────────────────
  if DirExists(LibraryPath) then
  begin
    case SuppressibleMsgBox(
      'Do you want to remove your image hash Library?' + #13#10 + #13#10 +
      'The Library stores pre-computed image hashes so repeat scans skip' + #13#10 +
      're-hashing unchanged files.  It can be several hundred MB for large' + #13#10 +
      'photo collections.' + #13#10 + #13#10 +
      'Selecting "Yes" will permanently delete all cached hashes.' + #13#10 +
      'Selecting "No" will keep the Library — if you reinstall, your next' + #13#10 +
      'scan will reuse it immediately without re-hashing anything.',
      mbConfirmation, MB_YESNO, IDNO)
    of
      IDYES: DelTree(LibraryPath, True, True, True);
      // IDNO: preserved
    end;
  end;
end;
