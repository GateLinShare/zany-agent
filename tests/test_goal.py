"""goal 模式自检: python3 tests/test_goal.py"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.goal import GOAL_MAX_TURNS, continuation_prompt, detach_goal, make_goal_tool
from core.sessions import load_session_data, save_session


class FakeAgent:  # attach/detach 只碰 .add_tools / .tools
    def __init__(self):
        self.tools = {}

    def add_tools(self, tools):
        for t in tools:
            self.tools[t.name] = t


def main():
    state = {
        "objective": "把 eval 通过率提到 90%",
        "status": "active",
        "turns_used": 3,
        "max_turns": GOAL_MAX_TURNS,
    }

    # 1. continuation prompt: objective 原样进投影(数据不是指令), 带轮次和审计规则
    p = continuation_prompt(state)
    assert "把 eval 通过率提到 90%" in p and 'status="active"' in p and f"3/{GOAL_MAX_TURNS}" in p
    assert "no-progress" in p and "blocked" in p and "证据" in p

    # 2. update_goal 工具改的是同一个 dict(闭包共享, 零同步)
    tool = make_goal_tool(state)
    assert tool.name == "update_goal"
    out = tool.run({"status": "complete"})
    assert state["status"] == "complete" and "complete" in out

    # 3. 非法 status 走错误即文本, 不改状态
    state["status"] = "active"
    out = tool.run({"status": "done"})
    assert out.startswith("错误") and state["status"] == "active"

    # 4. attach/detach: 工具挂上/摘掉
    a = FakeAgent()
    a.add_tools([tool])
    assert "update_goal" in a.tools
    detach_goal(a)
    assert "update_goal" not in a.tools

    # 5. 会话落盘/恢复往返(goal 与 marks/usage 同款顶层键)
    tmp = Path(tempfile.mkdtemp()) / "s.json"
    save_session(tmp, [{"role": "user", "content": "hi"}], goal=state)
    data = load_session_data(tmp)
    assert data["goal"]["objective"] == "把 eval 通过率提到 90%"
    assert json.loads(tmp.read_text())["goal"]["turns_used"] == 3

    # 6. 无 goal 时不写键(旧格式文件不膨胀)
    tmp2 = Path(tempfile.mkdtemp()) / "t.json"
    save_session(tmp2, [{"role": "user", "content": "hi"}])
    assert "goal" not in json.loads(tmp2.read_text())

    print("ok: 6 checks passed")


if __name__ == "__main__":
    main()
