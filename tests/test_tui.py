"""tui(claude 式 TUI)自检: python3 tests/test_tui.py"""
import io
import json
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rich.console import Console

from core import tui
from core.sessions import load_session_data, project_dir
from core.tui import Editor, Term, ZanyTui


class FakeAgent:
    tools = ["read_file"]

    def __init__(self):
        self.usage = {"input": 0, "output": 0, "calls": 0}
        self._last_prompt_tokens = None
        self.interrupts = []

    def _system(self):
        return "sys"

    def restore(self, u):
        self.usage.update(u)

    def interrupt(self, reason=""):
        self.interrupts.append(reason)

    def add_tools(self, tools):
        return []

    def stream(self, msgs):
        self.usage["calls"] = self.usage.get("calls", 0) + 1
        self.usage["last_in"] = 1234
        yield ("thinking", "step one")
        yield ("text", "你好世界")
        yield ("tool_start", "t1", "read_file", {"path": "a.py"})
        yield ("tool_result", "t1", "read_file", '{"path": "a.py"}', "ok")
        yield ("text", "done")
        yield ("usage", 1234, 5)
        # 与真 agent 同款: 回合消息原地追加(单协议), 恢复回放才有料
        msgs.append({"role": "assistant", "content": [
            {"type": "thinking", "thinking": "step one"},
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}},
            {"type": "text", "text": "done"},
        ]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        ]})


def make_tui(tmp, agent=None):
    agent = agent or FakeAgent()
    out = io.StringIO()
    buf = io.StringIO()
    t = ZanyTui(str(tmp), agent=agent,
                term=Term(out=out, inp=io.StringIO()),
                console=Console(file=buf, force_terminal=False, width=80))
    return t, agent, buf


def run_turn_sync(t):
    t.run_turn("hi")
    while t._th.is_alive():
        t._drain_q()
        time.sleep(0.01)
    t._drain_q()
    t._finish_turn()


def settle(t, timeout=5.0):
    """追完忙链(含 turn_finished 链式 goal/steer 续跑), 直到闲下来。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        t._drain_q()
        if t.busy and t._th is not None and not t._th.is_alive():
            t._finish_turn()
            continue
        if not t.busy and (t._th is None or not t._th.is_alive()):
            return
        time.sleep(0.01)
    raise AssertionError("忙链未在时限内结束")


def main():
    tmp = Path(tempfile.mkdtemp())

    # ---- Term 键解析: posix 字节流 → 键名 ----
    tm = Term()
    for pend, want in [
        (b"\x1b[A", "UP"), (b"\x1b[B", "DOWN"), (b"\x1b[3~", "DEL"),
        (b"\x1b[200~", "PASTE_ON"), (b"\x1b[201~", "PASTE_OFF"),
        (b"\x03", "CTRL_C"), (b"\x7f", "BS"), (b"\r", "\r"), (b"\x1a", "UNDO"),
    ]:
        tm._pend = pend
        assert tm._parse_posix() == want, (pend, want)
    tm._pend = "你".encode()
    assert tm._parse_posix() == "你"
    tm._pend = "你".encode()[:2]  # 不完整 utf-8 → 等更多字节
    assert tm._parse_posix() is None and tm._pend == "你".encode()[:2]
    tm._pend = b"\x1b[Q"  # 不认识的序列整体丢弃, 不卡死
    assert tm._parse_posix() is None and tm._pend == b""

    # ---- Editor: 多行/跨行删除/undo/软换行 ----
    ed = Editor()
    ed.insert("第一行\n第二行")
    assert ed.lines == ["第一行", "第二行"] and (ed.cy, ed.cx) == (1, 3)
    ed.handle("HOME")
    ed.handle("BS")  # 行首退格 → 并到上一行
    assert ed.lines == ["第一行第二行"] and (ed.cy, ed.cx) == (0, 3)  # 光标停拼接点
    ed.handle("UNDO")
    assert ed.lines == ["第一行", "第二行"] and ed.cy == 1
    ed2 = Editor()
    ed2.insert("abc\\")  # 行尾反斜杠 = 软换行
    ed2.handle("\r")
    ed2.insert("def")
    assert ed2.lines == ["abc", "def"] and ed2.text() == "abc\ndef"
    ed2.handle("KILL_BEG")
    assert ed2.lines[1] == "" and ed2.cx == 0
    ed2.handle("UNDO")
    assert ed2.lines[1] == "def"

    # ---- Editor: 大粘贴折叠成占位符, 提交时展开(claude code 同款) ----
    ed3 = Editor()
    ed3.handle("PASTE_ON")
    for ch in "abc\n\n\n末":  # bracketed paste 逐字符(含换行), 4 行 → 折叠
        ed3.handle(ch)
    ed3.handle("PASTE_OFF")
    assert ed3.lines == ["[Pasted text #1 +3 lines]"] and ed3.pastes[1] == "abc\n\n\n末"
    assert ed3.expand("前[Pasted text #1 +3 lines]后") == "前abc\n\n\n末后"
    ed4 = Editor()  # 单行短粘贴: 原样展开不折叠
    ed4.handle("PASTE_ON")
    ed4.handle("x")
    ed4.handle("PASTE_OFF")
    assert ed4.lines == ["x"] and not ed4.pastes
    ed5 = Editor()  # 单行超长(>800)也折叠, 无行数后缀
    ed5.handle("PASTE_ON")
    for _ in range(801):  # 真实路径: 逐字符进 paste 缓冲
        ed5.handle("a")
    ed5.handle("PASTE_OFF")
    assert ed5.lines == ["[Pasted text #1]"] and len(ed5.pastes[1]) == 801
    ed6 = Editor()  # ctrl+v 剪贴板路径同一入口
    ed6.handle("PASTE_ON")
    ed6.insert_paste("1\n2\n3\n4")
    ed6.handle("PASTE_OFF")
    assert ed6.lines == ["[Pasted text #1 +3 lines]"] and ed6.pastes[1] == "1\n2\n3\n4"

    # ---- 整轮管线: stream 事件 → 消息/marks/落盘/摘要行 ----
    t, agent, buf = make_tui(tmp)
    run_turn_sync(t)
    assert t.state["messages"][0]["role"] == "user"
    assert t.state["marks"][0]["tools"] == 1 and t.state["marks"][0]["thought"] is not None
    assert t.agent.usage["calls"] == 1 and t._last_tin == 1234
    assert not t.busy
    sess = t.sess_path
    assert sess is not None and sess.exists()
    data = load_session_data(sess)
    assert data["marks"][0]["tools"] == 1
    transcript = buf.getvalue()
    assert "📖" in transcript and "read" in transcript  # 工具行即时上屏(不憋到轮末)
    assert "Thought for" in transcript

    # ---- /new 清态, 再一轮落新会话 ----
    t.new_chat()
    assert t.state["messages"] == [] and t.sess_path is None
    time.sleep(1.1)  # 会话名秒级时间戳, 隔开才能断言新文件
    run_turn_sync(t)
    assert t.sess_path != sess

    # ---- busy 回车 = steer: 打断 + 排队, 不开第二条线程 ----
    t2, agent2, _ = make_tui(tmp / "b")
    (tmp / "b").mkdir()
    t2._busy_hold = True

    class SlowAgent(FakeAgent):
        def stream(self, msgs):
            time.sleep(0.3)
            yield ("text", "slow")

    t2.agent = SlowAgent()
    (tmp / "b").mkdir(exist_ok=True)
    t2.chat_line("user", "x")
    t2.run_turn("x")
    th_first = t2._th
    for ch in "你好":  # 忙时打字(逐键, 真实键盘同款) → 编辑器
        t2._on_key(ch)
    t2._on_key("\r")    # 忙时回车 → 打断+排队
    assert t2.agent.interrupts and t2._queued_text == "你好"
    assert t2._th is th_first  # 没开新线程
    settle(t2)  # 排队文本在轮末被消费, 追到链路结束
    assert t2.state["messages"][-1]["role"] == "user" and not t2.busy

    # ---- /resume: picker 返回列表, 编号恢复回放 ----
    t3, _, buf3 = make_tui(tmp)  # 复用 tmp 桶(前两步已落会话)
    picks = t3.resume_picker()
    assert isinstance(picks, list) and len(picks) >= 1
    t3.do_resume(picks[0][0])
    replay = buf3.getvalue()
    assert "已恢复" in replay and "📖" in replay

    # ---- /resume picker 态: ↑↓ 移高亮, esc 取消, 回车恢复(legacy 弹层同交互) ----
    t3.term.vt = True
    t3._pending_pick = t3.resume_picker()
    assert t3._pick_idx == 0
    t3._on_key("DOWN")
    assert t3._pick_idx == 1
    t3._on_key("ESC")
    assert t3._pending_pick is None
    t3._pending_pick = t3.resume_picker()
    t3._on_key("UP")  # 顶部向上 = 环绕到最后一个
    assert t3._pick_idx == len(t3._pending_pick) - 1
    t3._on_key("\r")
    assert t3._pending_pick is None  # 回车消费了 picker
    assert "已恢复" in buf3.getvalue()

    # ---- ctrl+c 两段式状态机(不测真时序, 只测分支) ----
    t4, agent4, _ = make_tui(tmp / "d")
    (tmp / "d").mkdir()
    t4.ed.insert("草稿")
    t4._on_key("CTRL_C")  # 有草稿: 只清空不退出
    assert t4.ed.text() == "" and t4._exit_pending == 0.0

    # ---- 无标记粘贴启发式: Enter 后有输入在途 → 纯换行不提交, 末尾 Enter 才整段发 ----
    t9, agent9, _ = make_tui(tmp / "i")
    (tmp / "i").mkdir()
    t9.term.more_input = lambda: True  # 模拟 PyCharm/ConPTY: 粘贴字符流还在注入
    t9.ed.insert("line1")
    t9._on_key("\r")
    assert t9.ed.lines == ["line1", ""] and not t9.busy  # 粘贴流内: 换行不提交
    t9.term.more_input = lambda: False  # 粘完, 末尾 Enter 无后续
    t9._on_key("\r")
    assert t9.busy and "line1" in str(t9.state["messages"][0]["content"])
    settle(t9)

    # picker 态不启用启发式: 回车必须消费弹层, 不能被当粘贴换行吞掉
    t9b, _, _ = make_tui(tmp)
    t9b.term.more_input = lambda: True
    t9b.term.vt = True
    t9b._pending_pick = t9b.resume_picker()
    t9b._on_key("\r")
    assert t9b._pending_pick is None

    # ---- 渲染帧: 输入框对齐 + 光标定位不炸 ----
    t5, _, _ = make_tui(tmp / "e")
    (tmp / "e").mkdir()
    t5.term.vt = True
    t5._render_frame()
    plain = re.sub("\x1b\\[[0-9;]*[A-Za-z]", "", t5.term.out.getvalue())
    assert "> " in plain  # 框头 ASCII > (U+276F 字形缺, 31874fe)
    t5.busy = True
    t5._turn_t0 = time.monotonic()
    t5._out_chars = 3000
    t5._render_frame()  # spinner 行引用 _out_chars, 曾经的 _out_max 会炸

    # ---- plain 退行式: input() 驱动, 无 VT 也能跑完整轮 ----
    t6, _, buf6 = make_tui(tmp / "f")
    (tmp / "f").mkdir()
    lines = iter(["hi", "exit"])

    def fake_input(*a, **k):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    import builtins
    builtins.input = fake_input
    t6._run_plain()
    assert t6.state["messages"][0]["role"] == "user" and not t6.busy
    assert "📖" in buf6.getvalue()

    # ---- VT 主循环整跑: 脚本化键盘走 提交→忙→exit, 帧重绘/光标定位真执行 ----
    t7, _, buf7 = make_tui(tmp / "g")
    (tmp / "g").mkdir()
    t7.term.vt = True
    q7 = ["h", "i", "\r"]

    def fake_read(timeout):
        if not q7 and not t7.busy and t7._th is not None and not t7._th.is_alive():
            q7.extend("exit\r")
        return q7.pop(0) if q7 else (time.sleep(min(timeout, 0.05)) or None)

    t7.term.read_key = fake_read
    t7.run()
    vt_out = t7.term.out.getvalue()
    assert "» " in vt_out and "📖" in buf7.getvalue()  # 提交跑完(工具条)+ 工具行烙定

    # ---- 擦帧/定位配对: 防重绘整体上浮(真机截图 bug: 每敲一键框上浮一行) ----
    t8, _, _ = make_tui(tmp / "h")
    (tmp / "h").mkdir()
    t8.term.vt = True
    t8.ed.insert("abc")
    t8._render_frame()
    assert t8._park_down == 3  # 定位进了输入行: 帧底到光标隔 3 行
    o1 = t8.term.out.getvalue()
    assert "\x1b[3A\x1b[6G" in o1  # 定位序列(上移 3; G 1-based: "> "2+3字符+1=6, 新帧无左边框)
    t8.ed.insert("d")  # 再打一键 → 重绘: 擦帧必须先原路下移回帧底
    t8._render_frame()
    o2 = t8.term.out.getvalue()[len(o1):]
    assert o2.startswith("\x1b[3B\r\x1b[4A\x1b[J"), repr(o2[:40])
    # 定位后编辑器状态变了(清空), 擦帧仍用记录值, 不按当前状态反推
    t8.ed.set_text("多\n行")
    t8._on_key("UP")  # 光标到第 1 行: park=2+2-0=4; 若按清空后状态反推会是 3
    t8._render_frame()
    assert t8._park_down == 4
    o3 = t8.term.out.getvalue()
    t8.ed.clear()  # 模拟回车清空: 光标物理位置还在旧处
    t8._erase_frame()
    assert "\x1b[4B\r\x1b[5A\x1b[J" in t8.term.out.getvalue()[len(o3):]

    # ---- 会话标题: /title 手动 + 自动生成清理 + 列表优先展示 ----
    import core.sessions as _cs
    from core.sessions import save_session, list_sessions as _ls
    _orig_pd = _cs.project_dir
    _cs.project_dir = lambda wd: tmp
    try:
        sp = tmp / "t_title.json"
        save_session(sp, [{"role": "user", "content": "帮我把日志报错归类"}], title="日志报错归类")
        assert load_session_data(sp)["title"] == "日志报错归类"
        assert _ls(str(tmp))[0][2] == "帮我把日志报错归类"  # 列表显示最后一个用户问题
        save_session(sp, [{"role": "user", "content": "第二问"}])
        assert "title" not in load_session_data(sp)  # 无标题不写键, 旧标题不残留
        t2, _, _ = make_tui(tmp, agent=FakeAgent())
        t2.state["title"] = None
        t2.sess_path = None
        t2._maybe_auto_title()  # FakeAgent 无 .model → 不起真调用
        assert t2.state["title"] is None
        t2.sess_path = sp
        t2.state["title"] = "手动的"
        t2._maybe_auto_title()  # 已有标题不动
        assert t2.state["title"] == "手动的"
    finally:
        _cs.project_dir = _orig_pd

    _f = ZanyTui._clean_title
    assert _f('"Hello, World".') == "Hello, World"
    assert _f("标题: 修网关") == "修网关"
    assert _f("Title: Fix gateway\nextra") == "Fix gateway"
    assert _f("x" * 100) == "x" * 40

    print("tests/test_tui.py 全部通过")


if __name__ == "__main__":
    main()
