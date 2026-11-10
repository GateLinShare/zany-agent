#!/bin/bash
# terminal-bench 判分链路从零起跑(oracle 先验证判分, 再接真 agent)
# 配方/踩坑/战绩详见 ~/.zany/skills/agent-quality-eval/SKILL.md
# 用法: bash eval/terminal-bench/setup.sh [tb run 额外参数, 如 --task-id ...]
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

# 4. oracle 冒烟一题, 判分链路通即环境就绪
export LITELLM_LOCAL_MODEL_COST_MAP=True
exec tb run --dataset-path "$DS/tasks" --task-id hello-world --agent oracle \
  --output-path /tmp/tb-runs/setup-smoke --no-cleanup --global-test-timeout-sec 600 "$@"

# 需 build 内 clone github 的 13 题(如 swe-bench-astropy-1/2)要预构建任务镜像再 --no-rebuild,
# 见 astropy.dockerfile 头注释; 任务镜像名规律 tb__<task-id>__client:latest
