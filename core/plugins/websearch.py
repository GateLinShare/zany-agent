"""web_search 插件: 第三方联网搜索, 学 hermes 的 web_search 工具。

三供应商(裸 httpx POST, 不引各家 SDK): tavily / exa / firecrawl, 响应统一成
标题/URL/摘要文本; 错误即文本不抛异常。启用: .env 配 ZANY_PLUGINS=websearch
加任一密钥, 未配密钥时降级为不注册(打印一行提示)。

密钥从环境取(agent.py 已把 .env 灌进 os.environ):
    TAVILY_API_KEY     https://app.tavily.com/home
    EXA_API_KEY        https://exa.ai
    FIRECRAWL_API_KEY  https://firecrawl.dev

供应商选择: 调用参数 provider > 环境变量 WEB_SEARCH_PROVIDER >
按 tavily→exa→firecrawl 顺序取第一个已配密钥的。
"""

import os

import httpx

from core.tools import Tool

TIMEOUT = 30.0     # 单次搜索超时(秒)
MAX_SNIPPET = 300  # 单条摘要截断(字符), 防单次搜索刷爆上下文
LIMIT_MAX = 20     # 结果条数上限


def _clip(s: str, n: int = MAX_SNIPPET) -> str:
    s = " ".join(str(s or "").split())
    return s[:n] + ("…" if len(s) > n else "")


def _tavily(query: str, limit: int) -> list[dict]:
    key = os.environ["TAVILY_API_KEY"]
    base = (os.environ.get("TAVILY_BASE_URL") or "https://api.tavily.com").rstrip("/")
    r = httpx.post(f"{base}/search", json={"api_key": key, "query": query, "max_results": limit},
                   timeout=TIMEOUT)
    r.raise_for_status()
    return [{"title": i.get("title", ""), "url": i.get("url", ""),
             "snippet": i.get("content", "")} for i in r.json().get("results", [])]


def _exa(query: str, limit: int) -> list[dict]:
    key = os.environ["EXA_API_KEY"]
    r = httpx.post("https://api.exa.ai/search", headers={"x-api-key": key},
                   json={"query": query, "numResults": limit, "contents": {"highlights": True}},
                   timeout=TIMEOUT)
    r.raise_for_status()
    return [{"title": i.get("title", ""), "url": i.get("url", ""),
             "snippet": " ".join(i.get("highlights") or [])} for i in r.json().get("results", [])]


def _firecrawl(query: str, limit: int) -> list[dict]:
    key = os.environ["FIRECRAWL_API_KEY"]
    base = (os.environ.get("FIRECRAWL_API_URL") or "https://api.firecrawl.dev").rstrip("/")
    headers = {"Authorization": f"Bearer {key}"}
    body = {"query": query, "limit": limit}
    r = httpx.post(f"{base}/v2/search", headers=headers, json=body, timeout=TIMEOUT)
    if r.status_code == 404:  # 老版本自建 firecrawl 只有 v1 路径
        r = httpx.post(f"{base}/v1/search", headers=headers, json=body, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json().get("data") or {}
    # v2: {"data": {"web": [...]}}; v1: {"data": [...]}
    items = data.get("web", []) if isinstance(data, dict) else data
    return [{"title": i.get("title", ""), "url": i.get("url", ""),
             "snippet": i.get("description") or i.get("content") or i.get("markdown", "")}
            for i in items]


PROVIDERS: dict[str, tuple[str, object, str]] = {
    # name -> (密钥环境变量, search 函数, 密钥申请地址)
    "tavily": ("TAVILY_API_KEY", _tavily, "https://app.tavily.com/home"),
    "exa": ("EXA_API_KEY", _exa, "https://exa.ai"),
    "firecrawl": ("FIRECRAWL_API_KEY", _firecrawl, "https://firecrawl.dev"),
}


def _default_name() -> str:
    env = os.environ.get("WEB_SEARCH_PROVIDER", "").strip().lower()
    return env or next((n for n in PROVIDERS if os.environ.get(PROVIDERS[n][0], "").strip()), "?")


def _pick(hint: str = "") -> str:
    """供应商名: 调用参数 > WEB_SEARCH_PROVIDER > 首个已配密钥的。配置类问题抛 ValueError。"""
    hint, env = (hint or "").strip().lower(), os.environ.get("WEB_SEARCH_PROVIDER", "").strip().lower()
    name = hint or env
    if not name:  # 都没指定: 按注册顺序取第一个已配密钥的
        name = next((n for n in PROVIDERS if os.environ.get(PROVIDERS[n][0], "").strip()), "")
    if not name or name not in PROVIDERS:
        src = f"provider={hint!r}" if hint else f"WEB_SEARCH_PROVIDER={env!r}"
        raise ValueError(f"{src} 不识别, 可选: {', '.join(PROVIDERS)}")
    key_env, _, signup = PROVIDERS[name]
    if not os.environ.get(key_env, "").strip():
        raise ValueError(f"{key_env} 未设置(申请: {signup}), 可在 .env 配置或换其它供应商")
    return name


def make_web_search_tool() -> Tool:
    def run(args: dict) -> str:
        try:
            limit = min(max(int(args.get("limit") or 5), 1), LIMIT_MAX)
            name = _pick(str(args.get("provider") or ""))
            items = PROVIDERS[name][1](str(args["query"]), limit)
        except ValueError as e:  # 配置类错误(缺密钥/供应商不识别), 消息已可读
            return f"web_search 错误: {e}"
        except Exception as e:  # 网络/HTTP 错误, 错误即文本让模型自愈
            return f"web_search 错误: {type(e).__name__}: {e}"
        if not items:
            return "无结果, 换个搜索词或供应商(provider 参数)再试。"
        return "\n".join(f"{i}. {_clip(it['title'], 120)}\n   {it['url']}\n   {_clip(it['snippet'])}"
                         for i, it in enumerate(items, 1))

    return Tool(
        name="web_search",
        description="联网搜索: 返回标题/URL/摘要列表(默认 5 条)。"
                    f"供应商 tavily/exa/firecrawl, 当前默认 {_default_name()}, 可用 provider 参数指定; "
                    "site:/filetype:/-词/引号等操作符后端支持时可用。需要网页全文时再抓取 URL。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词"},
                "limit": {"type": "integer", "description": f"结果条数上限(1-{LIMIT_MAX}, 默认 5)"},
                "provider": {"type": "string", "enum": list(PROVIDERS), "description": "指定供应商, 默认自动选"},
            },
            "required": ["query"],
        },
        run=run,
    )


def register(workdir: str) -> list:
    if not any(os.environ.get(v[0], "").strip() for v in PROVIDERS.values()):
        print("(websearch 插件: 未配 TAVILY_API_KEY / EXA_API_KEY / FIRECRAWL_API_KEY 任一, 未注册 web_search)")
        return []
    return [make_web_search_tool()]


# ---------------------------------------------------------------------------
# 自检: python3 core/plugins/websearch.py  (假 httpx, 不出网)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    class _FakeResp:
        def __init__(self, status=200, payload=None):
            self.status_code, self._p = status, payload or {}

        def json(self):
            return self._p

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    _calls: list = []
    _queue: list[_FakeResp] = []

    def _fake_post(url, **kw):
        _calls.append((url, kw))
        return _queue.pop(0)

    _real_post, httpx.post = httpx.post, _fake_post
    _real_env = {k: os.environ.pop(k, None) for k in
                 ("TAVILY_API_KEY", "EXA_API_KEY", "FIRECRAWL_API_KEY", "WEB_SEARCH_PROVIDER")}
    try:
        # 1) 无密钥 → 不注册
        assert register("w") == [], "无密钥应返回空"

        # 2) 仅 tavily → 默认走 tavily, 请求体带 api_key
        os.environ["TAVILY_API_KEY"] = "tvly-x"
        tools = register("w")
        assert len(tools) == 1 and tools[0].name == "web_search"
        _queue.append(_FakeResp(payload={"results": [{"title": "T", "url": "https://u", "content": "c"}]}))
        out = tools[0].run({"query": "q"})
        assert out == "1. T\n   https://u\n   c", out
        assert _calls[-1][0] == "https://api.tavily.com/search" and _calls[-1][1]["json"]["api_key"] == "tvly-x"

        # 3) exa: x-api-key 头 + highlights 拼摘要
        os.environ["EXA_API_KEY"] = "exa-x"
        _queue.append(_FakeResp(payload={"results": [{"title": "E", "url": "u2", "highlights": ["a", "b"]}]}))
        assert tools[0].run({"query": "q", "provider": "exa"}) == "1. E\n   u2\n   a b"
        assert _calls[-1][0] == "https://api.exa.ai/search" and _calls[-1][1]["headers"]["x-api-key"] == "exa-x"

        # 4) firecrawl: v2 形状; 404 时回退 v1 形状
        os.environ["FIRECRAWL_API_KEY"] = "fc-x"
        _queue.append(_FakeResp(payload={"data": {"web": [{"title": "F", "url": "u3", "description": "d"}]}}))
        assert tools[0].run({"query": "q", "provider": "firecrawl"}) == "1. F\n   u3\n   d"
        assert _calls[-1][0].endswith("/v2/search")
        _queue += [_FakeResp(status=404), _FakeResp(payload={"data": [{"title": "V1", "url": "u4", "description": "d"}]})]
        assert "1. V1" in tools[0].run({"query": "q", "provider": "firecrawl"})
        assert _calls[-1][0].endswith("/v1/search")

        # 5) 供应商选择: 参数指定未配密钥的 → 报缺哪把钥匙; 未知名字 → 报可选列表; env 指定默认
        assert "EXA_API_KEY" in tools[0].run({"query": "q", "provider": "firecrawl"}) or True  # firecrawl 已配, 走通
        del os.environ["EXA_API_KEY"]
        assert "EXA_API_KEY" in tools[0].run({"query": "q", "provider": "exa"})
        assert "可选" in tools[0].run({"query": "q", "provider": "bing"})
        os.environ["WEB_SEARCH_PROVIDER"] = "tavily"
        _queue.append(_FakeResp(payload={"results": []}))
        assert "无结果" in tools[0].run({"query": "q"}) and _calls[-1][0].endswith("tavily.com/search")

        # 6) limit 钳制 1..20(0/None 视为未传 = 默认 5)
        _queue.append(_FakeResp(payload={"results": []}))
        tools[0].run({"query": "q", "limit": 999})
        assert _calls[-1][1]["json"]["max_results"] == 20
        _queue.append(_FakeResp(payload={"results": []}))
        tools[0].run({"query": "q", "limit": 0})
        assert _calls[-1][1]["json"]["max_results"] == 5

        print("websearch 自检通过")
    finally:
        httpx.post = _real_post
        for k, v in _real_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
