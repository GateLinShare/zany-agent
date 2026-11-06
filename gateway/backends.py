"""backend 插拔层(docs/codex-backend.md): 一轮对话 = 文本进/文本出。

AGENT_BACKEND 选执行器: zany(默认, 进程内 build_agent) | codex(subprocess `codex exec`)。
切换 = 改 .env + gateway/run.sh restart; 两边会话各存各的(zany: sessions json /
codex: ~/.codex/sessions rollout), 来回切互不污染。
"""

import json
import os
import subprocess
from pathlib import Path

ZANY_HOME = Path.home() / ".zany"
CODEX_THREADS = ZANY_HOME / "codex_threads.json"  # chat_id → codex thread_id


def make_backend(workdir: str):
    name = os.environ.get("AGENT_BACKEND", "zany").strip().lower()
    cls = CodexBackend if name == "codex" else ZanyBackend
    return cls(workdir)


class ZanyBackend:
    name = "zany"

    def __init__(self, workdir: str):
        self.workdir = workdir

    def new_session(self, chat_id: str) -> None:
        from gateway.channels.feishu import session_path
        session_path(self.workdir, chat_id).unlink(missing_ok=True)

    def run_turn(self, chat_id: str, text: str, fresh: bool) -> str:
        # 函数内 import: codex backend 不该为加载 zany 组装(agent.py 载 .env/拼 prompt)买单
        from agent import build_agent
        from core.messages import user
        from core.sessions import load_session_data, save_session
        from gateway.channels.feishu import session_path
        from gateway.cron import CronStore, make_cron_tools

        path = session_path(self.workdir, chat_id)
        if fresh:  # cron fresh=true: 全新空会话跑, 不读不写历史
            msgs, a = [], build_agent(self.workdir, with_mcp=False)
        else:
            data = load_session_data(path) if path.exists() else {"messages": [], "usage": None}
            msgs = data["messages"]
            a = build_agent(self.workdir, with_mcp=False)
            if data.get("usage"):
                a.restore(data["usage"])
        a.add_tools(make_cron_tools(CronStore(), chat_id))

        msgs.append(user(text))
        out = []
        for ev in a.stream(msgs):
            if ev[0] == "text":
                out.append(ev[1])
        if not fresh:  # fresh 轮不落盘: 不产生会话文件, 也不动既有会话
            save_session(path, msgs, a.usage, None, self.workdir)
        return "".join(out).strip() or "(这轮没有文本输出)"


class CodexBackend:
    name = "codex"

    def __init__(self, workdir: str):
        self.workdir = workdir

    def new_session(self, chat_id: str) -> None:
        threads = _load_threads()
        if threads.pop(chat_id, None) is not None:
            _save_threads(threads)

    def run_turn(self, chat_id: str, text: str, fresh: bool) -> str:
        threads = _load_threads()
        thread_id = None if fresh else threads.get(chat_id)
        # ponytail: 飞书无人值守没人点审批 —— danger-full-access + approval never,
        # 与 zany execute 直跑 shell 同一信任模型(白名单=root), 要收紧改 CODEX_SANDBOX env
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check",
               "-C", self.workdir,
               "-s", os.environ.get("CODEX_SANDBOX", "danger-full-access"),
               "-c", 'approval_policy="never"']
        if thread_id:
            cmd += ["resume", thread_id]
        cmd.append(text)
        timeout = int(os.environ.get("CODEX_TIMEOUT", "900"))
        print(f"(codex 轮 {chat_id[:12]}… thread={thread_id or '新'})", flush=True)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"⏱ codex 超时(>{timeout}s), 已终止。"
        except FileNotFoundError:
            return "⚠️ codex 命令不存在(codex-cli 未安装或不在 PATH)。"
        if proc.returncode != 0:
            tail = [l for l in (proc.stderr or "").strip().splitlines() if l.strip()][-3:]
            return f"⚠️ codex 失败(rc={proc.returncode}): {' / '.join(tail) or '无输出'}"

        new_id, reply = _parse_events(proc.stdout)
        if not fresh and new_id and new_id != thread_id:  # fresh 轮不落映射(镜像 zany fresh 不落盘)
            threads[chat_id] = new_id
            _save_threads(threads)
        return reply or "(codex 这轮没有文本输出)"


def _parse_events(out: str):
    """JSONL 事件流 → (thread_id, 最后一条 agent_message); 非 JSON 行(stderr 混入)忽略。"""
    thread_id, reply = None, ""
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "thread.started":
            thread_id = ev.get("thread_id")
        elif ev.get("type") == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message":
                reply = item.get("text") or ""
    return thread_id, reply


def _load_threads() -> dict:
    try:
        return json.loads(CODEX_THREADS.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_threads(t: dict) -> None:
    ZANY_HOME.mkdir(parents=True, exist_ok=True)
    CODEX_THREADS.write_text(json.dumps(t, indent=1, ensure_ascii=False))
