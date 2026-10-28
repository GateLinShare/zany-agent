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
import re
import sys
import time
from pathlib import Path

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from agent import build_agent, text_of
from core.messages import user_content, est_tokens
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
REASONING_LINES = 3  # 正文到达后 thinking 折叠保留行数
LIVE_REASONING_LINES = 3  # live 流式区 thinking 只显示尾部几行, 防长思考刷屏

# markdown 标题对齐 Claude Code: 白粗体, 去掉 rich 默认的品红下划线(h2=underline magenta)。
# 覆盖点必须是 rich.themes.DEFAULT.styles(单例 Theme, 所有 Console 的 ThemeStack 底层共用):
# - 改 rich.default_styles.DEFAULT_STYLES 字典无效——DEFAULT 在 import 时就 copy 走了
# - 值必须 Style.parse(): Theme.styles 存 Style 对象, 塞 str 会在 get_style 崩(无 .link)
from rich.style import Style
from rich.themes import DEFAULT as _RICH_DEFAULT_THEME

for _lvl in ("h1", "h2", "h3", "h4"):
    _RICH_DEFAULT_THEME.styles[f"markdown.{_lvl}"] = Style.parse("bold #d4d4d4")


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
    "edit_file": ("🔧", "edit"), "execute": ("💻", "exec"), "task": ("⛓", "task"),
}


CMD_CLIP = 60  # 命令/prompt 单行显示宽度上限(用户定 60, 前后缀另占~24列): 超出尾部 …


def _clip(s: str, n: int = CMD_CLIP) -> str:
    """按显示宽截断(中文算 2 列, rich.cells 口径), 尾部加 …。"""
    from rich.cells import cell_len

    if cell_len(s) <= n:
        return s
    while s and cell_len(s + "…") > n:
        s = s[:-1]
    return s + "…"


def tool_line(name: str, args: dict, dur: float, output: str) -> str:
    """工具完成行: `┊ {emoji} {工具名:<9} {关键参数}  {耗时}`, 失败变红。
    命令/prompt 截到一行(用户定: 不全显, 防折行刷屏); 路径尾部截断;
    先 escape 再截断 —— markup=True 环境里任意文本必须转义(命令含 [a-z]
    之类会被当样式标签), 且转义后截断宽度才确定。"""
    from rich.markup import escape

    emoji, verb = TOOL_VERB.get(name, ("⚡", name))
    if name == "execute":
        detail = _clip(escape(str(args.get("command", ""))))
    elif name == "task":
        detail = _clip(escape(str(args.get("prompt", ""))))
    else:
        p = escape(str(args.get("path", "")))
        detail = ("..." + p[-32:]) if len(p) > 35 else p
    err = output.startswith("错误:")
    detail = f"[red]{escape(detail)}[/red]" if err else escape(detail)
    fail = "[red]✗ 失败[/red]" if err else ""
    return f"[dim]┊[/] {emoji} {verb:<9} {detail}  [dim]{dur:.1f}s[/dim]{fail}"


def _fold_thinking(think: str) -> str:
    """thinking 折叠: 超 REASONING_LINES 行截断加省略行(样式由调用方套)。"""
    lines = think.strip().splitlines()
    if len(lines) > REASONING_LINES:
        return "\n".join(lines[:REASONING_LINES]) + f"\n… 还有 {len(lines) - REASONING_LINES} 行 thinking"
    return think.strip()


SHOW_THINKING = os.environ.get("SHOW_THINKING", "0").lower() in ("1", "true", "yes", "on")  # .env 可配, 默认不显示


_MD_STRUCT = re.compile(r"^\s*(?:[#>|`]|```|[-*+ ]|\d+[.)])")  # 首行是这些开头时不切出, 保结构


def _turn_renderable(think: str, body: str, fold: bool = False, md: bool = True):
    """一轮流式内容渲染成 Rich 组合, live 区和 chatlog 定稿共用同一样式。
    Claude Code 风格(抄它的配色与层级): thinking 树形缩进(dark_khaki),
    正文 `名字> ` 引导行 + markdown 定稿渲染(md=True) / 纯文本粗排(md=False)。"""
    parts = []
    if think.strip() and SHOW_THINKING:  # .env SHOW_THINKING=0 时 UI 不渲染 thinking(数据层仍全量记录)
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
        lines = body.strip().splitlines()
        first, rest = lines[0], "\n".join(lines[1:])
        head = not (think.strip() and SHOW_THINKING)  # Thinking 行不在时, ⏺ 即段头标识
        if _MD_STRUCT.match(first):
            # 首行是 markdown 块结构(表格/标题/引用/代码围栏/列表), 切出会断结构 → 圆点独立行
            if head:
                parts.append(Text("⏺", style="bold #d4d4d4"))
            parts.append(Markdown(body.strip()) if md else Text(body.strip(), style="#d4d4d4"))
        else:
            # Claude 同款: 圆点+首句同行, 续行整块进 markdown 顶格
            dot = Text("⏺ ", style="bold #d4d4d4") if head else Text("")
            parts.append(dot + Text(first, style="#d4d4d4"))
            if rest:
                parts.append(Markdown(rest) if md else Text(rest, style="#d4d4d4"))
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
        self.cursor_blink = False  # Claude 同款: 光标常亮不闪
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


class ChatLog(RichLog):
    """chatlog 不可聚焦: 点历史/滚轮不抢焦点, 输入框永远持有光标(Claude 同款)。
    write 后强制滚底: Textual 的 scroll_end(immediate=False) 排队到下一帧,
    与 virtual_size reflow 竞态, 长内容时常滚不够, 新消息沉在视口下。"""

    can_focus = False

    def write(self, *args, **kwargs):
        result = super().write(*args, **kwargs)
        scroll_end = kwargs.get("scroll_end")
        if scroll_end is None and len(args) >= 5:  # 内部 on_resize 按 5 个位置参数重放
            scroll_end = args[4]
        if scroll_end is not False and self.is_mounted:
            self.scroll_end(animate=False, immediate=True, x_axis=False)  # 竞态兕底
        return result


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
        self.state = {"messages": [], "marks": []}
        self.mcp_stamp = config_stamp()
        self.sess_path = None
        self.resume_id = resume_id  # -r <id> / -r(最近一个) / None(新会话)
        self.busy = False
        self._interrupt_pending = False  # 已发打断, 下次 ctrl+c 强制退出
        self._queued_text: str | None = None  # busy 时用户输入 → 打断后排下一轮(hermes steer)
        self.wakeup: "queue.Queue[str]" = queue.Queue()  # cron 唤醒队列

    def compose(self) -> ComposeResult:
        yield ChatLog(id="chatlog", markup=True, wrap=True)
        yield Static("", id="live")  # 流式输出的在屏消息, 轮结束移入 chatlog
        yield ChatPrompt(placeholder="输入消息", id="prompt")
        yield Static("", id="ctx")  # 输入框下常驻一行: 状态提示 + ctx(替代 Footer)

    def on_mount(self) -> None:
        self.update_ctx(0)
        self.query_one("#prompt", ChatPrompt).focus()  # 焦点常驻输入框, 进来直接能打字
        log = self.query_one("#chatlog", RichLog)
        log.write(f"[bold]mini-agent[/] (tools: {', '.join(self.agent.tools)})")
        log.write(f"[dim]工作目录: {self.workdir}[/dim]\n")  # 快捷键提示在输入框下常驻行, 不占横幅
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
        log = self.query_one("#chatlog", RichLog)
        log.write(Text())  # 块间空行
        log.write(renderable)

    def chat_line(self, who: str, text: str) -> None:
        """聊天行: 用户消息金子+暗底色条(Claude Code 同款视觉); AI 正文白字无底。"""
        if who == "user":
            log = self.query_one("#chatlog", RichLog)
            log.write(Text())  # 块间空行
            t = Text()
            t.append(text, style="gold3 on grey23")  # 无前缀, 金色暗底条区分用户消息(Claude 同款)
            log.write(t)
        else:
            self.query_one("#chatlog", RichLog).write(
                Text(text, justify="left", style="#d4d4d4")
            )

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        if not busy:
            self._interrupt_pending = False  # 一轮安全落地, 重置两段式退出
        # 输入框不disable(Claude 同款: 任何时候都能打字; busy 时回车=打断+排队)
        self.query_one("#prompt", ChatPrompt).focus()
        self.update_ctx(getattr(self, "_last_tin", 0))  # 常驻提示行随忙/闲切换

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
        if text.lower() in ("/new", "/n"):
            if self.busy:
                self.log_write("[dim]当前轮还在跑, 先 ctrl+c 打断再 /new[/dim]")
                return
            self.new_chat()
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
            think_t0 = think_last = None  # thinking 首末时刻, 算思考耗时
            n_tools = 0
            # 核心事件流: ("thinking",d) / ("text",d) / ("tool_start",id,name)
            #            / ("tool_result",id,name,input_json,output)
            for ev in self.agent.stream(msgs):
                kind = ev[0]
                if kind == "thinking":
                    if think_t0 is None:
                        think_t0 = time.monotonic()
                    think_last = time.monotonic()
                    think_buf += ev[1]
                elif kind == "text":
                    text_buf += ev[1]
                elif kind == "tool_start":
                    tool_t0[ev[1]] = time.monotonic()
                elif kind == "tool_result":
                    n_tools += 1
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
                elif kind == "turn_limit":
                    self.call_from_thread(
                        self.log_write, f"[yellow]⏹ 已达最大轮数 {ev[1]}, 本轮中止(继续发消息可接着干)[/yellow]"
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
        # 回合侧账: 消息条数当锚, 恢复回放按锚对齐(防止 compact 后错位)
        self.state["marks"].append(
            {
                "at": len(self.state["messages"]),
                "thought": (
                    max(1, round(think_last - think_t0)) if think_t0 is not None else None
                ),
                "tools": n_tools,
            }
        )
        if self.sess_path is None:
            self.sess_path = new_session_file()
        try:
            save_session(
                self.sess_path, self.state["messages"], self.agent.usage, self.state["marks"]
            )
        except Exception as e:
            self.call_from_thread(self.log_write, f"[dim](会话保存失败: {e})[/dim]")
        # 末段定稿进 chatlog(与 live 区同一样式)
        seg = _turn_renderable(think_buf, text_buf, fold=True)
        self.call_from_thread(self.log_render, seg)
        # 回合摘要行(Claude 同款): 思考耗时 + 工具调用数
        marks = []
        if think_t0 is not None:
            # 有 thinking 就显示(哪怕单 delta 不到 1s, 取 1s 起步); 没思考才跳过
            marks.append(f"Thought for {max(1, round(think_last - think_t0))}s")
        if n_tools:
            marks.append(f"ran {n_tools} tool call{'s' if n_tools > 1 else ''}")
        if marks:
            self.call_from_thread(
                self.log_write, f"\n[dim]{', '.join(marks)}[/dim]"
            )  # 前置 \n 空一行(Claude 的 * Baked for ... 前也有空行)
        self.call_from_thread(self.live_hide)
        self.call_from_thread(self.turn_finished)

    # ---- 流式显示 ----

    def update_ctx(self, tin: int) -> None:
        """输入框下方常驻一行(Claude Code 的 auto-mode 行同款):
        状态提示 + ctx 占用; set_busy 也走这里, 提示随忙/闲切换。"""
        self._last_tin = tin
        state = ("[yellow]⏵ 运行中 · ctrl+c 打断[/yellow]" if self.busy
                 else "[dim]⏵⏵ /new 新对话 · /resume 恢复 · exit 退出[/dim]")
        self.query_one("#ctx", Static).update(f"{state} [dim]· {ctx_bar(tin)}[/dim]")

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
        self.query_one("#live", Static).update(
            _turn_renderable(think, text, fold=False, md=False))

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

    # ---- /new / /resume ----

    def new_chat(self) -> None:
        """新开一个对话: 清空消息/账本/续写目标, 清屏重绘欢迎横幅。

        旧会话已每轮落盘, 这里只是 UI 和内存态归零 —— /resume 随时能找回。"""
        self.state["messages"] = []
        self.state["marks"] = []
        self.sess_path = None  # 解除续写目标, 下轮落盘时 new_session_file 另起新文件
        self.agent.usage = {"input": 0, "output": 0, "calls": 0}  # 账本清零(与 loop 初始同款)
        self.agent._last_prompt_tokens = None  # 压缩判定同步清零, 防旧 last_in 误触发新会话压缩
        self._last_tin = 0
        self.update_ctx(0)
        log = self.query_one("#chatlog", RichLog)
        log.clear()
        log.write(f"[bold]mini-agent[/] (tools: {', '.join(self.agent.tools)})")
        log.write(f"[dim]工作目录: {self.workdir}[/dim]\n")

    def do_resume(self, path) -> None:
        """恢复指定会话文件: 接回消息/账本/续写目标, 并回显历史。"""
        data = load_session_data(path)
        self.state["messages"] = data["messages"]
        self.state["marks"] = data.get("marks", []) or []
        self.sess_path = path  # 原地续写原会话文件
        if data.get("usage"):
            self.agent.restore(data["usage"])
        # 进度条立即恢复(原要对话一轮才显示); 旧格式会话无 last_in 时用 est 兼底
        # (口径对齐真实 prompt tokens: system prompt + messages)
        tin = self.agent.usage.get("last_in") or est_tokens(
            [{"role": "system", "content": self.agent._system()}]
            + self.state["messages"])
        self.update_ctx(tin)
        self.log_write(f"[dim]已恢复 {path.stem} ({len(self.state['messages'])} 条消息):[/dim]")
        # 回放历史: 按"回合"分组(真用户消息开组; tool_result 也穿 user 皮, 要排除),
        # 组内 assistant 的 thinking/正文合并成一块渲染, 再按锚找 marks 补摘要行
        marks_by_at = {m["at"]: m for m in self.state["marks"]}
        groups: list[dict] = []  # [{user, th, text, at}]  at=组末消息序号(锚)
        msgs = self.state["messages"]
        for idx, m in enumerate(msgs):
            is_tool_result = isinstance(m.get("content"), list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in m["content"]
            )
            if m["role"] == "user" and not is_tool_result:
                groups.append({"user": text_of(m).strip(), "th": "", "text": "", "at": idx + 1})
            elif m["role"] == "assistant":
                if not groups:
                    groups.append({"user": None, "th": "", "text": "", "at": 0})
                g = groups[-1]
                if isinstance(m.get("content"), list):
                    g["th"] += "".join(
                        b.get("thinking", "") for b in m["content"]
                        if isinstance(b, dict) and b.get("type") == "thinking"
                    )
                t = text_of(m).strip()
                g["text"] += ("\n" if g["text"] else "") + t
                g["at"] = idx + 1  # 组内最后一条 assistant 消息的位置
        for g in groups:
            if g["user"]:
                self.chat_line("user", g["user"])
            if g["th"] or g["text"].strip():
                self.log_render(_turn_renderable(g["th"], g["text"].strip(), fold=True))
            mk = marks_by_at.get(g["at"])  # 锚对不上(compaction 后)就静默跳过
            if mk:
                parts = []
                if mk.get("thought"):
                    parts.append(f"Thought for {mk['thought']}s")
                if mk.get("tools"):
                    parts.append(f"ran {mk['tools']} tool call{'s' if mk['tools'] > 1 else ''}")
                if parts:
                    self.log_write(f"\n[dim]{', '.join(parts)}[/dim]")
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
                agent.restore(data["usage"])
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
