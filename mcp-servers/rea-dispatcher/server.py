"""rea-dispatcher — 把 REA(morluto/rea, npm 包 rea-agents, ~207 个逆向工具)折叠成
3 个瘦工具的本地 MCP server, 避免全量工具 schema(约 775KB ≈ 20 万 token)常驻宿主上下文。

模型侧工具:
  rea_tools(filter="")        列 REA 工具名 + 首行说明
  rea_help(name)              单工具的参数 schema 与完整说明
  rea_call(name, arguments)   调用真工具, 参数原样透传

运行: uv run --with mcp server.py   (stdio MCP)
依赖: npm i -g rea-agents; GHIDRA_INSTALL_DIR / JAVA_HOME 由宿主 env 透传(没配时
      只有依赖引擎的工具会报 REA 自己的错, 列目录/JS/Electron 分析不受影响)。
"""

import asyncio
import json
import os
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server import MCPServer

# ponytail: 每次工具调用都现拉一个 REA 子进程(宿主 mcp.py 本来就每调用重连),
# 慢 3-5s 换零状态管理; 出现深挖单一二进制的长会话需求再加常驻 REA。
CALL_TIMEOUT = 540   # 宿主侧 spec timeout 建议 600, 这里留头
LIST_TIMEOUT = 120
MAX_BYTES = int(os.environ.get("REA_MAX_BYTES", "20000"))  # 单次返回上限, 超出落盘
SPILL_DIR = Path(os.environ.get("REA_SPILL_DIR", str(Path.home() / ".zany" / "rea-spill")))

mcp = MCPServer("rea-dispatcher")


def _rea_mjs() -> str:
    """定位 rea-agents 的 MCP 入口 scripts/rea.mjs: REA_MJS 环境变量优先, 再 npm root -g。"""
    hit = os.environ.get("REA_MJS")
    if hit and Path(hit).is_file():
        return hit
    root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=60).stdout.strip()
    cand = Path(root) / "rea-agents" / "scripts" / "rea.mjs"
    if cand.is_file():
        return str(cand)
    raise RuntimeError(f"未找到 rea-agents 的 MCP 入口({cand} 不存在): 先 npm i -g rea-agents, 或设 REA_MJS")


@asynccontextmanager
async def _rea():
    """现拉一个 REA MCP 子进程, 会话内 initialize 后 yield ClientSession。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command="node", args=[_rea_mjs(), "--mcp"], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _spill(text: str) -> str:
    """超 MAX_BYTES 截断, 全文落盘, 头 40% + 尾 60%(错误在头、新输出在尾的惯例)。"""
    if len(text.encode()) <= MAX_BYTES:
        return text
    SPILL_DIR.mkdir(parents=True, exist_ok=True)
    path = SPILL_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.txt"
    path.write_text(text, encoding="utf-8")
    head, tail = text[: MAX_BYTES * 2 // 5], text[-MAX_BYTES * 3 // 5:]
    return f"{head}\n...[截断, 全文 {len(text)} 字符落盘于 {path}]...\n{tail}"


def _err_text(e: BaseException) -> str:
    if getattr(e, "exceptions", None):  # anyio TaskGroup 壳, 真话在叶子
        return "; ".join(_err_text(x) for x in e.exceptions)
    return getattr(e, "message", None) or str(e)


def _first_line(desc: str | None, cap: int = 120) -> str:
    d = " ".join((desc or "").strip().split())  # REA 说明是一大段, 压平后截短, 目录才小得下来
    return d[:cap] + ("…" if len(d) > cap else "") if d else "(无说明)"


@mcp.tool()
async def rea_tools(filter: str = "") -> str:
    """列出 REA 逆向工具集(~207 个: native/Ghidra、APK/JADX、JS/Electron、固件、网站)的工具名与一句话说明, 按名称子串过滤。用法: rea_help 查单个工具, rea_call 调用。"""
    async with _rea() as s:
        tools = (await asyncio.wait_for(s.list_tools(), LIST_TIMEOUT)).tools
    lines = [f"{t.name}: {_first_line(t.description)}" for t in tools if filter.lower() in t.name.lower()]
    return _spill("\n".join(lines) or f"无匹配: {filter}")


@mcp.tool()
async def rea_help(name: str) -> str:
    """查单个 REA 工具的必填参数、参数 schema 与完整说明(含已知陷阱)。"""
    async with _rea() as s:
        tools = (await asyncio.wait_for(s.list_tools(), LIST_TIMEOUT)).tools
    t = next((t for t in tools if t.name == name), None)
    if t is None:
        near = [x.name for x in tools if name.lower() in x.name.lower()][:10]
        return f"REA 无此工具: {name}。名称近似的: {near or [x.name for x in tools][:20]}"
    return _spill(f"# {t.name}\n{t.description or ''}\n\n参数 schema:\n"
                  f"{json.dumps(t.input_schema or {}, ensure_ascii=False, indent=1)}")


@mcp.tool()
async def rea_call(name: str, arguments: dict | None = None) -> str:
    """调用一个 REA 逆向工具: name=工具名, arguments=参数对象(结构用 rea_help 查)。错误原样返回。"""
    from mcp.types import TextContent

    try:
        async with _rea() as s:
            res = await asyncio.wait_for(s.call_tool(name, arguments or {}), CALL_TIMEOUT)
    except Exception as e:  # REA 对未知工具/坏参数回 JSON-RPC error, 客户端抛 McpError — 透传真话
        return f"REA error: {_err_text(e)}"
    text = "\n".join(b.text for b in res.content if isinstance(b, TextContent)) or "(无文本返回)"
    return ("REA error:\n" if res.is_error else "") + _spill(text)


if __name__ == "__main__":
    mcp.run()
