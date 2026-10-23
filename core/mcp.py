"""MCP 工具加载:mcp_servers.json 声明的服务器, 工具归一成 core.tools.Tool。

mcp SDK 只当协议客户端用(JSON-RPC 握手/传输), 不引任何 agent 框架。
每次调用即开即用重连一次 —— 简单、无连接池状态;工具调用低频, 慢一点无所谓。
"""

import asyncio
import json
import os
from pathlib import Path

from .tools import Tool

MCP_CONFIG = Path(__file__).resolve().parent.parent / "mcp_servers.json"


def config_stamp() -> float:
    """mcp_servers.json 的 mtime(不存在返回 0), TUI 每轮对比实现热重载。"""
    return MCP_CONFIG.stat().st_mtime if MCP_CONFIG.exists() else 0.0


def _server_cm(spec: dict):
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    if "url" in spec:  # 远程只支持 streamable-http(本地代理场景用不到 sse)
        from mcp.client.streamable_http import streamable_http_client

        if spec.get("headers"):
            import httpx

            return streamable_http_client(spec["url"], http_client=httpx.AsyncClient(headers=spec["headers"]))
        return streamable_http_client(spec["url"])
    return stdio_client(
        StdioServerParameters(command=spec["command"], args=spec.get("args", []),
                              env={**os.environ, **spec.get("env", {})})
    )


async def _call(spec: dict, tool_name: str, args: dict):
    from mcp import ClientSession

    async with _server_cm(spec) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool_name, args)


def _make_tool(server_name: str, spec: dict, raw) -> Tool:
    def run(args: dict) -> str:
        try:
            result = asyncio.run(_call(spec, raw.name, args))
        except Exception as e:
            return f"MCP tool '{raw.name}' 失败: {e}"
        parts = [b.text if getattr(b, "type", None) == "text" else str(b) for b in result.content]
        text = "\n".join(parts)
        return f"MCP tool '{raw.name}' error: {text}" if result.is_error else text

    return Tool(
        name=raw.name,
        description=f"[mcp:{server_name}] {raw.description or ''}",
        input_schema=raw.input_schema or {"type": "object", "properties": {}},
        run=run,
    )


def load_mcp_tools() -> list[Tool]:
    """拉取全部 MCP 工具; 没配置文件或为空返回 []。"""
    if not MCP_CONFIG.exists():
        return []
    servers = json.loads(MCP_CONFIG.read_text(encoding="utf-8")).get("mcpServers", {})

    async def _collect():
        from mcp import ClientSession

        tools = []
        for name, spec in servers.items():
            try:
                async with _server_cm(spec) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        for t in (await session.list_tools()).tools:
                            # 闭包捕获 spec/name 的浅拷贝, 防 async 域结束后变量复用
                            tools.append(_make_tool(name, dict(spec), t))
            except BaseException as e:  # 单台失败不拖垮其余
                errs = getattr(e, "exceptions", None) or [e]
                print(f"(MCP 服务器 {name} 加载失败, 跳过: {'; '.join(repr(x) for x in errs)})")
        return tools

    try:
        return asyncio.run(_collect())
    except BaseException as e:  # TaskGroup 报的是异常组壳, 真因在 .exceptions 里
        errs = getattr(e, "exceptions", None) or [e]
        print("(MCP 工具加载失败, 跳过: " + "; ".join(repr(x) for x in errs) + ")")
        return []
