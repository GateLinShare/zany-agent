"""上下文压缩(改写式):token 超阈值就把较早历史换成一条摘要。

选改写式而非 deepagents 默认的"非破坏式"(原文 offload + 私有状态)的原因:
本项目的持久化是 sessions/*.json 直接覆盖写, 没有 checkpointer —— 改写式压缩的
结果天然落在 messages 里, 落盘/恢复零依赖。全量原文仍留在历史 session 文件里。

触发判定学 hermes(context_engine.py 生命周期):
- postflight: 对话完(每轮响应后)用 API 返回的真实 prompt_tokens 校验 —— 准;
- preflight 兜底: 拿不到真实值(None/会话恢复首轮)回落 字符数/3 粗估,
  字符数天然高估, 往"提前压缩"的方向错, 保守方向正确。
不引 tokenizer, 阈值本身就是粗粒度的。
"""

import os

from .messages import est_tokens

SUMMARY_PROMPT = "把以下对话历史压缩成一份摘要, 保留: 用户的目标与约束、已完成的关键操作与结论、未完成事项、文件路径。直接输出摘要正文。\n\n"

# .env 可配(agent.py 先于 core 导入加载): 触发点 = 窗口上限 × 比例, 未配置 0.6
TRIGGER_TOKENS = int(int(os.environ.get("ANTHROPIC_CONTEXT_TOKENS", 200000))
                     * float(os.environ.get("COMPACT_TRIGGER_RATIO", 0.6)))


def maybe_summarize(
    model,
    messages: list[dict],
    prompt_tokens: int | None = None,
    trigger_tokens: int = TRIGGER_TOKENS,
    keep: int = 20,
) -> tuple[bool, int, int]:
    """超阈值时改写 messages(原地)为 [user(摘要)] + 尾部 keep 条。

    prompt_tokens: 上一次响应的真实 prompt token 数(含 cache 桶);
    None 时用 est_tokens 粗估。
    返回 (是否压缩, 判定用 token, 压缩前 est, 压缩后 est)。
    防抖对比必须同口径(est vs est); 判定值可以是真实 prompt_tokens。
    """
    est_before = est_tokens(messages)
    tokens = prompt_tokens if prompt_tokens is not None else est_before
    if tokens <= trigger_tokens:
        return False, tokens, est_before, est_before
    cut = max(0, len(messages) - keep)
    # 切分点不能落在工具调用对中间: 开头若是 tool_result 就往后挪, 防孤儿结果
    while cut < len(messages):
        c = messages[cut].get("content")
        if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            cut += 1
            continue
        break
    if cut == 0:
        return False, tokens, est_before, est_before
    stale = messages[:cut]
    transcript = "\n".join(
        f"[{m['role']}] {m['content'] if isinstance(m['content'], str) else _flat(m['content'])}"
        for m in stale
    )
    summary = model.complete("", SUMMARY_PROMPT + transcript)
    messages[:] = [{"role": "user", "content": f"(以下是此前对话的摘要)\n\n{summary}"}] + messages[cut:]
    return True, tokens, est_before, est_tokens(messages)


def _flat(blocks: list) -> str:
    """压缩用的一维化: thinking 块丢弃, tool_use 只留名字, tool_result 留前 500 字。"""
    parts = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "image":
            parts.append("(图片)")
        elif t == "text":
            parts.append(b.get("text", ""))
        elif t == "tool_use":
            parts.append(f"(调用工具 {b.get('name')})")
        elif t == "tool_result":
            parts.append(f"(工具结果) {str(b.get('content', ''))[:500]}")
    return "\n".join(parts)
