#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef AppSource
  #error AppSource is required
#endif
#ifndef OutputPath
  #error OutputPath is required
#endif

[Setup]
AppId={{29012AE9-65A2-46DC-A2E5-8746B874AF92}
AppName=Telegram Manager
AppVersion={#AppVersion}
AppPublisher=Telegram Manager
AppPublisherURL=https://github.com/sswagbaggworldwide/TGMANAGE
AppUpdatesURL=https://github.com/sswagbaggworldwide/TGMANAGE/releases
DefaultDirName={localappdata}\Programs\TelegramManager
DefaultGroupName=Telegram Manager
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputPath}
OutputBaseFilename=setup
Compression=lzma2
SolidCompression=yes
SetupIconFile={#AppSource}\..\..\app.ico
UninstallDisplayIcon={app}\TelegramManager.exe
WizardStyle=modern
AppMutex=Local\TelegramManager-29012AE9-65A2-46DC-A2E5-8746B874AF92
CloseApplications=yes
CloseApplicationsFilter=TelegramManager.exe
RestartApplications=no
Uninstallable=yes
CreateUninstallRegKey=not IsTestInstall
SetupLogging=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Files]
Source: "{#AppSource}\TelegramManager.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#AppSource}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Telegram Manager"; Filename: "{app}\TelegramManager.exe"; Check: not IsTestInstall
Name: "{autodesktop}\Telegram Manager"; Filename: "{app}\TelegramManager.exe"; Tasks: desktopicon; Check: not IsTestInstall

[Run]
Filename: "{app}\TelegramManager.exe"; Description: "Запустить Telegram Manager"; Flags: nowait postinstall skipifsilent
Filename: "{app}\TelegramManager.exe"; Flags: nowait; Check: RestartAfterUpdate

[Code]
function IsTestInstall: Boolean;
begin
  Result := ExpandConstant('{param:TESTINSTALL|0}') = '1';
end;

function RestartAfterUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RESTARTAPP|0}') = '1');
end;
