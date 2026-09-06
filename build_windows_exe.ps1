param([string]$Version = "2.3.0", [switch]$SkipInstaller)
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
$Ffmpeg = Join-Path $BuildRoot "ffmpeg.exe"
Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" -OutFile $YtDlp
$YtDlpChecksums = Join-Path $BuildRoot "yt-dlp-SHA2-256SUMS"
Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS" -OutFile $YtDlpChecksums
$YtDlpChecksumLine = Get-Content $YtDlpChecksums | Where-Object { $_ -match '(^|\s)yt-dlp\.exe$' } | Select-Object -First 1
if (-not $YtDlpChecksumLine) { throw "yt-dlp checksum entry was not found." }
$ExpectedYtDlpHash = ($YtDlpChecksumLine -split '\s+')[0].ToLowerInvariant()
$ActualYtDlpHash = (Get-FileHash $YtDlp -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ActualYtDlpHash -ne $ExpectedYtDlpHash) { throw "yt-dlp checksum verification failed." }
$FfmpegZip = Join-Path $BuildRoot "ffmpeg-n9.0-latest-win64-lgpl-9.0.zip"
$FfmpegExtract = Join-Path $BuildRoot "ffmpeg-extract"
Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n9.0-latest-win64-lgpl-9.0.zip" -OutFile $FfmpegZip
$FfmpegChecksums = Join-Path $BuildRoot "ffmpeg-checksums.sha256"
Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256" -OutFile $FfmpegChecksums
$FfmpegArchiveName = Split-Path -Leaf $FfmpegZip
$FfmpegChecksumLine = Get-Content $FfmpegChecksums | Where-Object { $_ -match ([regex]::Escape($FfmpegArchiveName) + '$') } | Select-Object -First 1
if (-not $FfmpegChecksumLine) { throw "FFmpeg checksum entry was not found." }
$ExpectedFfmpegHash = ($FfmpegChecksumLine -split '\s+')[0].ToLowerInvariant()
$ActualFfmpegHash = (Get-FileHash $FfmpegZip -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ActualFfmpegHash -ne $ExpectedFfmpegHash) { throw "FFmpeg archive checksum verification failed." }
Remove-Item -Recurse -Force $FfmpegExtract -ErrorAction SilentlyContinue
Expand-Archive -Path $FfmpegZip -DestinationPath $FfmpegExtract -Force
$FfmpegSource = Get-ChildItem $FfmpegExtract -Filter "ffmpeg.exe" -Recurse | Select-Object -First 1
if (-not $FfmpegSource) { throw "FFmpeg executable was not found in the downloaded archive." }
Copy-Item $FfmpegSource.FullName $Ffmpeg -Force
$FfmpegEncoders = & $Ffmpeg -hide_banner -encoders 2>&1
if (-not ($FfmpegEncoders -match '\saac\s')) { throw "Downloaded FFmpeg is missing the AAC encoder." }
Remove-Item -Recurse -Force $Dist -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Dist | Out-Null
& $VenvPython -m PyInstaller --noconfirm --clean --onefile --console --name "youtube-caption-backend" --hidden-import mvp.cloud_asr_mvp --hidden-import mvp.fast_groq_mvp --hidden-import mvp.next_round_mvp --distpath $Dist --workpath "$BuildRoot\work-backend" --specpath $BuildRoot "youtube_caption.py"
if ($LASTEXITCODE -ne 0) { throw "Backend packaging failed." }
& $VenvPython -m PyInstaller --noconfirm --clean --onefile --windowed --name "YouTube Transcript" --icon $Icon --hidden-import psutil --distpath $Dist --workpath "$BuildRoot\work-app" --specpath $BuildRoot "windows\YouTubeCaptionApp.py"
if ($LASTEXITCODE -ne 0) { throw "Desktop application packaging failed." }
Copy-Item $YtDlp (Join-Path $Dist "yt-dlp.exe") -Force
Copy-Item $Ffmpeg (Join-Path $Dist "ffmpeg.exe") -Force
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
[System.IO.File]::WriteAllText(
    $Installer.FullName + ".sha256",
    "$Hash  $($Installer.Name)",
    [System.Text.Encoding]::ASCII
)
Write-Host "Done: $($Installer.FullName)"
Write-Host "SHA-256: $Hash"
