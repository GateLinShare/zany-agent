"""peer 工具: 主代理对 peer zany 的控制面 —— send / check / restart / list。

peer 是另一个独立跑着的 zany 进程(可以是另一份 checkout = 不同代码):
- 对端起服务: uv run tui.py --serve(同机/远程同一条路)
- 注册表 workdir/peers/<name>.json: {"url": "http://host:port", "token": "与对端 PEER_TOKEN 一致"}
传输语义照 A2A(task_id + working/completed/failed), 换官方 a2a-sdk 时只动 _req。
"""

import json
import urllib.request
from pathlib import Path

from core.tools import Tool

HTTP_TIMEOUT = 60


def _req(url: str, token: str, method: str = "GET", body: dict | None = None) -> dict:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read())


def make_peer_tool(workdir: str) -> Tool:
    peer_dir = Path(workdir) / "peers"

    def run(args: dict) -> str:
        try:
            action = args.get("action", "")
            if action == "list":
                files = sorted(peer_dir.glob("*.json")) if peer_dir.is_dir() else []
                return "\n".join(f"{p.stem}: {p.read_text(encoding='utf-8').strip()}" for p in files) \
                    or "(无 peer, 建 peers/<name>.json: url/token)"
            name = args.get("name") or ""
            if not name:
                return "错误: name 必填(见 peers/<name>.json)"
            p = peer_dir / f"{name}.json"
            if not p.is_file():
                return f"错误: peer 不存在: {name}, 先建 {p}"
            cfg = json.loads(p.read_text(encoding="utf-8"))
            url, token = cfg["url"].rstrip("/"), cfg.get("token", "")
            if action == "send":
                r = _req(f"{url}/message", token, "POST", {"task": args["task"]})
                return f"已派发: task_id={r['task_id']} state={r['state']}(用 peer check 查结果)"
            if action == "check":
                r = _req(f"{url}/task/{args['task_id']}", token)
                return f"state={r['state']}\n{r.get('result', '')}".strip()
            if action == "restart":
                pull = bool(args.get("pull"))
                _req(f"{url}/restart", token, "POST", {"pull": pull})
                return "重启指令已发: 对端按磁盘上的当前代码重起新进程" + ("(先 git pull)" if pull else "")
            return f"错误: 未知 action {action}"
        except Exception as e:
            return f"错误: {e}"

    return Tool(
        name="peer",
        description=(
            "控制 peer zany(独立进程, 可在另一台机器/另一份代码上): "
            "send 派任务(立即返回, 对端跑完才标记 completed), check 查进度/收结论, "
            "restart 让对端拉新代码自我重启, list 列已有 peer。peer 注册在 peers/<name>.json。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["send", "check", "restart", "list"]},
                "name": {"type": "string", "description": "peer 名(peers/<name>.json 的 stem)"},
                "task": {"type": "string", "description": "任务完整描述(send 必填)"},
                "task_id": {"type": "string", "description": "send 返回的任务 id(check 必填)"},
                "pull": {"type": "boolean", "description": "restart 时是否先 git pull(代码走 git 分发才用, 默认直接用当前磁盘代码)"},
            },
            "required": ["action"],
        },
        run=run,
    )


def register(workdir: str) -> list:
    return [make_peer_tool(workdir)]
