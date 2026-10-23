"""agent loop:流式调模型 → 聚合 content 块 → 派发工具 → 回填结果 → 循环。

这就是从 deepagents 拿回来自己写的那一层。约定:
- messages 全程用 Anthropic 原生格式(dict), 会话落盘/回放/模型看到的是同一份数据。
- stream() 生成事件元组给 UI, 同时原地改写 messages; invoke() 是不关心事件时的便捷封装。
- 工具错误也返回文本不抛异常 —— 让模型自己看到错误并重试, 这是 agent 的自愈机制。
"""

import json
from pathlib import Path

from .messages import est_tokens, text_of, tool_uses
from .summarize import maybe_summarize

MAX_TURNS = 40  # 单次 run 的工具轮数上限, 防模型打转烧钱


class Agent:
    def __init__(self, model, system_base: str, tools: list, workdir: str, max_turns: int = MAX_TURNS):
        self.model = model
        self.system_base = system_base
        self.tools = {t.name: t for t in tools}
        self.workdir = workdir
        self.max_turns = max_turns

    def _system(self) -> str:
        """每轮重读 AGENTS.md —— 用户改记忆不用重启。"""
        mem = Path(self.workdir) / "AGENTS.md"
        return self.system_base + (f"\n\n# 项目记忆(AGENTS.md)\n{mem.read_text(encoding='utf-8')}" if mem.exists() else "")

    def _schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    def stream(self, messages: list[dict]):
        """跑一轮完整对话(可能多轮工具循环), 原地追加 messages, yield 事件:
        ("thinking", delta) / ("text", delta) / ("tool_start", id, name)
        / ("tool_result", id, name, input_json, output)
        """
        maybe_summarize(self.model, messages)
        for _ in range(self.max_turns):
            blocks = []          # 本轮 assistant 的 content 块(按到达顺序)
            pending = {}         # tool_use 块的 partial_json 累积, block index -> str
            for ev in self.model.stream(self._system(), messages, self._schemas()):
                etype = ev.type
                if etype == "content_block_start":
                    b = ev.content_block
                    blk = {"type": b.type}
                    if b.type == "tool_use":
                        blk.update(id=b.id, name=b.name, input={})
                        pending[ev.index] = ""
                    blocks.append(blk)
                elif etype == "content_block_delta":
                    d = ev.delta
                    blk = blocks[ev.index]
                    if d.type == "text_delta":
                        blk["text"] = blk.get("text", "") + d.text
                        yield ("text", d.text)
                    elif d.type == "thinking_delta":
                        blk["thinking"] = blk.get("thinking", "") + d.thinking
                        yield ("thinking", d.thinking)
                    elif d.type == "signature_delta":
                        blk["signature"] = d.signature
                    elif d.type == "input_json_delta":
                        pending[ev.index] += d.partial_json
                elif etype == "content_block_stop":
                    blk = blocks[ev.index]
                    if blk["type"] == "tool_use":
                        try:
                            blk["input"] = json.loads(pending[ev.index] or "{}")
                        except json.JSONDecodeError:
                            blk["input"] = {}
            messages.append({"role": "assistant", "content": blocks})

            calls = tool_uses(messages[-1])
            if not calls:
                return
            results = []
            for call in calls:
                name, args = call["name"], call.get("input", {})
                yield ("tool_start", call["id"], name)
                tool = self.tools.get(name)
                if tool is None:
                    out = f"错误: 未知工具 {name}"
                else:
                    try:
                        out = tool.run(args)
                    except Exception as e:  # 工具崩了也回文本, 让模型重试
                        out = f"错误: {e}"
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": out})
                yield ("tool_result", call["id"], name, json.dumps(args, ensure_ascii=False), out)
            messages.append({"role": "user", "content": results})
        raise RuntimeError(f"超过最大轮数 {self.max_turns}, 已停止")

    def invoke(self, messages: list[dict]) -> list[dict]:
        for _ in self.stream(messages):
            pass
        return messages
