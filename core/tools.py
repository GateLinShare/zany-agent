"""内置工具:ls / read_file / write_file / edit_file / execute。

接口设计上唯一的坑:**文件工具以 workdir 为虚拟根**(/agent.py = 真实的
workdir/agent.py),execute 却是真实 shell。这条约定写进 SYSTEM_PROMPT,
不在工具层做路径重写 —— 重写会掩盖两套路径的不一致,不如让规则显式。
"""

import json
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_OUTPUT = 200000  # 单个工具结果上限(字符), 超长截断 —— 上下文第一层防线
EXEC_TIMEOUT = 1200  # execute 默认超时(秒)


@dataclass
class Tool:
    """统一工具形态:内置工具和 MCP 工具都归一成这个, 循环层不区分来源。"""

    name: str
    description: str
    input_schema: dict
    run: Callable[[dict], str]  # 入参 dict -> 结果文本(错误也返回文本, 不抛异常)

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def _resolve(workdir: str, path: str) -> Path:
    """虚拟根路径解析: /agent.py -> workdir/agent.py; 拒绝越出 workdir。"""
    root = Path(workdir).resolve()
    p = (root / str(path).lstrip("/")).resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"路径越界: {path} (文件工具以工作目录为根)")
    return p


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n…(截断, 全长 {len(text)} 字符, 可用 offset/limit 分页)"


def _ls(workdir, args) -> str:
    d = _resolve(workdir, args.get("path", "/"))
    if not d.is_dir():
        return f"不是目录: {args.get('path', '/')}"
    lines = []
    for f in sorted(d.iterdir()):
        kind = "dir " if f.is_dir() else "file"
        size = f" {f.stat().st_size}B" if f.is_file() else ""
        lines.append(f"{kind}  {f.name}{size}")
    return "\n".join(lines) or "(空目录)"


def _read(workdir, args) -> str:
    f = _resolve(workdir, args["path"])
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    offset, limit = int(args.get("offset", 0)), int(args.get("limit", 100))
    page = lines[offset : offset + limit]
    out = "\n".join(f"{i + offset + 1:6d}\t{l}" for i, l in enumerate(page))
    if offset + limit < len(lines):
        out += f"\n…(共 {len(lines)} 行, 还有 {len(lines) - offset - limit} 行, 用 offset 继续读)"
    return out or "(空文件)"


def _write(workdir, args) -> str:
    f = _resolve(workdir, args["path"])
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(args["content"], encoding="utf-8")
    return f"已写入 {args['path']} ({len(args['content'])} 字符)"


_FUZZY_TABLE = str.maketrans({  # pi edit 同款归一化: 智能引号/破折号/特殊空格 → ASCII
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", "　": " ",
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


def _edit(workdir, args) -> str:
    f = _resolve(workdir, args["path"])
    text = f.read_text(encoding="utf-8")
    # 两种入参: 单处 old_string/new_string(兼容旧格式), 或 pi 式 edits 数组一次改多处
    edits = args.get("edits") or [{"old_string": args["old_string"], "new_string": args.get("new_string", "")}]
    if not edits:
        return "错误: edits 为空"

    # 所有 edit 都对原始全文定位(pi 的关键约定), 坐标存 spans, 最后统一从后往前替换
    spans = []  # (start, len, new_text)
    fuzzy = False
    for i, e in enumerate(edits):
        old, new = e["old_string"], e["new_string"]
        if not old:
            return f"错误: edits[{i}].old_string 为空"
        n = text.count(old)
        if e.get("replace_all", args.get("replace_all", False)):
            if n == 0:
                return f"错误: edits[{i}] 在 {args['path']} 中不存在"
            pos = text.find(old)
            while pos != -1:
                spans.append((pos, len(old), new))
                pos = text.find(old, pos + len(old))
            continue
        if n == 1:
            spans.append((text.index(old), len(old), new))
        elif n > 1:
            return f"错误: edits[{i}] 出现 {n} 次, 不唯一; 拆成 edits 数组各处一条, 或加更多上下文"
        else:
            fuzzy = True  # 精确失配 → 全体切换到归一化空间重试(混合坐标系没有意义)
            break

    ntext = ""
    if fuzzy:
        ntext = _fuzzy(text)
        spans = []
        for i, e in enumerate(edits):
            if e.get("replace_all"):
                return f"错误: edits[{i}] 模糊匹配场景不支持 replace_all, 先 read_file 看清再精确编辑"
            old = _fuzzy(e["old_string"])
            n = ntext.count(old)
            if n != 1:
                return f"错误: edits[{i}] 在 {args['path']} 中精确/模糊均未唯一匹配(模糊 {n} 处)"
            spans.append((ntext.index(old), len(old), _fuzzy(e["new_string"])))

    spans.sort()
    for a, b in zip(spans, spans[1:]):
        if a[0] + a[1] > b[0]:
            return "错误: 两处 edit 区域重叠, 合并成一条或改成不相交区域"

    if fuzzy:
        new = _overlay(text, ntext, spans)
    else:
        new = text
        for start, ln, new_s in reversed(spans):
            new = new[:start] + new_s + new[start + ln:]
    if new == text:
        return f"错误: 替换后内容无变化 ({args['path']})"
    f.write_text(new, encoding="utf-8")
    return f"已替换 {len(spans)} 处 ({args['path']})" + ("(模糊匹配)" if fuzzy else "")


def _execute(workdir, args) -> str:
    try:
        r = subprocess.run(
            args["command"], shell=True, capture_output=True, text=True,
            cwd=workdir, timeout=int(args.get("timeout", EXEC_TIMEOUT)),
        )
        out = (r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr else "")
        code = f"\n[exit {r.returncode}]" if r.returncode else ""
        return _truncate(out.strip() or "(无输出)") + code
    except subprocess.TimeoutExpired:
        return f"错误: 命令超时(>{args.get('timeout', EXEC_TIMEOUT)}s)"


def builtin_tools(workdir: str) -> list[Tool]:
    """五个内置工具; run 闭包绑死 workdir, 无全局状态。"""
    return [
        Tool("ls", "列目录。path 以工作目录为根(如 /agent.py 是工作目录下的 agent.py)。",
             {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
             lambda a: _ls(workdir, a)),
        Tool("read_file", "读文件, 默认前 100 行; 大文件用 offset/limit 分页。",
             {"type": "object", "properties": {
                 "path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
              "required": ["path"]},
             lambda a: _read(workdir, a)),
        Tool("write_file", "整文件覆盖写。content 必填。",
             {"type": "object", "properties": {
                 "path": {"type": "string"}, "content": {"type": "string"}},
              "required": ["path", "content"]},
             lambda a: _write(workdir, a)),
        Tool("edit_file",
             "编辑文件。单处: old_string/new_string; 多处一次改: edits=[{old_string,new_string}], "
             "可加 replace_all 替换全部同名出现。old_string 必须唯一, 否则报错; "
             "精确匹配失败会做归一化重试(智能引号/破折号/行尾空白), 命中则回写时保留未动行。",
             {"type": "object", "properties": {
                 "path": {"type": "string"},
                 "edits": {"type": "array", "items": {"type": "object", "properties": {
                     "old_string": {"type": "string"}, "new_string": {"type": "string"},
                     "replace_all": {"type": "boolean"}},
                     "required": ["old_string", "new_string"]}},
                 "old_string": {"type": "string"}, "new_string": {"type": "string"},
                 "replace_all": {"type": "boolean"}},
              "required": ["path"]},
             lambda a: _edit(workdir, a)),
        Tool("execute", "执行 shell 命令(真实 shell, 用真实路径), 默认超时 120 秒。",
             {"type": "object", "properties": {
                 "command": {"type": "string"}, "timeout": {"type": "integer"}},
              "required": ["command"]},
             lambda a: _execute(workdir, a)),
    ]
