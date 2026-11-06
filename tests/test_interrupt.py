"""打断即时性: interrupt() 必须立即废弃在途工具, 不等它跑完。

用假模型喂一条 assistant tool_use, 工具睡 5s, 0.5s 时打断:
- 轮次必须在 ~0.1s 级别结束(不等工具)
- tool_result 记"未完成", 消息序列保持配对可回放
- 同批剩余工具请求不再执行
- execute 的进程组被真正杀死
"""

import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.loop import Agent
from core.tools import Tool, builtin_tools


def Ev(type, **kw):  # SSE 事件: 纯 dict(model 层裸 httpx 后, 协议事件就是 dict)
    return {"type": type, **kw}


class FakeModel:
    """按剧本吐事件的假模型。"""

    def __init__(self, turns):
        self.turns = list(turns)

    def abort(self):
        pass

    def stream(self, system, messages, schemas):
        yield from self.turns.pop(0)


def tool_turn(*calls):
    """一条 assistant 消息含若干 tool_use 块的 SSE 事件剧本。"""
    evs = [Ev("message_start",
              message={"usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                                 "cache_creation_input_tokens": 0}})]
    for i, call in enumerate(calls):
        tid, name = call[0], call[1]
        evs.append(Ev("content_block_start", index=i,
                      content_block={"type": "tool_use", "id": tid, "name": name}))
        if len(call) > 2:  # (id, name, input_json) 形式: 补参数增量
            evs.append(Ev("content_block_delta", index=i,
                          delta={"type": "input_json_delta", "partial_json": call[2]}))
        evs.append(Ev("content_block_stop", index=i))
    evs.append(Ev("message_delta", usage={"output_tokens": 5}))
    return evs


def slow_tool(runs: list) -> Tool:
    def run(a):
        runs.append(1)
        time.sleep(5)
        return "done"

    return Tool("slow", "睡 5 秒", {"type": "object", "properties": {}}, run)


def run_and_interrupt(agent, delay=0.5):
    msgs = [{"role": "user", "content": "hi"}]
    events = []
    th = threading.Thread(target=lambda: events.extend(agent.stream(msgs)))
    th.start()
    time.sleep(delay)
    t0 = time.monotonic()
    agent.interrupt("测试打断")
    th.join(5)
    return events, msgs, time.monotonic() - t0


def test_interrupt_abandons_inflight_tool():
    runs = []
    agent = Agent(FakeModel([tool_turn(("t1", "slow"))]), "", [slow_tool(runs)],
                  tempfile.mkdtemp())
    events, msgs, elapsed = run_and_interrupt(agent)
    assert not any(e[0] == "turn_limit" for e in events)
    assert elapsed < 2, f"打断等了 {elapsed:.1f}s, 没有立即废弃在途工具"
    assert [e[0] for e in events][-1] == "interrupted"
    assert agent.was_interrupted
    tr = msgs[-1]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "t1"
    assert "未完成" in tr["content"]


def test_interrupt_discards_remaining_calls():
    runs = []
    agent = Agent(FakeModel([tool_turn(("t1", "slow"), ("t2", "slow"))]), "",
                  [slow_tool(runs)], tempfile.mkdtemp())
    events, msgs, elapsed = run_and_interrupt(agent)
    assert elapsed < 2
    assert len(runs) == 1, "同批剩余工具请求不应再执行"
    ids = [c["tool_use_id"] for c in msgs[-1]["content"]]
    assert ids == ["t1", "t2"], "废弃请求也要补 tool_result 保持配对"
    assert all("未完成" in c["content"] for c in msgs[-1]["content"])


def test_execute_killed_on_interrupt():
    if sys.platform == "win32":  # ponytail: pgrep 是 Linux 命令, Windows 上无法验证进程组被杀
        print("skip (pgrep 仅 Linux)")
        return
    tmp = tempfile.mkdtemp()
    agent = Agent(FakeModel([tool_turn(("t1", "execute",
                                        '{"command": "sleep 300", "timeout": 1200}'))]),
                  "", builtin_tools(tmp), tmp)
    msgs = [{"role": "user", "content": "hi"}]
    events = []
    th = threading.Thread(target=lambda: events.extend(agent.stream(msgs)))
    th.start()
    time.sleep(0.8)
    assert subprocess.run(["pgrep", "-f", "sleep 300"], capture_output=True).returncode == 0, \
        "前置失败: sleep 300 没跑起来, 测试无效"
    t0 = time.monotonic()
    agent.interrupt("测试打断")
    th.join(5)
    elapsed = time.monotonic() - t0
    assert not th.is_alive()
    assert elapsed < 2, f"打断等了 {elapsed:.1f}s, execute 没被杀"
    assert [e[0] for e in events][-1] == "interrupted"
    time.sleep(0.3)  # 给 worker 线程收尸留点时间
    r = subprocess.run(["pgrep", "-f", "sleep 300"], capture_output=True, text=True)
    assert r.returncode != 0, f"execute 进程组未被杀死: {r.stdout!r}"


def test_interrupt_reaches_subagent():
    """task 子代理与主代理共享打断标志: 主对话 interrupt 后子代理轮首检查点立即生效,
    不会把子任务整个跑完(子代理 owns_interrupt=False, 不清共享标志)。"""
    from core.subagent import make_task_tool
    shared: dict = {}
    tt = make_task_tool(FakeModel([]), "", tempfile.mkdtemp(), [], shared_interrupt=shared)
    shared["reason"] = "主对话已打断"
    out = tt.run({"prompt": "hi"})
    assert out == "hi", f"子代理应在轮首检查点立即中断, 实际: {out!r}"


def test_normal_turn_unaffected():
    turn = [Ev("message_start",
               message={"usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0}}),
            Ev("content_block_start", index=0, content_block={"type": "text"}),
            Ev("content_block_delta", index=0, delta={"type": "text_delta", "text": "ok"}),
            Ev("content_block_stop", index=0),
            Ev("message_delta", usage={"output_tokens": 2})]
    agent = Agent(FakeModel([turn]), "", [], tempfile.mkdtemp())
    msgs = [{"role": "user", "content": "hi"}]
    events = list(agent.stream(msgs))
    assert ("text", "ok") in events
    assert msgs[-1]["content"][0]["text"] == "ok"
    assert not agent.was_interrupted


if __name__ == "__main__":
    for fn in (test_interrupt_abandons_inflight_tool, test_interrupt_discards_remaining_calls,
               test_execute_killed_on_interrupt, test_interrupt_reaches_subagent,
               test_normal_turn_unaffected):
        fn()
        print(f"{fn.__name__} ok")
