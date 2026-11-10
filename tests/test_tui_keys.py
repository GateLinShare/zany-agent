# tui Windows 读键归一化: msvcrt 原始字符必须翻译成与 POSIX _parse_posix 同一套符号名。
# 注入假 msvcrt 模块喂键序, 不碰真终端。
import sys
import types

fake = types.ModuleType("msvcrt")
_state = {"buf": []}
fake.kbhit = lambda: bool(_state["buf"])
fake.getwch = lambda: _state["buf"].pop(0)
sys.modules["msvcrt"] = fake

from core import tui

t = tui.Term()
t._is_win = True
t._pend = ""

# _win_key_ready 走 PeekConsoleInput(真控制台), 管道环境下永远返回"无键",
# 喂键循环会永远等下去; 拍平成"假缓冲有键就绪"(t._pend 是 agent 自己的缓冲, 不算控制台队列)
tui._win_key_ready = lambda: bool(_state["buf"])


def keys(seq: list[str]) -> list[str]:
    _state["buf"] = list(seq)
    out = []
    while _state["buf"] or t._pend:
        k = t._read_key_win(0.1)
        if k is not None:
            out.append(k)
    return out


# 报障的两个键: backspace / ctrl+c 必须出符号名
assert keys(["\x08"]) == ["BS"], keys(["\x08"])
assert keys(["\x7f"]) == ["BS"]
assert keys(["\x03"]) == ["CTRL_C"]
assert keys(["\x04"]) == ["CTRL_D"]
# 扩展键(方向/Del)两段式
assert keys(["\xe0", "H"]) == ["UP"]
assert keys(["\x00", "S"]) == ["DEL"]
# ctrl 系编辑键与 POSIX 同表(ctrl+z 撤销曾是报障点: win 路径漏映射被丢弃)
assert keys(["\x1a"]) == ["UNDO"]
assert keys(["\x01"]) == ["HOME"]
assert keys(["\x05"]) == ["END"]
assert keys(["\x0b"]) == ["KILL_END"]
assert keys(["\x15"]) == ["KILL_BEG"]
assert keys(["\x17"]) == ["KILL_WORD"]
# 整段粘贴一次性到达: 标记解析 + 内部 \r 不丢
assert keys(["\x1b[200~", "h", "i", "\r", "y", "o", "\x1b[201~"]) == [
    "PASTE_ON", "h", "i", "\r", "y", "o", "PASTE_OFF",
]
# 普通字符原样透传
assert keys(["a", "中"]) == ["a", "中"]

# ---- 无标记粘贴启发式: more_input(Enter 后窗口内有无输入在途) ----
t._pend = ""
_state["buf"] = []
assert t.more_input(0.01) is False  # 窗口内静默 = 人手回车, 可提交
t._pend = "x"
assert t.more_input() is True       # 缓冲有存货 = 粘贴流
t._pend = ""
_state["buf"] = ["a"]
assert t.more_input(0.05) is True   # 窗口内到键 = 粘贴流
print("tui paste heuristic (win): OK")
