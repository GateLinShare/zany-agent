#!/usr/bin/env python3
"""MCP 工具加载:从 mcp_servers.json 拉工具包装成 LangChain 工具。

不用 langchain-mcp-adapters:它只支持 mcp 1.x,而 dcode 依赖 mcp>=2。

mcp_servers.json 示例(三种传输):
    {"mcpServers": {
        "fetch":  {"command": "uvx", "args": ["mcp-server-fetch"]},              # stdio(本地)
        "remote": {"url": "https://host/mcp"},                                    # streamable-http
        "old":    {"url": "https://host/sse", "transport": "sse",
                   "headers": {"Authorization": "Bearer xxx"}}                    # SSE + 请求头
    }}
"""

import asyncio
import json
import os
from pathlib import Path

from langchain_core.tools import StructuredTool

MCP_CONFIG = Path(__file__).resolve().parent / "mcp_servers.json"


def _server_cm(spec: dict):
    """按配置返回 yielding (read, write) 的 async 上下文管理器。"""
    from mcp import StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client

    if "url" in spec:  # 远程
        from mcp.client.streamable_http import streamable_http_client

        headers = spec.get("headers")
        if spec.get("transport", "streamable-http") == "sse":
            return sse_client(spec["url"], headers=headers)
        if headers:
            import httpx2

            client = httpx2.AsyncClient(headers=headers)
            return streamable_http_client(spec["url"], http_client=client)
        return streamable_http_client(spec["url"])

    # 本地 stdio
    return stdio_client(
        StdioServerParameters(
            command=spec["command"],
            args=spec.get("args", []),
            env={**os.environ, **spec.get("env", {})},
        )
    )


def config_stamp() -> float:
    """mcp_servers.json 的 mtime(不存在返回 0)。REPL 每轮对比,变了就重建 agent。"""
    return MCP_CONFIG.stat().st_mtime if MCP_CONFIG.exists() else 0.0


def load_mcp_tools():
    """返回 MCP 工具列表;没配置文件或为空就返回 []."""
    if not MCP_CONFIG.exists():
        return []
    servers = json.loads(MCP_CONFIG.read_text()).get("mcpServers", {})
    if not servers:
        return []
    from mcp import ClientSession

    async def _collect():
        tools = []
        for name, spec in servers.items():
            async with _server_cm(spec) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for tool in (await session.list_tools()).tools:
                        schema = tool.input_schema or {"type": "object", "properties": {}}
                        tools.append(
                            StructuredTool(
                                name=tool.name,
                                description=tool.description or "",
                                args_schema=schema,
                                # 会话按工具调用即开即用;每次重连,简单但慢一点
                                func=_make_sync_call(spec, tool.name),
                                coroutine=_make_call(spec, tool.name),
                            )
                        )
        return tools

    return asyncio.run(_collect())


def _make_sync_call(spec, tool_name: str):
    def call(**kwargs):
        return asyncio.run(_make_call(spec, tool_name)(**kwargs))

    return call


def _make_call(spec, tool_name: str):
    async def call(**kwargs):
        from mcp import ClientSession

        async with _server_cm(spec) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, kwargs)
        parts = []
        for block in result.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
            else:
                parts.append(str(block))
        text = "\n".join(parts)
        return f"MCP tool '{tool_name}' error: {text}" if result.is_error else text

    return call


if __name__ == "__main__":
    tools = load_mcp_tools()
    print("MCP tools:", [t.name for t in tools] or "(mcp_servers.json 为空或不存在)")
