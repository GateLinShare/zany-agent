"""restart 插件: AI 改完自身(框架)代码后调用, 轮末 execv 重起进程载入新代码。

状态(_pending_restart)和工具在同一模块 —— 宿主(tui/serve/gateway)轮末读
pending_restart() 决定是否 execv, 与本插件一起装拆。TUI 自动接 -r 最近会话;
serve 模式重起后继续监听同一端口。只装给主代理(build_agent) —— 子代理在同
进程内, execv 会连带杀掉主进程。
"""

import sys
from pathlib import Path

from core.tools import Tool

# 自我重启队列: 非 None = 本轮结束后按这组 argv execv 载入新代码。
# 延迟到轮末是为了先把当前轮落盘 —— mid-turn execv 会把本轮对话丢了。
_pending_restart: list[str] | None = None


def pending_restart() -> list[str] | None:
    return _pending_restart


def make_restart_tool() -> Tool:
    # 插件文件在 core/plugins/ 下, 上溯两级才是仓库根(搬进插件缝时 parent.parent 会算成 core/)
    root = Path(__file__).resolve().parents[2]
    entry = Path(sys.argv[0]).resolve()  # 进程入口: tui.py 或 gateway/main.py

    def run(args: dict) -> str:
        global _pending_restart
        argv = sys.argv[1:]
        if argv and argv[0] == "-p":
            return "无头模式一轮即退: 下次调用自然用新代码, 无需重启"
        if entry == root / "gateway" / "main.py":  # gateway 进程: execv 回自己, ws 断几秒自动重连
            _pending_restart = [sys.executable, str(entry), *argv]
            return "重启已排定: 本轮收尾后 gateway 进程重起, 飞书短暂断开后自动重连"
        if not argv or argv[0] in ("--resume", "-r"):
            argv = [*argv, "-r"]  # TUI: 自动接最近会话, 重启后对话续上
        _pending_restart = [sys.executable, str(root / "core" / "tui.py"), *argv]
        return "重启已排定: 本轮收尾后进程重起, 新代码生效(本轮照常完成)"

    return Tool(
        name="restart",
        description=(
            "改完本 agent 自身(框架)的代码后调用: 本轮结束后进程自我重启并载入新代码, "
            "对话/服务自动接续。普通的用户代码任务不要用它。"
        ),
        input_schema={"type": "object", "properties": {}},
        run=run,
    )


def register(workdir: str) -> list:
    return [make_restart_tool()]
