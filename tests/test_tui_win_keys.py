"""Windows 读键层自检: ESC 序列解析 + 流通道(2026-11-09 PyCharm 终端冻结修复)。

历史: PyCharm New Terminal 下 console 输入缓冲收不到键(isatty 真 console 形态
但 PeekConsoleInputW 永远空), msvcrt 单通道押注 → /resume 后整个 TUI 冻结,
Ctrl+C 也死。修法: _win_key_ready 三态化, Peek 失败标 _in_broken, read_key
自动切 stdin 流通道(pump 线程 + _stream_key 解析, 键名与 msvcrt 路径同一套)。
"""

import queue
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tui import Term


def _key(pend: str) -> str:
    t = Term()
    t._pend = pend  # 预置缓冲: read_key 跳过 msvcrt 读, 直接走解析分支
    return t._read_key_win(None)


def _stream_key(term: Term, chunk: bytes) -> str | None:
    term._pend = term._pend if isinstance(term._pend, bytes) else b""
    term._pend += chunk
    return term._stream_key()


# ---- msvcrt 路径: VT 序列形态的方向键(ConPTY/PyCharm 塞字符流而非 \xe0 扩展键) ----
for seq, want in [("\x1b[A", "UP"), ("\x1b[B", "DOWN"), ("\x1b[C", "RIGHT"),
                  ("\x1b[D", "LEFT"), ("\x1b[H", "HOME"), ("\x1b[F", "END"),
                  ("\x1b[3~", "DEL"), ("\x1bOA", "UP"), ("\x1bOH", "HOME")]:
    got = _key(seq)
    assert got == want, f"{seq!r} -> {got!r}, want {want!r}"

# bracketed paste 标记仍然优先, 余量留在缓冲
t = Term()
t._pend = "\x1b[200~hi\x1b[201~x"
assert t._read_key_win(None) == "PASTE_ON"
assert t._pend == "hi\x1b[201~x", f"残留不对: {t._pend!r}"

# 不认识的序列整体丢弃(不漏尾巴成普通字符)
t = Term()
t._pend = "\x1b[1;5A"
assert t._read_key_win(None) == "" and t._pend == ""

# conhost ctrl+v 不再是字面 0x16
assert _key("\x16") == "PASTE_CLIP"

# ---- 流通道: console 通道坏时(_in_broken)从 stdin 字节流解析, 键名同一套 ----
t = Term()
t._stream_q = queue.Queue()
for chunk, want in [(b"\x1b[A", "UP"), (b"\x1b[B", "DOWN"), (b"\x03", "CTRL_C"),
                    (b"\r", "\r"), (b"a", "a"), (b"\xe4\xbd\xa0", "你"),
                    (b"\x16", "PASTE_CLIP"), (b"\x1b", "ESC")]:
    t._pend = b""
    t._stream_q.put(chunk)
    got = t._read_key_stream(0.05)
    assert got == want, f"stream {chunk!r} -> {got!r}, want {want!r}"

# 粘贴整块(多行字符流): 一次解析一个键, \r 保留给编辑器 pasting 启发式
t = Term()
t._stream_q = queue.Queue()
t._stream_q.put(b"hello\rworld")
got = [t._read_key_stream(0.05) for _ in range(11)]
assert got == list("hello") + ["\r"] + list("world"), got

# 队列空且无残键: 返回 None 不冻结
t = Term()
t._stream_q = queue.Queue()
t._pend = b""
assert t._read_key_stream(0.05) is None

# 剪贴板助手: 非 win 恒空, win 上取 CF_UNICODETEXT(空剪贴板返回空串不炸)
Term().paste_clipboard()

# read_key 分流: console 通道标 broken 后自动走流通道且不冻结(2026-11-09 /resume 冻结回归)
# (真实触发形态: PyCharm New Terminal 等 isatty 真 console 但 PeekConsoleInputW 永远拿不到键)
t = Term()
t._in_broken = True
t._stream_q = queue.Queue()
t._stream_q.put(b"\x1b[A")
assert t.read_key(0.1) == "UP", "broken 后应从 stdin 流吃 VT 序列"
t._stream_q.queue.clear()
assert t.read_key(0.1) is None, "broken 且无键: 超时返回 None, 不冻结"

print("ok: msvcrt/流通道双路径解析全部通过")
