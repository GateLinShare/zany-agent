# ghcr.io/laude-institute/t-bench/python-3-13 的本地替身
FROM docker.1ms.run/library/python:3.13-slim
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
# 每题测试脚本 setup-uv-pytest.sh 会现场装 uv+pytest, 不预装+不配源必超时
COPY --from=docker.1ms.run/astral/uv:python3.12-bookworm-slim /usr/local/bin/uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends tmux asciinema \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
