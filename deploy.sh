#!/usr/bin/env bash
# zany 一键部署(非 docker): uv(缺则装) + uv sync + 浏览器内核 + 全局命令 zany
# 用法: bash deploy.sh   (在 fresh clone 上即可跑, python/依赖均由 uv 管理)
set -euo pipefail
cd "$(dirname "$0")"
REPO=$(pwd)

# 1/4 uv: 没有就装(官方安装器, 落 ~/.local/bin)
if command -v uv >/dev/null 2>&1; then
  echo "[1/4] uv 已有: $(command -v uv)"
else
  echo "[1/4] 安装 uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 2/4 依赖: 有 feishu extra 的分支(develop, gateway 所在)带上它; master 无此 extra, 普通 sync
echo "[2/4] uv sync$(grep -q 'feishu' pyproject.toml && echo ' --extra feishu')"
uv sync $(grep -q 'feishu' pyproject.toml && echo '--extra feishu')

# 3/4 浏览器内核: assets 自带 zip, 离线可装; 已装过就跳过
if ls .pw-browsers/chromium_headless_shell-1228/*/chrome-headless-shell >/dev/null 2>&1; then
  echo "[3/4] 浏览器内核已装, 跳过"
else
  echo "[3/4] 安装浏览器内核"
  bash install_browser.sh
fi

# 4/4 全局命令 zany: 代码路径固定到本 checkout, 调用时的 cwd 即 agent 工作目录
if [ -w /usr/local/bin ] || [ "$(id -u)" = 0 ]; then
  BIN=/usr/local/bin/zany
else
  BIN="$HOME/.local/bin/zany"
  mkdir -p "$HOME/.local/bin"
fi
cat > "$BIN" <<EOF
#!/usr/bin/env bash
# mini coding agent: 以当前目录为工作项目启动(cwd 即 workdir, AGENTS.md/文件工具/execute 全锚定它)
unset VIRTUAL_ENV  # 残留变量会让 uv run 每次警告
exec uv run --project $REPO python $REPO/tui.py "\$@"
EOF
chmod +x "$BIN"
echo "[4/4] 全局命令: $BIN"
[ "$BIN" = "$HOME/.local/bin/zany" ] && echo "  (确保 $HOME/.local/bin 在 PATH 里)"

echo
echo "部署完成。用法(任意目录):"
echo "  zany                   # TUI"
echo "  zany -p \"...\"          # 无头单次"
echo "  <仓库>/gateway/run.sh start # 飞书网关(需 .env 配 FEISHU_*)"
echo "  uv run eval/main.py [name]  # 评测"
