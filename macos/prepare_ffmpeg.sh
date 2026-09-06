#!/bin/zsh
set -euo pipefail

# Build one redistributable LGPL FFmpeg executable from the official source.
# Only FFmpeg's built-in codecs are enabled; the AI pipeline uses AAC/M4A, so
# libmp3lame and other optional GPL/non-free libraries are not required.

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${SCRIPT_DIR:h}"
BUILD_DIR="$PROJECT_ROOT/.build"
DOWNLOAD_DIR="$BUILD_DIR/downloads"
VERSION="9.0.1"
SOURCE_ARCHIVE="$DOWNLOAD_DIR/ffmpeg-$VERSION.tar.xz"
SOURCE_URL="https://ffmpeg.org/releases/ffmpeg-$VERSION.tar.xz"
EXPECTED_SHA256="cf38e0e28c7e5605942c4a77755349b0145804a397af37eb1fb4c77cb237f635"
OUTPUT="$DOWNLOAD_DIR/ffmpeg_macos"
WORK_DIR="$(mktemp -d /private/tmp/yca-ffmpeg.XXXXXX)"

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

mkdir -p "$DOWNLOAD_DIR"
if [ ! -f "$SOURCE_ARCHIVE" ]; then
  curl -fL --retry 3 --connect-timeout 20 "$SOURCE_URL" -o "$SOURCE_ARCHIVE"
fi

ACTUAL_SHA256="$(shasum -a 256 "$SOURCE_ARCHIVE" | awk '{print $1}')"
if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then
  echo "FFmpeg 源码校验失败；不会继续构建。" >&2
  exit 1
fi

tar -xf "$SOURCE_ARCHIVE" -C "$WORK_DIR"
SOURCE_DIR="$WORK_DIR/ffmpeg-$VERSION"
CPU_COUNT="$(sysctl -n hw.logicalcpu 2>/dev/null || echo 4)"

build_arch() {
  local architecture="$1"
  local build_root="$WORK_DIR/build-$architecture"
  local architecture_options=()
  if [ "$architecture" = "x86_64" ]; then
    # The Apple command-line tools do not ship NASM. FFmpeg's C routines are
    # more than fast enough for our low-bitrate speech-only conversion.
    architecture_options+=(--disable-x86asm)
  fi
  mkdir -p "$build_root"
  cd "$build_root"
  "$SOURCE_DIR/configure" \
    --target-os=darwin \
    --arch="$architecture" \
    --cc="clang -arch $architecture" \
    --extra-cflags="-mmacosx-version-min=13.0" \
    --extra-ldflags="-mmacosx-version-min=13.0" \
    --disable-doc \
    --disable-debug \
    --disable-ffplay \
    --disable-ffprobe \
    --disable-network \
    --disable-autodetect \
    --disable-everything \
    --disable-shared \
    --enable-static \
    --enable-small \
    --enable-protocol=file,pipe \
    --enable-demuxer=matroska,mov,ogg \
    --enable-muxer=segment,mov \
    --enable-decoder=opus,aac,vorbis \
    --enable-encoder=aac \
    --enable-parser=opus,aac,vorbis \
    --enable-filter=aresample,aformat,concat \
    "${architecture_options[@]}"
  make -j"$CPU_COUNT" ffmpeg >"$WORK_DIR/make-$architecture.log" 2>&1
}

build_arch arm64
build_arch x86_64
lipo -create "$WORK_DIR/build-arm64/ffmpeg" "$WORK_DIR/build-x86_64/ffmpeg" -output "$WORK_DIR/ffmpeg_macos"
chmod 755 "$WORK_DIR/ffmpeg_macos"

if otool -L "$WORK_DIR/ffmpeg_macos" | grep -E '/opt/homebrew|/usr/local' >/dev/null; then
  echo "FFmpeg 意外依赖构建机上的第三方动态库；不会发布。" >&2
  exit 1
fi
if ! "$WORK_DIR/ffmpeg_macos" -hide_banner -encoders 2>/dev/null | grep -q ' aac '; then
  echo "FFmpeg 构建缺少 AAC 编码器；不会发布。" >&2
  exit 1
fi

mv "$WORK_DIR/ffmpeg_macos" "$OUTPUT"
echo "已生成 LGPL 通用 FFmpeg：$OUTPUT"
file "$OUTPUT"
