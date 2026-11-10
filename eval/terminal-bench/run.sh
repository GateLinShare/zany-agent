#!/bin/bash
# 跑 terminal-bench 评测(环境未起先跑 setup.sh; oracle=标准答案, 先验证判分链路再接真 agent)
# 需 build 内 clone github 的 13 题(如 swe-bench-astropy-1/2)要预构建任务镜像再 --no-rebuild,
# 见 astropy.dockerfile 头注释; 任务镜像名规律 tb__<task-id>__client:latest
# 用法: bash run.sh --agent oracle --task-id hello-world
set -euo pipefail
DS=/tmp/tb-dataset
[ -d "$DS" ] || { echo "数据集不在 /tmp/tb-dataset, 先跑同目录 setup.sh"; exit 1; }
export LITELLM_LOCAL_MODEL_COST_MAP=True
exec tb run --dataset-path "$DS/tasks" \
  --output-path "/tmp/tb-runs/run-$(date +%Y%m%d-%H%M%S)" \
  --no-cleanup --global-test-timeout-sec 600 "$@"
