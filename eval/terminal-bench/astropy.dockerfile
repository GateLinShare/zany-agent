# swe-bench-astropy-1/2 任务镜像预构建(build 阶段要 git clone github, 裸跑必超时)
# 用法:
#   docker build --build-arg PROXY=http://<宿主侧代理IP>:10808 \
#     --build-arg COMMIT=d16bfe05a744909de4b27f5875fe0d4ed41ce607 \
#     -t tb__swe-bench-astropy-1__client:latest -f astropy.dockerfile .
#   astropy-2 用 COMMIT=7269fa3e33e8d02485a647da91a5a2a60a06af61
# 构建后跑法: tb run ... --task-id swe-bench-astropy-1 --no-rebuild
FROM docker.1ms.run/library/python:3.9-slim-bookworm
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PROXY=http://172.17.0.40:10808
# ponytail: PROXY 是本容器某次的 IP, 重启会变, 换环境必传
RUN apt-get update && apt-get install -y --no-install-recommends git tmux asciinema \
    && rm -rf /var/lib/apt/lists/*
RUN git config --global http.proxy $PROXY && git config --global https.proxy $PROXY && \
    git clone https://github.com/astropy/astropy.git /app/astropy
ARG COMMIT
RUN cd /app/astropy && git reset --hard $COMMIT && \
    rm -rf .git/logs && git gc --prune=now && git remote remove origin
WORKDIR /app/astropy
