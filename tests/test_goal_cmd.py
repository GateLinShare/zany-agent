"""回归: tui2 转正时 _submit 里的 /goal 分支保留了, handle_goal 方法本体没搬过来
——输 /goal 直接 AttributeError 崩进程(2026-11-13 重启后首输即炸)。
本用例锁住 handle_goal 四分支: 无参用法 / off / 设定+立即开跑 / resume+清除。"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core.tui as t


def fake_tui(calls: list):
    return types.SimpleNamespace(
        state={"goal": None, "messages": [], "marks": []}, busy=False,
        log_write=lambda s: calls.append(("log", s)),
        chat_line=lambda w, s: calls.append(("chat", w, s)),
        run_turn=lambda s: calls.append(("run", s)),
        agent=types.SimpleNamespace(
            tools={},
            add_tools=lambda tools: calls.append(("tools", [x.name for x in tools]))))


def test_goal_set_runs_immediately():
    calls: list = []
    f = fake_tui(calls)
    t.ZanyTui.handle_goal(f, "把回归测试跑绿")
    g = f.state["goal"]
    assert g["objective"] == "把回归测试跑绿"
    assert g["status"] == "active" and g["max_turns"] == t.GOAL_MAX_TURNS
    assert ("tools", ["update_goal"]) in calls  # attach 挂了 goal 工具
    assert any(c[0] == "run" for c in calls)     # 设定后立即开跑


def test_goal_view_and_off():
    calls: list = []
    f = fake_tui(calls)
    t.ZanyTui.handle_goal(f, "")                 # 无参: 用法
    assert any("用法" in c[1] for c in calls if c[0] == "log")
    t.ZanyTui.handle_goal(f, "off")              # 无 goal 时 off
    assert any("没有 goal" in c[1] for c in calls if c[0] == "log")
    t.ZanyTui.handle_goal(f, "目标X")
    t.ZanyTui.handle_goal(f, "off")              # 清除
    assert f.state["goal"] is None and "update_goal" not in f.agent.tools


def test_goal_resume_resets_budget():
    calls: list = []
    f = fake_tui(calls)
    f.state["goal"] = {"objective": "旧目标", "status": "paused",
                       "turns_used": 499, "max_turns": t.GOAL_MAX_TURNS}
    t.ZanyTui.handle_goal(f, "resume")
    g = f.state["goal"]
    assert g["status"] == "active" and g["turns_used"] == 0
    t.ZanyTui.handle_goal(f, "resume")           # active 状态不可再 resume
    assert any("没有可恢复" in c[1] for c in calls if c[0] == "log")


if __name__ == "__main__":
    test_goal_view_and_off()
    test_goal_resume_resets_budget()
    test_goal_view_and_off()
    print("3/3 ✓")
