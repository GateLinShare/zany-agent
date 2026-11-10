#!/usr/bin/env bash
# 本地构建 zany 单文件二进制 → release/dist/zany-<target>[.exe]
# 用法: release/build.sh [target]
#   无参 = 自动按当前平台构建; target 取值同 release.yml:
#   linux-x64 | linux-arm64 | darwin-arm64 | darwin-x64 | win32-x64
set -euo pipefail
cd "$(dirname "$0")/.."
DIST=release/dist
WORK=release/build
mkdir -p "$DIST" "$WORK"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)              TARGET=linux-x64 ;;
  Linux-aarch64|Linux-arm64) TARGET=linux-arm64 ;;
  Darwin-arm64)              TARGET=darwin-arm64 ;;
  Darwin-x86_64)             TARGET=darwin-x64 ;;
  MINGW*|MSYS*|CYGWIN*)      TARGET=win32-x64 ;;
  *) echo "无法识别平台 $(uname -s)-$(uname -m), 请显式传 target" >&2; exit 1 ;;
esac
TARGET="${1:-$TARGET}"
EXE=""; SEP=":"
case "$TARGET" in win32-*) EXE=".exe"; SEP=";" ;; esac
VERSION=$(python3 -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")

uv sync   # dev 组带 pyinstaller
uv run pyinstaller --onefile --name zany \
  --distpath "$DIST" --workpath "$WORK" \
  --collect-all textual --collect-all croniter \
  --add-data "styles${SEP}styles" --add-data "skills${SEP}skills" \
  --add-data "env.example${SEP}." \
  tui.py
mv "$DIST/zany$EXE" "$DIST/zany-$TARGET$EXE"
echo "built $DIST/zany-$TARGET$EXE (v$VERSION)"
