# zany 应用镜像: 不烧代码 —— 代码来自挂载的共享仓库 /srv/repo, 首启由 entrypoint
# 克隆到 $GATEWAY_WORKDIR(/zany 或 /zany-standby)。改码不再 rebuild 镜像。
# 构建(context=docker/ 目录, 先有 base): docker build -f docker/Dockerfile -t zany-gateway:latest docker/
FROM zany-base:latest

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && \
    install -d -o user_zany -g user_zany /opt/repo /opt/code/zany-primary /opt/code/zany-standby /opt/data/zany-primary /opt/data/zany-standby && \
    git config --system user.name zany && git config --system user.email zany@zany.local

USER user_zany
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "gateway/main.py"]
