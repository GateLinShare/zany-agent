#!/bin/bash
# M1 流水线端到端冒烟(完全隔离的临时部署根, 不碰真实 /data01/tsn/zany-gateway)
set -e
SUP=/data01/tsn/code/tsn_files/zany-agent-develop/supervisor
TD=$(mktemp -d /tmp/evolve-e2e.XXXXXX)
export ZANY_DEPLOY_ROOT=$TD
export ZANY_STABLE_BRANCH=master  # fake stable 仓用 master, 生产是 develop
trap 'rm -rf $TD' EXIT

echo "== 1. fake stable + 部署 checkout =="
git init --bare -q -b master "$TD/repo.git"
W="$TD/seed"; git clone -q "$TD/repo.git" "$W"
mkdir -p "$W/skills/evolution" "$W/core" "$W/eval"
echo "v1" > "$W/skills/evolution/placeholder.md"
echo "print('eval stub')" > "$W/eval/main.py"
git -C "$W" add . && git -C "$W" commit -qm seed && git -C "$W" push -q origin master
git clone -q "$TD/repo.git" "$TD/primary/code"
git clone -q "$TD/repo.git" "$TD/standby/code"

echo "== 2. init =="
cd "$SUP" && python3 evolve.py init | tail -1

echo "== 3. 模拟 worker: worktree 改动 → commit → 推 stable =="
cd "$TD/evolution/worktree"
git checkout -qb evolve/t-1
echo "improved" > skills/evolution/placeholder.md
git add . && git commit -qm "H1: candidate"
SHA=$(git rev-parse HEAD); BASE=$(git rev-parse HEAD~1)
git push -q stable evolve/t-1

echo "== 4. 任务单 + 干净 session + 提案 =="
mkdir -p "$TD/evolution/tasks" "$TD/evolution/worker-home/.zany/sessions/b"
printf -- "---\nid: t-1\nstatus: open\n---\n目标: 冒烟测试\n" > "$TD/evolution/tasks/t-1.md"
echo '[{"role":"user","content":"hello"}]' > "$TD/evolution/worker-home/.zany/sessions/b/s1.json"
cat > "$TD/evolution/reports/t-1.json" <<EOF
{"task_id":"t-1","branch":"evolve/t-1","candidate_sha":"$SHA","base_sha":"$BASE",
 "risk_layer":"a","motivation":"smoke","sessions":["s1"],
 "report":{"l1":{"passed":8,"total":8},"l2":{"score":2,"baseline":0,"cases":[{"case":"new-1","baseline_pass":false,"candidate_pass":true}]}}}
EOF
EVO="$SUP/evolve.py"

echo "== 5. 缺任务单场景应拒 =="
mv "$TD/evolution/tasks/t-1.md" "$TD/t-1.md.bak"
python3 "$EVO" gate --report "$TD/evolution/reports/t-1.json" 2>&1 | head -2 && true
mv "$TD/t-1.md.bak" "$TD/evolution/tasks/t-1.md"

echo "== 6. 污染 session 应拒 =="
echo '[{"role":"assistant","content":[{"type":"tool_use","name":"read_file","input":{"path":"/x/eval/benchmarks/y/rubric/README.md"}}]}]' \
    > "$TD/evolution/worker-home/.zany/sessions/b/s1.json"
python3 "$EVO" gate --report "$TD/evolution/reports/t-1.json" 2>&1 | head -2 && true
echo '[{"role":"user","content":"hello"}]' > "$TD/evolution/worker-home/.zany/sessions/b/s1.json"

echo "== 7. 正常 gate =="
python3 "$EVO" gate --report "$TD/evolution/reports/t-1.json" | head -2

echo "== 8. deploy standby(dry-run) + canary(观察期未满应拒) =="
python3 "$EVO" deploy --target standby --dry-run --sha "$SHA" | tail -1
python3 "$EVO" canary-check 2>&1 | tail -1 && true

echo "== 9. 拨快观察期 → canary 过 → promote(--force 免去) =="
python3 - "$TD" <<'PY'
import json, sys, datetime
from pathlib import Path
p = Path(sys.argv[1]) / "evolution/state.json"
s = json.loads(p.read_text())
s["canary"]["since"] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)).isoformat(timespec="seconds")
p.write_text(json.dumps(s))
PY
python3 "$EVO" canary-check | tail -1
python3 "$EVO" promote | tail -1

echo "== 10. 第二轮: 双 tag 后真回滚 =="
cd "$TD/evolution/worktree"
echo "v2" > skills/evolution/placeholder.md
git add . && git commit -qm "H2: second candidate"
SHA2=$(git rev-parse HEAD); BASE2=$(git rev-parse HEAD~1)
git push -q stable HEAD:refs/heads/evolve/t-2
cat > "$TD/evolution/reports/t-2.json" <<EOF
{"task_id":"t-1","branch":"evolve/t-2","candidate_sha":"$SHA2","base_sha":"$BASE2",
 "risk_layer":"a","motivation":"second","sessions":["s1"],
 "report":{"l1":{"passed":8,"total":8},"l2":{"score":3,"baseline":0}}}
EOF
python3 "$EVO" gate --report "$TD/evolution/reports/t-2.json" | head -1
python3 "$EVO" deploy --target standby --sha "$SHA2" --dry-run | tail -1
python3 - "$TD" <<'PY'
import json, sys, datetime
from pathlib import Path
p = Path(sys.argv[1]) / "evolution/state.json"
s = json.loads(p.read_text())
s["canary"]["since"] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)).isoformat(timespec="seconds")
p.write_text(json.dumps(s))
PY
python3 "$EVO" promote | tail -1
TAG1=$(git -C "$TD/repo.git" tag -l 'stable/*' | sort | head -1)
python3 "$EVO" rollback | tail -1
CUR=$(git -C "$TD/primary/code" rev-parse HEAD)
OLD=$(git -C "$TD/repo.git" rev-parse "$TAG1")
[ "$CUR" = "$OLD" ] || { echo "回滚未落在上一 tag: $CUR != $OLD"; exit 1; }
echo "回滚校验通过: primary 回到 $TAG1"

echo "== 11. status =="
python3 "$EVO" status | tail -5
echo "== E2E 冒烟通过 =="
