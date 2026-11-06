"""peer 端常驻服务: 让别的机器上的 zany 能被主代理派任务/查状态/换代码重启。

跨机且无 SSH 时(只有网络可达), "重启"只能进程自己干: git pull 新代码后 execv
替换自身。端点(Bearer token 鉴权, 对端 .env 配 PEER_TOKEN; 没配 token 只绑
127.0.0.1 —— hermes 同规则):

    POST /message {"task": "..."}  → {"task_id", "state": "working"}
    GET  /task/<task_id>           → {"state": "working|completed|failed", "result": "..."}
    POST /restart                  → 200 后自我重启(可选 {"pull":true} 先 git pull)

task_id/state 语义照 A2A; 哪天换官方 a2a-sdk, 只动这一层。任务表在内存,
重启即丢 —— 跨重启要留结果再落盘。入口: uv run tui.py --serve [--port 8787]
"""

import json
import os
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .messages import text_of
from .sessions import project_dir, save_session
from core.plugins.restart import pending_restart

ROOT = Path(__file__).resolve().parent.parent


def _self_restart(port: int, pull: bool = False) -> None:
    """换代码重启: 默认直接 execv 重读磁盘上的当前代码(AI 改完文件就够);
    pull=True 才先 git pull(代码走 git 分发时才用)。pull 失败也照常重启。"""
    if pull:
        try:
            r = subprocess.run(["git", "-C", str(ROOT), "pull", "--ff-only"],
                               capture_output=True, text=True, timeout=120)
            print(r.stdout + r.stderr, file=sys.stderr, flush=True)
        except Exception as e:
            print(f"git pull 失败, 按当前代码重启: {e}", file=sys.stderr, flush=True)
    import time
    time.sleep(0.3)  # 让 200 响应先落到 socket
    os.execv(sys.executable, [sys.executable, str(ROOT / "tui.py"), "--serve", "--port", str(port)])


def make_server(agent_factory, workdir: str, port: int = 8787) -> ThreadingHTTPServer:
    """组 HTTP 服务(不阻塞); serve() 里 serve_forever, 自检直接用这个。"""
    tasks: dict[str, dict] = {}
    token = os.environ.get("PEER_TOKEN", "")

    def run_task(task_id: str, task: str) -> None:
        agent = agent_factory(workdir)
        msgs = [{"role": "user", "content": task}]
        try:
            agent.invoke(msgs)  # 一轮跑完整 loop 才落 state —— "做完不中断"
            state = {"state": "completed", "result": text_of(msgs[-1]) or "(无文本输出)"}
        except Exception as e:
            state = {"state": "failed", "result": f"错误: {e}"}
        # 会话落盘: 按对端 workdir 分桶(.sessions/<桶>/peer-<task_id>.json), 可追溯/可回放 ——
        # 不落的话 peer 对话只活在内存里, 重启即蒸发。落盘完成后才对外报 completed(无竞态)
        try:
            save_session(project_dir(workdir) / f"peer-{task_id}.json",
                         msgs, agent.usage, workdir=workdir)
        except Exception as e:
            print(f"peer 会话落盘失败: {e}", file=sys.stderr, flush=True)
        tasks[task_id] = state
        # peer 的 agent 改完自身代码后 restart 排定的: 任务收尾后 execv 载新代码
        av = pending_restart()
        if av:
            print("peer 自我重启(载新代码)...", file=sys.stderr, flush=True)
            os.execv(av[0], av)

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            return not token or self.headers.get("Authorization") == f"Bearer {token}"

        def do_POST(self):
            if not self._authed():
                return self._reply(401, {"error": "bad token"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._reply(400, {"error": "bad json"})
            if self.path == "/message":
                task = (data.get("task") or "").strip()
                if not task:
                    return self._reply(400, {"error": "task 必填"})
                tid = uuid.uuid4().hex[:8]
                tasks[tid] = {"state": "working", "result": ""}
                threading.Thread(target=run_task, args=(tid, task), daemon=True).start()
                self._reply(200, {"task_id": tid, "state": "working"})
            elif self.path == "/restart":
                self._reply(200, {"state": "restarting"})
                threading.Thread(target=_self_restart, args=(port, bool(data.get("pull"))), daemon=True).start()
            else:
                self._reply(404, {"error": "not found"})

        def do_GET(self):
            if not self._authed():
                return self._reply(401, {"error": "bad token"})
            if self.path.startswith("/task/"):
                tid = self.path[len("/task/"):].strip("/")
                t = tasks.get(tid)
                self._reply(200 if t else 404, t or {"error": "unknown task"})
            else:
                self._reply(404, {"error": "not found"})

        def log_message(self, *a):  # 默认逐请求 stderr 太吵
            pass

    # hermes 规则: 没配 token 只绑回环 —— /restart 等于交出机器, 不能裸奔上外网
    host = "127.0.0.1" if not token else "0.0.0.0"
    return ThreadingHTTPServer((host, port), Handler)


def serve(agent_factory, workdir: str, port: int = 8787) -> None:
    httpd = make_server(agent_factory, workdir, port)
    print(f"peer serve: http://{httpd.server_address[0]}:{httpd.server_address[1]} (workdir={workdir})", flush=True)
    httpd.serve_forever()
