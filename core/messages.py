"""消息格式:直接用 Anthropic 协议的原生格式,不引入抽象层。

    {"role": "user" | "assistant", "content": str | [block, ...]}

block: {"type": "text"|"thinking"|"tool_use"|"tool_result", ...}
会话落盘、恢复、回放用的都是这一种格式 —— 对比 Penguin 的 OmniMessage 思路:
"流出去的 / 存下来的 / 模型看到的"是同一种东西,免去协议转换层。
"""

import json


def user(text: str) -> dict:
    return {"role": "user", "content": text}


def text_of(message: dict) -> str:
    """消息的正文(text 块拼接);纯字符串 content 原样返回。"""
    c = message.get("content", "")
    if isinstance(c, str):
        return c
    return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")


def tool_uses(message: dict) -> list[dict]:
    """assistant 消息里的 tool_use 块。"""
    c = message.get("content", [])
    if not isinstance(c, list):
        return []
    return [b for b in c if isinstance(b, dict) and b.get("type") == "tool_use"]


def est_tokens(messages: list[dict]) -> int:
    """粗估 token 数: JSON 长度 / 3(中英混排的经验值,压缩触发用,不求准)。"""
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 3
