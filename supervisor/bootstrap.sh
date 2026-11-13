#!/usr/bin/env bash
# bootstrap.sh — 自进化容器环境一键初始化(可重复执行, 幂等)。
#
# 前提: 已按 docker/README.md 部署好基础双网关(zany-gateway 镜像, 容器在跑)。
# 用法: bash supervisor/bootstrap.sh [部署根] [容器名]
#   部署根默认 $ZANY_DEPLOY_ROOT 或 /data01/tsn/zany-gateway
#   容器名默认 tsn-zany-gateway
# 环境变量: ZANY_STABLE_BRANCH  stable 仓主分支名(默认 develop, 生产 repo.git 的实情)
#
# 做什么(全部步骤可重复跑):
#   1. supervisor 代码拷到部署根(挂载使用, 不烧镜像)
#   2. 建进化目录树(evolution/ evolve.git/)
#   3. docker-compose.yml 追加三个挂载(已有则跳过)
#   4. docker compose up -d 重建容器(挂载变更需要 recreate, 双网关掉线约 10s)
#   5. 容器内 evolve.py init(建进化仓/worktree, 基线=stable 主分支)
#   6. 容器内 install-hook(stable 仓物理防线: 拒非 ff / 拒删改 tag)
#   7. task-intake 技能拷进主备 code/skills/(skills.py 只扫一层, 必须拍平放)
#   8. 重启 gateway 进程(entrypoint 3s 自动拉回, 技能清单重扫)
#
# 踩坑记录(改脚本前先读):
#   - init/install-hook 必须在容器内跑: 容器内 uid 1000, 宿主 root 跑 git 会撞
#     dubious ownership, 且宿主建的文件容器内 worker 写不进。
#   - evolution/ evolve.git/ 目录要先在宿主建好(chown 1000), 否则 compose 会
#     自动创建 root 属主目录, 容器内写不进。
#   - skill 不能放 skills/<dir>/<name>/SKILL.md 两层深, skills.py 的 glob 是
#     "*/SKILL.md" 只扫一层 —— 必须拍平成 skills/<name>/SKILL.md。
#   - 挂载点目录绝不能 rm 后重建(bind mount 挂的是 inode, rm 重建后容器看到的是
#     幽灵旧目录, 新内容不可见) —— 代码更新用 find 清内容 + cp -a SRC/. 覆盖。
#   - pkill 匹配串要写成 '[g]ateway/main.py', 否则会杀掉 docker exec 自己的 shell。
set -euo pipefail

ROOT="${1:-${ZANY_DEPLOY_ROOT:-/data01/tsn/zany-gateway}}"
CONTAINER="${2:-tsn-zany-gateway}"
BRANCH="${ZANY_STABLE_BRANCH:-develop}"
SRC="$(cd "$(dirname "$0")" && pwd)"          # 仓库内 supervisor/
REPO="$(cd "$SRC/.." && pwd)"                 # 仓库根(skills/ 在这)

say() { echo "[bootstrap] $*"; }

# 1) supervisor 代码就位: 只清内容不 rm 目录本身(保 bind mount inode, 见头注)
mkdir -p "$ROOT/supervisor"
find "$ROOT/supervisor" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
cp -a "$SRC/." "$ROOT/supervisor/"
rm -rf "$ROOT/supervisor/__pycache__"
say "supervisor 代码已就位: $ROOT/supervisor"

# 2) 进化目录树(占位, 容器内 init 填内容; 属主对齐容器 uid 1000)
mkdir -p "$ROOT/evolution" "$ROOT/evolve.git"
chown 1000:1000 "$ROOT/evolution" "$ROOT/evolve.git" 2>/dev/null || true

# 3) compose 挂载(文件以 volumes: 段结尾, 追加即落在段内; 已有则跳过)
COMPOSE="$ROOT/docker-compose.yml"
[ -f "$COMPOSE" ] || { echo "缺 $COMPOSE(先按 docker/README.md 部署基础网关)" >&2; exit 1; }
if grep -q "/opt/supervisor" "$COMPOSE"; then
    say "compose 挂载已存在, 跳过"
else
    cat >> "$COMPOSE" <<EOF
      # 自进化 M1: supervisor 代码(不烧镜像) + 进化仓 + 流水线状态目录
      - \${ZANY_DEPLOY_ROOT:-$ROOT}/supervisor:/opt/supervisor
      - \${ZANY_DEPLOY_ROOT:-$ROOT}/evolve.git:/opt/evolve.git
      - \${ZANY_DEPLOY_ROOT:-$ROOT}/evolution:/opt/evolution
EOF
    say "compose 已追加 3 个挂载"
fi

# 4) 容器重建(挂载变更必须 recreate; 勿加 --remove-orphans, 会删别人的容器)
#    兑底: 宿主侧曾 rm 重建过挂载目录的话, 容器绑的是已删 inode(幽灵), 内容级
#    修复救不回, 只能 force-recreate。首次搭环境目录全新, 走不到这个分支。
say "docker compose up -d (重建容器, 双网关掉线约 10s)…"
( cd "$ROOT" && docker compose up -d >/dev/null )
sleep 8
if ! docker exec "$CONTAINER" test -f /opt/supervisor/evolve.py 2>/dev/null; then
    say "容器内看不到 supervisor(幽灵 inode), force-recreate…"
    ( cd "$ROOT" && docker compose up -d --force-recreate >/dev/null )
    sleep 8
fi

# 5+6) 容器内 init + install-hook
say "容器内 init(基线=stable $BRANCH) + install-hook…"
docker exec -e ZANY_STABLE_BRANCH="$BRANCH" "$CONTAINER" \
    sh -c "ZANY_LAYOUT=container python /opt/supervisor/evolve.py init \
           && ZANY_LAYOUT=container python /opt/supervisor/evolve.py install-hook"

# 7) task-intake 技能进主备 code/skills/ (拍平一层!)
for t in primary standby; do
    CODE="$ROOT/$t/code"
    [ -d "$CODE" ] || { say "警告: $CODE 不存在, 跳过 $t 技能部署"; continue; }
    rm -rf "$CODE/skills/evolution"                 # 旧的两层深布局, 清掉
    mkdir -p "$CODE/skills/task-intake"
    cp "$REPO/skills/task-intake/SKILL.md" "$CODE/skills/task-intake/SKILL.md"
    chown -R 1000:1000 "$CODE/skills/task-intake"
done
say "task-intake 技能已部署到主备 code/skills/"

# 8) 重启 gateway 进程(entrypoint while 守护自动拉回), 技能清单重扫
docker exec "$CONTAINER" sh -c "pkill -f '[g]ateway/main.py' || true" || true
sleep 6

# 自检
say "=== 自检 ==="
docker exec -e ZANY_STABLE_BRANCH="$BRANCH" "$CONTAINER" \
    sh -c "ZANY_LAYOUT=container python /opt/supervisor/evolve.py status"
N=$(docker exec "$CONTAINER" sh -c "pgrep -fc '[g]ateway/main.py'" || true)
say "gateway 进程数: $N (期望 ≥2: 主备各一)"
say "完成。下一步: 写任务单 evolution/tasks/<id>.md(或让 gateway 会话走 task-intake), \
然后: docker exec $CONTAINER python /opt/supervisor/evolve.py spawn-worker --task <id>"
