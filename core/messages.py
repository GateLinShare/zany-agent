"""消息格式:直接用 Anthropic 协议的原生格式,不引入抽象层。

    {"role": "user" | "assistant", "content": str | [block, ...]}

block: {"type": "text"|"thinking"|"tool_use"|"tool_result", ...}
会话落盘、恢复、回放用的都是这一种格式 —— 对比 Penguin 的 OmniMessage 思路:
"流出去的 / 存下来的 / 模型看到的"是同一种东西,免去协议转换层。
"""

import base64
import json
import re
from pathlib import Path

_IMG_REF = re.compile(r"\[图片:\s*(.+?)\s*\]")


def user(text: str) -> dict:
    return {"role": "user", "content": user_content(text)}


def user_content(text: str) -> str | list:
    """把文本里的 [图片: 路径] 占位符换成 image 块(Anthropic 原生格式);
    无图时原样返回字符串。读不到的文件降级成提示文本, 不炸提交。"""
    if not _IMG_REF.search(text):
        return text
    blocks, pos = [], 0
    for m in _IMG_REF.finditer(text):
        if text[pos:m.start()].strip():
            blocks.append({"type": "text", "text": text[pos:m.start()].strip()})
        path = m.group(1)
        try:
            data = Path(path).read_bytes()
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.b64encode(data).decode()}})
        except OSError:
            blocks.append({"type": "text", "text": f"(图片读取失败: {path})"})
        pos = m.end()
    if text[pos:].strip():
        blocks.append({"type": "text", "text": text[pos:].strip()})
    return blocks


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
    """粗估 token 数: JSON 长度 / 3(中英混排的经验值,压缩触发用,不求准)。
    image 块不计 base64(会虚高几个数量级), 每图按 ~1500 token 估。"""
    total = 0
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            total += len(json.dumps(m, ensure_ascii=False))
            continue
        for b in c:
            if isinstance(b, dict) and b.get("type") == "image":
                total += 4500  # ponytail: 单图固定 1500 token 的 3 倍字符口径
            else:
                total += len(json.dumps(b, ensure_ascii=False))
    return total // 3
