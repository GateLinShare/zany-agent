"""Windows 读键层 ESC 序列解析自检(2026-11-09 PyCharm 终端方向键修复)。

_read_key_win 的 ESC 分支此前只认 bracketed paste 标记, 其余 ESC 序列整体丢弃:
部分 ConPTY/终端(PyCharm 实测)把方向键以 VT 序列塞进 console buffer, 导致
上下/回车(picker 态导航)全没反应。这里不走真实 console, 直接塞 _pend 验证解析。
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tui import Term


def _key(pend: str) -> str:
    t = Term()
    t._pend = pend  # 预置缓冲: read_key 跳过 msvcrt 读, 直接走解析分支
    return t._read_key_win(None)


# VT 序列形态的方向键(ConPTY/PyCharm 塞字符流而非 \xe0 扩展键)
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

# 剪贴板助手: 非 win 恒空, win 上取 CF_UNICODETEXT(空剪贴板返回空串不炸)
Term().paste_clipboard()

print("ok: ESC 序列/PASTE_CLIP 解析全部通过")
