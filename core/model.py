"""模型薄封装:Anthropic 兼容端点(GLM 代理)的流式调用。

只做三件事:Bearer 代理头、流式事件转发、非流式补全(压缩摘要用)。
不做重试/降级 —— ponytail: 代理断流直接报错给上层,加 tenacity 当真的频繁到需要。
"""

import os

import anthropic


def make_client() -> anthropic.Anthropic:
    key = os.environ["ANTHROPIC_API_KEY"]
    return anthropic.Anthropic(
        base_url=os.environ.get("ANTHROPIC_BASE_URL"),
        api_key=key,
        # 代理只认 Authorization: Bearer, anthropic SDK 默认发 x-api-key
        default_headers={"Authorization": f"Bearer {key}"},
    )


class ChatModel:
    def __init__(self, client: anthropic.Anthropic, model: str, max_tokens: int = 8192):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    def stream(self, system: str, messages: list[dict], tools: list[dict] | None = None):
        """流式调一次模型, yield 原始 SSE 事件(content_block_start/delta/stop 等),
        聚合逻辑在 loop.py —— 模型层保持无状态。"""
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools
        with self.client.messages.stream(**kwargs) as s:
            yield from s  # MessageStream 迭代即 RawMessageStreamEvent

    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> str:
        """非流式单轮, 给摘要/判分等后台活用。"""
        r = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
