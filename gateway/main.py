#!/usr/bin/env python3
"""gateway 入口: 飞书 ws 长连接 + 每会话 agent。启动即长驻, SIGTERM/SIGINT 优雅停。

必需配置(项目 .env 或环境变量):
    FEISHU_APP_ID / FEISHU_APP_SECRET   飞书自建应用凭证
    FEISHU_ALLOWED_USERS                白名单 open_id, 逗号分隔(空 = 所有人收到 open_id 加白引导)
    GATEWAY_WORKDIR                     agent 工作目录(默认当前目录)
可选:
    FEISHU_BOT_OPEN_ID                  bot 自身 open_id(群聊 @ 检测; 不配 = 群聊全过)
    FEISHU_HOME_CHANNEL                 cron 任务缺 chat_id 的兜底会话
"""

import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根进 path(直接跑本文件)

from core import agent  # noqa: E402  先 import: 顺带把项目 .env 载入环境(agent.py 的加载逻辑)

from gateway.cron import CronStore, start_tick
from gateway.router import GatewayRouter


def main() -> None:
    import os

    app_id = os.environ.get("FEISHU_APP_ID", "")
    secret = os.environ.get("FEISHU_APP_SECRET", "")
    allowed = {u.strip() for u in os.environ.get("FEISHU_ALLOWED_USERS", "").split(",") if u.strip()}
    workdir = os.environ.get("GATEWAY_WORKDIR") or os.getcwd()
    if not (app_id and secret):
        sys.exit("缺 FEISHU_APP_ID / FEISHU_APP_SECRET(.env 或环境变量)")
    if not allowed:
        print("⚠ FEISHU_ALLOWED_USERS 未配置: 陌生人发消息将收到 open_id 配置引导, 加白后才能用", flush=True)
    if not Path(workdir).is_dir():
        sys.exit(f"GATEWAY_WORKDIR 不存在: {workdir}")

    import fcntl

    _lock = open(f"/tmp/zany-gateway-{Path(workdir).name}.lock", "w")
    try:  # 单实例: 飞书 ws 会把事件广播给同 app 的所有连接, 双进程 = 双份回复
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("已有 gateway 在跑(同 workdir), 先 stop 再 start")

    from gateway.channels.feishu import FeishuChannel

    router = GatewayRouter(workdir, None, os.environ.get("FEISHU_HOME_CHANNEL", ""))
    channel = FeishuChannel(
        on_message=lambda item: router.submit(item["chat_id"], item["text"]),
        allowed_users=allowed,
        bot_open_id=os.environ.get("FEISHU_BOT_OPEN_ID", ""),
    )
    router.channel = channel  # channel 与 router 相互引用, 构造后回填

    def _stop(signum, _frame):
        print(f"(收到信号 {signum}, 等在跑的会话收尾…)", flush=True)
        router.shutdown()
        os._exit(0)  # 收尾完立即死: lark ws 线程不随 sys.exit 退, 会拖着进程和 flock 不放

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    start_tick(CronStore(), router.submit_task)
    print(f"gateway up: backend={router.backend.name} workdir={workdir} 白名单 {len(allowed)} 人, 等待飞书消息…", flush=True)
    channel.start()  # ws 阻塞


if __name__ == "__main__":
    main()
