#!/usr/bin/env bash
# miniagent 一键部署(非 docker): uv(缺则装) + uv sync + 浏览器内核, 装完打印用法
# 用法: bash deploy.sh   (在 fresh clone 上即可跑, python/依赖均由 uv 管理)
set -euo pipefail
cd "$(dirname "$0")"

# 1/3 uv: 没有就装(官方安装器, 落 ~/.local/bin)
if command -v uv >/dev/null 2>&1; then
  echo "[1/3] uv 已有: $(command -v uv)"
else
  echo "[1/3] 安装 uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 2/3 依赖: 有 feishu extra 的分支(develop, gateway 所在)带上它; master 无此 extra, 普通 sync
echo "[2/3] uv sync$(grep -q 'feishu' pyproject.toml && echo ' --extra feishu')"
uv sync $(grep -q 'feishu' pyproject.toml && echo '--extra feishu')

# 3/3 浏览器内核: assets 自带 zip, 离线可装; 已装过就跳过
if ls .pw-browsers/chromium_headless_shell-1228/*/chrome-headless-shell >/dev/null 2>&1; then
  echo "[3/3] 浏览器内核已装, 跳过"
else
  echo "[3/3] 安装浏览器内核"
  bash install_browser.sh
fi

echo
echo "部署完成。用法:"
echo "  uv run tui.py               # TUI"
echo "  uv run tui.py -p \"...\"      # 无头单次"
echo "  gateway/run.sh start        # 飞书网关(需 .env 配 FEISHU_*)"
echo "  uv run eval/main.py [name]  # 评测"
