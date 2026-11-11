"""agent loop:流式调模型 → 聚合 content 块 → 派发工具 → 回填结果 → 循环。

这就是从 deepagents 拿回来自己写的那一层。约定:
- messages 全程用 Anthropic 原生格式(dict), 会话落盘/回放/模型看到的是同一份数据。
- stream() 生成事件元组给 UI, 同时原地改写 messages; invoke() 是不关心事件时的便捷封装。
- 工具错误也返回文本不抛异常 —— 让模型自己看到错误并重试, 这是 agent 的自愈机制。
"""

import contextlib
import json
import os
import queue
import threading
from pathlib import Path

from .messages import result_text, tool_uses
from .summarize import maybe_summarize

MAX_TURNS = 40  # 单次 run 的工具轮数上限, 防模型打转烧钱


def _is_overflow(e: Exception) -> bool:
    """识别上下文超限类 API 错误(hermes FailoverReason.payload_too_large/
    context_overflow 的简化版): 413, 或 400 带超长特征文本。
    典型样本: Anthropic "prompt is too long: X tokens > Y maximum",
    vLLM "maximum context length is X tokens"。"""
    s = str(e)
    return "HTTP 413" in s or ("HTTP 400" in s
                               and any(k in s for k in ("too long", "context length", "maximum context")))


class AbortError(Exception):
    """用户取消(区别于真异常): 工具/子代理内部检测到 interrupt 并主动上抛时用。
    loop 识别后 tool_result 记为取消而非错误, UI 侧 ("interrupted", reason) 同语义。"""


def user_memory_path() -> Path:
    """个人记忆文件: ZANY_USER_MEMORY 优先(服务器多人共用时各自 export 指向自己的), 默认 ~/.zany/USER.md。"""
    return Path(os.environ.get("ZANY_USER_MEMORY") or Path.home() / ".zany" / "USER.md")


class Agent:
    def __init__(self, model, system_base: str, tools: list, workdir: str, max_turns: int = MAX_TURNS,
                 shared_interrupt: dict | None = None, owns_interrupt: bool = True):
        self.model = model
        self.system_base = system_base
        self.tools = {t.name: t for t in tools}
        self.workdir = workdir
        self.max_turns = max_turns
        # session 级 token 账本(hermes sessions 表语义): 真实 usage 累计,
        # 随 save_session 落盘、resume 时恢复。子代理消耗不计入。
        self.usage = {"input": 0, "output": 0, "calls": 0}
        # 打断标志(hermes agent.interrupt): UI 线程置位, loop 在事件间隙协作式停下。
        # 标志住 dict 槽(单键读写原子, 无需锁): 子代理与主代理共享同一 dict,
        # task 工具在途时主对话打断能传导进子代理的检查点 a/b/c。无共享时自建。
        self._interrupt_flag = shared_interrupt if shared_interrupt is not None else {}
        self._owns_interrupt = owns_interrupt  # 子代理 False: 只读共享标志, 不清不写
        self.was_interrupted = False  # 上一轮是否被打断(结果属性, 供 UI/headless 读)
        # 工具专职 worker 线程: 所有 tool.run 串行在这一个线程跑 —— 线程身份恒定,
        # playwright sync 这类线程绑定对象不跨线程; 打断时 loop 立即废弃在途调用
        # (结果不再取), worker 把旧调用跑完后自己丢弃, 继续取下一个。
        self._tool_q: queue.Queue = queue.Queue()
        threading.Thread(target=self._tool_loop, daemon=True, name="zany-tools").start()
        # 压缩防抖/熔断状态(hermes context_compressor 的三层保护, 简化版)
        self._last_prompt_tokens: int | None = None  # 上次响应真实 prompt tokens
        self._ineffective = 0        # 连续无效压缩计数(压缩后降幅 <10%)
        self._compact_dead = False   # 摘要 LLM 失败熔断: 本会话不再自动压
        # 空回复重试(hermes _empty_content_retries): GLM/vLLM 网关偶发吐空 content
        # assistant(2026-11-11 实测: 大 base64 图片 tool_result 后返回空消息), 不兜底=静默中断
        self._empty_retries = 0
        # overflow 强压重试(hermes overflow error handler): 一次 run 只试一次
        self._overflow_retried = False

    @property
    def _interrupt_requested(self) -> str | None:
        """读 dict 槽: 子代理共享主代理的 flag, 打断传导; 写兼容旧 str 赋值语义。"""
        return self._interrupt_flag.get("reason")

    @_interrupt_requested.setter
    def _interrupt_requested(self, v: str | None) -> None:
        if v is None:
            self._interrupt_flag.pop("reason", None)
        else:
            self._interrupt_flag["reason"] = v

    def add_tools(self, extra: list) -> list[str]:
        """后补工具(MCP 后台加载完), 返回新增名字; 同名覆盖。"""
        added = [t.name for t in extra if t.name not in self.tools]
        for t in extra:
            self.tools[t.name] = t
        return added

    # ---- 打断(hermes interrupt/clear_interrupt 语义) ----

    def interrupt(self, reason: str = "用户打断") -> None:
        """请求打断当前轮。置标志 + 立即 abort 在途模型流(hermes 及时性:
        不等下一个 SSE 事件); 在途工具立即废弃不等结果(execute 由 kill 钩子
        终止整个进程组), 批内剩余工具请求不再执行。"""
        self._interrupt_requested = reason
        abort = getattr(self.model, "abort", None)
        if abort:
            abort()

    def clear_interrupt(self) -> None:
        if self._owns_interrupt:  # 子代理共享主对话的标志: 它的 stream 首尾清理不能把主对话的打断抹掉
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
        self._overflow_retried = False  # overflow 强压机会每次 run 重置
        self.was_interrupted = False
        try:
            yield from self._run(messages)
        finally:
            self.clear_interrupt()

    def _run(self, messages: list[dict]):
        for _ in range(self.max_turns):
            if self._interrupt_requested:  # 检查点 a: 每轮开始(hermes tool loop 顶)
                yield ("interrupted", self._interrupt_requested)
                self.was_interrupted = True
                return
            for ev in self._compact_step(messages):  # 调用前兜底: 首轮 est 粗估, 之后用上轮真实值
                yield ev
            blocks = []          # 本轮 assistant 的 content 块(按到达顺序)
            pending = {}         # tool_use 块的 partial_json 累积, block index -> str
            turn_in = turn_out = 0
            aborted = False

            def _consume(ev: dict) -> list[tuple]:
                """一个协议事件 dict → 聚合进 blocks/pending, 返回给 UI 的增量列表。
                model 层裸 httpx 后, 事件就是 Anthropic SSE 的原始 JSON(单协议)。"""
                nonlocal turn_in, turn_out
                out = []
                etype = ev.get("type")
                if etype == "message_start":
                    # 真实账本就在这两个事件里, hermes/dcode 都从这里记账
                    u = (ev.get("message") or {}).get("usage") or {}
                    turn_in = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
                        + (u.get("cache_creation_input_tokens") or 0)
                elif etype == "message_delta":
                    u = ev.get("usage") or {}
                    if u.get("output_tokens"):
                        turn_out = u["output_tokens"]
                elif etype == "content_block_start":
                    b = ev.get("content_block") or {}
                    blk = {"type": b.get("type")}
                    if b.get("type") == "tool_use":
                        blk.update(id=b["id"], name=b["name"], input={})
                        pending[ev["index"]] = ""
                    blocks.append(blk)
                elif etype == "content_block_delta":
                    d = ev.get("delta") or {}
                    blk = blocks[ev["index"]]
                    if d.get("type") == "text_delta":
                        blk["text"] = blk.get("text", "") + d["text"]
                        out.append(("text", d["text"]))
                    elif d.get("type") == "thinking_delta":
                        blk["thinking"] = blk.get("thinking", "") + d["thinking"]
                        out.append(("thinking", d["thinking"]))
                    elif d.get("type") == "signature_delta":
                        blk["signature"] = d["signature"]
                    elif d.get("type") == "input_json_delta":
                        pending[ev["index"]] += d.get("partial_json", "")
                elif etype == "content_block_stop":
                    blk = blocks[ev["index"]]
                    if blk["type"] == "tool_use":
                        try:
                            blk["input"] = json.loads(pending[ev["index"]] or "{}")
                        except json.JSONDecodeError:
                            blk["input"] = {}
                return out

            try:
                for ev in self.model.stream(self._system(), messages, self._schemas()):
                    if self._interrupt_requested:  # 检查点 b: 流中, SSE 事件间隙
                        aborted = True
                        break  # 不调"final 聚合"—— hermes 对部分消费的流也只取已收到部分
                    for out in _consume(ev):
                        yield out
            except Exception as e:
                if self._interrupt_requested:
                    aborted = True  # abort() 关流导致的读错误 = 打断本身, 走收尾
                elif not self._overflow_retried and _is_overflow(e):
                    # 上下文超限兜底(hermes overflow error handler 语义): 单轮暴涨
                    # (如 200k 字符工具输出回填)会让 preflight 用"上次真实 tokens"
                    # 低估本次请求, 压缩不触发 → API 400; 历史不变, 重试永远 400 死锁。
                    # 对策: 强制压缩一次再重试(run 内限一次, 防循环烧钱);
                    # 摘要调用自身也超限时放弃并抛原始错误。此时 messages 尾部
                    # 要么是 user(tool_result) 要么是首轮 user, 均闭合, 压缩不破坏配对。
                    self._overflow_retried = True
                    try:
                        _, _, _, est_after = maybe_summarize(
                            self.model, messages, trigger_tokens=1 << 62)
                    except Exception:
                        raise e from None
                    self.reset_tool_ledger()  # 摘要改写掉旧 read 结果, 行为账本同步失效
                    yield ("compact", 0, est_after)
                    continue
                else:
                    raise
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

            # 空回复重试(hermes _empty_content_retries 语义): 无 tool_use、无非空 text
            # 即为空(thinking-only 在 Anthropic 协议下不是合法终止态, 同样算空)。
            # 不 append 空消息直接重试(历史里不留空 assistant 块), 3 次仍空则落
            # 哨兵文本收尾; 每次重试发事件给 UI, 失败可见不静默。
            if not any(b.get("type") == "tool_use"
                       or (b.get("type") == "text" and b.get("text", "").strip())
                       for b in blocks):
                self._account(turn_in, turn_out)  # 空回复也烧了 token, 记账
                if self._empty_retries < 3:
                    self._empty_retries += 1
                    yield ("empty_retry", self._empty_retries)
                    continue
                note = "模型连续返回空回复(已重试 3 次), 本轮到此为止; 请重发消息再试"
                messages.append({"role": "assistant", "content": [{"type": "text", "text": note}]})
                yield ("empty_giveup", note)
                return
            self._empty_retries = 0
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
                if self._interrupt_requested:  # 检查点 c: 打断后批内剩余请求直接废弃
                    out = f"错误: {self._interrupt_requested}, 本次执行未完成"
                else:
                    tool = self.tools.get(name)
                    out = f"错误: 未知工具 {name}" if tool is None else self._dispatch(tool, args)
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": out})
                # UI 侧永远收文本; 图片分支的 block 数组原样进 messages(模型看图, UI 看说明文字)
                yield ("tool_result", call["id"], name, json.dumps(args, ensure_ascii=False), result_text(out))
            messages.append({"role": "user", "content": results})
            # postflight(hermes): 对话完校验, 超阈值就地压缩, 下一轮带上瘦身后的历史
            if not self._interrupt_requested:  # 已请求打断: 直接收尾, 不再花一次摘要调用
                for ev in self._compact_step(messages):
                    yield ev
        note = f"已达最大轮数 {self.max_turns}, 本轮暂时中止; 继续对话可让模型接着干"
        messages.append({"role": "assistant", "content": [{"type": "text", "text": note}]})
        yield ("turn_limit", self.max_turns)

    def _tool_loop(self) -> None:
        """工具 worker 主循环: 串行执行派来的 (tool, args), 结果放 box。"""
        while True:
            tool, args, box = self._tool_q.get()
            try:
                box["out"] = tool.run(args)
            except AbortError:  # 用户取消 ≠ 工具故障, 文案对齐打断收尾
                box["out"] = "错误: 用户取消, 本次执行未完成"
            except Exception as e:  # 工具崩了也回文本, 让模型重试
                box["out"] = f"错误: {e}"
            finally:
                box["done"].set()

    def _dispatch(self, tool, args: dict) -> str:
        """派发单个工具到 worker 线程, 0.1s 轮询打断标志 —— 打断立即废弃在途调用。
        废弃 = 结果不再取; 有 kill 钩子就强杀(execute 终止整个进程组)。
        worker 仍会把旧调用跑完自己丢弃; 下一轮第一个工具最坏等旧调用自然结束
        (mcp 单调用 60s 上限 / execute 已被钩子杀死; ponytail: 不做线程强杀)。"""
        box: dict = {"done": threading.Event()}
        self._tool_q.put((tool, args, box))
        while not box["done"].wait(0.1):
            if self._interrupt_requested:
                if tool.kill:
                    with contextlib.suppress(Exception):
                        tool.kill()
                return f"错误: {self._interrupt_requested}, 本次执行未完成"
        return box["out"]

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
            if self._interrupt_requested:  # 打断引发的 abort 错误不是摘要故障, 不熔断
                return []
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
