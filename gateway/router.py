"""router: 每个飞书会话一条 FIFO 队列 + 一个 worker 线程 —— 会话内串行, 会话间并行。

会话执行走 backend 插拔层(docs/codex-backend.md): run_turn = 文本进/文本出,
AGENT_BACKEND 选 zany(进程内 agent, 会话盘为中心) 或 codex(codex exec 子进程)。
cron 注入的 prompt 与用户消息走同一队列(用户消息天然排在前面, 不做抢占)。
"""

import os
import queue
import threading

from gateway.backends import make_backend


class GatewayRouter:
    def __init__(self, workdir: str, channel, home_channel: str = ""):
        self.workdir = workdir
        self.channel = channel          # 有 .send(chat_id, text) 的渠道(当前只有飞书)
        self.home_channel = home_channel  # cron 任务缺 chat_id 时的兜底收件人
        self.backend = make_backend(workdir)
        self._workers: dict[str, tuple[queue.Queue, threading.Thread]] = {}
        self._lock = threading.Lock()
        self.stopping = threading.Event()

    # ---- 入口 ----

    def submit(self, chat_id: str, text: str, source: str = "user", fresh: bool = False) -> None:
        """入队一条消息(chat_id 为空时落到 home_channel)。fresh=cron 任务每次开新会话跑。"""
        if not chat_id:
            chat_id = self.home_channel
        q, _ = self._worker(chat_id)
        q.put({"text": text, "source": source, "fresh": fresh})
        if source != "user":  # cron 触发是异步的, 没有用户消息对应, 提示一句; 用户消息直接回结果
            self.channel.send(chat_id, "📋 定时任务触发, 开始执行")

    def submit_task(self, task: dict) -> None:
        """cron tick 回调: task 带 chat_id(建任务时的会话), 缺失落 home_channel。"""
        self.submit(task.get("chat_id") or "", task["prompt"], source="cron",
                    fresh=bool(task.get("fresh")))

    def shutdown(self, timeout: float = 300.0) -> None:
        """停收新活 → 等在跑的一轮收尾; 超时放弃(daemon 线程随进程退出)。"""
        self.stopping.set()
        for _, t in self._workers.values():
            t.join(timeout=min(timeout, 30.0))

    # ---- worker ----

    def _worker(self, chat_id: str):
        with self._lock:
            hit = self._workers.get(chat_id)
            if hit:
                return hit
            q: queue.Queue = queue.Queue()
            t = threading.Thread(target=self._loop, args=(chat_id, q),
                                 daemon=True, name=f"gw-{chat_id[:12]}")
            self._workers[chat_id] = (q, t)
            t.start()
            return (q, t)

    def _loop(self, chat_id: str, q: queue.Queue) -> None:
        while True:
            item = q.get()
            if item is None:
                return
            try:
                self._run_one(chat_id, item)
            except Exception as e:  # 一条消息炸不掉 worker
                print(f"(会话 {chat_id[:12]}… 处理失败: {e})", flush=True)
                try:
                    self.channel.send(chat_id, f"⚠️ 处理出错: {e}")
                except Exception:
                    pass
            if self.stopping.is_set() and q.empty():
                return

    def _run_one(self, chat_id: str, item: dict) -> None:
        print(f"(会话 {chat_id[:12]}… {item['source']}: {item['text'][:80]})", flush=True)

        if item["text"] == "/new":  # 会话管理指令(设计文档 §1.5), 不进模型
            self.backend.new_session(chat_id)
            self.channel.send(chat_id, "🧹 会话上下文已清空, 下条消息开新对话。")
            return

        reply = self.backend.run_turn(chat_id, item["text"], bool(item.get("fresh")))
        self.channel.send(chat_id, reply)

        from core.plugins.restart import pending_restart
        av = pending_restart()  # restart 工具排定的 gateway 自我重启(改完框架代码后)
        if av:  # ponytail: execv 会连带杀其他会话在跑的轮, 单人多会话场景可接受
            self.channel.send(chat_id, "🔄 重启 gateway 载入新代码, 飞书短暂断开后自动重连…")
            os.execv(av[0], av)
