"""gateway 定时任务: 飞书侧的 cron。任务存 gateway/cron_jobs.json(手编即时生效, 每轮重读)。

与根目录 cron_jobs.py(TUI REPL 注入)只同形状不硬复用 —— 差异是这里每个任务带
chat_id(到点把 prompt 交给对应飞书会话), 且模型可用 cron_job 工具自己增删查。
"""

from __future__ import annotations  # 类内 def list 遮蔽内置 list, 注解不求值才不炸

import json
import threading
from datetime import datetime, timedelta
import json
from pathlib import Path

from core.tools import Tool

CRON_FILE = Path(__file__).resolve().parent / "cron_jobs.json"


class CronStore:
    """JSON 文件存储: create/delete/list。写盘失败不抛给模型(错误即文本由 run 层转)。"""

    def __init__(self, path: Path = CRON_FILE):
        self.path = path

    def list(self) -> list[dict]:
        return self._load()

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"(cron_jobs.json 读取失败, 当作空: {e})", flush=True)
            return []

    def _save(self, tasks: list[dict]) -> None:
        self.path.write_text(json.dumps(tasks, ensure_ascii=False, indent=1), encoding="utf-8")

    def create(self, name: str, cron: str, prompt: str, chat_id: str, fresh: bool = False) -> str:
        tasks = self._load()
        if any(t["name"] == name for t in tasks):
            return f"错误: 已存在同名任务 [{name}], 换个名字或先 delete_cron"
        try:
            from croniter import croniter
            croniter(cron)  # 仅校验表达式
        except ImportError:
            return "错误: croniter 未安装(uv sync --extra feishu)"
        except Exception as e:
            return f"错误: cron 表达式非法({e}), 用标准 5 段式如 '0 9 * * *'"
        task = {"name": name, "cron": cron, "prompt": prompt, "chat_id": chat_id}
        if fresh:
            task["fresh"] = True  # 每次触发开全新空会话执行(不续跑对话历史)
        tasks.append(task)
        self._save(tasks)
        return f"定时任务 [{name}] 已创建: {cron}"

    def delete(self, name: str) -> str:
        tasks = self._load()
        kept = [t for t in tasks if t["name"] != name]
        if len(kept) == len(tasks):
            return f"错误: 没有叫 [{name}] 的任务; 现有: {', '.join(t['name'] for t in tasks) or '(无)'}"
        self._save(kept)
        return f"定时任务 [{name}] 已删除"


def start_tick(store: CronStore, on_fire, poll_s: float = 20.0):
    """后台线程: 20s 轮询, 命中即 on_fire(task)(整 dict, 含 chat_id)。同一分钟同一任务只触发一次。"""
    import threading

    def _matches(expr: str, now: datetime) -> bool:
        from croniter import croniter
        prev = croniter(expr, now - timedelta(minutes=1)).get_next(datetime)
        return prev <= now

    def loop():
        fired: set[tuple] = set()
        while True:
            now = datetime.now()
            slot = now.strftime("%Y%m%d%H%M")
            fired = {k for k in fired if k[1] == slot}  # 只留本分钟, 防无限涨
            for t in store.list():
                if not all(t.get(k) for k in ("name", "cron", "prompt", "chat_id")):
                    continue
                key = (t["name"], slot)
                try:
                    if key not in fired and _matches(t["cron"], now):
                        fired.add(key)
                        on_fire(t)
                except Exception as e:  # 单任务坏不拖累其他
                    print(f"(定时任务 [{t['name']}] 触发失败: {e})", flush=True)
            threading.Event().wait(poll_s)

    threading.Thread(target=loop, daemon=True).start()


def make_cron_tools(store: CronStore, chat_id: str) -> list[Tool]:
    """cron_job 工具(读/建/删一体): chat_id 闭包绑定 —— 任务永远挂到当前会话。"""

    def _run(args: dict) -> str:
        action = args.get("action")
        if action == "list":
            tasks = store.list()
            if not tasks:
                return "(还没有任何定时任务)"
            lines = []
            for t in tasks:
                mark = ""
                if t.get("chat_id") != chat_id:
                    mark += f" → 会话…{str(t.get('chat_id'))[-6:]}"
                if t.get("fresh"):
                    mark += " [每次新会话]"
                lines.append(f"- {t['name']} | {t['cron']} | {t['prompt'][:50]}{mark}")
            return "\n".join(lines)
        if action == "create":
            return store.create(
                (args.get("name") or "").strip(), (args.get("cron") or "").strip(),
                (args.get("prompt") or "").strip(),
                (args.get("chat_id") or "").strip() or chat_id,
                fresh=bool(args.get("fresh")))
        if action == "delete":
            return store.delete((args.get("name") or "").strip())
        return "错误: action 必须是 list/create/delete"

    return [Tool(
        name="cron_job",
        description=(
            "管理当前飞书会话的定时任务(标准 5 段 cron 表达式, 本地时区)。"
            "list=列出全部任务(含绑到别的会话的, 会标注); create=name+cron+prompt 新建, "
            "到点 gateway 自动把 prompt 交给绑定会话执行并推送结果; delete=name 删除。"
            "创建前和用户确认时间和内容。create 两种可选变体: "
            "chat_id=改绑到指定会话(oc_ 开头, 到点投到那边并续跑它的对话历史, 需 bot 在那个会话里); "
            "fresh=true=每次触发开全新空会话执行, 不带任何对话历史(结果仍推送到绑定会话)。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "delete"]},
                "name": {"type": "string", "description": "任务名(create/delete 必填)"},
                "cron": {"type": "string", "description": "5 段 cron 表达式(create 必填), 如 '0 9 * * 1-5'"},
                "prompt": {"type": "string", "description": "到点执行的指令(create 必填)"},
                "chat_id": {"type": "string", "description": "(可选)改绑到指定会话; 缺省绑当前会话"},
                "fresh": {"type": "boolean", "description": "(可选)true=每次触发开全新空会话执行, 不带历史"},
            },
            "required": ["action"],
        },
        run=_run,
    )]
