#!/usr/bin/env bash
# 浏览器安装: 自带 chrome-headless-shell zip 解压成 playwright 缓存布局 + uv sync 装 playwright 包。
# 对应关系: chromium_headless_shell-1228 <-> playwright 1.61.0(与 hermes-space/deploy_hermes_agent 同源)。
# 运行时 core/browser.py 会把 PLAYWRIGHT_BROWSERS_PATH 指到 .pw-browsers/。
set -euo pipefail
cd "$(dirname "$0")"

BUILD=chromium_headless_shell-1228

case "$(uname -m)" in
  x86_64)          ZIP=assets/chrome-headless-shell-linux64.zip ;;
  aarch64|arm64)   ZIP=assets/chrome-headless-shell-linux-arm64.zip ;;
  *) echo "不支持的架构: $(uname -m)"; exit 1 ;;
esac

if [ ! -f "$ZIP" ]; then
  echo "缺少 $ZIP —— 从 hermes-space/deploy_hermes_agent/assets/ 拷一份进来, 或到"
  echo "  https://playwright.azureedge.net/builds/chromium/ 下对应架构的 chrome-headless-shell"
  exit 1
fi

echo "[1/3] 解压 $ZIP -> .pw-browsers/$BUILD/"
mkdir -p ".pw-browsers/$BUILD"
unzip -oq "$ZIP" -d ".pw-browsers/$BUILD"   # zip 顶层目录名(chrome-headless-shell-linux*)正是 playwright 要的子目录名
chmod +x ".pw-browsers/$BUILD"/*/chrome-headless-shell

echo "[2/3] uv sync (playwright==1.61.0, 走 tuna 源)"
uv sync

echo "[3/3] 验证: headless 打开 example.com"
uv run python -m core.browser --selftest

echo "安装完成。"
