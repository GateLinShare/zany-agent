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
        self._current = None  # 在途流(UI 线程 abort 时立即关闭, 不等下一个 SSE 事件)

    def abort(self) -> None:
        """关闭在途流, 阻塞中的事件迭代立即抛错返回(hermes 及时打断的实现方式)。"""
        s = self._current
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

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
            self._current = s
            try:
                yield from s  # MessageStream 迭代即 RawMessageStreamEvent
            finally:
                self._current = None

    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> str:
        """单轮非流式语义, 实现走流式(注册进 _current)—— 压缩摘要等后台活也能被打断 abort。
        与 stream() 共用 _current 槽位; 调用方串行(都在 agent worker 线程), 不冲突。"""
        parts = []
        with self.client.messages.stream(
            model=self.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": prompt}],
        ) as s:
            self._current = s
            try:
                for chunk in s.text_stream:
                    parts.append(chunk)
            finally:
                self._current = None
        return "".join(parts)
