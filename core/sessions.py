"""会话记录与恢复: 每个 session 一个 JSON 文件, 存在 sessions/ 目录。

消息就是 Anthropic 原生 dict, 直接 JSON 序列化 —— 不自造格式, 不需要转换层。
注意: 与旧 deepagents/langchain 格式的 session 文件不兼容(那批文件留着不读)。
"""

import json
import time
from datetime import datetime
from pathlib import Path

SESSIONS_DIR = Path(__file__).resolve().parent.parent / "sessions"


def new_session_file() -> Path:
    SESSIONS_DIR.mkdir(exist_ok=True)
    return SESSIONS_DIR / (time.strftime("%Y%m%d_%H%M%S") + ".json")


def save_session(path: Path, messages: list[dict]) -> None:
    path.parent.mkdir(exist_ok=True)
    data = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "messages": messages,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _is_old_format(msgs: list) -> bool:
    """langchain 时代的格式: {"type": "human", "data": {...}}, 没有 role 键。"""
    return bool(msgs) and any("role" not in m for m in msgs)


def list_sessions(limit: int = 10) -> list:
    """最近 limit 个会话: [(路径, 时间, 首条用户消息截断, 条数)]; 旧格式文件跳过。"""
    out = []
    for f in sorted(SESSIONS_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            msgs = data.get("messages", [])
        except Exception:
            continue
        if _is_old_format(msgs):
            continue
        out.append((f, data.get("ts", f.stem), _first_user(msgs)[:40], len(msgs)))
        if len(out) >= limit:
            break
    return out


def _first_user(msgs: list[dict]) -> str:
    for m in msgs:
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, str):
                return c
            return "".join(
                b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def load_session(path: Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    msgs = data["messages"]
    if _is_old_format(msgs):
        raise SystemExit(f"{path.stem} 是旧版(langchain)格式的会话文件, 不再支持恢复")
    return msgs
