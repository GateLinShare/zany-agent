"""ChatPrompt 连击回车 / 高度去抖自检: python3 tests/test_prompt.py"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tui import ChatPrompt

from textual.app import App

submitted: list[str] = []


class T(App):
    def compose(self):
        yield ChatPrompt(id="p")

    def on_mount(self):
        self.query_one(ChatPrompt).focus()

    def on_chat_prompt_submitted(self, event: ChatPrompt.Submitted) -> None:
        submitted.append(event.text)


def arm(p: ChatPrompt, run: int, gap: float) -> None:
    """伪造连击状态: 刚打完 run 个字符, 距今 gap 秒。"""
    p._run = run
    p._last_char_ts = time.monotonic() - gap


async def main():
    app = T()
    async with app.run_test(size=(80, 24)) as pilot:
        p = app.query_one(ChatPrompt)

        # 1) 连击后紧贴字符流的 enter(≤0.03s, 真粘贴回放) → 换行不提交
        arm(p, 5, 0.01)
        await pilot.press("enter")
        assert "\n" in p.text and not submitted, f"粘贴回放 enter 应插入换行: {p.text!r}"

        # 2) 连击后 0.05s 的 enter(人手速, >字符间隔) → 提交, 不再误判换行
        p.clear()
        p._enter_ts = 0.0
        submitted.clear()
        await pilot.pause()
        p.insert("你好世界")
        arm(p, 5, 0.05)
        await pilot.press("enter")
        await pilot.pause()
        assert submitted == ["你好世界"], f"应提交而非换行: submitted={submitted} box={p.text!r}"
        assert p.text == ""

        # 3) 首 enter 被抑制后, 0.12s 内的连续 enter 仍是换行(粘贴空行)
        arm(p, 5, 0.01)
        await pilot.press("enter")   # 抑制 → 换行
        await pilot.press("enter")   # 窗内连续 enter → 仍换行
        assert p.text.count("\n") == 2, f"连续粘贴换行: {p.text!r}"

        # 4) 高度去抖: 换行后高度不立刻变, 0.15s 后才拉长
        p.clear()
        p._enter_ts = 0.0
        await pilot.pause()
        h0 = p.styles.height.value
        p.insert("\n第二行")
        await pilot.pause()
        assert p.styles.height.value == h0, f"去抖期间高度不应变: {p.styles.height} vs {h0}"
        await asyncio.sleep(0.25)
        await pilot.pause()
        assert p.styles.height.value == 4, f"去抖后应拉长到 4: {p.styles.height}"

        # 5) 提交后 clear, 高度最终缩回 3
        await pilot.press("enter")
        await asyncio.sleep(0.25)
        await pilot.pause()
        assert p.styles.height.value == 3 and p.text == ""

    print("PASS 连击回车两段判定 + 高度去抖")


if __name__ == "__main__":
    asyncio.run(main())
