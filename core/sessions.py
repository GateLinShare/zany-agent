"""会话记录与恢复: 每个 session 一个 JSON 文件, 存在 .sessions/<项目名>-<hash8>/ 下(按工作目录分桶)。

消息就是 Anthropic 原生 dict, 直接 JSON 序列化 —— 不自造格式, 不需要转换层。
注意: 与旧 deepagents/langchain 格式的 session 文件不兼容(那批文件留着不读)。
"""

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

SESSIONS_DIR = Path(__file__).resolve().parent.parent / ".sessions"


def project_dir(workdir: str) -> Path:
    """工作目录对应的会话桶: .sessions/<basename>-<path hash 前 8 位>/。

    basename 保证可读, hash 保证不同路径同名目录不撞; 恢复只认本桶(会话不跨项目)。
    """
    wd = Path(workdir).resolve()
    tag = hashlib.md5(str(wd).encode()).hexdigest()[:8]
    return SESSIONS_DIR / f"{wd.name}-{tag}"


def new_session_file(workdir: str) -> Path:
    d = project_dir(workdir)
    d.mkdir(parents=True, exist_ok=True)
    return d / (time.strftime("%Y%m%d_%H%M%S") + ".json")


def save_session(
    path: Path,
    messages: list[dict],
    usage: dict | None = None,
    marks: list | None = None,
    workdir: str = "",
) -> None:
    path.parent.mkdir(exist_ok=True)
    data = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "workdir": str(Path(workdir)) if workdir else "",  # 归属审计: 会话跑在哪个工作目录
        "messages": messages,
    }
    if usage:
        data["usage"] = usage  # session 级 token 累计(hermes sessions 表语义)
    if marks:
        # 回合侧账(Thought for Xs / ran N tool calls): at=回合结束时的消息条数,
        # 恢复回放拿它当对齐锚 —— compact 改写历史后锚失配的条目自动丢弃, 不误导
        data["marks"] = marks
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _is_old_format(msgs: list) -> bool:
    """langchain 时代的格式: {"type": "human", "data": {...}}, 没有 role 键。"""
    return bool(msgs) and any("role" not in m for m in msgs)


def list_sessions(workdir: str, limit: int = 10) -> list:
    """当前工作目录的最近 limit 个会话, 按最近活跃(mtime)排序:
    [(路径, 时间, 首条用户消息截断, 条数)]; 旧格式/别的项目的会话不在列。"""
    out = []
    files = sorted(
        project_dir(workdir).glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True
    )
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            msgs = data.get("messages", [])
        except Exception:
            continue
        if _is_old_format(msgs):
            continue
        # 展示 mtime(最后写入时间)而非会话开始时间, 和"最近活跃"排序一致
        ts = time.strftime("%m-%d %H:%M", time.localtime(f.stat().st_mtime))
        out.append((f, ts, _first_user(msgs)[:40], len(msgs)))
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


def load_session_data(path: Path) -> dict:
    """读整个 session 文件(含 usage 账本), 旧格式报错。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if _is_old_format(data.get("messages", [])):
        raise SystemExit(f"{path.stem} 是旧版(langchain)格式的会话文件, 不再支持恢复")
    return data


def load_session(path: Path) -> list[dict]:
    return load_session_data(path)["messages"]
