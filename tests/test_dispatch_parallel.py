"""批内并行派发: 同批多个 tool_use 并行执行(慢工具批内总时长 sum→max),
thread_bound 工具保串行(恒定 worker 线程, playwright 语义)。

- 3 个 0.5s 假工具同批: 墙钟 <1.0s(并行)而非 >=1.5s(串行)
- 3 个 thread_bound 假工具同批: 墙钟 >=1.4s(单 worker 串行)
- 混批(并行+线程绑定+未知工具): 结果齐全且按批序与 tool_use 对齐
- 打断: 未完成调用立即废弃填"未完成", 已完成的照常取结果
- 错误即文本: 工具抛异常回 "错误: ..." 不炸批
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.loop import Agent
from core.tools import Tool


def make_agent(tools) -> Agent:
    return Agent(model=None, system_base="", tools=tools, workdir=".", max_turns=1)


def call(cid, name, args=None):
    return {"id": cid, "name": name, "input": args or {}}


def slow_tool(name: str, sec: float = 0.5, thread_bound: bool = False, runs: list | None = None):
    def run(a):
        if runs is not None:
            runs.append(threading.get_ident())
        time.sleep(sec)
        return "ok"
    return Tool(name=name, description="", input_schema={}, run=run, thread_bound=thread_bound)


def test_parallel_sum_to_max():
    ag = make_agent([slow_tool(f"s{i}") for i in range(3)])
    t0 = time.monotonic()
    rs = ag._run_batch([call(f"c{i}", f"s{i}") for i in range(3)])
    dt = time.monotonic() - t0
    assert [r[3] for r in rs] == ["ok"] * 3, rs
    assert dt < 1.0, f"应并行(max≈0.5s), 实际 {dt:.2f}s —— 退化了?"


def test_thread_bound_stays_serial():
    ag = make_agent([slow_tool(f"b{i}", thread_bound=True) for i in range(3)])
    t0 = time.monotonic()
    rs = ag._run_batch([call(f"c{i}", f"b{i}") for i in range(3)])
    dt = time.monotonic() - t0
    assert all(r[3] == "ok" for r in rs), rs
    assert dt >= 1.4, f"thread_bound 应串行(≈1.5s), 实际 {dt:.2f}s —— 线程身份语义被破坏"


def test_mixed_batch_order_and_unknown():
    ag = make_agent([slow_tool("slow"), slow_tool("bound", thread_bound=True)])
    t0 = time.monotonic()
    rs = ag._run_batch([call("c1", "slow"), call("c2", "nope"), call("c3", "bound")])
    dt = time.monotonic() - t0
    assert [r[0]["id"] for r in rs] == ["c1", "c2", "c3"], "结果必须按批序"
    assert rs[1][3] == "错误: 未知工具 nope", rs[1]
    assert rs[0][3] == "ok" and rs[2][3] == "ok", rs
    assert dt < 1.0, f"混批应并行推进(≈0.5s), 实际 {dt:.2f}s"


def test_error_is_text_not_raise():
    def boom(a):
        raise RuntimeError("炸了")
    ag = make_agent([Tool(name="boom", description="", input_schema={}, run=boom), slow_tool("s")])
    rs = ag._run_batch([call("c1", "boom"), call("c2", "s")])
    assert rs[0][3] == "错误: 炸了" and rs[1][3] == "ok", rs


def test_interrupt_discards_pending():
    runs: list = []

    def run(a):
        runs.append(1)
        time.sleep(5)
        return "done"

    ag = make_agent([Tool(name="slow", description="", input_schema={}, run=run,
                          kill=lambda: None)])
    killer = threading.Timer(0.3, lambda: ag._interrupt_flag.update(reason="用户打断"))
    killer.start()
    t0 = time.monotonic()
    rs = ag._run_batch([call("c1", "slow")])
    dt = time.monotonic() - t0
    killer.join()
    assert "未完成" in rs[0][3], rs
    assert dt < 1.0, f"打断应立即返回, 实际 {dt:.2f}s"


def test_interrupt_before_batch_skips_all():
    ag = make_agent([slow_tool("s")])
    ag._interrupt_flag["reason"] = "用户打断"  # 开批前已打断: 全部废弃不启动
    rs = ag._run_batch([call("c1", "s"), call("c2", "s")])
    assert all("未完成" in r[3] for r in rs), rs


if __name__ == "__main__":
    for fn in (test_parallel_sum_to_max, test_thread_bound_stays_serial,
               test_mixed_batch_order_and_unknown, test_error_is_text_not_raise,
               test_interrupt_discards_pending, test_interrupt_before_batch_skips_all):
        fn()
        print(f"{fn.__name__} 通过")
