param([string]$Version = "2.1.1", [switch]$SkipInstaller)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
if ($env:OS -ne "Windows_NT") { throw "Windows 安装包必须在 Windows 10/11 或 Windows GitHub Actions 上构建。" }
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw "构建机请先安装 Python 3.11。" }
$BuildRoot = Join-Path $ProjectRoot ".build\windows"
$Venv = Join-Path $BuildRoot "venv"
$Dist = Join-Path $BuildRoot "dist"
$Release = Join-Path $ProjectRoot "release\windows"
New-Item -ItemType Directory -Force $BuildRoot, $Release | Out-Null
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) { & py -3.11 -m venv $Venv }
$VenvPython = Join-Path $Venv "Scripts\python.exe"
& $VenvPython -m pip install --disable-pip-version-check --upgrade pip pyinstaller psutil pillow
& $VenvPython "windows\make_icon.py"
$YtDlp = Join-Path $BuildRoot "yt-dlp.exe"
Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" -OutFile $YtDlp
Remove-Item -Recurse -Force $Dist -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Dist | Out-Null
& $VenvPython -m PyInstaller --noconfirm --clean --onefile --console --name "youtube-caption-backend" --distpath $Dist --workpath "$BuildRoot\work-backend" --specpath $BuildRoot "youtube_caption.py"
& $VenvPython -m PyInstaller --noconfirm --clean --onefile --windowed --name "YouTube Caption Archive" --icon "windows\AppIcon.ico" --hidden-import psutil --distpath $Dist --workpath "$BuildRoot\work-app" --specpath $BuildRoot "windows\YouTubeCaptionApp.py"
Copy-Item $YtDlp (Join-Path $Dist "yt-dlp.exe") -Force
if ($SkipInstaller) { Write-Host "Windows 应用已生成：$Dist"; exit 0 }
$Iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $Iscc) {
  $Iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc) { throw "未找到 Inno Setup 6（ISCC.exe）。" }
& $Iscc "/DAppVersion=$Version" "/DProjectRoot=$ProjectRoot" "windows\installer.iss"
$Installer = Get-ChildItem $Release -Filter "*-Setup.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Installer) { throw "安装程序没有生成。" }
$Hash = (Get-FileHash $Installer.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -Encoding ascii -Path ($Installer.FullName + ".sha256") -Value "$Hash  $($Installer.Name)"
Write-Host "完成：$($Installer.FullName)"
Write-Host "SHA-256：$Hash"
