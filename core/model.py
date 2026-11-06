"""模型薄封装:Anthropic 兼容端点(GLM 代理)的流式调用。

只用 POST {base}/v1/messages 一种调用, 不引 anthropic SDK —— 它拖着
pydantic 一大家子, Windows 上(安全软件过滤驱动放大每文件打开开销)导入 6s+。
裸 httpx + 手解 SSE, 事件直接是协议原生的 dict(单协议: 模型吐的/loop 聚合的/
会话落盘的是同一种 dict)。不做重试/降级 —— ponytail: 代理断流直接报错给上层,
加 tenacity 当真的频繁到需要。
"""

from __future__ import annotations

import json
import os

import httpx

_TIMEOUT = httpx.Timeout(600.0, connect=10.0)  # 长流式: read 放宽到 SDK 同款 600s


def make_client() -> httpx.Client:
    """兼容 SDK 行为的裸客户端: x-api-key + Bearer(GLM 代理只认 Bearer)。"""
    key = os.environ["ANTHROPIC_API_KEY"]
    return httpx.Client(
        base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/"),
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Authorization": f"Bearer {key}",
        },
        timeout=_TIMEOUT,
    )


def _sse_events(resp) -> dict:
    """手解 SSE: 按 data: 行取 JSON, 空行成帧。event: 行不看 —— type 在 data 里。"""
    data: list[str] = []
    for line in resp.iter_lines():
        line = line.rstrip("\r")
        if line.startswith(":"):  # 注释/keep-alive
            continue
        if line.startswith("data:"):
            data.append(line[5:].strip())
            continue
        if not line.strip() and data:  # 空行 = 帧结束
            payload = "\n".join(data)
            data = []
            if payload and payload != "[DONE]":
                yield json.loads(payload)


class ChatModel:
    def __init__(self, model: str, max_tokens: int = 8192, client=None):
        os.environ["ANTHROPIC_API_KEY"]  # fail-fast 前移: 缺密钥仍在 build 时报, 不等首条消息
        self._client = client
        self.model = model
        self.max_tokens = max_tokens
        self._current = None  # 在途流(UI 线程 abort 时立即关闭, 不等下一个 SSE 事件)

    @property
    def client(self):
        if self._client is None:
            self._client = make_client()
        return self._client

    def abort(self) -> None:
        """关闭在途流, 阻塞中的事件迭代立即抛错返回(hermes 及时打断的实现方式)。"""
        s = self._current
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    def stream(self, system: str, messages: list[dict], tools: list[dict] | None = None):
        """流式调一次模型, yield 协议原生事件 dict(message_start/content_block_*/
        message_delta, 字段同 Anthropic SSE 文档) —— 聚合逻辑在 loop.py。"""
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "stream": True,
            "system": system,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
        with self.client.stream("POST", "/v1/messages", json=body) as resp:
            if resp.status_code >= 400:
                # 读出错误体再抛: 代理的报错文案(配额/限流)要能透给 UI
                body_text = resp.read().decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {resp.status_code}: {body_text[:500]}")
            self._current = resp
            try:
                for ev in _sse_events(resp):
                    if ev.get("type") == "error":
                        raise RuntimeError(f"API error: {ev.get('error')}")
                    yield ev
            finally:
                self._current = None

    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> str:
        """单轮非流式语义, 实现走流式(注册进 _current)—— 压缩摘要等后台活也能被打断 abort。
        与 stream() 共用 _current 槽位; 调用方串行(都在 agent worker 线程), 不冲突。"""
        parts = []
        with self.client.stream(
            "POST", "/v1/messages",
            json={"model": self.model, "max_tokens": max_tokens, "stream": True,
                  "system": system, "messages": [{"role": "user", "content": prompt}]},
        ) as resp:
            if resp.status_code >= 400:
                body_text = resp.read().decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {resp.status_code}: {body_text[:500]}")
            self._current = resp
            try:
                for ev in _sse_events(resp):
                    if ev.get("type") == "content_block_delta" and ev["delta"]["type"] == "text_delta":
                        parts.append(ev["delta"]["text"])
            finally:
                self._current = None
        return "".join(parts)
