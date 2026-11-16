"""防闪回归检查: 活动区重绘必须单次 write + 内容光标都没变时跳过。
跑法: uv run tests/test_tui_frame.py"""

import io
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.tui import ZanyTui as Tui  # noqa: E402


class _FakeTerm:
    vt = True

    def __init__(self):
        self.buf = io.StringIO()
        self.writes = 0

    def write(self, s):
        self.writes += 1
        self.buf.write(s)


def make_tui():
    stub = types.SimpleNamespace(tools=[])
    t = Tui.__new__(Tui)  # 绕过 __init__ 的 build_agent
    for k, v in {
        "workdir": ".", "agent": stub, "term": _FakeTerm(), "busy": False,
        "ed": type("E", (), {"lines": ["hello"], "cy": 0, "cx": 5})(),
        "_frame_rows": 0, "_park_down": 0, "_last_rows": None, "_last_cur": None,
        "_seg": {"think": "", "text": ""}, "_seg_lock": __import__("threading").Lock(),
        "_inflight": {}, "_turn_t0": None, "_verb": 0, "_out_chars": 0,
        "_last_tin": 0, "_exit_pending": 0.0, "_pending_pick": None,
    }.items():
        setattr(t, k, v)
    return t


t = make_tui()
t._render_frame()
assert t.term.writes == 1, f"首帧应 1 次 write, 实际 {t.term.writes}"
n1 = len(t.term.buf.getvalue())

t._render_frame()  # 状态没变: 不许写
assert t.term.writes == 1, "内容与光标都没变却重绘了"

t.ed.cx = 3  # 只动光标: 必须重定位(帧内容同)
t._render_frame()
assert t.term.writes == 2, "光标移动被跳过, 终端光标会停在旧位"

t.ed.lines = ["hello", "world"]  # 内容变了: 重绘
t._render_frame()
assert t.term.writes == 3

for w in t.term.buf.getvalue().split("\x1b[?2026h")[1:]:
    assert w.count("\x1b[?2026l") == 1 and w.count("\x1b[?25l") == 1 and w.count("\x1b[?25h") == 1
print("ok: 单次 write / 未变跳过 / 移位重定位 / 同步更新成对")
