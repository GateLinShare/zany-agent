#!/usr/bin/env bash
# 本地构建一个 Release 二进制，不负责发布。
# GitHub Actions 的 .github/workflows/release.yml 会把这些二进制上传到
# GitHub Release，再发布 npm/main 里的安装器到 GitHub Packages。
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET="${1:?usage: release.sh <linux-x64|darwin-arm64|main>}"
VERSION=$(python3 -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
OUT=dist
mkdir -p "$OUT"
EXE=""; SEP=":"
case "$TARGET" in win32-*) EXE=".exe"; SEP=";" ;; esac
uv run pyinstaller --onefile --name zany \
  --collect-all textual --collect-all croniter \
  --add-data "styles${SEP}styles" --add-data "skills${SEP}skills" \
  --add-data "env.example${SEP}." \
  tui.py
cp "dist/zany$EXE" "$OUT/zany-$TARGET$EXE"
echo "built $OUT/zany-$TARGET$EXE (version $VERSION)"
