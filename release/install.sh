#!/usr/bin/env bash
# 非 bash 调用(sh/dash)转交 bash 重跑: 本脚本用 pipefail 等 bash 语法
[ -n "$BASH_VERSION" ] || exec bash "$0" "$@"
# 把构建好的二进制装进系统 PATH(zany 命令)。会覆盖 deploy.sh 装的源码版 zany。
# 用法: release/install.sh [二进制路径]   无参 = 自动按当前平台找 release/dist/zany-<target>
set -euo pipefail
cd "$(dirname "$0")/.."

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)  TARGET=linux-x64 ;;
  Darwin-arm64)  TARGET=darwin-arm64 ;;
  MINGW*|MSYS*|CYGWIN*) echo "Windows 请手工拷 exe 或用 npm 安装器" >&2; exit 1 ;;
  *) TARGET=$(echo "$(uname -s)-$(uname -m)" | tr 'A-Z_' 'a-z-') ;;
esac
BIN="${1:-release/dist/zany-$TARGET}"
[ -f "$BIN" ] || { echo "找不到 $BIN, 先跑 release/build.sh" >&2; exit 1; }

PREV=$(command -v zany || true)
if [ -w /usr/local/bin ] || [ "$(id -u)" = 0 ]; then
  DEST=/usr/local/bin/zany
else
  DEST="$HOME/.local/bin/zany"; mkdir -p "$HOME/.local/bin"
fi
# 与源码部署(core/deploy/deploy.sh)互斥: 目标位上已有源码 shim(#! 开头且走 uv run)先移除
if [ -f "$DEST" ] && [ "$(head -c 2 "$DEST")" = "#!" ] && grep -qs "uv run --project" "$DEST"; then
  echo "检测到源码版 zany(core/deploy/deploy.sh 装的), 移除后装二进制"
  rm -f "$DEST"
fi
install -m 755 "$BIN" "$DEST"
echo "installed: $DEST"
if [ -n "$PREV" ] && [ "$PREV" != "$DEST" ]; then
  echo "  (覆盖了原 $PREV)"
fi
"$DEST" --help >/dev/null && echo "smoke: --help ok"
