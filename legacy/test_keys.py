"""键位表自检: python3 tests/test_keys.py"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.loop import AbortError
from tui import DEFAULT_KEYBINDINGS, EXIT_WINDOW, load_keybindings


def write(tmp, obj):
    p = Path(tmp) / "keybindings.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def main():
    # 1. 无配置文件 → 默认表
    table, issues = load_keybindings(Path(tempfile.mkdtemp()) / "nope.json")
    assert table == DEFAULT_KEYBINDINGS and issues == [], (table, issues)

    tmp = tempfile.mkdtemp()

    # 2. Claude 形状数组: 覆盖 + unbind(null) + 未知 action 记 issue
    p = write(tmp, [{"context": "Global",
                     "bindings": {"ctrl+c": "app:interrupt", "ctrl+d": None, "ctrl+e": "app:exit"}}])
    table, issues = load_keybindings(p)
    assert table == {"ctrl+c": "app.interrupt", "ctrl+e": "app.exit"}, table
    assert "ctrl+d" not in table
    assert len(issues) == 0, issues

    # 3. 直接对象形状 + 未知 action
    p = write(tmp, {"ctrl+q": "app:exit", "ctrl+w": "app:boom"})
    table, issues = load_keybindings(p)
    assert table["ctrl+q"] == "app.exit" and "ctrl+c" in table, table
    assert len(issues) == 1 and "app:boom" in issues[0], issues

    # 4. 坏 JSON / 非数组非对象 → 默认表 + issue
    p = Path(tmp) / "bad.json"
    p.write_text("{oops", encoding="utf-8")
    table, issues = load_keybindings(p)
    assert table == DEFAULT_KEYBINDINGS and len(issues) == 1, issues
    p.write_text(json.dumps([1, 2]), encoding="utf-8")
    table, issues = load_keybindings(p)
    assert table == DEFAULT_KEYBINDINGS and len(issues) == 2, issues

    # 5. 冒号写法(Claude 的 app:interrupt)自动转 textual 点号语法
    p = write(tmp, {"ctrl+c": "app:interrupt"})
    table, issues = load_keybindings(p)
    assert table == {**DEFAULT_KEYBINDINGS, "ctrl+c": "app.interrupt"}, (table, issues)
    assert issues == [], issues

    # 6. AbortError: 取消 ≠ 真异常, 文案对齐打断收尾
    assert issubclass(AbortError, Exception)
    try:
        raise AbortError("用户取消")
    except AbortError:
        pass

    # 7. 真实路由(textual): ctrl+c/ctrl+d 要能穿透输入框(TextArea 内置
    # ctrl+c=copy 会吃键, 靠 priority=True 顶住), 路由到 App 的 action
    import asyncio

    import tui

    async def route_check():
        calls = []
        tui.ChatApp.action_interrupt = lambda self: (calls.append("interrupt"), setattr(tui.ChatApp, "_exit_pending", 1.0))
        tui.ChatApp.action_exit = lambda self: calls.append("exit")
        app = tui.ChatApp()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+d")
            await pilot.pause()
        return calls

    calls = asyncio.run(route_check())
    assert calls == ["interrupt", "exit"], calls

    print(f"ok: 键位表 + 真实路由 + AbortError ({EXIT_WINDOW}s 双击窗)")


if __name__ == "__main__":
    main()
