# zany gateway docker 部署(两层镜像 + 同容器双网关主备)

> 本文是唯一事实源。路径以 compose/entrypoint.sh 实际内容为准。
>
> 2026-11-05 起 compose 文件住部署根(挂载目录), repo 的 docker/ 只留构建源(Dockerfile*/entrypoint.sh/build.sh)——起停/改配置都在部署根做, build 仍回 checkout 跑(上下文要 repo 源码)。

## 1. 结构

    zany-base     python:3.12-slim-bookworm + apt/uv 清华源
                       + tini(PID 1 回收僵尸) git curl
                       + chromium 运行库 + 自带 chrome-headless-shell -> /opt/pw-browsers
                       + uv 二进制(ghcr 镜像直拷) + /opt/venv(uv sync --frozen --extra feishu)
    zany-gateway  zany-base + entrypoint.sh(首启克隆 + 双进程守护), **不含代码**

非 root 运行, uid **1000**(宿主目录属主要对齐, 见 §3)。`build.sh` 常规构建,
`WITH_BASE=1 bash build.sh` 才重建底座(依赖变了才需要)。

## 2. 布局

单容器 `tsn-zany-gateway` 跑主备两个 gateway 进程, 各连一个飞书 bot。

容器内:

    /opt/code/zany-primary     主网关代码(entrypoint 首启从 /opt/repo 克隆)
    /opt/code/zany-standby     备网关代码
    /opt/data/zany-primary     主网关 HOME(其下 .zany/ 是挂载点: 会话/技能/记忆)
    /opt/data/zany-standby     备网关 HOME
    /opt/repo                  共享裸仓库(宿主 bind mount, 只读使用)

宿主(可用 `ZANY_DEPLOY_ROOT` 环境变量改根):

    /data01/tsn/zany-gateway/
      docker-compose.yml            运行侧 compose(2026-11-05 从 repo docker/ 迁入)
      repo.git                      共享裸仓库
      primary/code/                 → /opt/code/zany-primary
      primary/home/                 → /opt/data/zany-primary/.zany   ← 注意挂在 .zany 层
      primary/home/.env             → /opt/code/zany-primary/.env (ro)
      standby/…                     备对称

关键映射: 代码找会话走 `$HOME/.zany/sessions`, 所以宿主 home 目录内容 = `.zany`
的内容(sessions/ USER.md skills/ …), 别挂到 HOME 根。`.env` 是代码目录单文件挂载,
改完 `docker compose restart` 即生效。

## 3. 首次部署

```bash
R=/data01/tsn/zany-gateway

# 1) 宿主目录, 属主对齐镜像 uid 1000(权限不对会 Permission denied)
mkdir -p $R/primary/home $R/standby/home
chown -R 1000:1000 $R

# 2) 共享仓库 seed(一次性, 之后独立演化): 从 develop checkout 打 bare 包
git clone --bare /data01/tsn/code/tsn_files/zany-agent-develop $R/repo.git
chown -R 1000:1000 $R/repo.git

# 3) 两份 .env: cp env.example 后填; 必配 FEISHU_APP_ID/SECRET/ALLOWED_USERS/BOT_OPEN_ID
#    (从老部署搬 .env 直接可用, 会话数据同理放 home/ 下)

# 4) 构建 + 起停: 构建在 checkout 内跑, 起停在部署根跑(compose 文件已迁到部署根)
WITH_BASE=1 bash docker/build.sh      # 仅依赖变化时
cp docker/docker-compose.yml /data01/tsn/zany-gateway/   # 首次或 compose 有更新时
cd /data01/tsn/zany-gateway && docker compose up -d
docker logs -f tsn-zany-gateway       # 应看到两行 "gateway up: workdir=/opt/code/..."
```

首启 entrypoint 发现代码目录无 `.git` 时从 `/opt/repo` 克隆(clone 到 /tmp 再拷入,
因为目录里已挂 .env 非空)。已有 clone 原样放行——重启/崩溃拉起不覆盖本地改动。

## 4. .env 的 bot 身份三要素(换 bot 必看)

open_id **按应用隔离**, 旧 bot 的 id 对新 bot 无效, 静默拒收(不回复, 仅敲门口碑日志)。
换 bot 流程:

1. `.env` 改 `FEISHU_APP_ID/FEISHU_APP_SECRET`, `compose restart`
2. **用户 open_id**: 白名单外消息会打日志 → 用户给新 bot 发条消息 →
   `docker logs tsn-zany-gateway | grep 白名单外` 挖 `sender=ou_…` →
   回填 `FEISHU_ALLOWED_USERS` → 再 restart
3. **bot 自身 open_id**(群聊 @ 检测用): 用新凭据调 API 挖 →
   `POST /open-apis/auth/v3/tenant_access_token/internal` 拿 token,
   `GET /open-apis/bot/v3/info` 取 `open_id` → 回填 `FEISHU_BOT_OPEN_ID`
   (不配 = 群聊宽松放行)

## 5. 日常运维

```bash
# compose 命令一律在部署根 /data01/tsn/zany-gateway/ 下跑(下同)
# 改码: 改宿主 code 目录(或容器内改) → compose restart; 或让 agent 自己调 restart 工具
# 同步给另一个网关: 两边 remote 都指 /opt/repo, push/pull 交换
# 从开发 checkout 同步: checkout 上已配 remote "container" → git push container develop
#   (容器改动回收: git fetch container + merge; repo.git 与 origin/GitHub 独立演化)
docker exec tsn-zany-gateway git -C /opt/code/zany-standby pull

# 改 .env: 编辑宿主 primary|standby/home/.env → compose restart

# 重生(代码推倒重来): 删宿主对应 code/ 目录 + compose restart(首启重新克隆; home 数据保留)

# 进程守护: entrypoint 两个 while 循环, 单进程崩 3s 自动拉起(拿磁盘当前代码)
```

同容器互查天然可见: 主视角看备 = 代码 `/opt/code/zany-standby`、会话
`/opt/data/zany-standby/sessions/`; 备视角对称。`ps` 能看到双方进程。

## 6. 排障

| 症状 | 根因 | 处置 |
|---|---|---|
| 飞书回 `处理出错: [Errno 2] … sessions/<桶>` | 旧版 save_session 单层 mkdir, `$HOME/.zany/sessions` 整层缺失时炸(已修: parents=True) | 拉最新代码 restart; 数据目录注意挂到 `.zany` 层(§2) |
| `[Errno 13] Permission denied` 建会话桶 | 宿主目录属主不是 1000 | `chown -R 1000:1000 <home>` |
| 白名单外不回复 | open_id 按应用隔离, id 是旧 bot 作用域的 | 按 §4 流程挖新 id 回填 |
| 拉代码 fatal /opt/repo not a repository | 老 clone 的 remote 指旧路径 | `git -C /opt/code/<x> remote set-url origin /opt/repo` |
| 进程反复退出 3s 拉起 | 看日志具体异常(.env 缺项/依赖缺失) | `docker logs tsn-zany-gateway` |

## 7. 与 hermes-space 部署的差异(为什么砍掉)

| hermes-space | 这里 | 原因 |
|---|---|---|
| s6-overlay 监督树(main/dashboard/多 profile gateway) | tini + entrypoint 守护循环 | 双进程够了, 不上 s6 |
| 双架构 amd64+arm64 | 仅 amd64 | 部署目标单机 x86 |
| hermes-base-s6 底座(Node/Playwright 全家桶, 2.9GB) | slim + 按需系统库(~600MB) | 干净轻量 |
