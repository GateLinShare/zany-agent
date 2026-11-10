"""tui 漂移回归(真机截图 bug: 每敲一键输入框上浮一行): uv run --with pyte tests/test_tui_pty.py

真 pty + pyte 虚拟终端逐键喂字, 断言输入框顶边行号在打字全程不动。
"""
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time

import pyte

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DRIVER = r'''
import sys
sys.path.insert(0, "/data01/tsn/code/tsn_files/zany-agent")
from core import tui


class FakeAgent:
    tools = ["read_file"]
    def __init__(self):
        self.usage = {"input": 0, "output": 0, "calls": 0}
        self._last_prompt_tokens = None
    def _system(self): return "sys"
    def restore(self, u): self.usage.update(u)
    def interrupt(self, reason=""): pass
    def add_tools(self, tools): return []
    def stream(self, msgs):
        yield ("text", "drift check")
        yield ("usage", 100, 5)
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "done"}]})


tui.build_agent = lambda *a, **k: FakeAgent()
tui.main()
'''


def main():
    cols, rows = 100, 30
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    proc = subprocess.Popen([sys.executable, "-c", DRIVER], stdin=slave, stdout=slave,
                            stderr=slave, cwd=REPO, close_fds=True)
    os.close(slave)
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)

    def pump(dur=0.3):
        end = time.time() + dur
        while time.time() < end:
            r, _, _ = select.select([master], [], [], 0.05)
            if not r:
                continue
            try:
                data = os.read(master, 65536)
            except OSError:
                return False
            if not data:
                return False
            stream.feed(data)
        return True

    def top_row():
        for i, line in enumerate(screen.display):
            if line.strip() and set(line.strip()) == {"─"}:
                return i  # 输入框顶边线(转正后无侧边框, 纯 ─ 整行)
        return None

    def input_text():
        for line in screen.display:
            if line.startswith("> "):
                return line[2:].strip()
        return ""

    try:
        assert pump(1.2), "子进程提前退出"
        base = top_row()
        assert base is not None, "没画出输入框"

        # 逐键打字: 框顶行必须钉死(核心断言, 修复前每键上浮 1 行)
        for ch in "nihao":
            os.write(master, ch.encode())
            assert pump(0.3), f"输入 {ch} 后进程退出"
            row = top_row()
            assert row == base, f"输入 {ch} 后框顶从 {base} 漂到 {row}(上浮 bug 复现)"
        assert input_text() == "nihao", f"输入回显不对: {input_text()!r}"

        # 软换行变 2 行 → 框向下长高(顶边锤死); 退格并回 → 顶边仍在基线
        os.write(master, b"\\")
        assert pump(0.3)
        os.write(master, b"\r")
        assert pump(0.3)
        os.write(master, "好".encode())
        assert pump(0.3)
        assert top_row() == base, f"两行编辑器框顶应钉在 {base}, 实际 {top_row()}"
        os.write(master, b"\x7f\x7f")  # 退格删字 + 并回上一行
        assert pump(0.3)
        assert top_row() == base and input_text() == "nihao"

        # 跑一轮对话再打字: 轮次烙定后框也不能漂
        os.write(master, b"\x15hi\r")  # ctrl+u 清残留, 提交
        assert pump(1.0)
        deadline = time.time() + 10
        while time.time() < deadline:
            pump(0.3)
            if "tool call" in "".join(screen.display) or "done" in "".join(screen.display):
                break
        os.write(master, b"z")
        assert pump(0.3)
        after_turn = top_row()
        os.write(master, b"z")
        assert pump(0.3)
        assert top_row() == after_turn, "轮次后打字仍漂移"

        os.write(master, b"\x15exit\r")
        time.sleep(0.5)
        pump(0.5)
        rc = proc.wait(timeout=10)
        assert rc == 0, f"exit 退出码 {rc}"
        print("tests/test_tui_pty.py 全部通过(框顶行全程钉死)")
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)


if __name__ == "__main__":
    main()
