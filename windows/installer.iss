#ifndef AppVersion
  #define AppVersion "2.1.1"
#endif
#ifndef ProjectRoot
  #define ProjectRoot ".."
#endif
[Setup]
AppId={{4C7CBBC5-9921-48AF-90C8-0D77A738C50D}
AppName=YouTube 字幕抓取
AppVersion={#AppVersion}
AppPublisher=YouTube Caption Archive
DefaultDirName={localappdata}\Programs\YouTube Caption Archive
DefaultGroupName=YouTube 字幕抓取
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#ProjectRoot}\release\windows
OutputBaseFilename=YouTube-Caption-Archive-{#AppVersion}-Windows-x64-Setup
SetupIconFile={#ProjectRoot}\windows\AppIcon.ico
UninstallDisplayIcon={app}\YouTube Caption Archive.exe
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
Source: "{#ProjectRoot}\.build\windows\dist\YouTube Caption Archive.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\.build\windows\dist\youtube-caption-backend.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\.build\windows\dist\yt-dlp.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\WINDOWS_DOWNLOAD_GUIDE.txt"; DestDir: "{app}"; Flags: ignoreversion
[Icons]
Name: "{group}\YouTube 字幕抓取"; Filename: "{app}\YouTube Caption Archive.exe"
Name: "{autodesktop}\YouTube 字幕抓取"; Filename: "{app}\YouTube Caption Archive.exe"; Tasks: desktopicon
[Run]
Filename: "{app}\YouTube Caption Archive.exe"; Description: "启动 YouTube 字幕抓取"; Flags: nowait postinstall skipifsilent
