#!/usr/bin/env python3
"""对话入口（REPL / 单次提问），agent 构建见 agent.py。

Usage:
    uv run chat.py            # interactive REPL
    uv run chat.py "问题"     # single shot
    uv run chat.py --resume   # 从历史会话恢复（选一条接着聊）
"""

import os
import queue
import select
import sys

from langchain_core.messages import HumanMessage

from agent import TOOLS, build_agent, text_of
from cron_jobs import start_scheduler
from mcp_tools import config_stamp
from sessions import list_sessions, load_session, new_session_file, save_session


def read_input(prompt: str) -> str:
    # 终端可能发 GBK（实测 root@容器 ssh 默认 locale 非 UTF-8）,
    # input() 按 UTF-8 解码直接炸; 先按 UTF-8 试, 不行退 GBK
    print(prompt, end="", flush=True)
    line = sys.stdin.buffer.readline()
    if not line:
        raise EOFError
    for enc in ("utf-8", "gbk"):
        try:
            return line.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return line.decode("utf-8", "replace").strip()


def show_history(messages: list) -> None:
    """恢复后回显历史（工具消息跳过, 全文显示）。"""
    for m in messages:
        t = text_of(m).strip()
        if not t:
            continue
        if m.type == "human":
            print(f"你> {t}")
        elif m.type == "ai":
            print(f"助手> {t}")


def resume_flow() -> "tuple[list, Path] | None":
    """列出会话选一条载入; 取消返回 None。返回 (消息, 会话文件路径)。"""
    picks = list_sessions()
    if not picks:
        print("sessions/ 下没有历史会话\n")
        return None
    for i, (f, ts, snippet, n) in enumerate(picks, 1):
        print(f"{i}. {ts} ({n} 条) {snippet}")
    choice = read_input("选择序号(回车取消)> ")
    if not (choice.isdigit() and 1 <= int(choice) <= len(picks)):
        return None
    sess_file = picks[int(choice) - 1][0]
    msgs = load_session(sess_file)
    print(f"已恢复 {len(msgs)} 条消息:")
    show_history(msgs)
    print()
    return msgs, sess_file


def main() -> None:
    workdir = os.getcwd()
    agent = build_agent(workdir)
    print(f"deepagents mini-agent (tools: {', '.join(TOOLS)}) | 工作目录: {workdir}")
    print("输入 exit/quit 退出; --resume 恢复历史会话\n")

    state = {"messages": []}
    sess_path = None
    if len(sys.argv) > 1 and sys.argv[1] not in ("--resume", "-r"):  # single shot
        state = agent.invoke({"messages": [HumanMessage(sys.argv[1])]})
        print(text_of(state["messages"][-1]))
        return

    if "--resume" in sys.argv or "-r" in sys.argv:
        resumed = resume_flow()
        if resumed is None:
            return
        state["messages"], sess_path = resumed

    # 定时任务: 调度线程只入队, 主循环统一 invoke（agent 非线程安全）
    wakeup: "queue.Queue[str]" = queue.Queue()
    start_scheduler(lambda name, prompt: wakeup.put(prompt))

    def invoke(text: str):
        nonlocal agent, sess_path
        # mcp_servers.json 变了 → 重建 agent(dcode 同款:重建图,messages 原样带过去;
        # skills 无需重建,SkillsMiddleware 每次 invoke 都会重扫目录)
        nonlocal mcp_stamp
        if config_stamp() != mcp_stamp:
            mcp_stamp = config_stamp()
            try:
                agent = build_agent(workdir)
                print("\n(检测到 mcp_servers.json 变更,已重载 MCP 工具)\n")
            except Exception as e:  # 编辑到一半的坏 json:保留旧 agent
                print(f"\n(MCP 重载失败,沿用旧配置: {e})\n")
        state["messages"] = agent.invoke(
            {"messages": state["messages"] + [HumanMessage(text)]}
        )["messages"]
        # 每轮落盘一次: 首轮创建会话文件, 之后覆盖写（崩溃最多丢当轮）
        if sess_path is None:
            sess_path = new_session_file()
        try:
            save_session(sess_path, state)
        except Exception as e:
            print(f"(会话保存失败: {e})")
        print(f"\n助手> {text_of(state['messages'][-1])}\n")

    mcp_stamp = config_stamp()
    while True:
        # 定时任务到点: 主线程里替用户发一轮（REPL 忙时队列暂存, 不丢）
        while not wakeup.empty():
            prompt = wakeup.get()
            print(f"\n⏰ [定时任务触发] {prompt}")
            invoke(prompt)
        # select 等输入 1 秒, 空闲时回来看队列; 非 tty 退化为阻塞读
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 1.0)
        except (OSError, ValueError):
            ready = True
        if not ready:
            continue
        try:
            user = read_input("你> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not user:
            continue
        if user.lower() in ("exit", "quit"):
            break
        # 会话内恢复: 列最近会话选一条, 整体换掉当前上下文, 原地续写该文件
        if user.lower() in ("/resume", "/r"):
            resumed = resume_flow()
            if resumed is not None:
                state["messages"], sess_path = resumed
            continue
        invoke(user)


if __name__ == "__main__":
    main()
