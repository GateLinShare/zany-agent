#!/bin/bash
# terminal-bench 环境准备(数据集 + harness + base 镜像), 跑评测见 run.sh
# 配方/踩坑/战绩详见 ~/.zany/skills/agent-quality-eval/SKILL.md
set -euo pipefail
DS=/tmp/tb-dataset
here=$(cd "$(dirname "$0")" && pwd)

# 1. 数据集(榜单口径 86 题, github 直连超时, 走本机 xray)
[ -d "$DS" ] || git -c http.proxy=socks5h://127.0.0.1:10808 \
  clone -b dataset/terminal-bench-core/v0.1.x --depth 1 \
  https://github.com/harbor-framework/terminal-bench-1 "$DS"

# 2. harness; --with httpx[socks]: litellm 抓价目表遇 socks 代理会崩
command -v tb >/dev/null || uv tool install terminal-bench --with "httpx[socks]"

# 3. base 镜像本地重建(ghcr 拉不动; 覆盖 python-3-13×37 题 + ubuntu-24-04×29 题)
docker build -q -t ghcr.io/laude-institute/t-bench/python-3-13:latest \
  -f "$here/base-python-3-13.dockerfile" "$here"
docker build -q -t ghcr.io/laude-institute/t-bench/ubuntu-24-04:latest \
  -f "$here/base-ubuntu-24-04.dockerfile" "$here"
echo "环境就绪; 跑评测: bash $here/run.sh --task-id hello-world --agent oracle"
