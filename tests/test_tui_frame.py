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

# ---- 2026-11-16 刷屏回归: ambiguous 宽字符(——“”–)必须按全宽计 ----
# rich 按半宽算, 中文终端(JetBrains/CJK 字体)画全宽 → 帧行被终端二次折行 →
# 帧高失配 → 擦帧欠擦, 每次重绘漏顶部残帧行进滚动区(忙时每秒堆 2 行)。
from core.tui import cells
assert cells("——") == 4 and cells("“”") == 4 and cells("–·") == 4, "ambiguous 应全宽"
assert cells("─│") == 2, "box drawing 恒窄(边框靠它)"
assert cells("ab12") == 4 and cells("你好") == 4
# 帧行在 w-2 内折完, 按“ambiguous 全宽”语义量也不超终端宽 → 终端不再自行折行
_txt = ("两帧的框都罩着人（f192 是整个人，f158 是垂手的手臂——手持烟在体侧的位置）。"
        "像是“抽烟的人”检测器而非烟头检测器，命中窗口落在方案C 判定的 4.5–10s 内。跑负例测误报：")
for r in t._wrap_tail(_txt, 148, 6):
    assert cells(r) <= 148, f"折行后仍超宽: {cells(r)} {r[:30]}"
print("ok: ambiguous 全宽计入, 帧行终端内不二次折行")
