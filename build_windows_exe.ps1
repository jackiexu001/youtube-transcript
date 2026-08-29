param([string]$Version = "2.1.1", [switch]$SkipInstaller)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
if ($env:OS -ne "Windows_NT") { throw "Build this Windows installer on Windows 10/11 or GitHub Actions." }
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw "Python 3.11 is required on the build machine." }
$BuildRoot = Join-Path $ProjectRoot ".build\windows"
$Venv = Join-Path $BuildRoot "venv"
$Dist = Join-Path $BuildRoot "dist"
$Release = Join-Path $ProjectRoot "release\windows"
New-Item -ItemType Directory -Force $BuildRoot, $Release | Out-Null
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) { & py -3.11 -m venv $Venv }
$VenvPython = Join-Path $Venv "Scripts\python.exe"
& $VenvPython -m pip install --disable-pip-version-check --upgrade pip pyinstaller psutil pillow
& $VenvPython "windows\make_icon.py"
$Icon = Join-Path $ProjectRoot "windows\AppIcon.ico"
$YtDlp = Join-Path $BuildRoot "yt-dlp.exe"
Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" -OutFile $YtDlp
Remove-Item -Recurse -Force $Dist -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Dist | Out-Null
& $VenvPython -m PyInstaller --noconfirm --clean --onefile --console --name "youtube-caption-backend" --distpath $Dist --workpath "$BuildRoot\work-backend" --specpath $BuildRoot "youtube_caption.py"
if ($LASTEXITCODE -ne 0) { throw "Backend packaging failed." }
& $VenvPython -m PyInstaller --noconfirm --clean --onefile --windowed --name "YouTube Caption Archive" --icon $Icon --hidden-import psutil --distpath $Dist --workpath "$BuildRoot\work-app" --specpath $BuildRoot "windows\YouTubeCaptionApp.py"
if ($LASTEXITCODE -ne 0) { throw "Desktop application packaging failed." }
Copy-Item $YtDlp (Join-Path $Dist "yt-dlp.exe") -Force
if ($SkipInstaller) { Write-Host "Windows application built: $Dist"; exit 0 }
$Iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $Iscc) {
  $Iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc) { throw "Inno Setup 6 (ISCC.exe) was not found." }
& $Iscc "/DAppVersion=$Version" "/DProjectRoot=$ProjectRoot" "windows\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed." }
$Installer = Get-ChildItem $Release -Filter "*-Setup.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Installer) { throw "The installer was not generated." }
$Hash = (Get-FileHash $Installer.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -Encoding ascii -Path ($Installer.FullName + ".sha256") -Value "$Hash  $($Installer.Name)"
Write-Host "Done: $($Installer.FullName)"
Write-Host "SHA-256: $Hash"
