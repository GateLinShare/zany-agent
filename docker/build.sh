#!/usr/bin/env bash
# 镜像不含代码(代码来自挂载仓库), 应用镜像只烧 entrypoint 一层。
# 用法: bash docker/build.sh              # 只刷应用镜像(常规操作)
#       WITH_BASE=1 bash docker/build.sh  # 依赖/浏览器/系统包变了连 base 一起刷
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "${WITH_BASE:-0}" = 1 ]; then
  docker build -f docker/Dockerfile.base -t zany-base:latest .
fi
docker build -f docker/Dockerfile -t zany-gateway:latest docker/
