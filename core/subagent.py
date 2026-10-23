"""task 子代理:general-purpose 子代理跑独立子任务, 只把结论带回主对话。

工具和主代理同一套(builtin + MCP), 唯独不给 task —— 子代理不再派子代理, 防递归。
"""

from .loop import Agent
from .messages import text_of
from .tools import Tool, builtin_tools


def make_task_tool(model, system_base: str, workdir: str, mcp_tools: list) -> Tool:
    def run(args: dict) -> str:
        inner = Agent(model, system_base, builtin_tools(workdir) + list(mcp_tools), workdir)
        msgs = [{"role": "user", "content": args["prompt"]}]
        try:
            inner.invoke(msgs)
            return text_of(msgs[-1]) or "(子代理无文本输出)"
        except Exception as e:
            return f"task 子代理失败: {e}"

    return Tool(
        name="task",
        description="派 general-purpose 子代理独立跑子任务(检索、批量评测等), prompt 写清目标和约束, 只返回结论文本。",
        input_schema={
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "子任务完整描述"}},
            "required": ["prompt"],
        },
        run=run,
    )
