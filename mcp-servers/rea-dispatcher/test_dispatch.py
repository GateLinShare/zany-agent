"""自测: 直接对 dispatcher 走一遍 rea_tools → rea_help → rea_call 链路。
跑法: cd rea-dispatcher && uv run --with mcp python test_dispatch.py
"""

import asyncio
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PARAMS = StdioServerParameters(
    command="uv", args=["run", "--with", "mcp", os.path.join(os.path.dirname(__file__), "server.py")],
    env=dict(os.environ),
)


async def main():
    async with stdio_client(PARAMS) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            names = sorted(t.name for t in tools)
            assert names == ["rea_call", "rea_help", "rea_tools"], names
            print("[1] 模型侧工具:", names)

            res = (await s.call_tool("rea_tools", {"filter": "image"})).content[0].text
            print(f"[2] rea_tools(filter=image) {len(res.splitlines())} 条, 前 3 行:")
            print("\n".join(l[:150] for l in res.splitlines()[:3]))
            assert "inspect" in res or "image" in res.lower(), res[:200]

            first = res.splitlines()[0].split(":")[0].strip()
            res = (await s.call_tool("rea_help", {"name": first})).content[0].text
            print(f"[3] rea_help({first}) 前 8 行:")
            print("\n".join(res.splitlines()[:8]))

            res = (await s.call_tool("rea_call", {"name": "no_such_tool_xyz", "arguments": {}})).content[0].text
            print("[4] rea_call 未知工具(应报 REA 错误而非崩):", res[:200])
            assert "error" in res.lower() or "无" in res or "unknown" in res.lower()

    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
