"""code_search 插件: rg(ripgrep) 代码检索, claude code Grep 工具同款语义。

面向"代码检索"的工具化:正则搜代码, 文件:行号 定位, 默认尊重 .gitignore
(不扫 node_modules/.venv 这类)。子进程列表传参, pattern 不经 shell 无注入面。
启用: .env 配 ZANY_PLUGINS=codesearch; 环境没有 rg 二进制时不注册(打印提示)。

output_mode 三种:
    files_with_matches  只出文件路径(找"哪些文件涉及")
    count               每文件匹配行数(评估改动面)
    content             默认, file:line: 正文(上下文行用 context 参数)
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from core.tools import Tool

TIMEOUT = 30.0     # rg 秒级出结果, 30s 足够大仓库
DEFAULT_HEAD = 100 # content/files 模式默认行数上限
HEAD_MAX = 1000
CTX_MAX = 5        # 上下文行数钳制


def _resolve(workdir: str, path: str):
    p = Path(str(path or ".")).expanduser()
    return p if p.is_absolute() else Path(workdir) / p


def _run_rg(argv: list[str]) -> tuple[str, str, bool, int]:
    """跑 rg, (stdout, stderr, 超时与否, 退出码)。rc 1=无匹配 是正常结果不是错误, 2=真错误。"""
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           errors="replace", timeout=TIMEOUT)
        return r.stdout, r.stderr, False, r.returncode
    except subprocess.TimeoutExpired:
        return "", "", True, -1


def make_code_search_tool(workdir: str) -> Tool:
    def run(args: dict) -> str:
        pattern = str(args.get("pattern") or "")
        if not pattern.strip():
            return "code_search 错误: pattern 不能为空"
        mode = str(args.get("output_mode") or "content")
        if mode not in ("content", "files_with_matches", "count"):
            return f"code_search 错误: output_mode={mode!r} 不识别, 可选 content/files_with_matches/count"

        path = _resolve(workdir, str(args.get("path") or "."))
        if not path.exists():
            return f"code_search 错误: 路径不存在: {path}"

        head = min(max(int(args.get("head_limit") or DEFAULT_HEAD), 1), HEAD_MAX)
        argv = ["rg", "--no-heading"]
        if mode == "files_with_matches":
            argv.append("-l")
        elif mode == "count":
            argv.append("-c")
        else:
            argv += ["-n", "--color", "never"]
            ctx = min(max(int(args.get("context") or 0), 0), CTX_MAX)
            if ctx:
                argv += ["-C", str(ctx)]
        g = str(args.get("glob") or "").strip()
        if g:
            argv += ["-g", g]
        argv += ["--", pattern, str(path)]

        out, err, timed_out, rc = _run_rg(argv)
        if timed_out:
            return f"code_search 错误: 超时({TIMEOUT:.0f}s), 收窄 path/glob 或换更精确的 pattern 再试"
        if rc == 2:  # 坏正则/路径无权限等, rg 把原因写在 stderr
            return f"code_search 错误: {err.strip() or f'rg 退出码 2(无 stderr)'}"
        if not out.strip():
            return "无匹配。rg 默认区分大小写、跳过 .gitignore 文件与隐藏文件; " \
                   "想含隐藏目录加进 path 指到具体位置, 不区分大小写用 (?i) 前缀。"

        lines = out.rstrip("\n").split("\n")
        if len(lines) > head:
            return "\n".join(lines[:head]) + \
                f"\n…(共 {len(lines)} 行, 已截断到 {head} 行 —— 收窄 pattern/glob 或减小 path 再查)"
        return "\n".join(lines)

    return Tool(
        name="code_search",
        description="在代码库里正则检索(ripgrep): 文件:行号:内容 定位, 尊重 .gitignore。"
                    "找定义/引用/用法/TODO 都用它, 比反复 read_file 猜路径省得多。"
                    "rg 正则语法(\\d, \\b, |), 想搜纯文本把 . 等转义。"
                    "配合 read_file(offset/limit) 读命中处上下文。",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "rg 正则, 如 def\\s+run_agent|class Tool"},
                "path": {"type": "string", "description": "搜索目录或单文件, 默认工作目录; 相对路径相对工作目录"},
                "glob": {"type": "string", "description": "文件名过滤, 如 '*.py' 或 '*.{ts,tsx}'; 匹配相对路径, 跨目录用 '**/plugins/*.py'"},
                "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"],
                                "description": "content=文件:行号:内容(默认), files_with_matches=仅路径, count=每文件匹配行数"},
                "head_limit": {"type": "integer", "description": f"输出行数上限(默认 {DEFAULT_HEAD}, 最大 {HEAD_MAX})"},
                "context": {"type": "integer", "description": f"content 模式上下文行数(0-{CTX_MAX})"},
            },
            "required": ["pattern"],
        },
        run=run,
    )


def register(workdir: str) -> list:
    if not shutil.which("rg"):
        print("(codesearch 插件: 未找到 rg(ripgrep), 未注册 code_search)")
        return []
    return [make_code_search_tool(workdir)]


# ---------------------------------------------------------------------------
# 自检: python3 core/plugins/codesearch.py  (临时目录造假树, 不依赖真实仓库)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "a.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
        (root / "b.js").write_text("function hello() {}\n// hello again\n", encoding="utf-8")
        (root / ".gitignore").write_text("ignored*\n", encoding="utf-8")
        (root / "ignored.c").write_text("hello hidden\n", encoding="utf-8")
        sub = root / "sub"; sub.mkdir()
        (sub / "c.py").write_text("x = 'hello world'\n", encoding="utf-8")
        (root / "d.py").write_text("pass\ndef hello(): pass\npass\n", encoding="utf-8")

        tool = register(td)[0]
        assert tool.name == "code_search"

        # 1) content 默认: file:line:正文, 且尊重 .gitignore(ignored.c 不出现)
        out = tool.run({"pattern": "hello"})
        assert "a.py:1:def hello" in out and "b.js:1" in out and "ignored.c" not in out, out

        # 2) glob 过滤 + context 上下文行(匹配行 : 连接, 上下文行 - 连接, 组间 --)
        out = tool.run({"pattern": "^def hello", "output_mode": "content", "context": 1, "path": "a.py"})
        assert out == "1:def hello():\n2-    return 'world'", out  # 单文件时 rg 只出行号
        out = tool.run({"pattern": "def hello", "glob": "d.py", "output_mode": "content", "context": 1})
        assert "d.py-1-pass" in out and "d.py:2:def hello" in out and "d.py-3-pass" in out, out

        # 3) files_with_matches / count
        out = tool.run({"pattern": "hello", "glob": "*.py", "output_mode": "files_with_matches"})
        assert len(out.splitlines()) == 3 and all(f in out for f in ("a.py", "c.py", "d.py")), out
        out = tool.run({"pattern": "hello", "output_mode": "count"})
        assert "b.js:2" in out, out

        # 4) head_limit 截断带提示
        out = tool.run({"pattern": "hello", "head_limit": 2})
        assert "已截断" in out and len(out.splitlines()) == 3 and "共 5 行" in out, out

        # 5) 无匹配 → 引导性提示; 坏正则/坏路径 → 错误即文本
        assert "无匹配" in tool.run({"pattern": "zzz_not_exist_zzz"})
        assert "错误" in tool.run({"pattern": "(unclosed"})
        assert "不存在" in tool.run({"pattern": "hello", "path": "/no/such/dir"})

        # 6) 空 pattern / 坏 mode
        assert "pattern" in tool.run({"pattern": ""})
        assert "不识别" in tool.run({"pattern": "x", "output_mode": "xml"})

        # 7) 无 rg 二进制 → 不注册
        import shutil as _sh
        real = _sh.which
        _sh.which = lambda n: None if n == "rg" else real(n)
        try:
            assert register(td) == []
        finally:
            _sh.which = real

        print("codesearch 自检通过")
