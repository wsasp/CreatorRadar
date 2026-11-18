[Setup]
AppId=CreatorRadar2.Workstation
AppName=CreatorRadar
AppVersion={#Version}
DefaultDirName={localappdata}\Programs\CreatorRadar
DefaultGroupName=CreatorRadar
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputRoot}
OutputBaseFilename={#OutputName}
Compression=zip/1
UninstallDisplayIcon={app}\runtime\python\python.exe

[Files]
Source: "{#PayloadRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\CreatorRadar"; Filename: "{app}\runtime\python\python.exe"; Parameters: "-I -X utf8 -B ""{app}\packaging\component.py"" backend ""{app}\packaging\launch.py"""; WorkingDir: "{app}"

[Run]
Filename: "{app}\runtime\python\python.exe"; Parameters: "-I -X utf8 -B ""{app}\packaging\component.py"" backend ""{app}\packaging\launch.py"""; WorkingDir: "{app}"; Description: "Start CreatorRadar"; Flags: postinstall nowait skipifsilent
