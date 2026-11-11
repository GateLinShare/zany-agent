"""空回复重试(hermes _empty_content_retries 语义): GLM/vLLM 网关偶发吐空
content assistant, 不兜底 = 回合静默无输出。

- 无 tool_use / 无非空 text 即空(thinking-only、空 text 都算), 不 append 直接重试
- 每次重试发 ("empty_retry", n), 3 次耗尽落哨兵文本 ("empty_giveup", note)
- 成功有内容后计数清零(不跨回合累计)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.loop import Agent
from core.tools import Tool


def Ev(type, **kw):
    return {"type": type, **kw}


class FakeModel:
    def __init__(self, turns):
        self.turns = list(turns)

    def stream(self, system, messages, schemas):
        yield from self.turns.pop(0)


def empty_turn():
    """空回复的 SSE 剧本: 只有 usage, 无内容块。"""
    return [Ev("message_start", message={"usage": {"input_tokens": 10}}),
            Ev("message_delta", usage={"output_tokens": 1})]


def text_turn(text):
    return [Ev("message_start", message={"usage": {"input_tokens": 10}}),
            Ev("content_block_start", index=0, content_block={"type": "text"}),
            Ev("content_block_delta", index=0, delta={"type": "text_delta", "text": text}),
            Ev("content_block_stop", index=0),
            Ev("message_delta", usage={"output_tokens": 5})]


def thinking_only_turn():
    return [Ev("message_start", message={"usage": {"input_tokens": 10}}),
            Ev("content_block_start", index=0, content_block={"type": "thinking"}),
            Ev("content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": "hmm"}),
            Ev("content_block_stop", index=0),
            Ev("message_delta", usage={"output_tokens": 1})]


def make_agent(model):
    tool = Tool(name="noop", description="noop", input_schema={"type": "object", "properties": {}},
                run=lambda a: "ok")
    return Agent(model, system_base="t", tools=[tool], workdir=str(Path(__file__).parent))


def test_empty_then_success():
    """空 x2 后成功: 重试 2 次, 不留空 assistant 块, 计数清零。"""
    a = make_agent(FakeModel([empty_turn(), thinking_only_turn(), text_turn("ok")]))
    msgs = [{"role": "user", "content": "hi"}]
    evs = list(a.stream(msgs))
    retries = [e[1] for e in evs if e[0] == "empty_retry"]
    assert retries == [1, 2], retries
    assert not any(e[0] == "empty_giveup" for e in evs)
    # 消息序列: user + 最终 assistant(成功那条), 没有空 assistant 块混入
    assert len(msgs) == 2
    assert msgs[-1]["content"][0]["text"] == "ok"
    assert a._empty_retries == 0


def test_exhausted_giveup():
    """连空 4 次(原始 + 3 重试): 落哨兵文本, 消息序列仍合法。"""
    a = make_agent(FakeModel([empty_turn() for _ in range(4)]))
    msgs = [{"role": "user", "content": "hi"}]
    evs = list(a.stream(msgs))
    assert [e[1] for e in evs if e[0] == "empty_retry"] == [1, 2, 3]
    giveup = [e for e in evs if e[0] == "empty_giveup"]
    assert len(giveup) == 1
    assert len(msgs) == 2  # user + 哨兵 assistant
    assert "空回复" in msgs[-1]["content"][0]["text"]


def tool_turn():
    return [Ev("message_start", message={"usage": {"input_tokens": 10}}),
            Ev("content_block_start", index=0,
               content_block={"type": "tool_use", "id": "t1", "name": "noop"}),
            Ev("content_block_stop", index=0),
            Ev("message_delta", usage={"output_tokens": 5})]


def test_counter_resets_after_success():
    """空→tool_use 轮成功→下回合再连空: 计数已清零, 仍要 3 次才 giveup。"""
    a = make_agent(FakeModel([empty_turn(), tool_turn(),
                              empty_turn(), empty_turn(), empty_turn(), empty_turn()]))
    msgs = [{"role": "user", "content": "hi"}]
    evs = list(a.stream(msgs))
    assert [e[1] for e in evs if e[0] == "empty_retry"] == [1, 1, 2, 3]
    assert len([e for e in evs if e[0] == "empty_giveup"]) == 1


def test_whitespace_text_is_empty():
    """纯空白 text 也算空回复。"""
    a = make_agent(FakeModel([text_turn("   \n"), text_turn("real")]))
    msgs = [{"role": "user", "content": "hi"}]
    evs = list(a.stream(msgs))
    assert [e[1] for e in evs if e[0] == "empty_retry"] == [1]
    assert msgs[-1]["content"][0]["text"] == "real"


if __name__ == "__main__":
    test_empty_then_success()
    test_exhausted_giveup()
    test_counter_resets_after_success()
    test_whitespace_text_is_empty()
    print("test_empty_retry: all passed")
