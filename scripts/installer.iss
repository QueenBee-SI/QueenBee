; QueenBee's Windows installer (Inno Setup 6): one setup.exe that installs the built app for the current user, no admin
; rights, with a Start menu entry, an optional desktop shortcut and an uninstaller. Built after scripts\build-exe.ps1:
;
;   ISCC.exe /DVersion=0.1.1 scripts\installer.iss        ->  dist\QueenBee-0.1.1-setup.exe
;
; The zip of the same build stays available for people who prefer no installer. Neither is code-signed yet, so Windows
; SmartScreen shows "Windows protected your PC" once ("More info", "Run anyway").

#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId={{7C1F2C8E-9B5B-4C1E-9F2A-0B0E0E0E0111}
AppName=QueenBee
AppVersion={#Version}
AppVerName=QueenBee {#Version}
AppPublisher=QueenBee
AppPublisherURL=https://github.com/QueenBee-SI/QueenBee
VersionInfoVersion={#Version}
VersionInfoProductName=QueenBee
VersionInfoDescription=QueenBee installer
VersionInfoCompany=QueenBee
DefaultDirName={localappdata}\Programs\QueenBee
DefaultGroupName=QueenBee
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=QueenBee-{#Version}-setup
SetupIconFile=..\queenbee\assets\queenbee.ico
UninstallDisplayIcon={app}\QueenBee.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Put a QueenBee shortcut on the desktop"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\QueenBee\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\QueenBee"; Filename: "{app}\QueenBee.exe"
Name: "{group}\Stop every QueenBee worker"; Filename: "{app}\QueenBee.exe"; Parameters: "--stop-all"
Name: "{userdesktop}\QueenBee"; Filename: "{app}\QueenBee.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\QueenBee.exe"; Description: "Start QueenBee now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{app}\QueenBee.exe"; Parameters: "--stop-all"; Flags: runhidden; RunOnceId: "stopall"
