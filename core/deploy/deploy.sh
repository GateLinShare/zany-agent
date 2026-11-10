#!/usr/bin/env bash
# zany 一键部署(非 docker): uv(缺则装) + uv sync + 浏览器内核 + 全局命令 zany
# 用法: bash deploy.sh   (在 fresh clone 上即可跑, python/依赖均由 uv 管理)
set -euo pipefail

# Windows: Git Bash 里跑则转原生 deploy.cmd; WSL 会把环境装到 Linux 侧, 直接指路
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) exec cmd //c "$(cygpath -w "${0%.sh}.cmd")" ;;
esac
if uname -r | grep -qi microsoft; then
  echo "WSL 检测到: 本脚本会装 Linux 侧环境; Windows 原生请用 deploy.cmd"
  exit 1
fi

cd "$(dirname "$0")"
REPO=$(pwd)

# 0/4 .env: 运行时数据根(~/.zany)下缺 .env 时自 env.example 播种(填好密钥即可用)
if [ ! -f "$HOME/.zany/.env" ]; then
  mkdir -p "$HOME/.zany"
  cp env.example "$HOME/.zany/.env"
  echo "[0/4] 已播种 $HOME/.zany/.env(自 env.example, 需填 ANTHROPIC_* 密钥)"
fi

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

# 3/4 浏览器内核: 浏览器属 develop 增量(master 无 core/browser.py, 跳过); assets 自带 zip, 离线可装; 已装过就跳过
if [ ! -f core/browser.py ]; then
  echo "[3/4] 本分支无浏览器模块, 跳过内核安装"
elif ls .pw-browsers/chromium_headless_shell-1228/*/chrome-headless-shell >/dev/null 2>&1; then
  echo "[3/4] 浏览器内核已装, 跳过"
else
  echo "[3/4] 安装浏览器内核"
  bash install_browser.sh
fi

# 4/4 全局命令: 代码路径固定到本 checkout, 调用时的 cwd 即 agent 工作目录。
# master 装 zany; develop 装 zany-dev(避免 develop 上跑 deploy.sh 把全局 zany 覆盖到 develop 代码)
if git branch --show-current | grep -q '^develop$'; then
  CMD=zany-dev
else
  CMD=zany
fi
if [ -w /usr/local/bin ] || [ "$(id -u)" = 0 ]; then
  BIN=/usr/local/bin/$CMD
else
  BIN="$HOME/.local/bin/$CMD"
  mkdir -p "$HOME/.local/bin"
fi
cat > "$BIN" <<EOF
#!/usr/bin/env bash
# zany coding agent: 以当前目录为工作项目启动(cwd 即 workdir, AGENTS.md/文件工具/execute 全锚定它)
unset VIRTUAL_ENV  # 残留变量会让 uv run 每次警告
exec uv run --project $REPO python $REPO/core/tui.py "\$@"
EOF
chmod +x "$BIN"
echo "[4/4] 全局命令: $BIN"
[ "$BIN" = "$HOME/.local/bin/$CMD" ] && echo "  (确保 $HOME/.local/bin 在 PATH 里)"

echo
echo "部署完成。用法(任意目录):"
echo "  $CMD                   # TUI"
echo "  $CMD -p \"...\"          # 无头单次"
if grep -q 'feishu' pyproject.toml; then
  echo "  <仓库>/gateway/run.sh start # 飞书网关(需 .env 配 FEISHU_*)"
fi
echo "  uv run eval/main.py [name]  # 评测"
