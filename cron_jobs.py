#!/usr/bin/env python3
"""定时任务: 到点把 prompt 交给正在跑的 chat.py REPL（唤醒 agent 干活）。

任务声明在同目录 cron_jobs.json（标准 5 段 cron 表达式）:

    [
      {"name": "日报", "cron": "0 9 * * *", "prompt": "生成本周工作日报"}
    ]

只做"到点注入 prompt"，不单独起 agent 进程 —— REPL 没开就不触发。
编辑 cron_jobs.json 即时生效（每轮重新读盘），无需重启。
"""

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from croniter import croniter

CRON_FILE = Path(__file__).resolve().parent / "cron_jobs.json"


def load_tasks(path: Path = CRON_FILE) -> list:
    """读任务清单; 缺字段/表达式坏的条目跳过并告警, 不拖累其他任务。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"(cron_jobs.json 读取失败, 忽略本轮: {e})")
        return []
    tasks = []
    for i, t in enumerate(data if isinstance(data, list) else []):
        if not (isinstance(t, dict) and t.get("name") and t.get("prompt")):
            print(f"(cron_jobs.json 第 {i} 项缺 name/prompt, 跳过)")
            continue
        try:
            croniter(t["cron"])  # 仅校验表达式
        except Exception as e:
            print(f"(cron 任务 [{t['name']}] 表达式非法: {e})")
            continue
        tasks.append(t)
    return tasks


def _matches(expr: str, now: datetime) -> bool:
    """now 所在分钟是否命中 expr（取前一分钟的下一个触发点判断）。"""
    prev = croniter(expr, now - timedelta(minutes=1)).get_next(datetime)
    return prev <= now


def start_scheduler(on_fire, path: Path = CRON_FILE, poll_s: float = 20.0):
    """后台线程轮询, 命中时调用 on_fire(task)。同一分钟同一任务只触发一次。"""
    def loop():
        fired = set()  # (name, 分钟槽)
        while True:
            now = datetime.now()
            slot = now.strftime("%Y%m%d%H%M")
            fired = {k for k in fired if k[1] == slot}  # 只留本分钟, 防无限涨
            for t in load_tasks(path):
                key = (t["name"], slot)
                if key not in fired and _matches(t["cron"], now):
                    fired.add(key)
                    try:
                        on_fire(t["name"], t["prompt"])
                    except Exception as e:
                        print(f"(定时任务 [{t['name']}] 回调失败: {e})")
            time.sleep(poll_s)

    threading.Thread(target=loop, daemon=True).start()
