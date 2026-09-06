#ifndef AppVersion
  #define AppVersion "2.3.1"
#endif
#ifndef ProjectRoot
  #define ProjectRoot ".."
#endif
[Setup]
AppId={{4C7CBBC5-9921-48AF-90C8-0D77A738C50D}
AppName=YouTube Transcript
AppVersion={#AppVersion}
AppPublisher=YouTube Transcript
DefaultDirName={localappdata}\Programs\YouTube Transcript
DefaultGroupName=YouTube Transcript
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#ProjectRoot}\release\windows
OutputBaseFilename=YouTube-Transcript-{#AppVersion}-Windows-x64-Setup
SetupIconFile={#ProjectRoot}\windows\AppIcon.ico
UninstallDisplayIcon={app}\YouTube Transcript.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile={#ProjectRoot}\LICENSE
CloseApplications=yes
RestartApplications=no
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项："; Flags: unchecked
[Files]
Source: "{#ProjectRoot}\.build\windows\dist\YouTube Transcript.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\.build\windows\dist\youtube-caption-backend.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\.build\windows\dist\yt-dlp.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\.build\windows\dist\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\WINDOWS_DOWNLOAD_GUIDE.txt"; DestDir: "{app}"; Flags: ignoreversion
[Icons]
Name: "{group}\YouTube Transcript"; Filename: "{app}\YouTube Transcript.exe"
Name: "{autodesktop}\YouTube Transcript"; Filename: "{app}\YouTube Transcript.exe"; Tasks: desktopicon
[Run]
Filename: "{app}\YouTube Transcript.exe"; Description: "启动 YouTube Transcript"; Flags: nowait postinstall skipifsilent
