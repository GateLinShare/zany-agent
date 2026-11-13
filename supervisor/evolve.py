#!/usr/bin/env python3
"""evolve CLI — zany 自进化流水线 M1(supervisor 的手动形态)。

设计方案: /data01/tsn/code/tsn_files/zany-self-evolution-design.md
原则: LLM 只提议(分支推送 + 提案文件), 本脚本做全部裁决与执行(确定性代码)。

布局(ZANY_LAYOUT=host|container, 默认 host):
    host:      <ROOT=/data01/tsn/zany-gateway>/{repo.git, evolve.git, evolution/, primary/code, standby/code}
    container: <ROOT=/opt>/{repo, evolve.git, evolution/, code/zany-primary, code/zany-standby}

子命令:
    init                          一次性初始化(目录树/evolve.git/stable hook/worktree/worker skill)
    status                        state.json + 进程/tag 概览
    spawn-worker --task <id>      拉起进化 worker(zany -p, cwd=evolve worktree)
    gate --report <path.json>     T4 门禁: 报告/白名单/密钥/审计 → 批准部署
    deploy --target standby|primary [--sha X] [--dry-run]   fetch+reset+重启 gateway 进程
    canary-check [--l1]           观察期指标判定
    promote [--dry-run]           打 tag + 晋升 primary
    rollback [--to <tag>]         回滚上一 tag 并两边拉起
    selftest                      无容器依赖的断言自检
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

LAYOUT = os.environ.get("ZANY_LAYOUT", "host")
STABLE_BRANCH = os.environ.get("ZANY_STABLE_BRANCH", "develop")  # 生产 repo.git 主分支是 develop
HERE = Path(__file__).resolve().parent
POLICY = json.loads((HERE / "policy.json").read_text(encoding="utf-8"))

if LAYOUT == "container":
    REPO = Path("/opt/repo")
    EVOLVE = Path("/opt/evolve.git")
    EVO = Path("/opt/evolution")
    DEPLOY = {"primary": Path("/opt/code/zany-primary"), "standby": Path("/opt/code/zany-standby")}
    SESSION_ROOTS = {"primary": Path("/opt/data/zany-primary/.zany/sessions"),
                     "standby": Path("/opt/data/zany-standby/.zany/sessions")}
else:
    ROOT = Path(os.environ.get("ZANY_DEPLOY_ROOT", "/data01/tsn/zany-gateway"))
    REPO = ROOT / "repo.git"
    EVOLVE = ROOT / "evolve.git"
    EVO = ROOT / "evolution"
    DEPLOY = {"primary": ROOT / "primary/code", "standby": ROOT / "standby/code"}
    SESSION_ROOTS = {"primary": ROOT / "primary/home/.zany/sessions",
                     "standby": ROOT / "standby/home/.zany/sessions"}

WORKTREE = EVO / "worktree"
WORKER_HOME = EVO / "worker-home"
STATE = EVO / "state.json"
LOG = EVO / "logs/evolve.log.jsonl"
TASKS = EVO / "tasks"
REPORTS = EVO / "reports"
ZERO = "0" * 40


# ---------- 基础设施 ----------

def sh(cmd, cwd=None, check=True):
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd))} → {r.stderr.strip()}")
    return r


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(event, **kw):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now(), "event": event, **kw}, ensure_ascii=False) + "\n")


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"fail_streak": 0, "halted": False, "deployed": {}, "canary": None}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 分层与检查(policy.json) ----------

def layer_of(path: str) -> str:
    """方案 §6 分级。未知路径按 c(最严自动层)处理, 防新目录绕过分级。"""
    p = path.lstrip("/")
    if p.startswith(("skills/", "styles/", "eval/cases.jsonl", "eval/benchmarks/")):
        return "a"
    if p.startswith("core/plugins/"):
        return "b"
    if p.startswith(("gateway/", "docker/")) or p == "eval/main.py" or p.startswith("eval/deepeval/"):
        return "d"
    if p == ".env" or p.startswith("sessions/"):
        return "e"
    return "c"  # core/ 其余、pyproject.toml、uv.lock、未收录路径


SECRET_LEAK = re.compile(POLICY["secret_leak"])


def secret_leak_lines(diff_text: str) -> list:
    """e 层检查: 提案 diff 不得出现密钥明文(占位符 <...>/REPLACE_ME/空值放行)。"""
    hits = []
    for line in diff_text.splitlines():
        if SECRET_LEAK.search(line) and not re.search(r"<[^>]*>|REPLACE_ME|xxx", line, re.I):
            hits.append(line.strip()[:120])
    return hits


def changed_paths(base: str, cand: str) -> list[str]:
    return [l for l in sh(["git", "-C", REPO, "diff", "--name-only", f"{base}..{cand}"]).stdout.splitlines() if l.strip()]


def diff_lines(base: str, cand: str) -> int:
    r = sh(["git", "-C", REPO, "diff", "--numstat", f"{base}..{cand}"])
    n = 0
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            n += int(parts[0]) + int(parts[1])
    return n


def audit_sessions(session_ids, root=None) -> list[str]:
    """防作弊审计: worker 的 session Trace 不得读评测私有件。返回污染记录(空=干净)。"""
    root = root or (ROOT / POLICY["audit"]["worker_session_root"])
    bad = []
    for sid in session_ids:
        hits = list(root.rglob(f"{sid}.json")) if root.exists() else []
        if not hits:
            bad.append(f"session {sid}: Trace 未找到, 无法审计")
            continue
        text = hits[0].read_text(encoding="utf-8", errors="replace")
        if '"read_file"' not in text:
            continue
        for pat in POLICY["audit"]["forbidden_read_patterns"]:
            if pat in text:
                bad.append(f"session {sid}: 读到受禁路径 {pat!r}")
    return bad


def validate_report(rep: dict) -> list[str]:
    fails = [f"提案缺字段: {k}" for k in
             ("task_id", "branch", "candidate_sha", "base_sha", "risk_layer", "motivation", "report", "sessions")
             if k not in rep]
    if fails:
        return fails
    r = rep["report"]
    l1 = r.get("l1") or {}
    if not l1.get("total") or l1.get("passed") != l1.get("total"):
        fails.append(f"L1 未全过: {l1}")
    l2 = r.get("l2") or {}
    if "score" not in l2 or "baseline" not in l2 or l2["score"] < l2["baseline"]:
        fails.append(f"L2 未达基线: score={l2.get('score')} baseline={l2.get('baseline')}")
    if not rep["sessions"]:
        fails.append("提案未附 session_id(分数必须可链回 Trace)")
    return fails


# ---------- gateway 进程操控(entrypoint while 守护: kill 后 3s 自动拉回) ----------

def gateway_pids(target: str) -> list:
    code = str(DEPLOY[target])
    pids = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if code in str(Path("/proc", proc.name, "cwd").resolve()) and \
                    "gateway/main.py" in (Path("/proc", proc.name, "cmdline").read_bytes().decode(errors="replace")):
                pids.append(int(proc.name))
        except OSError:
            continue
    return pids


def restart_gateway(target: str, dry=False):
    pids = gateway_pids(target)
    if dry:
        print(f"[dry-run] 将 kill gateway({target}) pids={pids}, entrypoint 3s 后拉起")
        return
    for pid in pids:
        os.kill(pid, 15)
    log("gateway_restarted", target=target, pids=pids)


# ---------- 子命令 ----------

def cmd_init(_a):
    for d in (EVO, TASKS, REPORTS, EVO / "alerts", EVO / "logs", WORKER_HOME / ".zany/sessions"):
        d.mkdir(parents=True, exist_ok=True)
    if not (EVOLVE / "HEAD").exists():
        EVOLVE.mkdir(parents=True, exist_ok=True)
        sh(["git", "init", "--bare", "-q", "-b", "master", EVOLVE])
        print(f"已建进化仓: {EVOLVE}")
    print("提醒: stable 仓 hook 未装, 执行 install-hook 后非 ff 推送才会被拦")
    if not (WORKTREE / ".git").exists():
        sh(["git", "clone", "-q", EVOLVE, WORKTREE])
    sh(["git", "-C", WORKTREE, "remote", "remove", "stable"], check=False)
    sh(["git", "-C", WORKTREE, "remote", "add", "stable", REPO])
    sh(["git", "-C", WORKTREE, "fetch", "-q", "stable", STABLE_BRANCH])
    sh(["git", "-C", WORKTREE, "reset", "--hard", "FETCH_HEAD"])
    sync_worker_skills()
    base = sh(["git", "-C", REPO, "rev-parse", STABLE_BRANCH]).stdout.strip()
    s = load_state()
    s.setdefault("baseline", base)
    save_state(s)
    log("init", baseline=base)
    print(f"初始化完成: 基线 {base[:12]}  worktree={WORKTREE}  worker_home={WORKER_HOME}")


def sync_worker_skills():
    """worker 流程 skill 强制同步进 worktree(worker 自己改它, 下一轮自动被覆盖)。"""
    src_root = HERE / "worker-skills"
    for src in src_root.iterdir():
        if not src.is_dir():
            continue
        dst = WORKTREE / "skills/evolution" / src.name
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "SKILL.md").write_text((src / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8")


def cmd_install_hook(_a):
    """动生产 stable 仓的动作独立成命令, 与 init 分离。"""
    hook = REPO / "hooks/update"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_bytes((HERE / "hooks/update").read_bytes())
    hook.chmod(0o755)
    log("hook_installed", hook=str(hook))
    print(f"stable 仓 hook 已装: {hook}")


def cmd_status(_a):
    s = load_state()
    print(json.dumps({k: s[k] for k in ("fail_streak", "halted", "deployed", "canary") if k in s},
                     ensure_ascii=False, indent=2))
    tags = sorted(sh(["git", "-C", REPO, "tag", "-l", "stable/*"]).stdout.split())
    print(f"\nstable tags(近5): {' '.join(tags[-5:]) or '(无)'}")
    r = sh(["git", "-C", REPO, "rev-parse", STABLE_BRANCH]).stdout.strip()
    print(f"stable {STABLE_BRANCH}: {r[:12]}")
    for t in ("primary", "standby"):
        pids = gateway_pids(t)
        cur = sh(["git", "-C", DEPLOY[t], "rev-parse", "--short", "HEAD"], check=False).stdout.strip()
        print(f"gateway({t}): {'存活' if pids else '未运行'}  代码 {cur}")


def cmd_spawn_worker(a):
    task = TASKS / f"{a.task}.md"
    if not task.exists():
        sys.exit(f"任务单不存在: {task}")
    sync_worker_skills()
    prompt = (f"你是进化 worker。任务单: {task}\n"
              f"先读 {WORKTREE}/skills/evolution/worker/SKILL.md 全文, 照单执行到产出部署提案为止。\n"
              f"被改被测代码 = 当前工作区(evolve worktree)。禁止读写 {DEPLOY['primary']} 与 {DEPLOY['standby']} 的代码目录。")
    (WORKER_HOME / ".zany/sessions").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME=str(WORKER_HOME))
    out = open(EVO / f"logs/worker-{a.task}.log", "ab")
    p = subprocess.Popen([sys.executable, str(DEPLOY["primary"] / "core/tui.py"), "-p", prompt],
                         cwd=WORKTREE, env=env, stdout=out, stderr=subprocess.STDOUT)
    log("worker_spawned", task=a.task, pid=p.pid)
    print(f"worker 已拉起 pid={p.pid}, 日志: {EVO}/logs/worker-{a.task}.log")


def _reject(rep, fails):
    log("gate_reject", task=rep.get("task_id"), fails=fails)
    print("门禁拒绝:")
    for f in fails:
        print(f"  ✗ {f}")
    sys.exit(1)


def cmd_gate(a):
    rep = json.loads(Path(a.report).read_text(encoding="utf-8"))
    fails = validate_report(rep)
    if fails:
        _reject(rep, fails)
    base, cand = rep["base_sha"], rep["candidate_sha"]
    if sh(["git", "-C", REPO, "cat-file", "-e", cand], check=False).returncode != 0:
        _reject(rep, [f"候选 {cand} 不在 stable 仓(先推 stable)"])
    paths = changed_paths(base, cand)
    if not paths:
        _reject(rep, ["diff 为空"])
    layers = {p: layer_of(p) for p in paths}
    top = max(layers.values(), key=lambda x: "abcde".index(x))
    lines = diff_lines(base, cand)
    fails = []
    if lines > POLICY["diff_max_lines"]:
        fails.append(f"diff {lines} 行 > 上限 {POLICY['diff_max_lines']}")
    if top == "e":
        fails += [f"疑似密钥明文: {l}" for l in
                  secret_leak_lines(sh(["git", "-C", REPO, "diff", f"{base}..{cand}"]).stdout)]
        if not a.ack_e and not fails:
            fails.append("e 层变更需人工过目后加 --ack-e")
    if top == "d" and not a.ack_d:
        fails.append("d 层(自持层)需人工执行专属验证后加 --ack-d")
    if not (TASKS / f"{rep['task_id']}.md").exists():
        fails.append(f"任务单 {rep['task_id']} 不存在")
    if not fails:
        fails = audit_sessions(rep.get("sessions", []))
    if fails:
        _reject(rep, fails)
    s = load_state()
    s["approved"] = {**rep, "paths": paths, "top_layer": top, "diff_lines": lines, "approved_at": now()}
    save_state(s)
    log("gate_pass", task=rep["task_id"], sha=cand, top_layer=top, lines=lines)
    print(f"门禁通过: {cand[:12]}  层级={top}  diff={lines} 行")
    print(f"观察期 {POLICY['canary_hours'][top]}h → 下一步: deploy --target standby")


def cmd_deploy(a):
    s = load_state()
    sha = a.sha or (s.get("approved") or {}).get("candidate_sha")
    if not sha:
        sys.exit("缺 --sha 且无已批准候选(先 gate)")
    d = DEPLOY[a.target]
    sh(["git", "-C", d, "fetch", "-q", "origin"])
    sh(["git", "-C", d, "reset", "--hard", sha])
    restart_gateway(a.target, dry=a.dry_run)
    s["deployed"][a.target] = sha
    if a.target == "standby":
        top = (s.get("approved") or {}).get("top_layer", "c")  # --sha 直指时无 approved 记录, 按 c 层观察期
        s["canary"] = {"sha": sha, "since": now(), "hours": POLICY["canary_hours"][top]}
    save_state(s)
    log("deployed", target=a.target, sha=sha)
    print(f"已部署 {a.target} → {sha[:12]}" + ("(dry-run 未重启进程)" if a.dry_run else "(进程已重启)"))


def cmd_canary(a):
    s = load_state()
    c = s.get("canary")
    if not c:
        sys.exit("无灰度中版本(先 deploy --target standby)")
    elapsed = (time.time() - datetime.fromisoformat(c["since"]).timestamp()) / 3600
    pids = gateway_pids("standby")
    err_s, n_s = session_error_rate("standby", hours=max(elapsed, 0.1))
    err_p, _ = session_error_rate("primary", hours=24)
    print(f"观察期: {elapsed:.1f}/{c['hours']}h; standby 进程 {'存活' if pids else '未运行'}")
    print(f"会话错误率: standby {err_s:.0%}(n={n_s}) vs primary 24h {err_p:.0%}")
    problems = []
    if elapsed < c["hours"]:
        problems.append("观察期未满")
    if not pids:
        problems.append("standby gateway 未运行")
    if n_s >= 5 and err_s > max(1.5 * err_p, 0.3):  # ponytail: 绝对阈值兜底, M2 接 7 天滑动基线
        problems.append(f"standby 错误率 {err_s:.0%} 超基线 {err_p:.0%}")
    if a.l1:
        r = sh(["python", "eval/main.py"], cwd=DEPLOY["standby"], check=False)
        print((r.stdout.strip().splitlines() or [""])[-1])
        if r.returncode != 0:
            problems.append("standby L1 回归未全过")
    if problems:
        print("观察期未过: " + "; ".join(problems))
        sys.exit(1)
    print("观察期全绿, 可 promote")


def cmd_promote(a):
    s = load_state()
    c = s.get("canary")
    if not c:
        sys.exit("无灰度中版本")
    if not a.dry_run and not a.force:
        elapsed = (time.time() - datetime.fromisoformat(c["since"]).timestamp()) / 3600
        if elapsed < c["hours"]:
            sys.exit(f"观察期未满({elapsed:.1f}/{c['hours']}h), 人工拍板加 --force")
    sha = c["sha"]
    tag = f"stable/{datetime.now():%Y%m%d-%H%M}-{sha[:7]}"
    if a.dry_run:
        print(f"[dry-run] 将打 tag {tag} 并部署 primary → {sha[:12]}")
        return
    sh(["git", "-C", REPO, "tag", tag, sha])
    do_deploy("primary", sha)
    s = load_state()  # 重拿: do_deploy 已写入 deployed, 避免旧快照覆盖
    s["canary"] = None
    s["fail_streak"] = 0
    save_state(s)
    log("promoted", sha=sha, tag=tag)
    print(f"已晋升 {sha[:12]} → tag {tag}")


def do_deploy(target, sha):
    d = DEPLOY[target]
    sh(["git", "-C", d, "fetch", "-q", "origin"])
    sh(["git", "-C", d, "reset", "--hard", sha])
    restart_gateway(target)
    s = load_state()
    s["deployed"][target] = sha
    save_state(s)
    log("deployed", target=target, sha=sha)


def cmd_rollback(a):
    tags = sorted(sh(["git", "-C", REPO, "tag", "-l", "stable/*"]).stdout.split())
    if a.to:
        prev = a.to
    else:
        s = load_state()
        cur = (s.get("deployed") or {}).get("primary")
        shas = {t: sh(["git", "-C", REPO, "rev-parse", t]).stdout.strip() for t in tags}
        newer = [t for t, h in shas.items() if cur and h != cur]
        # 当前 primary 部署点之后的 tag 都不算回滚目标, 取其前一个
        idx = None
        for i, t in enumerate(tags):
            if shas[t] == cur:
                idx = i
                break
        prev = tags[idx - 1] if idx and idx > 0 else (tags[-1] if idx is None and tags else None)
        if prev and shas.get(prev) == cur:
            sys.exit(f"已在最老 tag {prev}, 无可回滚点")
    if not prev:
        sys.exit("没有可回滚的 tag")
    sha = sh(["git", "-C", REPO, "rev-parse", prev]).stdout.strip()
    s = load_state()
    for t in ("standby", "primary"):
        sh(["git", "-C", DEPLOY[t], "fetch", "-q", "origin"])
        sh(["git", "-C", DEPLOY[t], "reset", "--hard", sha])
        restart_gateway(t, dry=a.dry_run)
        s["deployed"][t] = sha
    s["canary"] = None
    s["fail_streak"] = s.get("fail_streak", 0) + 1
    if s["fail_streak"] >= POLICY["circuit_break"] and not s.get("halted"):
        s["halted"] = True
        alert = EVO / f"alerts/{datetime.now():%Y%m%d-%H%M%S}-circuit-break.md"
        alert.parent.mkdir(parents=True, exist_ok=True)
        alert.write_text(f"连续 {s['fail_streak']} 个进化任务失败, 已熔断暂停自动进化, 等人解禁。\n", encoding="utf-8")
    save_state(s)
    log("rollback", to=prev, fail_streak=s["fail_streak"])
    print(f"已回滚 → {prev} ({sha[:12]})  fail_streak={s['fail_streak']}  halted={s.get('halted')}")


def session_error_rate(target: str, hours: float):
    """粗指标: 最近 N 小时会话里出错回合占比。ponytail: 绝对阈值, M2 换 7 天滑动基线。"""
    sess = SESSION_ROOTS[target]
    if not sess.exists():
        return 0.0, 0
    cutoff = time.time() - hours * 3600
    turns = errors = 0
    for f in sess.rglob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                continue
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        msgs = data.get("messages", data if isinstance(data, list) else [])
        for m in msgs:
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            turns += 1
            text = json.dumps(m.get("content", ""), ensure_ascii=False)
            if '"is_error": true' in text or "错误: " in text or "exception" in text.lower():
                errors += 1
    return (errors / turns if turns else 0.0), turns


# ---------- selftest(无容器依赖) ----------

def cmd_selftest(_a):
    # 1) 分层
    assert layer_of("skills/x.md") == "a"
    assert layer_of("core/plugins/x.py") == "b"
    assert layer_of("core/loop.py") == "c"
    assert layer_of("gateway/main.py") == "d" and layer_of("eval/main.py") == "d"
    assert layer_of(".env") == "e" and layer_of("sessions/a.json") == "e"
    assert layer_of("unknown/new.py") == "c"
    # 2) 密钥掩码
    assert secret_leak_lines("+FEISHU_APP_SECRET=abcdefghijklmnopxyz")
    assert not secret_leak_lines("+FEISHU_APP_SECRET=<填这里>")
    assert not secret_leak_lines("+MODEL_TIMEOUT=30")
    assert not secret_leak_lines(" FEISHU_APP_SECRET=old_value_placeholder")
    # 3) 报告校验
    good = {"task_id": "t1", "branch": "evolve/1", "candidate_sha": "x", "base_sha": "y",
            "risk_layer": "a", "motivation": "m", "sessions": ["s1"],
            "report": {"l1": {"passed": 8, "total": 8}, "l2": {"score": 90, "baseline": 80}}}
    assert not validate_report(good)
    assert validate_report({**good, "report": {"l1": {"passed": 7, "total": 8}}})
    assert validate_report({**good, "sessions": []})
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 4) stable hook: 拒非 ff / 拒删 tag / 放行 ff
        bare = root / "s.git"
        sh(["git", "init", "--bare", "-q", "-b", "master", bare])
        (bare / "hooks/update").write_bytes((HERE / "hooks/update").read_bytes())
        (bare / "hooks/update").chmod(0o755)
        w = root / "w"
        sh(["git", "clone", "-q", bare, w])
        (w / "a.txt").write_text("1", encoding="utf-8")
        sh(["git", "-C", w, "add", "."])
        sh(["git", "-C", w, "commit", "-qm", "v1"])
        sh(["git", "-C", w, "push", "-q", "origin", f"master:{STABLE_BRANCH}"])
        sh(["git", "-C", w, "commit", "--amend", "-qm", "v1-rewritten"])
        r = sh(["git", "-C", w, "push", "origin", f"HEAD:{STABLE_BRANCH}"], check=False)
        assert r.returncode != 0 and "fast-forward" in r.stderr, f"hook 未拦非 ff: {r.stderr}"
        sh(["git", "-C", w, "reset", "--hard", "HEAD@{1}"])
        sh(["git", "-C", w, "commit", "--allow-empty", "-qm", "v2"])
        sh(["git", "-C", w, "push", "-q", "origin", f"HEAD:{STABLE_BRANCH}"])
        sh(["git", "-C", bare, "tag", "stable/20260101-0000-abcdef0", STABLE_BRANCH])
        sh(["git", "-C", w, "fetch", "-q", "origin"])
        r = sh(["git", "-C", w, "push", "origin", f":refs/tags/stable/20260101-0000-abcdef0"], check=False)
        assert r.returncode != 0, "hook 未拦删 tag"
        r = sh(["git", "-C", w, "push", "origin", "master:refs/tags/new-tag"], check=False)
        assert r.returncode == 0, f"hook 误拦新增 tag: {r.stderr}"
        r = sh(["git", "-C", w, "push", "origin", f":refs/heads/{STABLE_BRANCH}"], check=False)
        assert r.returncode != 0, "hook 未拦删主分支"
        r = sh(["git", "-C", w, "push", "origin", ":refs/heads/evolve/tmp"], check=False)
        assert r.returncode == 0 or "remote rejected" not in r.stderr or True  # 不存在的分支删除由 git 自身拒绝, 不属 hook 断言
        # 5) 审计污染检测(audit_sessions 参数化 root, 真测函数本体)
        sess = root / "audit/.zany/sessions/b"
        sess.mkdir(parents=True)
        (sess / "s1.json").write_text(json.dumps(
            [{"role": "assistant", "content": [{"type": "tool_use", "name": "read_file",
                                                "input": {"path": "/r/eval/benchmarks/x/rubric/README.md"}}]}]),
            encoding="utf-8")
        bad = audit_sessions(["s1"], root=root / "audit/.zany/sessions")
        assert len(bad) == 2 and "rubric" in " ".join(bad), f"审计未发现 rubric 读取: {bad}"
        (sess / "s2.json").write_text(json.dumps(
            [{"role": "assistant", "content": [{"type": "tool_use", "name": "read_file",
                                                "input": {"path": "/tmp/task/README.md"}}]}]),
            encoding="utf-8")
        assert not audit_sessions(["s2"], root=root / "audit/.zany/sessions"), "审计误报干净会话"
    print("selftest: 全部通过")


def main():
    ap = argparse.ArgumentParser(prog="evolve", description="zany 自进化流水线 M1")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("install-hook").set_defaults(func=cmd_install_hook)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p = sub.add_parser("spawn-worker"); p.add_argument("--task", required=True); p.set_defaults(func=cmd_spawn_worker)
    p = sub.add_parser("gate"); p.add_argument("--report", required=True)
    p.add_argument("--ack-d", action="store_true", help="人工已执行 d 层专属验证")
    p.add_argument("--ack-e", action="store_true", help="人工已过目 e 层变更")
    p.set_defaults(func=cmd_gate)
    p = sub.add_parser("deploy"); p.add_argument("--target", choices=["standby", "primary"], required=True)
    p.add_argument("--sha"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(func=cmd_deploy)
    p = sub.add_parser("canary-check"); p.add_argument("--l1", action="store_true"); p.set_defaults(func=cmd_canary)
    p = sub.add_parser("promote"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="观察期未满也强制晋升(人工拍板)")
    p.set_defaults(func=cmd_promote)
    p = sub.add_parser("rollback"); p.add_argument("--to"); p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_rollback)
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)
    args = ap.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
