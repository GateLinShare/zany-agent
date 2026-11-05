"""内置工具:ls / read_file / write_file / edit_file / execute。

路径约定(pi/hermes 同款):**不做任何路径重写** —— 绝对路径字面直用,
相对路径相对 workdir 解析; execute 本就是真实 shell, 四件套与它同一套
文件系统视图。不做虚拟根/越界 jail(execute 能干任何事, 工具层圈地无意义),
约定写进各文件工具 description(调用决策点, 单一信息源)。

防爆层(学 pi/hermes, 逐项见 docs/tools-comparison.md 优化清单):
- execute 输出保头 40% + 保尾 60%(错误常在前头, 最新输出在后头), 全量落盘给路径
- ls 有条目上限, read 截断提示带精确续读参数
- edit 剥 BOM / CRLF 透明还原(pi: 匹配在 LF 空间, 回写按原行尾风格)
- 行为熔断账本 _Ledger: 重复读去重 / edit 连败升级提示 / 同命令重跑警告;
  上下文压缩后由 loop.reset_tool_ledger() 清空(旧 read 结果可能已被摘要掉)
"""

import contextlib
import os
import signal
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

MAX_OUTPUT = 200000  # 单个工具结果上限(字符), 超长截断 —— 上下文第一层防线
EXEC_TIMEOUT = 1200  # execute 默认超时(秒)
LS_MAX = 500         # ls 单次列出的条目上限


@dataclass
class Tool:
    """统一工具形态:内置工具和 MCP 工具都归一成这个, 循环层不区分来源。"""

    name: str
    description: str
    input_schema: dict
    run: Callable[[dict], str]  # 入参 dict -> 结果文本(错误也返回文本, 不抛异常)
    ledger: Any | None = None   # 行为熔断账本(仅内置工具); loop 在压缩后调 reset()
    kill: Callable[[], None] | None = None  # 打断废弃时的强杀钩子(execute: 杀进程组)

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class _Ledger:
    """工具行为熔断账本(hermes file_tools 防循环语义的简化版)。

    每个 agent 实例一份(builtin_tools 构造时创建), 跟随 agent 生命周期;
    上下文压缩后 loop 层调 reset() 清空 —— 摘要会改写掉旧 read 结果,
    此时"内容见上文"的去重 stub 会误导模型。"""

    def __init__(self) -> None:
        self.reads: dict = {}                 # 解析后路径 -> {(offset, limit), ...}
        self.edit_fails: dict = {}            # 路径 -> 连续编辑失败次数
        self._last_exec: tuple | None = None  # (command, 连续次数)

    def seen_read(self, path: str, offset: int, limit: int) -> bool:
        """同参数重复读返回 True(首读记一笔)。"""
        seen = self.reads.setdefault(path, set())
        if (offset, limit) in seen:
            return True
        seen.add((offset, limit))
        return False

    def forget(self, path: str) -> None:
        """write/edit 落盘后调用: 该文件的读去重记录与连败计数一并失效。"""
        self.reads.pop(path, None)
        self.edit_fails.pop(path, None)

    def fail_edit(self, path: str) -> int:
        self.edit_fails[path] = self.edit_fails.get(path, 0) + 1
        return self.edit_fails[path]

    def ok_edit(self, path: str) -> None:
        self.edit_fails.pop(path, None)

    def see_exec(self, command: str) -> int:
        """同一条命令连续执行计数(换命令即重置)。"""
        last, n = self._last_exec or (None, 0)
        n = n + 1 if last == command else 1
        self._last_exec = (command, n)
        return n

    def reset(self) -> None:
        self.__init__()  # 字段都是简单容器, 重新初始化即全清


def _resolve(workdir: str, path: str) -> Path:
    """pi/hermes 同款语义: 绝对路径字面直用, 相对路径相对 workdir。

    不存在时抛 FileNotFoundError, 报错带 workdir 锚点让模型自纠
    (异常由 loop 的错误即文本兜底转回 tool_result)。
    """
    p = Path(str(path)).expanduser()
    return p.resolve() if p.is_absolute() else (Path(workdir) / p).resolve()


def _truncate(text: str) -> str:
    """200k 上限, 保头 40% + 保尾 60%(hermes: 错误常在前头, 最新输出在后头)。"""
    if len(text) <= MAX_OUTPUT:
        return text
    head, tail = int(MAX_OUTPUT * 0.4), int(MAX_OUTPUT * 0.6)
    return text[:head] + f"\n…(中间省略 {len(text) - head - tail} 字符)…" + text[-tail:]


def _ls(workdir, args) -> str:
    d = _resolve(workdir, args.get("path", "."))
    if not d.is_dir():
        return f"不是目录: {args.get('path', '/')}"
    entries = sorted(d.iterdir())
    lines = []
    for f in entries[:LS_MAX]:
        kind = "dir " if f.is_dir() else "file"
        size = f" {f.stat().st_size}B" if f.is_file() else ""
        lines.append(f"{kind}  {f.name}{size}")
    if len(entries) > LS_MAX:
        lines.append(f"…(共 {len(entries)} 项, 只列前 {LS_MAX} 项, 换更具体的 path)")
    return "\n".join(lines) or "(空目录)"


def _read(workdir, args, ledger) -> str:
    f = _resolve(workdir, args["path"])
    offset, limit = int(args.get("offset", 0)), int(args.get("limit", 100))
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    if ledger.seen_read(str(f), offset, limit):
        # hermes 去重语义: 同文件重读不回显, 指回早前结果(write/edit 会失效本记录)
        return (f"提示: {args['path']} (offset={offset}, limit={limit}) 刚读过且此后无写入, 全文见上文, 不重复回显;"
                " 若文件可能已被 execute 等外部改动, 或确实需要再看, 换一组 offset/limit 重读")
    page = lines[offset : offset + limit]
    out = "\n".join(f"{i + offset + 1:6d}\t{l}" for i, l in enumerate(page))
    if offset + limit < len(lines):
        out += f"\n…(共 {len(lines)} 行, 还有 {len(lines) - offset - limit} 行, 用 offset={offset + limit} 继续读)"
    return out or "(空文件)"


def _write(workdir, args, ledger) -> str:
    f = _resolve(workdir, args["path"])
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(args["content"], encoding="utf-8")
    ledger.forget(str(f))  # 新内容落盘, 该文件的读去重/连败记录失效
    return f"已写入 {args['path']} ({len(args['content'])} 字符)"


_FUZZY_TABLE = str.maketrans({  # pi edit 同款归一化: 智能引号/破折号/特殊空格 → ASCII
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", "　": " ",
    **{chr(c): " " for c in range(0x2002, 0x200B)},
})


def _fuzzy(text: str) -> str:
    """NFKC + 特殊字符转 ASCII + 每行去尾空白; 不改行数, 用于失配重试。"""
    t = unicodedata.normalize("NFKC", text).translate(_FUZZY_TABLE)
    return "\n".join(line.rstrip() for line in t.split("\n"))


def _overlay(original: str, base: str, spans: list) -> str:
    """模糊命中后的回写: 只有替换覆盖到的行取归一化文本, 其余行保留原字节。"""
    o_lines = original.splitlines(keepends=True)
    b_lines = base.splitlines(keepends=True)
    offs, acc = [], 0
    for l in b_lines:
        offs.append((acc, acc + len(l)))
        acc += len(l)

    def line_of(pos: int) -> int:
        for i, (s, e) in enumerate(offs):
            if s <= pos < e:
                return i
        return len(offs) - 1

    groups = []  # [起始行, 结束行(含), [(start, len, new)]], 行区间重叠的合并
    for start, ln, new in sorted(spans):
        s_line, e_line = line_of(start), line_of(start + ln - 1)
        if groups and s_line <= groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], e_line)
            groups[-1][2].append((start, ln, new))
        else:
            groups.append([s_line, e_line, [(start, ln, new)]])

    out, li = [], 0
    for s_line, e_line, grp in groups:
        out.extend(o_lines[li:s_line])
        seg = "".join(b_lines[s_line:e_line + 1])
        g_off = offs[s_line][0]
        for start, ln, new in sorted(grp, reverse=True):  # 从后往前套用, 偏移稳定
            rel = start - g_off
            seg = seg[:rel] + new + seg[rel + ln:]
        out.append(seg)
        li = e_line + 1
    out.extend(o_lines[li:])
    return "".join(out)


def _edit(workdir, args, ledger) -> str:
    f = _resolve(workdir, args["path"])

    def fail(msg: str) -> str:
        """错误出口: 记连败, ≥3 次附升级提示(hermes: 把"你在循环里"喟给模型)。"""
        n = ledger.fail_edit(str(f))
        extra = (f"\n提示: {args['path']} 已连续编辑失败 {n} 次, 停止用变体重试 ——"
                 " 先 read_file 重读原文核对, 或改用 write_file 整文件重写") if n >= 3 else ""
        return f"错误: {msg}{extra}"

    if not f.is_file():
        # 不走异常: "对不存在的文件反复 edit"是最典型的打转场景, 必须计连败
        return fail(f"文件不存在: {args['path']} (先 ls 确认路径; 工作目录 {workdir})")
    # BOM/CRLF 透明处理(pi edit-diff): 模型的 old_string 不会带 BOM,
    # CRLF 文件在 LF 空间匹配, 回写时还原原行尾 —— 防止"edit 成功但全文行尾被改"。
    raw = f.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    if had_bom:
        raw = raw[3:]
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8", errors="replace")
    if crlf:
        text = text.replace("\r\n", "\n")

    # 两种入参: 单处 old_string/new_string(兼容旧格式), 或 pi 式 edits 数组一次改多处
    edits = args.get("edits") or [{"old_string": args["old_string"], "new_string": args.get("new_string", "")}]
    if not edits:
        return fail("edits 为空")

    # 所有 edit 都对原始全文定位(pi 的关键约定), 坐标存 spans, 最后统一从后往前替换
    spans = []  # (start, len, new_text)
    fuzzy = False
    for i, e in enumerate(edits):
        old, new = e["old_string"], e["new_string"]
        if not old:
            return fail(f"edits[{i}].old_string 为空")
        n = text.count(old)
        if e.get("replace_all", args.get("replace_all", False)):
            if n == 0:
                return fail(f"edits[{i}] 在 {args['path']} 中不存在")
            pos = text.find(old)
            while pos != -1:
                spans.append((pos, len(old), new))
                pos = text.find(old, pos + len(old))
            continue
        if n == 1:
            spans.append((text.index(old), len(old), new))
        elif n > 1:
            return fail(f"edits[{i}] 出现 {n} 次, 不唯一; 拆成 edits 数组各处一条, 或加更多上下文")
        else:
            fuzzy = True  # 精确失配 → 全体切换到归一化空间重试(混合坐标系没有意义)
            break

    ntext = ""
    if fuzzy:
        ntext = _fuzzy(text)
        spans = []
        for i, e in enumerate(edits):
            if e.get("replace_all"):
                return fail(f"edits[{i}] 模糊匹配场景不支持 replace_all, 先 read_file 看清再精确编辑")
            old = _fuzzy(e["old_string"])
            n = ntext.count(old)
            if n != 1:
                return fail(f"edits[{i}] 在 {args['path']} 中精确/模糊均未唯一匹配(模糊 {n} 处)")
            spans.append((ntext.index(old), len(old), _fuzzy(e["new_string"])))

    spans.sort()
    for a, b in zip(spans, spans[1:]):
        if a[0] + a[1] > b[0]:
            return fail("两处 edit 区域重叠, 合并成一条或改成不相交区域")

    if fuzzy:
        new = _overlay(text, ntext, spans)
    else:
        new = text
        for start, ln, new_s in reversed(spans):
            new = new[:start] + new_s + new[start + ln:]
    if new == text:
        return fail(f"替换后内容无变化 ({args['path']})")

    if crlf:
        new = new.replace("\n", "\r\n")
    data = new.encode("utf-8")
    if had_bom:
        data = b"\xef\xbb\xbf" + data
    f.write_bytes(data)
    ledger.forget(str(f))  # 落盘成功: 连败清零 + 读去重失效
    return f"已替换 {len(spans)} 处 ({args['path']})" + ("(模糊匹配)" if fuzzy else "")


def _killpg(p: subprocess.Popen) -> None:
    """杀 p 所在进程组; 已死/无权限则忽略。"""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)  # 杀整组, 防 shell 的孙子进程残留孤儿


def _execute_tool(workdir, ledger) -> Tool:
    """execute 工具工厂: 闭包持有在途 Popen, 供 loop 打断废弃时 kill()
    (立即打断语义: 整组 SIGKILL, 不等命令自然结束)。
    同一 agent 串行派发工具, live 单槽足够。"""
    live: list = []

    def run(args: dict) -> str:
        command, t = args["command"], int(args.get("timeout", EXEC_TIMEOUT))
        # start_new_session(POSIX): 子进程自成进程组, 超时/打断可整组击杀(pi bash.ts 语义)
        p = subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", cwd=workdir, start_new_session=True,
        )
        live[:] = [p]
        try:
            out, err = p.communicate(timeout=t)
        except subprocess.TimeoutExpired:
            _killpg(p)
            out, err = p.communicate()  # 收尸, 保住超时前已产出的输出
            partial = _truncate(((out or "") + (("\n[stderr] " + err) if err else "")).strip())
            return f"错误: 命令超时(>{t}s), 已 kill 整个进程组。\n[超时前的输出]\n{partial}" if partial else \
                f"错误: 命令超时(>{t}s), 已 kill 整个进程组(无输出)"
        text = (out or "") + (("\n[stderr] " + err) if err else "")
        text = (text.strip() or "(无输出)") + (f"\n[exit {p.returncode}]" if p.returncode else "")
        n = ledger.see_exec(command)
        if n >= 3:
            text += f"\n[提示: 同一命令已连续执行 {n} 次, 结果大概率不变; 若在等外部变化, 换个验证方式]"
        if len(text) > MAX_OUTPUT:
            tmp = tempfile.NamedTemporaryFile(prefix="agent-exec-", suffix=".log", delete=False)
            tmp.write(text.encode("utf-8", errors="replace"))
            tmp.close()
            return _truncate(text) + f"\n…(完整输出 {len(text)} 字符已存 {tmp.name}, 可 execute tail/cat 查看)"
        return text

    def kill() -> None:
        if live and live[0].poll() is None:
            _killpg(live[0])

    return Tool("execute", f"执行 shell 命令(真实 shell, 用真实路径), 默认超时 {EXEC_TIMEOUT} 秒。",
                {"type": "object", "properties": {
                     "command": {"type": "string"}, "timeout": {"type": "integer"}},
                 "required": ["command"]},
                run, ledger=ledger, kill=kill)


def builtin_tools(workdir: str) -> list[Tool]:
    """五个内置工具; run 闭包绑死 workdir, 无全局状态。
    行为熔断账本每个 agent 实例一份(本函数在构造 agent 时各调一次)。"""
    ledger = _Ledger()
    return [
        Tool("ls", "列目录。path 相对工作目录解析, 绝对路径按真实文件系统。",
             {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
             lambda a: _ls(workdir, a), ledger=ledger),
        Tool("read_file", "读文件, 默认前 100 行; 大文件用 offset/limit 分页。"
             "path 相对工作目录解析, 绝对路径按真实文件系统。",
             {"type": "object", "properties": {
                 "path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
              "required": ["path"]},
             lambda a: _read(workdir, a, ledger), ledger=ledger),
        Tool("write_file", "整文件覆盖写, 可新建文件(自动建父目录)。content 必填。"
             "path 相对工作目录解析, 绝对路径按真实文件系统。",
             {"type": "object", "properties": {
                 "path": {"type": "string"}, "content": {"type": "string"}},
              "required": ["path", "content"]},
             lambda a: _write(workdir, a, ledger), ledger=ledger),
        Tool("edit_file",
             "编辑文件。单处: old_string/new_string; 多处一次改: edits=[{old_string,new_string}], "
             "可加 replace_all 替换全部同名出现。old_string 必须唯一, 否则报错; "
             "精确匹配失败会做归一化重试(智能引号/破折号/行尾空白), 命中则回写时保留未动行。"
             "path 相对工作目录解析, 绝对路径按真实文件系统。",
             {"type": "object", "properties": {
                 "path": {"type": "string"},
                 "edits": {"type": "array", "items": {"type": "object", "properties": {
                     "old_string": {"type": "string"}, "new_string": {"type": "string"},
                     "replace_all": {"type": "boolean"}},
                     "required": ["old_string", "new_string"]}},
                 "old_string": {"type": "string"}, "new_string": {"type": "string"},
                 "replace_all": {"type": "boolean"}},
              "required": ["path"]},
             lambda a: _edit(workdir, a, ledger), ledger=ledger),
        _execute_tool(workdir, ledger),
    ]
