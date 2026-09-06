#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${SCRIPT_DIR:h}"
SOURCE_FILE="$SCRIPT_DIR/YouTubeCaptionApp.swift"
INFO_FILE="$SCRIPT_DIR/Info.plist"
ICON_FILE="$SCRIPT_DIR/AppIcon.icns"
BACKEND_FILE="$PROJECT_ROOT/youtube_caption.py"
BACKEND_EXECUTABLE="${YCA_BACKEND_EXECUTABLE:-$PROJECT_ROOT/.build/backend-dist/youtube-caption-backend}"
YTDLP_EXECUTABLE="${YCA_YTDLP_EXECUTABLE:-$PROJECT_ROOT/.build/downloads/yt-dlp_macos}"
FFMPEG_EXECUTABLE="${YCA_FFMPEG_EXECUTABLE:-$PROJECT_ROOT/.build/downloads/ffmpeg_macos}"
# Keep local development builds inside a hidden, non-indexed products folder.
# A root-level .app is picked up by Spotlight (and cloud-drive clients) as a
# second installed copy, which confuses users after they install the DMG build.
OUTPUT_APP="${YCA_OUTPUT_APP:-$PROJECT_ROOT/.build/Products.noindex/YouTube Transcript.app}"
BUILD_ROOT="$(mktemp -d /private/tmp/youtube-caption-macos.XXXXXX)"
STAGED_APP="$BUILD_ROOT/YouTube Transcript.app"
STAGED_MACOS="$STAGED_APP/Contents/MacOS"
STAGED_RESOURCES="$STAGED_APP/Contents/Resources"

cleanup() {
  rm -rf "$BUILD_ROOT"
}
trap cleanup EXIT

mkdir -p "$STAGED_MACOS" "$STAGED_RESOURCES"

# The current machine can have more than one Command Line Tools SDK.  Prefer
# the stable SDK when present; otherwise use the active developer SDK.
if [ -d /Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk ]; then
  SDK_PATH=/Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk
else
  SDK_PATH="$(xcrun --show-sdk-path)"
fi

SWIFTC="$(xcrun --find swiftc)"
ARM_BINARY="$BUILD_ROOT/YouTubeCaptionApp-arm64"
INTEL_BINARY="$BUILD_ROOT/YouTubeCaptionApp-x86_64"
MODULE_CACHE_ARM="$BUILD_ROOT/module-cache-arm64"
MODULE_CACHE_INTEL="$BUILD_ROOT/module-cache-x86_64"

"$SWIFTC" \
  -module-cache-path "$MODULE_CACHE_ARM" \
  -sdk "$SDK_PATH" \
  -framework AppKit \
  -framework Security \
  -target arm64-apple-macosx13.0 \
  -O \
  -o "$ARM_BINARY" \
  "$SOURCE_FILE"

"$SWIFTC" \
  -module-cache-path "$MODULE_CACHE_INTEL" \
  -sdk "$SDK_PATH" \
  -framework AppKit \
  -framework Security \
  -target x86_64-apple-macosx13.0 \
  -O \
  -o "$INTEL_BINARY" \
  "$SOURCE_FILE"

lipo -create "$ARM_BINARY" "$INTEL_BINARY" -output "$STAGED_MACOS/YouTubeCaptionApp"
chmod 755 "$STAGED_MACOS/YouTubeCaptionApp"
cp "$INFO_FILE" "$STAGED_APP/Contents/Info.plist"
cp "$ICON_FILE" "$STAGED_RESOURCES/AppIcon.icns"
cp "$BACKEND_FILE" "$STAGED_RESOURCES/youtube_caption.py"
mkdir -p "$STAGED_RESOURCES/mvp"
cp "$PROJECT_ROOT/mvp/__init__.py" "$STAGED_RESOURCES/mvp/__init__.py"
cp "$PROJECT_ROOT/mvp/cloud_asr_mvp.py" "$STAGED_RESOURCES/mvp/cloud_asr_mvp.py"
cp "$PROJECT_ROOT/mvp/fast_groq_mvp.py" "$STAGED_RESOURCES/mvp/fast_groq_mvp.py"
cp "$PROJECT_ROOT/mvp/next_round_mvp.py" "$STAGED_RESOURCES/mvp/next_round_mvp.py"
cp "$PROJECT_ROOT/LICENSE" "$STAGED_RESOURCES/LICENSE.txt"
cp "$PROJECT_ROOT/THIRD_PARTY_NOTICES.md" "$STAGED_RESOURCES/THIRD_PARTY_NOTICES.md"
cp "$PROJECT_ROOT/MAC_DOWNLOAD_GUIDE.txt" "$STAGED_RESOURCES/MAC_DOWNLOAD_GUIDE.txt"
if [ -x "$BACKEND_EXECUTABLE" ]; then
  cp "$BACKEND_EXECUTABLE" "$STAGED_MACOS/youtube-caption-backend"
  chmod 755 "$STAGED_MACOS/youtube-caption-backend"
fi
if [ -x "$YTDLP_EXECUTABLE" ]; then
  cp "$YTDLP_EXECUTABLE" "$STAGED_RESOURCES/yt-dlp_macos"
  chmod 755 "$STAGED_RESOURCES/yt-dlp_macos"
fi
if [ -x "$FFMPEG_EXECUTABLE" ]; then
  cp "$FFMPEG_EXECUTABLE" "$STAGED_RESOURCES/ffmpeg_macos"
  chmod 755 "$STAGED_RESOURCES/ffmpeg_macos"
fi

plutil -lint "$STAGED_APP/Contents/Info.plist"
xattr -cr "$STAGED_APP"
codesign --force --deep --sign - "$STAGED_APP"

# Swap only after a complete build, so a compile failure never destroys the
# currently working application.
PREVIOUS_APP="$BUILD_ROOT/previous.app"
mkdir -p "${OUTPUT_APP:h}"
if [ -d "$OUTPUT_APP" ]; then
  mv "$OUTPUT_APP" "$PREVIOUS_APP"
fi
if ! mv "$STAGED_APP" "$OUTPUT_APP"; then
  if [ -d "$PREVIOUS_APP" ]; then
    mv "$PREVIOUS_APP" "$OUTPUT_APP"
  fi
  exit 1
fi

# Finder can briefly restore legacy metadata at the destination path when an
# older script app is replaced.  Clear and sign with a short retry window.
SIGNED=0
for ATTEMPT in 1 2 3; do
  xattr -cr "$OUTPUT_APP"
  # Finder/File Provider can immediately reattach these two root attributes
  # after the bundle is moved into Documents.  Remove them explicitly just
  # before signing; otherwise codesign reports misleading "resource fork"
  # damage even though the compiled bundle itself is valid.
  xattr -d com.apple.FinderInfo "$OUTPUT_APP" 2>/dev/null || true
  xattr -d com.apple.ResourceFork "$OUTPUT_APP" 2>/dev/null || true
  # The staged bundle was already signed successfully.  Re-signing inside a
  # File Provider folder can itself cause FinderInfo to be reattached, so only
  # verify after cleaning the destination metadata.
  if codesign --verify --deep --strict "$OUTPUT_APP"; then
    SIGNED=1
    break
  fi
  sleep 1
done
if [ "$SIGNED" -ne 1 ]; then
  echo "应用签名失败。" >&2
  mv "$OUTPUT_APP" "$BUILD_ROOT/failed.app"
  if [ -d "$PREVIOUS_APP" ]; then
    mv "$PREVIOUS_APP" "$OUTPUT_APP"
  fi
  exit 1
fi
# Do not touch the bundle after signing.  On folders managed by Finder or a
# file provider, that can immediately restore com.apple.FinderInfo and make a
# valid ad-hoc signature fail strict verification.
codesign --verify --deep --strict "$OUTPUT_APP"
echo "已生成：$OUTPUT_APP"
