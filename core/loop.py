"""agent loop:流式调模型 → 聚合 content 块 → 派发工具 → 回填结果 → 循环。

这就是从 deepagents 拿回来自己写的那一层。约定:
- messages 全程用 Anthropic 原生格式(dict), 会话落盘/回放/模型看到的是同一份数据。
- stream() 生成事件元组给 UI, 同时原地改写 messages; invoke() 是不关心事件时的便捷封装。
- 工具错误也返回文本不抛异常 —— 让模型自己看到错误并重试, 这是 agent 的自愈机制。
"""

import json
import os
from pathlib import Path

from .messages import text_of, tool_uses
from .summarize import maybe_summarize

MAX_TURNS = 40  # 单次 run 的工具轮数上限, 防模型打转烧钱


def user_memory_path() -> Path:
    """个人记忆文件: ZANY_USER_MEMORY 优先(服务器多人共用时各自 export 指向自己的), 默认 ~/.miniagent/USER.md。"""
    return Path(os.environ.get("ZANY_USER_MEMORY") or Path.home() / ".miniagent" / "USER.md")


class Agent:
    def __init__(self, model, system_base: str, tools: list, workdir: str, max_turns: int = MAX_TURNS):
        self.model = model
        self.system_base = system_base
        self.tools = {t.name: t for t in tools}
        self.workdir = workdir
        self.max_turns = max_turns
        # session 级 token 账本(hermes sessions 表语义): 真实 usage 累计,
        # 随 save_session 落盘、resume 时恢复。子代理消耗不计入。
        self.usage = {"input": 0, "output": 0, "calls": 0}
        # 打断标志(hermes agent.interrupt): UI 线程置位, loop 在事件间隙协作式停下。
        # CPython 下 str 赋值原子, 无需锁; 工具在线程内同步跑, 无 hermes 的 tid fan-out。
        self._interrupt_requested: str | None = None
        self.was_interrupted = False  # 上一轮是否被打断(结果属性, 供 UI/headless 读)
        # 压缩防抖/熔断状态(hermes context_compressor 的三层保护, 简化版)
        self._last_prompt_tokens: int | None = None  # 上次响应真实 prompt tokens
        self._ineffective = 0        # 连续无效压缩计数(压缩后降幅 <10%)
        self._compact_dead = False   # 摘要 LLM 失败熔断: 本会话不再自动压

    def add_tools(self, extra: list) -> list[str]:
        """后补工具(MCP 后台加载完), 返回新增名字; 同名覆盖。"""
        added = [t.name for t in extra if t.name not in self.tools]
        for t in extra:
            self.tools[t.name] = t
        return added

    # ---- 打断(hermes interrupt/clear_interrupt 语义) ----

    def interrupt(self, reason: str = "用户打断") -> None:
        """请求打断当前轮。置标志 + 立即 abort 在途模型流(hermes 及时性:
        不等下一个 SSE 事件); 正在执行的工具跑完当前这一个即停(不杀线程,
        等同 hermes 的"当前工具批次完成后停止")。"""
        self._interrupt_requested = reason
        abort = getattr(self.model, "abort", None)
        if abort:
            abort()

    def clear_interrupt(self) -> None:
        self._interrupt_requested = None

    def _system(self) -> str:
        """每轮重读 AGENTS.md(项目记忆) + 个人记忆(路径见 user_memory_path) —— 改记忆不用重启。"""
        s = self.system_base
        mem = Path(self.workdir) / "AGENTS.md"
        if mem.exists():
            s += f"\n\n# 项目记忆(AGENTS.md)\n{mem.read_text(encoding='utf-8')}"
        user_mem = user_memory_path()
        if user_mem.exists():
            s += f"\n\n# 个人记忆(USER.md)\n{user_mem.read_text(encoding='utf-8')}"
        return s

    def _schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    def stream(self, messages: list[dict]):
        """跑一轮完整对话(可能多轮工具循环), 原地追加 messages, yield 事件:
        ("thinking", delta) / ("text", delta) / ("tool_start", id, name)
        / ("tool_result", id, name, input_json, output)
        / ("usage", turn_in, turn_out)          每次模型响应的真实 token
        / ("compact", before, after)            触发了上下文压缩
        / ("compact_error", err)                摘要失败, 本会话停用自动压缩
        / ("interrupted", reason)               用户打断, 本轮到此为止

        打断(hermes 语义): interrupt() 置位后, 流式循环在下一个 SSE 事件间隙
        break, 已流出的 thinking/text 保留为 partial assistant —— 但丢弃
        thinking 块(未拿到 signature, 回传会被 API 拒), tool_use 一律补
        cancelled tool_result 保持配对, 消息序列始终合法可回放。
        """
        self.clear_interrupt()  # 防御: 上一轮残留的标志不带入新一轮
        self.was_interrupted = False
        try:
            yield from self._run(messages)
        finally:
            self.clear_interrupt()

    def _run(self, messages: list[dict]):
        for _ in range(self.max_turns):
            if self._interrupt_requested:  # 检查点 a: 每轮开始(hermes tool loop 顶)
                yield ("interrupted", self._interrupt_requested)
                return
            for ev in self._compact_step(messages):  # 调用前兜底: 首轮 est 粗估, 之后用上轮真实值
                yield ev
            blocks = []          # 本轮 assistant 的 content 块(按到达顺序)
            pending = {}         # tool_use 块的 partial_json 累积, block index -> str
            turn_in = turn_out = 0
            aborted = False
            try:
                for ev in self.model.stream(self._system(), messages, self._schemas()):
                    if self._interrupt_requested:  # 检查点 b: 流中, SSE 事件间隙
                        aborted = True
                        break  # 不调"final 聚合"—— hermes 对部分消费的流也只取已收到部分
                    etype = ev.type
                    if etype == "message_start":
                        # 真实账本就在这两个事件里, hermes/dcode 都从这里记账
                        u = getattr(ev.message, "usage", None)
                        if u:
                            turn_in = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
                                + (getattr(u, "cache_creation_input_tokens", 0) or 0)
                    elif etype == "message_delta":
                        u = getattr(ev, "usage", None)
                        if u and getattr(u, "output_tokens", 0):
                            turn_out = u.output_tokens
                    elif etype == "content_block_start":
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
            except Exception:
                if not self._interrupt_requested:
                    raise
                aborted = True  # abort() 关流导致的读错误 = 打断本身, 走收尾
                etype = ev.type
                if etype == "message_start":
                    # 真实账本就在这两个事件里, hermes/dcode 都从这里记账
                    u = getattr(ev.message, "usage", None)
                    if u:
                        turn_in = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
                            + (getattr(u, "cache_creation_input_tokens", 0) or 0)
                elif etype == "message_delta":
                    u = getattr(ev, "usage", None)
                    if u and getattr(u, "output_tokens", 0):
                        turn_out = u.output_tokens
                elif etype == "content_block_start":
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
            # 打断收尾(hermes close_interrupted_tool_sequence 的 Anthropic 版):
            # 丢未完成的 thinking 块(没有 signature, 原样回传会被 API 拒),
            # 保留 text/tool_use; 有 tool_use 就补 cancelled tool_result 保配对。
            if aborted:
                blocks = [b for b in blocks if b.get("type") in ("text", "tool_use")]
                if blocks:
                    messages.append({"role": "assistant", "content": blocks})
                    self._account(turn_in, turn_out)  # 打断轮也记账; usage 事件不发, UI 直接看累计
                    results = [
                        {"type": "tool_result", "tool_use_id": c["id"],
                         "content": f"错误: {self._interrupt_requested}, 本次执行未完成"}
                        for c in tool_uses(messages[-1])
                    ]
                    if results:
                        messages.append({"role": "user", "content": results})
                yield ("interrupted", self._interrupt_requested)
                self.was_interrupted = True
                return

            messages.append({"role": "assistant", "content": blocks})

            # 记账(hermes update_from_response): 每次响应后累计真实 usage
            usage_ev = self._account(turn_in, turn_out)
            if usage_ev:
                yield ("usage", *usage_ev)

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
            # postflight(hermes): 对话完校验, 超阈值就地压缩, 下一轮带上瘦身后的历史
            for ev in self._compact_step(messages):
                yield ev
        note = f"已达最大轮数 {self.max_turns}, 本轮暂时中止; 继续对话可让模型接着干"
        messages.append({"role": "assistant", "content": [{"type": "text", "text": note}]})
        yield ("turn_limit", self.max_turns)

    def _account(self, turn_in: int, turn_out: int) -> tuple | None:
        """真实 usage 记账(hermes update_from_response), 打断路径也走这里。
        有增量时返回 (in, out), 由调用方决定是否发 ("usage", ...) 事件。"""
        if not (turn_in or turn_out):
            return None
        self._last_prompt_tokens = turn_in
        self.usage["input"] += turn_in
        self.usage["output"] += turn_out
        self.usage["calls"] += 1
        self.usage["last_in"] = turn_in  # resume 接回后进度条/压缩判定用
        return (turn_in, turn_out)

    def reset_tool_ledger(self) -> None:
        """清空内置工具的行为熔断账本(hermes 语义: 压缩后清空)。
        read 去重依赖"早前读过且内容见上文"这一事实, 摘要改写后不再成立。"""
        for t in self.tools.values():
            if t.ledger:
                t.ledger.reset()

    def restore(self, usage: dict) -> None:
        """resume 接回账本与压缩判定状态(sessions 落盘的 usage dict)。
        补齐缺 key 防旧会话文件 KeyError; hermes resume 语义: 状态立即可见。"""
        self.usage = {k: int(usage.get(k, 0)) for k in ("input", "output", "calls", "last_in")}
        self._last_prompt_tokens = usage.get("last_in") or None

    def _compact_step(self, messages: list[dict]) -> list[tuple]:
        """压缩步: 判定(真实优先/est 兜底) → 摘要改写。带两层保护:
        - 防抖(hermes #14690): 压缩后降幅 <10% 连续两次 → 放弃, 防无限压缩循环
        - 熔断(hermes 600s 冷却的简化): 摘要 LLM 失败一次就停, 防每轮重试拖垮对话
        """
        if self._compact_dead or self._ineffective >= 2:
            return []
        try:
            compressed, tokens, est_before, est_after = maybe_summarize(
                self.model, messages, prompt_tokens=self._last_prompt_tokens)
        except Exception as e:  # 压缩失败不炸轮次 —— 错误即文本
            self._compact_dead = True
            return [("compact_error", str(e))]
        if not compressed:
            return []
        self.reset_tool_ledger()  # 摘要改写掉旧 read 结果, 行为账本(去重 stub)必须同步失效
        # 防抖同口径: est vs est(真实值与 est 差一个 system+tools 的量, 不能混比)
        self._ineffective = self._ineffective + 1 if est_after >= est_before * 0.9 else 0
        return [("compact", tokens, est_after)]

    def invoke(self, messages: list[dict]) -> list[dict]:
        for _ in self.stream(messages):
            pass
        return messages
