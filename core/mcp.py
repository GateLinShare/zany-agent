"""MCP 工具加载:mcp_servers.json 声明的服务器, 工具归一成 core.tools.Tool。

mcp SDK 只当协议客户端用(JSON-RPC 握手/传输), 不引任何 agent 框架。
每次调用即开即用重连一次 —— 简单、无连接池状态;工具调用低频, 慢一点无所谓。
"""

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

from .tools import Tool

MCP_CONFIG = Path(__file__).resolve().parent.parent / "mcp_servers.json"
PROBE_TIMEOUT = 15   # 单台服务器握手+list_tools 超时: uvx 拉包卡 pypi 时快速认输
CALL_TIMEOUT = 60    # 工具实际调用超时(抓网页可能慢, 放宽)

MCP_CONFIG = Path(__file__).resolve().parent.parent / "mcp_servers.json"


def config_stamp() -> float:
    """mcp_servers.json 的 mtime(不存在返回 0), TUI 每轮对比实现热重载。"""
    return MCP_CONFIG.stat().st_mtime if MCP_CONFIG.exists() else 0.0


@contextlib.contextmanager
def _stderr_sink(spec: dict):
    """stdio 子进程的 stderr 默认收进 devnull(uvx 拉包时的 spinner/报错对用户是噪音,
    失败已由跳过提示承担); 设 MCP_STDERR=1 可透传排障。"""
    if "url" in spec or os.environ.get("MCP_STDERR"):
        yield sys.stderr
        return
    with open(os.devnull, "w") as sink:
        yield sink


@contextlib.asynccontextmanager
async def _server_cm(spec: dict):
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    if "url" in spec:  # 远程只支持 streamable-http(本地代理场景用不到 sse)
        from mcp.client.streamable_http import streamable_http_client

        if spec.get("headers"):
            import httpx

            cm = streamable_http_client(spec["url"], http_client=httpx.AsyncClient(headers=spec["headers"]))
        else:
            cm = streamable_http_client(spec["url"])
        async with cm as (read, write):
            yield read, write
        return
    with _stderr_sink(spec) as sink:
        cm = stdio_client(
            StdioServerParameters(command=spec["command"], args=spec.get("args", []),
                                  env={**os.environ, **spec.get("env", {})}),
            errlog=sink,
        )
        async with cm as (read, write):
            yield read, write


async def _call(spec: dict, tool_name: str, args: dict):
    from mcp import ClientSession

    async with _server_cm(spec) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await asyncio.wait_for(session.call_tool(tool_name, args), CALL_TIMEOUT)


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

    async def _probe(name: str, spec: dict) -> list:
        from mcp import ClientSession

        async with _server_cm(spec) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return (await session.list_tools()).tools

    async def _collect():
        tools = []
        for name, spec in servers.items():
            try:
                raws = await asyncio.wait_for(_probe(name, spec), PROBE_TIMEOUT)
                for t in raws:
                    # 闭包捕获 spec/name 的浅拷贝, 防 async 域结束后变量复用
                    tools.append(_make_tool(name, dict(spec), t))
            except Exception as e:  # 单台失败不拖垮其余
                errs = getattr(e, "exceptions", None) or [e]
                print(f"(MCP 服务器 {name} 加载失败, 跳过: {'; '.join(repr(x) for x in errs)})")
        return tools

    try:
        return asyncio.run(_collect())
    except Exception as e:  # TaskGroup 报的是异常组壳, 真因在 .exceptions 里
        errs = getattr(e, "exceptions", None) or [e]
        print("(MCP 工具加载失败, 跳过: " + "; ".join(repr(x) for x in errs) + ")")
        return []
