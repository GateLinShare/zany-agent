# ghcr.io/laude-institute/t-bench/ubuntu-24-04 的本地替身
FROM docker.1ms.run/library/ubuntu:24.04
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
COPY --from=docker.1ms.run/astral/uv:python3.12-bookworm-slim /usr/local/bin/uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends tmux asciinema python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
