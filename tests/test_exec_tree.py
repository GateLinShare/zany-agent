"""execute 工具超时/打断杀整树: 孤儿不得残留攥住输出管道挂死 communicate()。

背景实测: `timeout 60 ffmpeg ...` 里 GNU timeout 自立进程组(PGID≠sh 的),
killpg 只杀得到 sh, timeout+ffmpeg 成孤儿继续攥 stdout/stderr 写端, 工具收尾
的 communicate() 等管道 EOF 永久挂死 —— 整个 agent 循环冻死在"等工具返回"。
修复 = stdin=DEVNULL(ffmpeg 读 stdin 不再挂) + 按 SID 扫 /proc 杀整树。

运行: uv run tests/test_exec_tree.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tools import _execute_tool, _Ledger


def _tool():
    return _execute_tool("/tmp", _Ledger())


def _cmdline_hits(*needles: str) -> list[int]:
    """cmdline 含任一子串的存活 pid。"""
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            if any(n in cmd for n in needles):
                out.append(int(entry))
        except OSError:
            pass
    return out


def test_stdin_devnull():
    """读交互 stdin 的命令立即拿 EOF 干活, 不再永久挂死(ffmpeg 同款死法)。"""
    out = _tool().run({"command": "python3 -c \"import sys; print('eof-len', len(sys.stdin.read()))\"",
                       "timeout": 5})
    assert "eof-len 0" in out, out


def test_timeout_kills_whole_tree():
    """超时后 timeout/sleep 整树消失 —— 老代码 killpg 会留 timeout+sleep 孤儿。"""
    out = _tool().run({"command": "timeout 300 sleep 3017 & timeout 300 sleep 3029; wait",
                       "timeout": 2})
    assert "命令超时" in out, out
    leftovers = _cmdline_hits("3017", "3029")
    assert not leftovers, f"孤儿残留: {leftovers}"


def test_heredoc_still_works():
    """stdin=DEVNULL 不破坏 heredoc: heredoc 由 sh 接到命令的 stdin, 不经 sh 的。"""
    out = _tool().run({"command": "python3 - <<'EOF'\nprint('heredoc-ok')\nEOF", "timeout": 10})
    assert "heredoc-ok" in out, out


def test_pipeline_stdin_still_works():
    """管道喂 stdin 不受 DEVNULL 影响: 管道由 sh 建立。"""
    out = _tool().run({"command": "echo piped | cat", "timeout": 10})
    assert "piped" in out, out


if __name__ == "__main__":
    for fn in (test_stdin_devnull, test_timeout_kills_whole_tree,
               test_heredoc_still_works, test_pipeline_stdin_still_works):
        fn()
        print(f"ok {fn.__name__}")
    print("all pass")
