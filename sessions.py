#!/usr/bin/env python3
"""会话记录与恢复: 每个 session 一个 JSON 文件, 存在 sessions/ 目录。

- chat.py 每轮 invoke 后自动保存（首次对话时创建文件）
- `uv run chat.py --resume` 列出最近会话, 选一条恢复（消息原样带回去,
  上下文接着聊）
- 序列化用 langchain 自带的 message_to_dict / messages_from_dict,
  不自造格式。
"""

import json
import time
from datetime import datetime
from pathlib import Path

from langchain_core.messages import message_to_dict, messages_from_dict

SESSIONS_DIR = Path(__file__).resolve().parent / "sessions"


def new_session_file() -> Path:
    SESSIONS_DIR.mkdir(exist_ok=True)
    return SESSIONS_DIR / (time.strftime("%Y%m%d_%H%M%S") + ".json")


def save_session(path: Path, state: dict) -> None:
    path.parent.mkdir(exist_ok=True)  # 指定 id 新开会话时目录可能还没建
    data = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "messages": [message_to_dict(m) for m in state["messages"]],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def list_sessions(limit: int = 10) -> list:
    """最近 limit 个会话: [(路径, 时间, 首条用户消息截断, 条数)]。"""
    out = []
    for f in sorted(SESSIONS_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            msgs = data.get("messages", [])
        except Exception:
            continue
        first_user = next(
            (
                (m.get("data") or {}).get("content", "")
                for m in msgs
                if m.get("type") == "human"
            ),
            "",
        )
        if not isinstance(first_user, str):
            first_user = str(first_user)
        out.append((f, data.get("ts", f.stem), first_user[:40], len(msgs)))
    return out


def load_session(path: Path) -> list:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return messages_from_dict(data["messages"])
