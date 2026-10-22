#!/usr/bin/env python3
"""唯一入口 —— 交互走 Textual TUI, -p 走无头模式(供后台/脚本调用, 同 claude -p)。

Usage:
    uv run tui.py                # 交互 TUI
    uv run tui.py -p "问题"      # 无头单次提问, 打印回复退出
    ... | uv run tui.py -p       # prompt 从 stdin 读

交互逻辑（MCP 热重载 / 每轮落盘 / cron 唤醒 / fork 恢复）都在 TUI 里,
agent.invoke 放后台线程跑, UI 不卡; 同一轮没跑完先排队。
"""

import os
import queue
import sys
import time

from langchain_core.messages import HumanMessage
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Footer, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from agent import TOOLS, build_agent, text_of
from cron_jobs import start_scheduler
from mcp_tools import config_stamp
from sessions import (
    SESSIONS_DIR,
    list_sessions,
    load_session,
    new_session_file,
    save_session,
)


class ChatPrompt(TextArea):
    """多行输入框。回车提交; shift+enter/alt+enter/ctrl+j 换行。

    无 bracketed paste 的终端把多行粘贴回放成一串按键, 换行以 enter 键到达。
    参考 dcode PasteBurstTextArea 的最小子集: 字符连击(间隔≤0.03s)之后的
    短时间窗内, enter 当作粘贴的换行插入, 不触发提交。
    """

    class Submitted(Message):
        """回车提交, text 为完整输入(可含换行)。"""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    BINDINGS = [
        Binding("shift+enter,alt+enter,ctrl+enter,ctrl+j", "newline", "换行", show=False),
    ]

    # ponytail: 只搬连击检测, dcode 的折叠占位符/路径识别/便签粘贴没要
    _BURST_GAP = 0.03  # 字符间隔 ≤ 此值算连击
    _ENTER_WINDOW = 0.12  # 连击后此时间窗内的 enter 算粘贴换行
    _BURST_MIN = 3  # 连击至少这么多字符才武装

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._last_char_ts = 0.0
        self._run = 0

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            now = time.monotonic()
            if self._run >= self._BURST_MIN and now - self._last_char_ts <= self._ENTER_WINDOW:
                self.insert("\n")  # 粘贴回放的换行, 不提交
                self._last_char_ts = now
                return
            text = self.text.strip()
            if text:
                self.post_message(self.Submitted(text))
            self.clear()
            self._run = 0
            return
        await super()._on_key(event)
        if event.is_printable:
            now = time.monotonic()
            self._run = self._run + 1 if now - self._last_char_ts <= self._BURST_GAP else 1
            self._last_char_ts = now

    def action_newline(self) -> None:
        self.insert("\n")

    # 高度跟随内容: 行数 + 边框, 3~36 行封顶
    def on_text_area_changed(self, event) -> None:
        n = self.document.line_count + 2
        self.styles.height = max(3, min(36, n))


class ResumeScreen(ModalScreen):
    """会话选择弹层: 上下键选, 回车恢复, esc 取消。"""

    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, picks: list):
        self.picks = picks
        super().__init__()

    def compose(self) -> ComposeResult:
        yield OptionList(
            *[
                Option(f"{i}. {ts}  ({n} 条)  {snippet}", id=str(i))
                for i, (_f, ts, snippet, n) in enumerate(self.picks, 1)
            ]
        )

    def on_option_list_option_selected(self, event) -> None:
        # Option.id 是 str, 换回下标; None = esc 取消
        self.dismiss(int(event.option.id) - 1 if event.option.id else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChatApp(App):
    TITLE = "deepagents mini-agent"
    BINDINGS = [("ctrl+c", "quit", "退出")]

    CSS = """
    #chatlog { height: 1fr; border: none; padding: 0 1; }
    #live    { display: none; padding: 0 1; }
    #live.on { display: block; }
    #prompt  { border: round $accent; height: 3; }
    """

    def __init__(self):
        super().__init__()
        self.workdir = os.getcwd()
        self.agent = build_agent(self.workdir)
        self.state = {"messages": []}
        self.mcp_stamp = config_stamp()
        self.sess_path = None
        self.busy = False
        self.wakeup: "queue.Queue[str]" = queue.Queue()  # cron 唤醒队列

    def compose(self) -> ComposeResult:
        yield RichLog(id="chatlog", markup=True, wrap=True)
        yield Static("", id="live")  # 流式输出的在屏消息, 轮结束移入 chatlog
        yield ChatPrompt(placeholder="输入消息 · /resume 恢复会话 · exit 退出 · shift+enter 换行", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#chatlog", RichLog)
        log.write(f"[bold]deepagents mini-agent[/] (tools: {', '.join(TOOLS)})")
        log.write(
            f"[dim]工作目录: {self.workdir} · /resume 恢复会话 · exit 退出[/dim]\n"
        )
        # cron 调度线程只入队, UI 线程消费
        start_scheduler(
            lambda name, prompt: self.call_from_thread(self.wakeup.put, prompt)
        )

    # ---- UI 工具 ----

    def log_write(self, text: str) -> None:
        self.query_one("#chatlog", RichLog).write(text)

    def chat_line(self, who: str, text: str) -> None:
        """聊天行: 用户消息金黄, AI 消息白色。"""
        style = "gold1" if who == "user" else "white"
        self.query_one("#chatlog", RichLog).write(Text(text, justify="left", style=style))

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        inp = self.query_one("#prompt", ChatPrompt)
        inp.disabled = busy
        inp.placeholder = "agent 思考中…" if busy else "输入消息 · /resume 恢复会话 · exit 退出 · shift+enter 换行"
        inp.focus()  # 每轮结束焦点回输入框, 不用手动点

    # ---- 提交 / 一轮对话 ----

    def on_chat_prompt_submitted(self, event: ChatPrompt.Submitted) -> None:
        text = event.text
        if not text:
            return
        if text.lower() in ("exit", "quit"):
            self.exit()
            return
        if text.lower() in ("/resume", "/r"):
            self.resume_picker()
            return
        if self.busy:
            self.log_write("[dim](上一轮还没跑完, 稍等…)[/dim]")
            return
        self.chat_line("user", text)
        self.run_turn(text)

    @work(thread=True, exclusive=True)
    def run_turn(self, text: str) -> None:
        """一轮对话, 后台线程执行; UI 更新一律 call_from_thread。"""
        self.call_from_thread(self.set_busy, True)
        # mcp_servers.json 变了 → 重建 agent（与 chat.py 同款）
        if config_stamp() != self.mcp_stamp:
            self.mcp_stamp = config_stamp()
            try:
                self.agent = build_agent(self.workdir)
                self.call_from_thread(
                    self.log_write, "[dim](检测到 mcp_servers.json 变更,已重载 MCP 工具)[/dim]"
                )
            except Exception as e:
                self.call_from_thread(
                    self.log_write, f"[dim](MCP 重载失败,沿用旧配置: {e})[/dim]"
                )
        try:
            inputs = {"messages": self.state["messages"] + [HumanMessage(text)]}
            self.call_from_thread(self.live_start)
            think_buf: list[str] = []
            text_buf: list[str] = []
            pending_args: dict[str, str] = {}  # tool_call_id -> 累积的入参 json
            last_tc_id = ""  # 续片没有 id, 归到最近见过的 id
            final = None
            # messages 模式逐 token 出流, values 模式取最终 state
            for mode, payload in self.agent.stream(inputs, stream_mode=["messages", "values"]):
                if mode == "values":
                    final = payload
                    continue
                chunk = payload[0]
                ctype = getattr(chunk, "type", "")
                if ctype == "tool":  # 工具结果: 只打一行摘要, 不刷全量内容
                    result = text_of(chunk).strip().replace("\n", " ")
                    self.call_from_thread(
                        self.log_write,
                        f"[dim]🔧 {chunk.name}({pending_args.get(chunk.tool_call_id, '')[:100]})"
                        f" → {result[:80]}{'…' if len(result) > 80 else ''}[/dim]",
                    )
                    continue
                # AI 块: 累积工具入参 + 思考/正文
                for tc in getattr(chunk, "tool_call_chunks", None) or []:
                    if tc.get("id"):
                        last_tc_id = tc["id"]
                    pending_args[last_tc_id] = pending_args.get(last_tc_id, "") + (tc.get("args") or "")
                th, tx = self._chunk_text(chunk)
                if not th and not tx:
                    continue
                if th:
                    think_buf.append(th)
                if tx:
                    text_buf.append(tx)
                self.call_from_thread(self.live_update, "".join(think_buf), "".join(text_buf))
            if final is None:
                raise RuntimeError("stream 未返回最终状态")
            self.state["messages"] = final["messages"]
            # 定稿取最终 state 的最后一条(流里可能混着工具调用前的中间叙述)
            reply = text_of(self.state["messages"][-1])
        except Exception as e:
            self.call_from_thread(self.live_end, "", err=str(e))
            self.call_from_thread(self.turn_finished)
            return
        # 每轮落盘: 首轮创建会话文件, 之后覆盖写（fork 恢复后开新文件）
        if self.sess_path is None:
            self.sess_path = new_session_file()
        try:
            save_session(self.sess_path, self.state)
        except Exception as e:
            self.call_from_thread(self.log_write, f"[dim](会话保存失败: {e})[/dim]")
        self.call_from_thread(self.live_end, reply)
        self.call_from_thread(self.turn_finished)

    @staticmethod
    def _chunk_text(chunk) -> "tuple[str, str]":
        """从流式 chunk 里抠出 (思考, 正文); 工具调用块返回空。"""
        c = getattr(chunk, "content", "")
        if isinstance(c, str):
            return "", c
        th = "".join(b.get("thinking", "") for b in c if isinstance(b, dict) and b.get("type") == "thinking")
        tx = "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        return th, tx

    # ---- 流式显示 ----

    def live_start(self) -> None:
        self.query_one("#live", Static).add_class("on")

    def live_update(self, think: str, text: str) -> None:
        t = Text()
        if think:
            t.append(think, style="magenta")  # 思考内容: 洋红
            t.append("\n\n")
        t.append(text, style="white")
        self.query_one("#live", Static).update(t)

    def live_end(self, reply: str, err: str | None = None) -> None:
        """隐藏流式区(组件保留, 只藏不删), 回复定稿进 chatlog。"""
        live = self.query_one("#live", Static)
        live.remove_class("on")
        live.update("")
        if err:
            self.log_write(f"[red]对话出错: {err}[/red]")
        elif reply.strip():
            self.chat_line("ai", reply)

    def turn_finished(self) -> None:
        """UI 线程收尾: 解锁输入, 跑掉忙时攒下的 cron 唤醒。"""
        self.set_busy(False)
        while not self.wakeup.empty():
            prompt = self.wakeup.get()
            self.log_write("[bold cyan]⏰ [定时任务触发][/bold cyan]")
            self.run_turn(prompt)

    # ---- /resume ----

    def resume_picker(self) -> None:
        picks = list_sessions()
        if not picks:
            self.log_write("[dim]sessions/ 下没有历史会话[/dim]")
            return

        def done(idx) -> None:
            if idx is None:
                return
            self.state["messages"] = load_session(picks[idx][0])
            self.sess_path = picks[idx][0]  # 原地续写原会话文件
            self.log_write(f"[dim]已恢复 {len(self.state['messages'])} 条消息:[/dim]")
            for m in self.state["messages"]:  # 回显历史（工具消息跳过, 长文本截断）
                t = text_of(m).strip()
                if not t:
                    continue
                if m.type == "human":
                    self.chat_line("user", t)
                elif m.type == "ai":
                    self.chat_line("ai", t)
            self.log_write("")

        self.push_screen(ResumeScreen(picks), done)


def _headless(argv: list[str]) -> None:
    """无头模式: 单次 invoke, 不起 UI, 不挂 cron。

    uv run tui.py -p "问题"            # 新会话跑一轮, 落盘并在 stderr 报 session id
    uv run tui.py -p -r <id> "问题"    # 接指定会话继续(原地续写); id 不存在则以该 id 新开
    uv run tui.py -p -r "问题"         # -r 不带 id = 接最近一个会话
    uv run ... -p -r <id> < prompt.txt # prompt 也可走 stdin
    """
    prompt_parts: list[str] = []
    resume_id: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] in ("--resume", "-r"):
            # -r 后面紧跟的算会话 id; 不带 id = 最近一个会话
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                resume_id = argv[i + 1]
                i += 2
            else:
                resume_id = ""
                i += 1
        else:
            prompt_parts.append(argv[i])
            i += 1
    prompt = " ".join(prompt_parts).strip() or sys.stdin.read().strip()
    if not prompt:
        raise SystemExit("无头模式需要 prompt: -p \"问题\" 或 stdin 传入")

    agent = build_agent(os.getcwd())
    messages = []
    sess_path = None
    if resume_id is not None:
        if resume_id:
            sess_path = SESSIONS_DIR / f"{resume_id}.json"
        else:
            files = sorted(SESSIONS_DIR.glob("*.json"))
            if not files:
                raise SystemExit("sessions/ 下没有历史会话")
            sess_path = files[-1]
        if sess_path.exists():
            messages = load_session(sess_path)
            print(f"(接着会话 {sess_path.stem}, 已有 {len(messages)} 条)", file=sys.stderr)
        else:
            # id 不存在 = 以该 id 新开会话, 本轮结束后落盘, 后续可复用
            print(f"(新会话 {sess_path.stem})", file=sys.stderr)
    state = agent.invoke({"messages": messages + [HumanMessage(prompt)]})
    print(text_of(state["messages"][-1]))
    if sess_path is None:
        sess_path = new_session_file()
    save_session(sess_path, state)
    print(f"(session: {sess_path.stem})", file=sys.stderr)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "-p":
        _headless(sys.argv[2:])
        return
    ChatApp().run()


if __name__ == "__main__":
    main()
