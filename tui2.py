#!/usr/bin/env python3
"""zany 交互 TUI(claude 式重构版) —— 原 tui.py(Textual 全屏)暂留不动。

架构 = claude code/Ink 同款(它能在 Windows 跑的原因就这一条):
- 烙定区(baked): 定稿内容逐块 print 进终端原生滚动回带, 永不重绘 —— rich 渲染
  (markdown/工具行/用户消息条), 零光标操作, 任何终端都稳。
- 活动区(live): 屏幕底部一帧 = 流式预览尾 + spinner 行 + 输入框 + 工具条,
  每次变化整帧原地重绘(光标上移 N 行 + 清到底重写), 只用 CUU/ED/CHA 三种 ANSI。
- 单写者: 主循环独占全部终端输出; agent.stream 跑在 worker 线程只改共享状态,
  终端里永远不会有两股输出打架。

无头模式(-p)与 tui.py 同语义。

Usage:
    uv run tui2.py                # 交互 TUI
    uv run tui2.py -p "问题"      # 无头单次
    uv run tui2.py -r [会话id]    # 恢复会话(不带 id = 最近一个)
"""

import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.text import Text

from agent import build_agent, text_of
from core.messages import user_content, est_tokens, result_text
from core.mcp import config_stamp
from core.sessions import (
    list_sessions,
    load_session_data,
    new_session_file,
    project_dir,
    save_session,
)
from cron_jobs import start_scheduler
from core.goal import (
    GOAL_MAX_TURNS,
    attach_goal,
    continuation_prompt,
    detach_goal,
)

# ---- 渲染约定(与 tui.py 同源: hermes 盒子/折叠/token 三件套 + claude 配色) ----
REASONING_LINES = 3
LIVE_REASONING_LINES = 3

from rich.style import Style
from rich.themes import DEFAULT as _RICH_DEFAULT_THEME

for _lvl in ("h1", "h2", "h3", "h4"):
    _RICH_DEFAULT_THEME.styles[f"markdown.{_lvl}"] = Style.parse("bold #d4d4d4")


def fmt_tokens(n: int) -> str:
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
    ctx_max = int(os.environ.get("ANTHROPIC_CONTEXT_TOKENS", "200000"))
    pct = min(100, round(tin / ctx_max * 100)) if tin else 0
    style = "red" if pct >= 95 else "dark_orange" if pct > 80 else "yellow" if pct >= 50 else "green"
    filled = round(pct / 100 * 10)
    return f"[{style}]{'█' * filled}{'░' * (10 - filled)}[/] {fmt_tokens(tin)}/{fmt_tokens(ctx_max)} · {pct}%"


TOOL_VERB = {
    "ls": ("📂", "ls"), "read_file": ("📖", "read"), "write_file": ("✍️", "write"),
    "edit_file": ("🔧", "edit"), "execute": ("💻", "exec"), "task": ("⛓", "task"),
}

CMD_CLIP = 60
EXIT_WINDOW = 2.0  # ctrl+c 双击退出计时窗(秒)


def _clip(s: str, n: int = CMD_CLIP) -> str:
    from rich.cells import cell_len

    if cell_len(s) <= n:
        return s
    while s and cell_len(s + "…") > n:
        s = s[:-1]
    return s + "…"


def tool_line(name: str, args: dict, dur: float, output: str) -> str:
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
    durs = f"  [dim]{dur:.1f}s[/dim]" if dur else ""
    return f"[dim]┊[/] {emoji} {verb:<9} {detail}{durs}{fail}"


def _fold_thinking(think: str) -> str:
    lines = think.strip().splitlines()
    if len(lines) > REASONING_LINES:
        return "\n".join(lines[:REASONING_LINES]) + f"\n… 还有 {len(lines) - REASONING_LINES} 行 thinking"
    return think.strip()


SHOW_THINKING = os.environ.get("SHOW_THINKING", "0").lower() in ("1", "true", "yes", "on")

_MD_STRUCT = re.compile(r"^\s*(?:[#>|`]|```|[-*+ ]|\d+[.)])")


def _turn_renderable(think: str, body: str, fold: bool = False, md: bool = True):
    parts = []
    if think.strip() and SHOW_THINKING:
        collapsed = fold or bool(body.strip())
        n = max(1, len(think) // 3)
        parts.append(Text("⌐ ▼ Thinking", style="dark_khaki")
                     + Text(f" ~{n} tokens", style="dim dark_khaki"))
        content = _fold_thinking(think) if collapsed else think.strip()
        lines = content.splitlines()
        parts.append(Text(
            "\n".join(f"  ⌐ {lines[0]}" if i == 0 else f"    {l}"
                      for i, l in enumerate(lines)),
            style="dark_khaki italic"))
    if body.strip():
        lines = body.strip().splitlines()
        first, rest = lines[0], "\n".join(lines[1:])
        head = not (think.strip() and SHOW_THINKING)
        if _MD_STRUCT.match(first):
            if head:
                parts.append(Text("●", style="#d4d4d4"))
            parts.append(Markdown(body.strip()) if md else Text(body.strip(), style="#d4d4d4"))
        else:
            dot = Text("● ", style="#d4d4d4") if head else Text("")
            parts.append(dot + Text(first, style="#d4d4d4"))
            if rest:
                parts.append(Markdown(rest) if md else Text(rest, style="#d4d4d4"))
    return Group(*parts) if parts else Text("")


# ---- 终端底层: 原始读键 + 活动区 ANSI(手写, 不引库) ----
# POSIX: termios 关 ECHO/ICANON/ISIG(ctrl+c 进字节流, 语义自己做);
# Windows: msvcrt.getwch + SetConsoleMode 清 ENABLE_PROCESSED_INPUT + 开 stdout VT。
# VT 开不出来(老 conhost)→ 退行式(烙定区照常, 活动区关)。
ESC = "\x1b"
_SPINNER_FRAMES = "✻✽✶✳✢·"
_SPINNER_VERBS = ("Deliberating", "Pondering", "Scheming", "Simmering",
                  "Noodling", "Herding", "Conjuring", "Brewing", "Vibing")

C_BORDER = "\x1b[90m"
C_PROMPT = "\x1b[95m"
C_DIM = "\x1b[90m"
C_TEXT = "\x1b[37m"
C_SPIN = "\x1b[93m"
C_WARN = "\x1b[33m"
C_RESET = "\x1b[0m"


def cells(s: str) -> int:
    from rich.cells import cell_len
    return cell_len(s)


def clip_width(s: str, width: int) -> str:
    """按显示宽截断(不折行, 活动区行数才可控)。"""
    if cells(s) <= width:
        return s
    while s and cells(s) > width:
        s = s[:-1]
    return s


class Term:
    """终端读写/原始模式/读键。键 = 单字符或控制名(UP/BS/CTRL_C/…)。"""

    def __init__(self, out=sys.stdout, inp=sys.stdin):
        self.out = out
        self.inp = inp
        self.vt = False
        self._raw_saved = None
        self._pend = b""
        self._is_win = sys.platform == "win32"

    # ---- 生命周期 ----
    def setup(self) -> None:
        if not self.inp.isatty():
            return
        if self._is_win:
            self.vt = self._win_setup()
        else:
            self.vt = self._posix_setup()
        if self.vt:
            self.write(f"{ESC}[?2004h")  # bracketed paste

    def teardown(self) -> None:
        if self.vt:
            self.write(f"{ESC}[?2004l{ESC}[?25h")
        if self._raw_saved is not None:
            import termios
            try:
                termios.tcsetattr(self.inp.fileno(), termios.TCSADRAIN, self._raw_saved)
            except Exception:
                pass

    def _posix_setup(self) -> bool:
        import termios, tty
        fd = self.inp.fileno()
        self._raw_saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ISIG  # cbreak 留着 ISIG, 显式关掉: ctrl+c 自己读
        # setcbreak 不动输入标志: ICRNL 会把 Enter 的 \r 翻成 \n, 必须关(实测踩过)
        attrs[0] &= ~(termios.ICRNL | termios.INLCR | termios.IGNCR)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return True

    def _win_setup(self) -> bool:
        """stdout 开 VT(失败=活动区关); stdin 关 PROCESSED/LINE/ECHO(ctrl+c 走字节流)。"""
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if not k32.GetConsoleMode(h, ctypes.byref(mode)):
                return False
            k32.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            hin = k32.GetStdHandle(-10)
            m2 = ctypes.c_uint32()
            if k32.GetConsoleMode(hin, ctypes.byref(m2)):
                k32.SetConsoleMode(hin, m2.value & ~0x0001 & ~0x0002 & ~0x0004)
            return True
        except Exception:
            return False

    # ---- 写 ----
    def write(self, s: str) -> None:
        self.out.write(s)
        self.out.flush()

    # ---- 读键 ----
    def read_key(self, timeout: float | None) -> str | None:
        if self._is_win:
            return self._read_key_win(timeout)
        import select
        while True:
            if self._pend:
                k = self._parse_posix()
                if k is not None:
                    return k
            r, _, _ = select.select([self.inp], [], [], timeout)
            if not r:
                # 只有孤立 ESC 时, 超时判定为单按 ESC
                if self._pend == b"\x1b":
                    self._pend = b""
                    return "ESC"
                return None
            data = os.read(self.inp.fileno(), 4096)
            if not data:
                return "CTRL_D"
            self._pend += data

    def _parse_posix(self) -> str | None:
        """_pend → 一个完整键; 不完整返回 None(等更多字节), 垃圾序列丢弃。"""
        p = self._pend
        if not p:
            return None
        b = p[0]
        if b == 0x1B:
            if len(p) < 2:
                import select
                r, _, _ = select.select([self.inp], [], [], 0.03)
                return None if r else ("ESC" if not r else None)  # 有后续字节: 等拼齐
            for pat, key in ((b"\x1b[A", "UP"), (b"\x1b[B", "DOWN"),
                             (b"\x1b[C", "RIGHT"), (b"\x1b[D", "LEFT"),
                             (b"\x1b[H", "HOME"), (b"\x1b[F", "END"),
                             (b"\x1bOH", "HOME"), (b"\x1bOF", "END"),
                             (b"\x1b[3~", "DEL"),
                             (b"\x1b[200~", "PASTE_ON"), (b"\x1b[201~", "PASTE_OFF")):
                if p.startswith(pat):
                    self._pend = p[len(pat):]
                    return key
            self._pend = b""  # 不认识的序列整体丢弃, 防卡死
            return None
        self._pend = p[1:]
        if b == 0x03:
            return "CTRL_C"
        if b == 0x04:
            return "CTRL_D"
        if b in (0x0D,):
            return "\r"
        if b in (0x7F, 0x08):
            return "BS"
        if b == 0x01:
            return "HOME"
        if b == 0x05:
            return "END"
        if b == 0x0B:
            return "KILL_END"
        if b == 0x15:
            return "KILL_BEG"
        if b == 0x17:
            return "KILL_WORD"
        if b == 0x1A:
            return "UNDO"
        if b == 0x0A:
            return "\n"
        if b == 0x09:
            return "\t"
        # utf-8 多字节
        if b < 0x80:
            return chr(b)
        n = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
        if len(p) < n:
            self._pend = p  # 凑齐再解
            return None
        try:
            return p[:n].decode("utf-8")
        finally:
            self._pend = p[n:]

    def _read_key_win(self, timeout: float | None) -> str | None:
        import msvcrt
        if timeout is not None:
            end = time.monotonic() + timeout
            while not msvcrt.kbhit():
                if time.monotonic() >= end:
                    return None
                time.sleep(0.02)
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT",
                    "G": "HOME", "O": "END", "S": "DEL"}.get(code, "")
        return ch


# ---- 输入编辑器(多行, claude 最小键位集) ----
class Editor:
    """lines[] 多行缓冲 + cy/cx 光标。UP/DOWN/CTRL_C/CTRL_D 由 TUI 层截获。"""

    def __init__(self):
        self.lines = [""]
        self.cy = 0
        self.cx = 0
        self.pasting = False
        self._undo: list[tuple[list[str], int, int]] = []

    def _snap(self) -> None:
        self._undo.append((list(self.lines), self.cy, self.cx))
        if len(self._undo) > 100:
            self._undo.pop(0)

    def text(self) -> str:
        return "\n".join(self.lines)

    def clear(self) -> None:
        self.lines = [""]
        self.cy = self.cx = 0
        self._undo.clear()

    def set_text(self, s: str) -> None:
        self.clear()
        self.insert(s)

    def insert(self, s: str) -> None:
        if not s:
            return
        self._snap()
        parts = s.split("\n")
        for i, part in enumerate(parts):
            line = self.lines[self.cy]
            self.lines[self.cy] = line[:self.cx] + part + line[self.cx:]
            self.cx += len(part)
            if i < len(parts) - 1:  # 中间换行: 落新行
                self.lines.insert(self.cy + 1, "")
                self.cy += 1
                self.cx = 0

    def handle(self, key: str) -> None:
        if key == "PASTE_ON":
            self.pasting = True
            self._snap()
            return
        if key == "PASTE_OFF":
            self.pasting = False
            return
        if self.pasting:
            if len(key) == 1 or (key and key[0] not in "\x1b"):
                self.insert(key)
            return
        if key == "\r":
            line = self.lines[self.cy]
            if line.endswith("\\"):  # claude 同款: 行尾反斜杠 = 软换行
                self._snap()
                self.lines[self.cy] = line[:-1]
                self.lines.insert(self.cy + 1, "")
                self.cy += 1
                self.cx = 0
            else:
                self.insert("\n")
            return
        if key == "BS":
            self._snap()
            line = self.lines[self.cy]
            if self.cx > 0:
                self.lines[self.cy] = line[:self.cx - 1] + line[self.cx:]
                self.cx -= 1
            elif self.cy > 0:
                self.cx = len(self.lines[self.cy - 1])
                self.lines[self.cy - 1] += line
                self.lines.pop(self.cy)
                self.cy -= 1
            return
        if key == "DEL":
            self._snap()
            line = self.lines[self.cy]
            if self.cx < len(line):
                self.lines[self.cy] = line[:self.cx] + line[self.cx + 1:]
            elif self.cy < len(self.lines) - 1:
                self.lines[self.cy] += self.lines.pop(self.cy + 1)
            return
        if key == "LEFT":
            if self.cx > 0:
                self.cx -= 1
            elif self.cy > 0:
                self.cy -= 1
                self.cx = len(self.lines[self.cy])
            return
        if key == "RIGHT":
            if self.cx < len(self.lines[self.cy]):
                self.cx += 1
            elif self.cy < len(self.lines) - 1:
                self.cy += 1
                self.cx = 0
            return
        if key == "HOME":
            self.cx = 0
            return
        if key == "END":
            self.cx = len(self.lines[self.cy])
            return
        if key == "KILL_END":
            self._snap()
            self.lines[self.cy] = self.lines[self.cy][:self.cx]
            return
        if key == "KILL_BEG":
            self._snap()
            self.lines[self.cy] = self.lines[self.cy][self.cx:]
            self.cx = 0
            return
        if key == "KILL_WORD":
            self._snap()
            line = self.lines[self.cy]
            head = line[:self.cx].rstrip()
            keep = head.rpartition(" ")[0]
            tail = line[self.cx:]
            joiner = " " if keep and tail else ""
            self.lines[self.cy] = keep + joiner + tail
            self.cx = len(keep) + len(joiner)
            return
        if key == "UNDO":
            if self._undo:
                self.lines, self.cy, self.cx = self._undo.pop()
            return
        if len(key) == 1 and key.isprintable():
            self.insert(key)


# ---- claude 式 TUI ----
class ZanyTui:
    def __init__(self, workdir: str, resume_id: str | None = None, agent=None,
                 term: Term | None = None, console: Console | None = None):
        self.workdir = workdir
        self.agent = agent or build_agent(workdir, with_mcp=False)  # MCP 后台补, 界面秒起
        self.state = {"messages": [], "marks": [], "goal": None}
        self._turn_ok = False
        self.mcp_stamp = config_stamp()
        self.sess_path = None
        self.resume_id = resume_id
        self.term = term or Term()
        self.console = console or Console()
        self.busy = False
        self._exit_pending = 0.0
        self._interrupt_pending = False
        self._last_cc = 0.0  # conpty ctrl+c 双投递去重(原 tui 同款坑)
        self._queued_text: str | None = None
        self.wakeup: "queue.Queue[str]" = queue.Queue()  # cron 唤醒
        self._bg: "queue.Queue" = queue.Queue()          # 后台线程 → 主循环
        self.ed = Editor()
        self.hist: list[str] = []
        self.hist_idx = -1
        self.hist_draft = ""
        self._frame_rows = 0
        self._dirty = True
        self._turn_t0 = None
        self._verb = 0
        self._last_tin = 0
        self._out_chars = 0  # 流式输出字符数(worker 写, spinner 估算 token 用)
        self._pending_pick: list | None = None  # /resume 编号选择态
        # 活动段共享状态(worker 写 / 主循环读)
        self._seg = {"think": "", "text": ""}
        self._seg_lock = threading.Lock()
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._th: threading.Thread | None = None

    # ---- 启动/收摊 ----
    def start(self) -> None:
        self.term.setup()
        self.console.print("[bold]✻ Welcome to zany[/] [dim](claude 式 TUI)[/]")
        self.console.print(f"[dim]  工作目录: {self.workdir} · tools: {', '.join(self.agent.tools)}[/]")
        self.console.print("[dim]  /new 新对话 · /resume 恢复 · /goal 持续目标 · ctrl+c 打断/退出 · ctrl+z 撤销输入[/]\n")
        if self.resume_id is not None:
            if self.resume_id:
                path = project_dir(self.workdir) / f"{self.resume_id}.json"
                if not path.exists():
                    self.console.print(f"[red]会话 {self.resume_id} 不存在(仅列本工作目录的会话)[/red]")
                    path = None
            else:
                picks = list_sessions(self.workdir)
                path = picks[0][0] if picks else None
                if not picks:
                    self.console.print("[dim]~/.zany/sessions/ 下没有历史会话[/dim]")
            if path and path.exists():
                self.do_resume(path)
        threading.Thread(target=self._mcp_worker, daemon=True).start()
        start_scheduler(lambda name, prompt: self.wakeup.put(prompt))

    def stop(self) -> None:
        self._erase_frame()  # 退出不留活动区残帧
        self.term.teardown()
        if self.state["messages"] and self.sess_path is not None:
            print(f"(继续对话: uv run tui2.py -r {self.sess_path.stem})", file=sys.stderr)

    def _mcp_worker(self) -> None:
        from core.mcp import failed_servers, load_mcp_tools
        for _ in range(6):
            try:
                tools = load_mcp_tools()
            except Exception:
                tools = []
            if tools:
                added = self.agent.add_tools(tools)
                if added:
                    self._bg.put(("line", f"[dim](MCP 工具就绪: {', '.join(added)})[/dim]"))
            if not failed_servers():
                return
            time.sleep(60)

    # ---- 烙定区打印(仅主循环调用; 打印前擦掉活动区) ----
    def _erase_frame(self) -> None:
        if self.term.vt and self._frame_rows:
            self.term.write(f"{ESC}[{self._frame_rows}A{ESC}[J")
        self._frame_rows = 0

    def log_write(self, text: str) -> None:
        self._erase_frame()
        self.console.print(text)

    def log_render(self, renderable) -> None:
        self._erase_frame()
        self.console.print(Text())  # 块间空行
        self.console.print(renderable)

    def chat_line(self, who: str, text: str) -> None:
        self._erase_frame()
        if who == "user":
            self.console.print(Text())
            self.console.print(Text(text, style="#d4d4d4 on grey27"))
        else:
            self.console.print(Text(text, justify="left", style="#d4d4d4"))

    # ---- 活动区整帧重绘 ----
    def _wrap_tail(self, s: str, width: int, max_rows: int) -> list[str]:
        """尾部 max_rows 视觉行(超宽折行, CJK 按双宽); 源太长只取尾部防 O(n²)。"""
        s = "".join(c for c in s if ord(c) >= 32 or c == "\t")  # 模型输出别往终端塞控制序列
        s = s[-(max_rows * width * 2 + 100):]
        rows: list[str] = []
        for line in s.splitlines() or [""]:
            line = line.expandtabs(4)
            while cells(line) > width and width > 0:
                cut = width
                while cut > 1 and cells(line[:cut]) > width:
                    cut -= 1
                rows.append(line[:cut])
                line = line[cut:]
            rows.append(line)
        return rows[-max_rows:]

    def _render_frame(self) -> None:
        w = shutil.get_terminal_size((100, 24)).columns
        rows: list[str] = []
        with self._seg_lock:
            think, text = self._seg["think"], self._seg["text"]
        if self.busy:
            src = text if text.strip() else (think if SHOW_THINKING else "")
            if src.strip():
                style = C_TEXT if text.strip() else C_DIM
                rows += [f"{style}{r}{C_RESET}" for r in self._wrap_tail(src, w - 2, 6)]
        if self.busy and self._turn_t0 is not None:
            fr = _SPINNER_FRAMES[int(time.monotonic() * 8) % len(_SPINNER_FRAMES)]
            verb = _SPINNER_VERBS[self._verb]
            secs = int(time.monotonic() - self._turn_t0)
            toks = fmt_tokens(max(1, self._out_chars // 3))
            rows.append(f"{C_SPIN}{fr}{C_RESET} {C_DIM}{verb}… (ctrl+c 打断 · ~{toks} tok · {secs}s){C_RESET}")
        # 输入框
        iw = max(16, w - 5)
        rows.append(f"{C_BORDER}╭{'─' * (w - 2)}╮{C_RESET}")
        for i, line in enumerate(self.ed.lines):
            mark = f"{C_PROMPT}❯{C_RESET} " if i == 0 else "  "
            body = clip_width(line, iw)
            pad = " " * max(0, iw - cells(body))
            rows.append(f"{C_BORDER}│{C_RESET} {mark}{body}{pad}{C_RESET}{C_BORDER}│{C_RESET}")
        rows.append(f"{C_BORDER}╰{'─' * (w - 2)}╯{C_RESET}")
        # 工具条
        if self._exit_pending and time.monotonic() - self._exit_pending <= EXIT_WINDOW:
            hint = f"{C_WARN}» 再按一次 ctrl+c 退出{C_RESET}"
        elif self.busy:
            hint = f"{C_WARN}» 运行中 · ctrl+c 打断{C_RESET}"
        else:
            hint = f"{C_DIM}» /new /resume /goal · exit 退出{C_RESET}"
        ctx_max = int(os.environ.get("ANTHROPIC_CONTEXT_TOKENS", "200000"))
        pct = min(100, round(self._last_tin / ctx_max * 100)) if self._last_tin else 0
        cc = "\x1b[31m" if pct >= 95 else "\x1b[33m" if pct >= 50 else "\x1b[32m"
        filled = round(pct / 100 * 10)
        tin_bar = f"{cc}{'█' * filled}{'░' * (10 - filled)}{C_RESET} {C_DIM}{fmt_tokens(self._last_tin)}/{fmt_tokens(ctx_max)} · {pct}%{C_RESET}"
        rows.append(f" {hint}{C_DIM} · {tin_bar}{C_RESET}")
        # 重绘
        self._erase_frame()
        self.term.write("\n".join(rows) + "\n")
        self._frame_rows = len(rows)
        if self.term.vt:
            up = len(self.ed.lines) + 2 - self.ed.cy  # 帧尾写完带 \n(光标在框下一空行): 框底边+工具条隔 2 行到输入行
            col = 4 + cells(self.ed.lines[self.ed.cy][:self.ed.cx])
            self.term.write(f"{ESC}[{up}A{ESC}[{col + 1}G")

    # ---- 键处理 ----
    def _on_key(self, key: str) -> None:
        if key == "CTRL_C":
            now = time.monotonic()
            if now - self._last_cc < 0.1:
                return  # conpty 同击双投递去重
            self._last_cc = now
            if self.busy:
                if self._exit_pending and now - self._exit_pending <= EXIT_WINDOW:
                    raise SystemExit(0)
                self._exit_pending = now
                if not self._interrupt_pending:
                    self._interrupt_pending = True
                    self.agent.interrupt("用户按了 ctrl+c")
                    self._q.put(("line", "[dim]⚡ 打断请求已发出(再按一次 ctrl+c 退出)…[/dim]"))
                return
            if self.ed.text() or self._pending_pick is not None:
                self.ed.clear()
                self._pending_pick = None
                self._dirty = True
                return
            if self._exit_pending and now - self._exit_pending <= EXIT_WINDOW:
                raise SystemExit(0)
            self._exit_pending = now
            self._dirty = True
            return
        self._exit_pending = 0.0
        if key == "CTRL_D":
            if not self.busy and not self.ed.text():
                raise SystemExit(0)
            return
        if key in ("UP", "DOWN"):
            self._nav(key)
            self._dirty = True
            return
        if key in ("\r", "\n"):  # \n 兼底: 没关掉 ICRNL 的环境 Enter 会变 \n
            t = self.ed.text().strip()
            self.ed.clear()
            self.hist_idx = -1
            if self._pending_pick is not None:
                picks, self._pending_pick = self._pending_pick, None
                if t.isdigit() and 1 <= int(t) <= len(picks):
                    self.do_resume(picks[int(t) - 1][0])
                self._dirty = True
                return
            if not t:
                self._dirty = True
                return
            if t.lower() in ("exit", "quit"):
                raise SystemExit(0)
            if self.busy:
                # claude/hermes 同款: 忙时回车 = 打断当前轮 + 文本排下一轮(steer)
                if not self._interrupt_pending:
                    self._interrupt_pending = True
                    self.agent.interrupt("用户按了 ctrl+c")
                    self._q.put(("line", "[dim]⚡ 已请求打断, 这条排下一轮[/dim]"))
                self._queued_text = (self._queued_text + "\n" + t) if self._queued_text else t
                self._dirty = True
                return
            self.hist.insert(0, t)
            self._dirty = True
            self._submit(t)
            return
        self.ed.handle(key)
        self._dirty = True

    def _nav(self, key: str) -> None:
        ed = self.ed
        # 多行缓冲先走行间移动, 顶/底边缘才是历史
        if key == "UP" and ed.cy > 0:
            ed.cy -= 1
            ed.cx = min(ed.cx, len(ed.lines[ed.cy]))
            return
        if key == "DOWN" and ed.cy < len(ed.lines) - 1:
            ed.cy += 1
            ed.cx = min(ed.cx, len(ed.lines[ed.cy]))
            return
        if not self.hist:
            return
        if key == "UP":
            if self.hist_idx == -1:
                self.hist_draft = ed.text()
                self.hist_idx = 0
            elif self.hist_idx < len(self.hist) - 1:
                self.hist_idx += 1
            else:
                return
        else:
            if self.hist_idx == -1:
                return
            self.hist_idx -= 1
            if self.hist_idx < 0:
                ed.set_text(self.hist_draft)
                self.hist_idx = -1
                return
        ed.set_text(self.hist[self.hist_idx])

    # ---- 主循环 ----
    def run(self) -> None:
        self.start()
        if not self.term.vt:
            self._run_plain()  # 管道 stdin / 老 conhost 无 VT: 烙定区照常 + cooked input()
            return
        self._render_frame()
        try:
            while True:
                key = self.term.read_key(0.1 if self.busy else 0.2)
                self._drain_bg()
                self._drain_q()  # 轮内的工具行/分段定稿要即时上屏, 不能憋到轮末
                if key:
                    self._on_key(key)
                if self.busy and self._th is not None and not self._th.is_alive():
                    self._finish_turn()
                if self._dirty or self.busy:
                    self._render_frame()
                    self._dirty = False
        except (SystemExit, KeyboardInterrupt):
            pass
        finally:
            self.stop()

    def _run_plain(self) -> None:
        """无 VT 退行式: 不画框、不玩光标, input() 接行; 忙时排水队列到轮末。"""
        import signal as _s

        def _int(sig, frm):
            self.agent.interrupt("用户打断")
            _s.signal(_s.SIGINT, _s.SIG_DFL)  # 再按直接退出

        try:
            while True:
                try:
                    text = input(f"{C_PROMPT}❯ {C_RESET}").strip()
                except EOFError:
                    break
                except KeyboardInterrupt:
                    print()
                    continue
                if not text:
                    continue
                try:
                    if text.lower() in ("exit", "quit"):
                        break
                    self._submit(text)
                    if self._pending_pick is not None:
                        try:
                            num = input("编号> ").strip()
                        except (EOFError, KeyboardInterrupt):
                            num = ""
                        picks, self._pending_pick = self._pending_pick, None
                        if num.isdigit() and 1 <= int(num) <= len(picks):
                            self.do_resume(picks[int(num) - 1][0])
                        continue
                    if self.busy:
                        _s.signal(_s.SIGINT, _int)
                        while True:
                            while self.busy and self._th is not None and self._th.is_alive():
                                self._drain_q()
                                self._drain_bg()
                                time.sleep(0.1)
                            self._drain_q()
                            self._finish_turn()
                            if not self.busy:
                                break  # turn_finished 可能链式 goal/cron 续跑, 追到停为止
                        _s.signal(_s.SIGINT, _s.SIG_DFL)
                except SystemExit:
                    break
        finally:
            self.stop()

    def _drain_bg(self) -> None:
        while True:
            try:
                kind, payload = self._bg.get_nowait()
            except queue.Empty:
                return
            if kind == "line":
                self.log_write(payload)

    def _drain_q(self) -> None:
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                return
            kind = item[0]
            if kind == "line":
                self.log_write(item[1])
            elif kind == "render":
                self.log_render(item[1])
            elif kind == "usage":
                self._last_tin = item[1]

    # ---- 一轮对话 ----
    def _submit(self, text: str) -> None:
        if text.lower() in ("/resume", "/r"):
            picks = self.resume_picker()
            if picks:
                self._pending_pick = picks
            return
        if text.lower() in ("/new", "/n"):
            if self.busy:
                self.log_write("[dim]当前轮还在跑, 先 ctrl+c 打断再 /new[/dim]")
                return
            self.new_chat()
            return
        if text.lower().startswith("/goal"):
            self.handle_goal(text[5:].strip())
            return
        self.chat_line("user", text)
        self.run_turn(text)

    def run_turn(self, text: str) -> None:
        self._turn_ok = False
        self.busy = True
        self._turn_t0 = time.monotonic()
        self._verb = hash(text) % len(_SPINNER_VERBS)
        self._out_chars = 0
        with self._seg_lock:
            self._seg = {"think": "", "text": ""}
        # mcp_servers.json 变了 → 重建 agent(与原 tui 同款)
        if config_stamp() != self.mcp_stamp:
            self.mcp_stamp = config_stamp()
            try:
                self.agent = build_agent(self.workdir)
                if self.state["goal"]:
                    attach_goal(self.agent, self.state["goal"])
                self._q.put(("line", "[dim](检测到 mcp_servers.json 变更, 已重载 MCP 工具)[/dim]"))
            except Exception as e:
                self._q.put(("line", f"[dim](MCP 重载失败, 沿用旧配置: {e})[/dim]"))
        msgs = self.state["messages"] + [{"role": "user", "content": user_content(text)}]
        self._th = threading.Thread(target=self._turn_worker, args=(msgs,), daemon=True)
        self._th.start()

    def _turn_worker(self, msgs: list[dict]) -> None:
        """worker 线程: 只改 _seg 与 _q, 不碰终端(单写者原则)。"""
        q = self._q
        think_buf = text_buf = ""
        tool_t0 = {}
        think_t0 = think_last = None
        n_tools = 0

        def flush_seg() -> None:
            nonlocal think_buf, text_buf
            if (think_buf.strip() and SHOW_THINKING) or text_buf.strip():
                q.put(("render", _turn_renderable(think_buf, text_buf, fold=True)))
                think_buf, text_buf = "", ""

        try:
            for ev in self.agent.stream(msgs):
                kind = ev[0]
                if kind == "thinking":
                    now = time.monotonic()
                    think_t0 = think_t0 or now
                    think_last = now
                    think_buf += ev[1]
                elif kind == "text":
                    text_buf += ev[1]
                    self._out_chars = len(text_buf)
                elif kind == "tool_start":
                    tool_t0[ev[1]] = time.monotonic()
                elif kind == "tool_result":
                    flush_seg()
                    n_tools += 1
                    _, _id, name, input_json, output = ev
                    dur = time.monotonic() - tool_t0.pop(_id, time.monotonic())
                    try:
                        args = json.loads(input_json)
                    except json.JSONDecodeError:
                        args = {}
                    q.put(("line", tool_line(name, args, dur, output)))
                elif kind == "usage":
                    q.put(("usage", ev[1]))
                elif kind == "compact":
                    q.put(("line", f"[dim]🗜 上下文压缩: {fmt_tokens(ev[1])} → ~{fmt_tokens(ev[2])} tokens[/dim]"))
                elif kind == "interrupted":
                    q.put(("line", f"[yellow]⚡ 已打断: {ev[1]}[/yellow]"))
                elif kind == "compact_error":
                    q.put(("line", f"[dim](自动压缩已停用: {ev[1]})[/dim]"))
                elif kind == "turn_limit":
                    q.put(("line", f"[yellow]■ 已达最大轮数 {ev[1]}, 本轮中止(继续发消息可接着干)[/yellow]"))
                with self._seg_lock:
                    self._seg = {"think": think_buf, "text": text_buf}
            self.state["messages"] = msgs
            self._turn_ok = True
        except Exception as e:
            flush_seg()
            q.put(("line", f"[red]对话出错: {e}[/red]"))
            return
        # 落盘 + marks(与原 tui 同口径)
        self.state["marks"].append({
            "at": len(self.state["messages"]),
            "thought": (max(1, round(think_last - think_t0)) if think_t0 is not None else None),
            "tools": n_tools,
        })
        if self.sess_path is None:
            self.sess_path = new_session_file(self.workdir)
        try:
            save_session(self.sess_path, self.state["messages"], self.agent.usage,
                         self.state["marks"], self.workdir, goal=self.state["goal"])
        except Exception as e:
            q.put(("line", f"[dim](会话保存失败: {e})[/dim]"))
        flush_seg()
        marks = []
        if think_t0 is not None:
            marks.append(f"Thought for {max(1, round(think_last - think_t0))}s")
        if n_tools:
            marks.append(f"ran {n_tools} tool call{'s' if n_tools > 1 else ''}")
        if marks:
            q.put(("line", f"[dim]{', '.join(marks)}[/dim]"))

    def _finish_turn(self) -> None:
        self._drain_q()
        self.busy = False
        self._turn_t0 = None
        self._interrupt_pending = False
        with self._seg_lock:
            self._seg = {"think": "", "text": ""}
        self._last_tin = self.agent.usage.get("last_in") or self._last_tin
        self._dirty = True
        self.turn_finished()

    def turn_finished(self) -> None:
        """忙时插话(steer) > cron 唤醒 > goal 续跑。"""
        if self._queued_text:
            queued, self._queued_text = self._queued_text, None
            self.chat_line("user", queued)
            self.run_turn(queued)
            return
        while not self.wakeup.empty():
            prompt = self.wakeup.get()
            self.log_write("[bold cyan]⏰ [定时任务触发][/bold cyan]")
            self.run_turn(prompt)
            return
        g = self.state["goal"]
        if g and g["status"] == "active" and self._turn_ok:
            if g["turns_used"] >= g["max_turns"]:
                g["status"] = "paused"
                self.log_write(f"[yellow]🎯 goal 预算用完({g['max_turns']} 轮), 已暂停 — /goal resume 继续, /goal off 放弃[/yellow]")
            else:
                g["turns_used"] += 1
                self.log_write(f"[bold cyan]🎯 [goal 续跑 {g['turns_used']}/{g['max_turns']}][/bold cyan]")
                self.run_turn(continuation_prompt(g))

    # ---- /new / /resume / /goal ----
    def new_chat(self) -> None:
        self.state["messages"] = []
        self.state["marks"] = []
        if self.state["goal"]:
            detach_goal(self.agent)
        self.state["goal"] = None
        self.sess_path = None
        self.agent.usage = {"input": 0, "output": 0, "calls": 0}
        self.agent._last_prompt_tokens = None
        self._last_tin = 0
        self._erase_frame()
        self.console.print("[dim](新对话, 下轮落盘另起新会话)[/dim]")
        self._dirty = True

    def do_resume(self, path) -> None:
        data = load_session_data(path)
        self.state["messages"] = data["messages"]
        self.state["marks"] = data.get("marks", []) or []
        g = data.get("goal")
        self.state["goal"] = g
        if g:
            attach_goal(self.agent, g)
            self.log_write(f"[cyan]🎯 goal 恢复[{g['status']}] 第 {g['turns_used']}/{g['max_turns']} 轮: {g['objective']}[/cyan]")
        self.sess_path = path
        if data.get("usage"):
            self.agent.restore(data["usage"])
        tin = self.agent.usage.get("last_in") or est_tokens(
            [{"role": "system", "content": self.agent._system()}] + self.state["messages"])
        self._last_tin = tin
        self.log_write(f"[dim]已恢复 {path.stem} ({len(self.state['messages'])} 条消息):[/dim]")
        marks_by_at = {m["at"]: m for m in self.state["marks"]}
        msgs = self.state["messages"]
        tool_uses_by_id: dict = {}

        def _marks_at(at: int) -> None:
            mk = marks_by_at.get(at)
            if mk:
                parts = []
                if mk.get("thought"):
                    parts.append(f"Thought for {mk['thought']}s")
                if mk.get("tools"):
                    parts.append(f"ran {mk['tools']} tool call{'s' if mk['tools'] > 1 else ''}")
                if parts:
                    self.log_write(f"[dim]{', '.join(parts)}[/dim]")

        for idx, m in enumerate(msgs):
            content = m.get("content")
            is_tool_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            if m["role"] == "assistant" and isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_uses_by_id[b["id"]] = (b.get("name", "?"), b.get("input", {}))
            if is_tool_result:
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        name, args = tool_uses_by_id.get(b.get("tool_use_id"), ("?", {}))
                        self.log_write(tool_line(name, args, 0.0, result_text(b.get("content", ""))))
                _marks_at(idx + 1)
            elif m["role"] == "user":
                self.chat_line("user", text_of(m).strip())
            elif m["role"] == "assistant":
                th = "".join(
                    b.get("thinking", "") for b in (content or [])
                    if isinstance(b, dict) and b.get("type") == "thinking"
                ) if isinstance(content, list) else ""
                t = text_of(m).strip()
                if (th and SHOW_THINKING) or t:
                    self.log_render(_turn_renderable(th, t, fold=True))
                    _marks_at(idx + 1)

    def resume_picker(self) -> list | None:
        from rich.markup import escape

        picks = list_sessions(self.workdir)
        if not picks:
            self.log_write("[dim]本工作目录还没有历史会话[/dim]")
            return None
        self._erase_frame()
        for i, (_f, ts, snippet, n) in enumerate(picks, 1):
            self.console.print(f"  [cyan]{i}[/]. {ts}  ({n} 条)  {escape(snippet)}")
        self.console.print("[dim]输入编号选会话, 空回车取消[/dim]")
        self._dirty = True
        return picks


# ---- 无头模式(与 tui.py 同语义) ----
def _headless(argv: list[str]) -> None:
    prompt_parts: list[str] = []
    resume_id: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] in ("--resume", "-r"):
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
    workdir = os.getcwd()
    messages = []
    sess_path = None
    if resume_id is not None:
        if resume_id:
            sess_path = project_dir(workdir) / f"{resume_id}.json"
        else:
            files = sorted(project_dir(workdir).glob("*.json"))
            if not files:
                raise SystemExit("本工作目录没有历史会话")
            sess_path = files[-1]
        if sess_path.exists():
            data = load_session_data(sess_path)
            messages = data["messages"]
            if data.get("usage"):
                agent.restore(data["usage"])
            print(f"(接着会话 {sess_path.stem}, 已有 {len(messages)} 条)", file=sys.stderr)
        else:
            print(f"(新会话 {sess_path.stem})", file=sys.stderr)
    messages = messages + [{"role": "user", "content": prompt}]
    import signal as _sig

    def _sigint(signum, frame):
        agent.interrupt("用户打断")
        _sig.signal(_sig.SIGINT, _sig.SIG_DFL)

    _sig.signal(_sig.SIGINT, _sigint)
    agent.invoke(messages)
    if agent.was_interrupted:
        print("(已打断, 以上是收到的部分内容)", file=sys.stderr)
    print(text_of(messages[-1]))
    if sess_path is None:
        sess_path = new_session_file(workdir)
    save_session(sess_path, messages, agent.usage, workdir=workdir)
    u = agent.usage
    print(f"(tokens: in {u['input']:,} / out {u['output']:,}, {u['calls']} calls)", file=sys.stderr)
    print(f"(session: {sess_path.stem})", file=sys.stderr)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print('用法: tui2.py [-r <会话id>] | [-p "任务"]')
        return
    if len(sys.argv) > 1 and sys.argv[1] == "-p":
        _headless(sys.argv[2:])
        return
    argv = sys.argv[1:]
    resume_id = None
    if argv and argv[0] in ("--resume", "-r"):
        resume_id = argv[1] if len(argv) > 1 else ""
    ZanyTui(os.getcwd(), resume_id).run()


if __name__ == "__main__":
    main()
