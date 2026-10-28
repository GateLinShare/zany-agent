#!/usr/bin/env python3
"""唯一入口 —— 交互走 Textual TUI, -p 走无头模式(供后台/脚本调用, 同 claude -p)。

Usage:
    uv run tui.py                # 交互 TUI
    uv run tui.py -p "问题"      # 无头单次提问, 打印回复退出
    ... | uv run tui.py -p       # prompt 从 stdin 读

交互逻辑（MCP 热重载 / 每轮落盘 / cron 唤醒 / fork 恢复）都在 TUI 里,
agent.invoke 放后台线程跑, UI 不卡; 同一轮没跑完先排队。
"""

import json
import os
import queue
import sys
import time
from pathlib import Path

from rich.console import Group
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Footer, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from agent import build_agent, text_of
from core.messages import user_content
from core.mcp import config_stamp
from core.sessions import (
    SESSIONS_DIR,
    list_sessions,
    load_session_data,
    new_session_file,
    save_session,
)
from cron_jobs import start_scheduler

# ---- 渲染约定(参考 hermes cli.py 的盒子/折叠/token 三件套) ----
# - thinking: dim 灰逐行流式(hermes _stream_reasoning_delta), 正文到达即折叠定稿
# - 正文: accent 标签分段, 逐段定稿(hermes _emit_stream_text 的 turn boundary flush)
# - token: 紧凑格式(hermes format_token_count_compact 同语义) + ctx/压缩计数
REASONING_LINES = 10  # 正文到达后 thinking 折叠保留行数(hermes reasoning_full=False 的 10 行)
LIVE_REASONING_LINES = 6  # live 流式区 thinking 只显示尾部几行, 防长思考刷屏


def fmt_tokens(n: int) -> str:
    """token 紧凑格式, hermes format_token_count_compact 同语义:
    <1000 原数; 否则 K/M/B 三档, <10 两位小数, <100 一位, 去尾零。"""
    n = int(n)
    if abs(n) < 1000:
        return str(n)
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(n) >= threshold:
            v = abs(n) / threshold
            text = f"{v:.2f}" if v < 10 else f"{v:.1f}" if v < 100 else f"{v:.0f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return f"{n:,}"


def ctx_bar(tin: int) -> str:
    """状态栏 ctx 条(hermes _build_context_bar/_status_bar_context_style 同款):
    10 格进度条按占用率变色(<50 绿/50-80 黄/>80 橙/≥95 红) + 已用/上限 + 百分比。"""
    ctx_max = int(os.environ.get("ANTHROPIC_CONTEXT_TOKENS", "200000"))  # ponytail: 静态窗口, 端点不暴露
    pct = min(100, round(tin / ctx_max * 100)) if tin else 0
    style = "red" if pct >= 95 else "dark_orange" if pct > 80 else "yellow" if pct >= 50 else "green"
    filled = round(pct / 100 * 10)
    return f"[{style}]{'█' * filled}{'░' * (10 - filled)}[/] {fmt_tokens(tin)}/{fmt_tokens(ctx_max)} · {pct}%"


# 工具行样式映射(hermes agent/display.py get_cute_tool_message): emoji + 动词
TOOL_VERB = {
    "ls": ("📂", "ls"), "read_file": ("📖", "read"), "write_file": ("✍️", "write"),
    "edit_file": ("🔧", "edit"), "execute": ("💻", "$"), "task": ("⛓", "task"),
}


def tool_line(name: str, args: dict, dur: float, output: str) -> str:
    """工具完成行(hermes 同款): `┊ {emoji} {动词:9} {关键参数}  {耗时}`, 失败变红。
    关键参数取 path/命令; 路径尾部截断(hermes _path), 命令头部截断(_trunc)。"""
    emoji, verb = TOOL_VERB.get(name, ("⚡", name))
    if name == "execute":
        detail = str(args.get("command", ""))[:42]
    elif name == "task":
        detail = str(args.get("prompt", ""))[:42]
    else:
        p = str(args.get("path", ""))
        detail = ("..." + p[-32:]) if len(p) > 35 else p
    err = output.startswith("错误:")
    detail = f"[red]{detail}[/red]" if err else detail
    fail = "[red]✗ 失败[/red]" if err else ""
    return f"[dim]┊[/] {emoji} {verb:<9} {detail}  [dim]{dur:.1f}s[/dim]{fail}"


def _fold_thinking(think: str) -> str:
    """thinking 折叠: 超 REASONING_LINES 行截断加省略行(样式由调用方套)。"""
    lines = think.strip().splitlines()
    if len(lines) > REASONING_LINES:
        return "\n".join(lines[:REASONING_LINES]) + f"\n… 还有 {len(lines) - REASONING_LINES} 行 thinking"
    return think.strip()


AGENT_NAME = os.environ.get("AGENT_NAME", "mini-agent")  # 正文前缀, .env 可配
USER_NAME = os.environ.get("USER_NAME", "user")  # 用户消息前缀, .env 可配


def _turn_renderable(think: str, body: str, fold: bool = False):
    """一轮流式内容渲染成 Rich 组合, live 区和 chatlog 定稿共用同一样式。
    Claude Code 风格(抄它的配色与层级): thinking 树形缩进(dark_khaki),
    正文 `名字> ` 前缀引导(gold1), 整行 **粗体** 行渲染成金色小标题。"""
    parts = []
    if think.strip():
        collapsed = fold or bool(body.strip())
        n = max(1, len(think) // 3)  # ~tokens 粗估, est_tokens 同口径(chars/3)
        parts.append(Text("⌐ ▼ Thinking", style="dark_khaki")
                     + Text(f" ~{n} tokens", style="dim dark_khaki"))
        content = _fold_thinking(think) if collapsed else think.strip()
        lines = content.splitlines()
        # 树形缩进: 首行挂 ⌐, 其余行对齐到下一层
        parts.append(Text(
            "\n".join(f"  ⌐ {lines[0]}" if i == 0 else f"    {l}"
                      for i, l in enumerate(lines)),
            style="dark_khaki italic"))
    if body.strip():
        prefix = f"{AGENT_NAME}> "
        pad = " " * len(prefix)
        t = Text()
        for i, line in enumerate(body.strip().splitlines()):
            t.append(prefix if i == 0 else pad, style="white")  # 名字与正文同色, 不抢眼
            s = line.strip()
            if s.startswith("**") and s.endswith("**") and len(s) > 4:
                t.append(s[2:-2], style="bold gold1")  # ponytail: 只认整行 **粗体**, 行内粗体不渲染
            else:
                t.append(line, style="white")
            t.append("\n")
        parts.append(t)
    return Group(*parts) if parts else Text("")


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
    # ctrl+c 语义学 hermes: 运行中第一次=打断当前轮, 第二次(或空闲时)=退出
    BINDINGS = [("ctrl+c", "quit", "打断 / 退出")]

    CSS = """
    #chatlog { height: 1fr; border: none; padding: 0 1; }
    #live    { display: none; padding: 0 1; }
    #live.on { display: block; }
    #prompt  { border: round $accent; height: 3; }
    """

    def __init__(self, resume_id: str | None = None):
        super().__init__()
        self.workdir = os.getcwd()
        self.agent = build_agent(self.workdir, with_mcp=False)  # MCP 后台补, 界面秒起
        self.state = {"messages": []}
        self.mcp_stamp = config_stamp()
        self.sess_path = None
        self.resume_id = resume_id  # -r <id> / -r(最近一个) / None(新会话)
        self.busy = False
        self._interrupt_pending = False  # 已发打断, 下次 ctrl+c 强制退出
        self._queued_text: str | None = None  # busy 时用户输入 → 打断后排下一轮(hermes steer)
        self.wakeup: "queue.Queue[str]" = queue.Queue()  # cron 唤醒队列

    def compose(self) -> ComposeResult:
        yield RichLog(id="chatlog", markup=True, wrap=True)
        yield Static("", id="live")  # 流式输出的在屏消息, 轮结束移入 chatlog
        yield ChatPrompt(placeholder="输入消息 · /resume 恢复会话 · exit 退出 · shift+enter 换行", id="prompt")
        yield Static("", id="ctx")  # 状态栏: ctx 已用/上限(hermes footer 状态栏同款)
        yield Footer()

    def on_mount(self) -> None:
        self.update_ctx(0)
        log = self.query_one("#chatlog", RichLog)
        log.write(f"[bold]mini-agent[/] (tools: {', '.join(self.agent.tools)})")
        log.write(
            f"[dim]工作目录: {self.workdir} · /resume 恢复会话 · exit 退出[/dim]\n"
        )
        if self.resume_id is not None:  # uv run tui.py -r [id] 启动即恢复
            if self.resume_id:
                path = SESSIONS_DIR / f"{self.resume_id}.json"
                if not path.exists():
                    self.log_write(f"[red]会话 {self.resume_id} 不存在[/red]")
                    return
            else:
                picks = list_sessions()
                if not picks:
                    self.log_write("[dim]sessions/ 下没有历史会话[/dim]")
                    return
                path = picks[0][0]
            self.do_resume(path)
        # cron 调度线程只入队, UI 线程消费
        start_scheduler(
            lambda name, prompt: self.call_from_thread(self.wakeup.put, prompt)
        )
        self.run_worker(self._load_mcp_worker, thread=True)  # MCP probe 慢, 别堵界面

    def _load_mcp_worker(self) -> None:
        from core.mcp import load_mcp_tools

        try:
            tools = load_mcp_tools()
        except Exception:
            return
        if not tools:
            return

        def apply() -> None:
            added = self.agent.add_tools(tools)
            self.log_write(f"[dim](MCP 工具就绪: {', '.join(added)})[/dim]")

        self.call_from_thread(apply)

    # ---- UI 工具 ----

    def log_write(self, text: str) -> None:
        self.query_one("#chatlog", RichLog).write(text)

    def log_render(self, renderable) -> None:
        """渲染定稿: Rich renderable(Group+Text 组合)直接入 chatlog。"""
        self.query_one("#chatlog", RichLog).write(renderable)

    def chat_line(self, who: str, text: str) -> None:
        """聊天行: 用户消息 `名字> ` 金黄(与 AI 正文同款前缀风格)。"""
        if who == "user":
            t = Text()
            t.append(f"{USER_NAME}> ", style="bold gold1")
            t.append(text, style="bold gold1")
            self.query_one("#chatlog", RichLog).write(t)
        else:
            self.query_one("#chatlog", RichLog).write(
                Text(text, justify="left", style="white")
            )

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        if not busy:
            self._interrupt_pending = False  # 一轮安全落地, 重置两段式退出
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
            # hermes busy_input_mode="interrupt": 忙时输入=打断当前轮, 文本排下一轮
            if not self._interrupt_pending:
                self.agent.interrupt()
                self._interrupt_pending = True
            self._queued_text = (self._queued_text + "\n" + text) if self._queued_text else text
            self.log_write(f"[dim](已请求打断, 这条排下一轮: {text[:60]}…)[/dim]")
            return
        self.chat_line("user", text)
        self.run_turn(text)

    def action_quit(self) -> None:
        """两段式退出(hermes): 运行中第一次 ctrl+c 打断, 第二次/空闲退出。"""
        if self.busy and not self._interrupt_pending:
            self._interrupt_pending = True
            self.agent.interrupt("用户按了 ctrl+c")
            self.log_write("[dim]⚡ 打断请求已发出(正在跑的工具会跑完当前这个)…再按一次 ctrl+c 强制退出[/dim]")
            return
        self.exit()

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
            msgs = self.state["messages"] + [{"role": "user", "content": user_content(text)}]
            self.call_from_thread(self.live_start)
            # 分段状态机(hermes turn boundary flush): thinking→正文为一段,
            # 工具回填时定稿入 chatlog 并开新段, 保证多轮工具交错不混排
            think_buf = ""
            text_buf = ""
            turn_tokens = (0, 0)
            tool_t0 = {}  # tool_use_id -> 起始时刻, 算工具耗时
            # 核心事件流: ("thinking",d) / ("text",d) / ("tool_start",id,name)
            #            / ("tool_result",id,name,input_json,output)
            for ev in self.agent.stream(msgs):
                kind = ev[0]
                if kind == "thinking":
                    think_buf += ev[1]
                elif kind == "text":
                    text_buf += ev[1]
                elif kind == "tool_start":
                    tool_t0[ev[1]] = time.monotonic()
                elif kind == "tool_result":
                    # 段边界: 先把 thinking/正文定稿进 chatlog(正文到达时
                    # thinking 已折叠), 再打工具行 —— 对应 hermes 的
                    # _flush_stream + 工具 feed 行
                    if think_buf.strip() or text_buf.strip():
                        seg = _turn_renderable(think_buf, text_buf, fold=True)
                        self.call_from_thread(self.log_render, seg)
                        think_buf, text_buf = "", ""
                    _, _id, name, input_json, output = ev
                    dur = time.monotonic() - tool_t0.pop(_id, time.monotonic())
                    try:
                        args = json.loads(input_json)
                    except json.JSONDecodeError:
                        args = {}
                    self.call_from_thread(self.log_write, tool_line(name, args, dur, output))
                elif kind == "usage":
                    turn_tokens = (ev[1], ev[2])
                    self.call_from_thread(self.update_ctx, ev[1])
                elif kind == "compact":
                    self.call_from_thread(
                        self.log_write,
                        f"[dim]🗜 上下文压缩: {fmt_tokens(ev[1])} → ~{fmt_tokens(ev[2])} tokens[/dim]",
                    )
                elif kind == "interrupted":
                    self.call_from_thread(self.log_write, f"[yellow]⚡ 已打断: {ev[1]}[/yellow]")
                elif kind == "compact_error":
                    self.call_from_thread(
                        self.log_write, f"[dim](自动压缩已停用: {ev[1]})[/dim]"
                    )
                self.call_from_thread(self.live_update, think_buf, text_buf)
            self.state["messages"] = msgs
        except Exception as e:
            # 现场定稿(已到达的 thinking/正文不丢), 然后红字报错 —— 错误即文本
            self.call_from_thread(self.log_render, _turn_renderable(think_buf, text_buf, fold=True))
            self.call_from_thread(self.log_write, f"[red]对话出错: {e}[/red]")
            self.call_from_thread(self.live_hide)
            self.call_from_thread(self.turn_finished)
            return
        # 每轮落盘: 首轮创建会话文件, 之后覆盖写（fork 恢复后开新文件）
        if self.sess_path is None:
            self.sess_path = new_session_file()
        try:
            save_session(self.sess_path, self.state["messages"], self.agent.usage)
        except Exception as e:
            self.call_from_thread(self.log_write, f"[dim](会话保存失败: {e})[/dim]")
        # 末段定稿进 chatlog(与 live 区同一样式)
        seg = _turn_renderable(think_buf, text_buf, fold=True)
        self.call_from_thread(self.log_render, seg)
        self.call_from_thread(self.live_hide)
        self.call_from_thread(self.turn_finished)

    # ---- 流式显示 ----

    def update_ctx(self, tin: int) -> None:
        """状态栏刷新: 最近一次响应的真实 prompt tokens = 当前 ctx 占用。"""
        self.query_one("#ctx", Static).update(f"[dim]· {ctx_bar(tin)}[/dim]")

    # 贴图走文件路径: [图片: 路径] 占位符(core/messages.user_content 转成 image 块)。
    # 不做剪贴板粘贴 —— SSH 终端(VS Code/Windows Terminal)对图片粘贴什么都不发, 读不到。


    def live_start(self) -> None:
        self.query_one("#live", Static).add_class("on")

    def live_update(self, think: str, text: str) -> None:
        # live 区 thinking 只看尾部几行(hermes 正文一到就关 reasoning 盒的等效做法),
        # 全量仍进 messages/session 文件; 正文到达后走 _turn_renderable 的折叠
        lines = think.strip().splitlines()
        if lines and not text.strip() and len(lines) > LIVE_REASONING_LINES:
            think = f"… 前 {len(lines) - LIVE_REASONING_LINES} 行\n" + "\n".join(lines[-LIVE_REASONING_LINES:])
        self.query_one("#live", Static).update(_turn_renderable(think, text, fold=False))

    def live_hide(self) -> None:
        """隐藏流式区(组件保留, 只藏不删)。定稿已分段写进 chatlog。"""
        live = self.query_one("#live", Static)
        live.remove_class("on")
        live.update("")

    def turn_finished(self) -> None:
        """UI 线程收尾: 解锁输入; 忙时插进来的话(打断+steer)接着跑; 再跑 cron 唤醒。"""
        self.set_busy(False)
        if self._queued_text:
            queued, self._queued_text = self._queued_text, None
            self.chat_line("user", queued)
            self.run_turn(queued)
            return
        while not self.wakeup.empty():
            prompt = self.wakeup.get()
            self.log_write("[bold cyan]⏰ [定时任务触发][/bold cyan]")
            self.run_turn(prompt)

    # ---- /resume ----

    def do_resume(self, path) -> None:
        """恢复指定会话文件: 接回消息/账本/续写目标, 并回显历史。"""
        data = load_session_data(path)
        self.state["messages"] = data["messages"]
        self.sess_path = path  # 原地续写原会话文件
        if data.get("usage"):
            self.agent.usage = dict(data["usage"])  # 接回旧账本
        self.log_write(f"[dim]已恢复 {path.stem} ({len(self.state['messages'])} 条消息):[/dim]")
        for m in self.state["messages"]:  # 回显历史（工具消息跳过）
            t = text_of(m).strip()
            if not t:
                continue
            if m["role"] == "user":
                self.chat_line("user", t)
            elif m["role"] == "assistant":
                # thinking 折叠展示(与当轮定稿同款), 不只是干巴巴正文
                th = ""
                if isinstance(m.get("content"), list):
                    th = "".join(
                        b.get("thinking", "") for b in m["content"]
                        if isinstance(b, dict) and b.get("type") == "thinking"
                    )
                self.log_render(_turn_renderable(th, t, fold=True))
        self.log_write("")

    def resume_picker(self) -> None:
        picks = list_sessions()
        if not picks:
            self.log_write("[dim]sessions/ 下没有历史会话[/dim]")
            return

        def done(idx) -> None:
            if idx is None:
                return
            self.do_resume(picks[idx][0])

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
            data = load_session_data(sess_path)
            messages = data["messages"]
            if data.get("usage"):
                agent.usage = dict(data["usage"])
            print(f"(接着会话 {sess_path.stem}, 已有 {len(messages)} 条)", file=sys.stderr)
        else:
            # id 不存在 = 以该 id 新开会话, 本轮结束后落盘, 后续可复用
            print(f"(新会话 {sess_path.stem})", file=sys.stderr)
    messages = messages + [{"role": "user", "content": prompt}]
    # Ctrl+C 两段式(hermes 非交互同语义): 第一次置打断标志让 loop 协作式收尾
    # (partial 内容照常落盘, token 照常记账), 第二次恢复默认行为硬退。
    import signal

    def _sigint(signum, frame):
        agent.interrupt("用户打断")
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    signal.signal(signal.SIGINT, _sigint)
    agent.invoke(messages)
    if agent.was_interrupted:
        print("(已打断, 以上是收到的部分内容)", file=sys.stderr)
    print(text_of(messages[-1]))
    if sess_path is None:
        sess_path = new_session_file()
    save_session(sess_path, messages, agent.usage)
    u = agent.usage
    print(f"(tokens: in {u['input']:,} / out {u['output']:,}, {u['calls']} calls)", file=sys.stderr)
    print(f"(session: {sess_path.stem})", file=sys.stderr)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "-p":
        _headless(sys.argv[2:])
        return
    argv = sys.argv[1:]
    resume_id = None
    if argv and argv[0] in ("--resume", "-r"):
        resume_id = argv[1] if len(argv) > 1 else ""  # 不带 id = 最近活跃会话
    app = ChatApp(resume_id)
    try:
        app.run()
    except KeyboardInterrupt:  # Textual 没接住的 ^C(启动中/空闲期), 直接退
        pass
    # 退出后提示续聊命令(有对话内容才提示); ctrl+c 打断轮也已落盘
    if app.state["messages"] and app.sess_path is not None:
        print(f"(继续对话: uv run tui.py -r {app.sess_path.stem})", file=sys.stderr)
    # 后台线程(@work 的 agent.invoke / MCP probe)可能还挂着, sys.exit 会死等
    # 它们 —— 会话每轮都已落盘, 直接硬退
    os._exit(0)


if __name__ == "__main__":
    main()
