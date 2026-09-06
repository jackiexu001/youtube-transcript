#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${SCRIPT_DIR:h}"
BUILD_DIR="$PROJECT_ROOT/.build"
VENV_DIR="$BUILD_DIR/macos-arm64-venv"
YTDLP_FILE="$BUILD_DIR/downloads/yt-dlp_macos"
FFMPEG_FILE="$BUILD_DIR/downloads/ffmpeg_macos"
BACKEND_FILE="$BUILD_DIR/backend-dist/youtube-caption-backend"
RELEASE_DIR="$PROJECT_ROOT/release"
APP_VERSION="$(plutil -extract CFBundleShortVersionString raw "$SCRIPT_DIR/Info.plist")"
VOLUME_NAME="YouTube Transcript $APP_VERSION"
DMG_ROOT="$(mktemp -d /private/tmp/youtube-caption-dmg.XXXXXX)"
DMG_WORK="$(mktemp -d /private/tmp/youtube-caption-layout.XXXXXX)"
DMG_MOUNT="$(mktemp -d /private/tmp/youtube-caption-mount.XXXXXX)"
BACKGROUND_TOOL="$DMG_ROOT/MakeDMGBackground"

cleanup() {
  hdiutil detach "$DMG_MOUNT" >/dev/null 2>&1 || true
  rm -rf "$DMG_ROOT"
  rm -rf "$DMG_WORK"
  rmdir "$DMG_MOUNT" 2>/dev/null || true
}
trap cleanup EXIT

if [ ! -x "$VENV_DIR/bin/pyinstaller" ]; then
  echo "缺少本地 PyInstaller 构建环境：$VENV_DIR" >&2
  echo "请先运行：/usr/bin/python3 -m venv '$VENV_DIR'，再在其中安装 pyinstaller。" >&2
  exit 1
fi
if [ ! -x "$YTDLP_FILE" ]; then
  echo "缺少 yt-dlp 官方 macOS 通用程序：$YTDLP_FILE" >&2
  exit 1
fi
if [ ! -x "$FFMPEG_FILE" ]; then
  echo "正在从 FFmpeg 官方源码准备 macOS 通用音频组件……"
  "$SCRIPT_DIR/prepare_ffmpeg.sh"
fi
if [ ! -x "$VENV_DIR/bin/dmgbuild" ]; then
  echo "缺少 dmgbuild。请在项目构建环境安装：'$VENV_DIR/bin/python' -m pip install dmgbuild" >&2
  exit 1
fi

mkdir -p "$BUILD_DIR/backend-dist" "$BUILD_DIR/backend-work" "$BUILD_DIR/pyinstaller-cache" "$RELEASE_DIR"
if [ "${YCA_REUSE_BACKEND:-0}" != "1" ] || [ ! -x "$BACKEND_FILE" ]; then
  PYINSTALLER_CONFIG_DIR="$BUILD_DIR/pyinstaller-cache" \
    "$VENV_DIR/bin/pyinstaller" \
    --clean --noconfirm --onefile \
    --name youtube-caption-backend \
    --target-architecture universal2 \
    --hidden-import mvp.cloud_asr_mvp \
    --hidden-import mvp.fast_groq_mvp \
    --hidden-import mvp.next_round_mvp \
    --distpath "$BUILD_DIR/backend-dist" \
    --workpath "$BUILD_DIR/backend-work" \
    --specpath "$BUILD_DIR" \
    "$PROJECT_ROOT/youtube_caption.py"
fi

YCA_BACKEND_EXECUTABLE="$BACKEND_FILE" \
YCA_YTDLP_EXECUTABLE="$YTDLP_FILE" \
YCA_FFMPEG_EXECUTABLE="$FFMPEG_FILE" \
YCA_OUTPUT_APP="$DMG_ROOT/YouTube Transcript.app" \
  "$SCRIPT_DIR/build_macos_app.sh"

APP_NAME="YouTube Transcript.app"

mkdir -p "$DMG_ROOT/.background"
if [ -d /Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk ]; then
  SDK_PATH=/Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk
else
  SDK_PATH="$(xcrun --show-sdk-path)"
fi
xcrun swiftc \
  -module-cache-path "$DMG_ROOT/swift-module-cache" \
  -sdk "$SDK_PATH" \
  -framework AppKit \
  -target arm64-apple-macosx13.0 \
  -O \
  -o "$BACKGROUND_TOOL" \
  "$SCRIPT_DIR/MakeDMGBackground.swift"
"$BACKGROUND_TOOL" "$DMG_ROOT/.background/background.png"
sips -z 440 920 \
  --setProperty dpiWidth 72 \
  --setProperty dpiHeight 72 \
  "$DMG_ROOT/.background/background.png" >/dev/null
rm -rf "$BACKGROUND_TOOL" "$DMG_ROOT/swift-module-cache"

codesign --verify --deep --strict "$DMG_ROOT/$APP_NAME"

DMG_FILE="$RELEASE_DIR/YouTube-Transcript-$APP_VERSION-macOS-universal-unsigned.dmg"
rm -f "$DMG_FILE"
"$VENV_DIR/bin/dmgbuild" \
  -s "$SCRIPT_DIR/dmg_settings.py" \
  -D app="$DMG_ROOT/$APP_NAME" \
  -D background="$DMG_ROOT/.background/background.png" \
  "$VOLUME_NAME" \
  "$DMG_FILE"

# dmgbuild writes icon coordinates reliably, while current Finder versions
# create the most compatible background alias themselves.  Reopen a writable
# copy only to refresh that alias; do not let Finder rearrange the icons.
RW_DMG="$DMG_WORK/layout-rw.dmg"
FINAL_DMG="$DMG_WORK/layout-final.dmg"
hdiutil convert "$DMG_FILE" -format UDRW -o "$RW_DMG" >/dev/null
hdiutil attach -readwrite -noverify -mountpoint "$DMG_MOUNT" "$RW_DMG" >/dev/null
mkdir -p "$DMG_MOUNT/.background"
cp "$DMG_MOUNT/.background.png" "$DMG_MOUNT/.background/background.png"

osascript <<APPLESCRIPT
set mountFolder to POSIX file "$DMG_MOUNT" as alias
set backgroundFile to POSIX file "$DMG_MOUNT/.background/background.png" as alias
tell application "Finder"
  open mountFolder
  delay 1
  set installerWindow to front window
  set current view of installerWindow to icon view
  delay 1
  set toolbar visible of installerWindow to false
  set statusbar visible of installerWindow to false
  set bounds of installerWindow to {120, 120, 1040, 560}
  set background picture of icon view options of installerWindow to backgroundFile
  update mountFolder without registering applications
  delay 2
  close installerWindow
end tell
APPLESCRIPT

hdiutil detach "$DMG_MOUNT" >/dev/null
hdiutil convert "$RW_DMG" -format UDZO -o "$FINAL_DMG" >/dev/null
mv "$FINAL_DMG" "$DMG_FILE"

DMG_NAME="${DMG_FILE:t}"
(
  cd "$RELEASE_DIR"
  shasum -a 256 "$DMG_NAME" > "$DMG_NAME.sha256"
)
echo "已生成：$DMG_FILE"
echo "说明：这是免费、未公证版本；首次打开需要在系统设置的隐私与安全性中选择仍要打开。"
