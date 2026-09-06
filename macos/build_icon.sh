#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
WORK_DIR="$(mktemp -d /private/tmp/yca-icon.XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

xcrun swiftc -module-cache-path "$WORK_DIR/module-cache" -framework AppKit -o "$WORK_DIR/MakeAppIcon" "$SCRIPT_DIR/MakeAppIcon.swift"
"$WORK_DIR/MakeAppIcon" "$SCRIPT_DIR/AppIcon.png" "$SCRIPT_DIR/AppIcon.icns"
echo "已生成：$SCRIPT_DIR/AppIcon.png"
echo "已生成：$SCRIPT_DIR/AppIcon.icns"
