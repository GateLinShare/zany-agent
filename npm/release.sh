#!/usr/bin/env bash
# 发布一个 npm 包: bash npm/release.sh linux-x64|darwin-arm64|main
# 版本单源 pyproject.toml; CI 由 .github/workflows/release.yml 按矩阵调用。
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET="${1:?usage: release.sh <linux-x64|darwin-arm64|main>}"
VERSION=$(python3 -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
OUT=tmp/npm
mkdir -p "$OUT"

if [ "$TARGET" = main ]; then
  DIR="$OUT/zany-agent"
  mkdir -p "$DIR/bin"
  cp npm/main/package.json "$DIR/"
  cp npm/main/bin/zany.js "$DIR/bin/"
  cp env.example "$DIR/"
  cp npm/main/README.md "$DIR/" 2>/dev/null || echo "zany-agent" > "$DIR/README.md"
else
  # win32: 产物名 zany.exe; --add-data 分隔符平台差异(linux/macos=":", windows=";", 路径含 C: 盘符)
  EXE=""; SEP=":"
  case "$TARGET" in win32-*) EXE=".exe"; SEP=";" ;; esac
  uv run pyinstaller --onefile --name zany \
    --collect-all textual --collect-all croniter \
    --add-data "styles${SEP}styles" --add-data "skills${SEP}skills" \
    tui.py
  DIR="$OUT/zany-agent-$TARGET"
  mkdir -p "$DIR/bin"
  cp dist/zany$EXE "$DIR/bin/zany$EXE"
  OS="${TARGET%%-*}"; CPU="${TARGET##*-}"
  cat > "$DIR/package.json" <<EOF
{
  "name": "zany-agent-$TARGET",
  "version": "$VERSION",
  "description": "zany binary for $TARGET",
  "license": "Apache-2.0",
  "os": ["$OS"],
  "cpu": ["$CPU"],
  "files": ["bin/"]
}
EOF
fi

# 版本号统一写回(主包 optionalDependencies 与平台包版本必须与 pyproject 一致)
node -e "const fs=require('fs');const f=process.argv[1];const j=JSON.parse(fs.readFileSync(f));j.version='$VERSION';if(j.optionalDependencies)for(const k of Object.keys(j.optionalDependencies))j.optionalDependencies[k]='$VERSION';fs.writeFileSync(f,JSON.stringify(j,null,2)+'\n')" "$DIR/package.json"

npm publish "$DIR" --registry https://registry.npmjs.org
echo "published zany-agent($TARGET)@$VERSION"
