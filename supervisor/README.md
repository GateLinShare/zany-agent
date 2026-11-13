# supervisor — 自进化流水线（M1）

设计方案（唯一事实源）：`/data01/tsn/code/tsn_files/zany-self-evolution-design.md`
格式约定：本目录 `formats.md`。代码在 develop 仓 `supervisor/`，部署副本挂进容器 `/opt/supervisor`。

## M1 范围

手动流水线：supervisor 是 CLI（`evolve.py`），人按按钮。自动巡检/自动灰度判定（daemon 常驻）是 M2。

## 部署(宿主一键, 幂等)

前提: 已按 `docker/README.md` 部署好基础双网关(镜像 + compose 起容器)。然后仓库里一条命令:

```bash
bash supervisor/bootstrap.sh                 # 默认部署根 /data01/tsn/zany-gateway, 容器 tsn-zany-gateway
bash supervisor/bootstrap.sh /path/to/root my-gateway   # 或显式指定
```

脚本做八件事: supervisor 代码就位(挂载不烧镜像) → 目录树 → compose 追加挂载 → 容器重建 → 容器内 init → install-hook → task-intake 技能进主备 skills/ → 重启 gateway 进程。幂等, 可重复跑; 踩坑记录写在脚本头部注释(init 必须容器内跑、skill 只能一层深、stable 主分支默认 develop 等)。

手动等价步骤(想逐件理解的看这; 与脚本一一对应):

```bash
# 1) 代码就位 + 目录树(属主对齐容器 uid 1000)
cp -a supervisor /data01/tsn/zany-gateway/supervisor && rm -rf /data01/tsn/zany-gateway/supervisor/__pycache__
mkdir -p /data01/tsn/zany-gateway/{evolution,evolve.git}
chown 1000:1000 /data01/tsn/zany-gateway/{evolution,evolve.git}
# 2) compose 追加挂载(见下方 YAML)后 docker compose up -d 重建
# 3) 容器内初始化(init 不动 stable 仓; hook 单独装, 拦非 ff/删改 tag)
docker exec tsn-zany-gateway sh -c "ZANY_LAYOUT=container python /opt/supervisor/evolve.py init \
  && ZANY_LAYOUT=container python /opt/supervisor/evolve.py install-hook"
# 4) task-intake 技能拷到主备 code/skills/task-intake/(一层!), 重启 gateway 进程
```

compose 需要的三个挂载(bootstrap 会自动追加, 手动时自己加):

```yaml
      - ${ZANY_DEPLOY_ROOT:-/data01/tsn/zany-gateway}/supervisor:/opt/supervisor
      - ${ZANY_DEPLOY_ROOT:-/data01/tsn/zany-gateway}/evolve.git:/opt/evolve.git
      - ${ZANY_DEPLOY_ROOT:-/data01/tsn/zany-gateway}/evolution:/opt/evolution
```

.ev 前提:.env 挂载去掉 `:ro`(worker 需要 mkdir/clone 进 evolve worktree,不再写主 .env 也可,保守起见 M1 保持 ro 也能跑--worker 只动 worktree)。首启初始化时把 worker 会话 HOME 建在 `evolution/worker-home`。

## 日常命令（容器内）

```bash
EVOLVE="python /opt/supervisor/evolve.py"
docker exec tsn-zany-gateway sh -c "$EVOLVE status"
docker exec tsn-zany-gateway sh -c "$EVOLVE spawn-worker --task t-20261120-01"
docker exec tsn-zany-gateway sh -c "$EVOLVE gate --report /opt/evolution/reports/t-20261120-01.json"
docker exec tsn-zany-gateway sh -c "$EVOLVE deploy --target standby --dry-run"   # 先干跑
docker exec tsn-zany-gateway sh -c "$EVOLVE deploy --target standby"
docker exec tsn-zany-gateway sh -c "$EVOLVE canary-check --l1"                    # 观察期到点后
docker exec tsn-zany-gateway sh -c "$EVOLVE promote"
docker exec tsn-zany-gateway sh -c "$EVOLVE rollback"                             # 任何不对劲
```

## 宿主自检（不依赖容器）

```bash
python3 supervisor/evolve.py selftest
```

## M1 流转口诀

任务单（人或 gateway 会话写）→ spawn-worker → worker 推候选 + 提案 → gate → deploy standby → 观察期 → canary-check → promote（打 stable/* tag）。失败：rollback（连败 3 次自动熔断 + alerts/ 落盘告警）。
