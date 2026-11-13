"""task 子代理:general-purpose 子代理跑独立子任务, 只把结论带回主对话。

工具和主代理同一套(builtin + MCP), 唯独不给 task —— 子代理不再派子代理, 防递归。
"""

from .loop import Agent
from .messages import text_of
from .model import ChatModel
from .tools import Tool, builtin_tools


def make_task_tool(model, system_base: str, workdir: str, mcp_tools: list,
                   shared_interrupt: dict | None = None) -> Tool:
    active: set = set()  # 在途子代理的 model: kill 钩子逐个 abort(SSE 长流阻塞中的也能立即断)

    def run(args: dict) -> str:
        # 每个 task 调用一份独立 ChatModel(共享 httpx client, 连接池复用):
        # ChatModel._current 是单槽, 共享实例时并行子代理互相覆盖 ——
        # 主对话 abort() 关错流、子代理的流反而关不掉。测试假模型非
        # ChatModel, 原样直通。ponytail: clone 而非新建 client ——
        # httpx.Client 线程安全, 没必要多连接池。
        sub_model = (ChatModel(model.model, model.max_tokens, client=model.client)
                     if isinstance(model, ChatModel) else model)
        active.add(sub_model)
        try:
            # 子代理与主代理共享打断标志: 主对话 interrupt 时子代理的检查点 a/b/c 同步生效,
            # 不会在 worker 线程里继续把子任务跑完烧 token。
            inner = Agent(sub_model, system_base, builtin_tools(workdir) + list(mcp_tools), workdir,
                          shared_interrupt=shared_interrupt, owns_interrupt=False)
            msgs = [{"role": "user", "content": args["prompt"]}]
            inner.invoke(msgs)
            return text_of(msgs[-1]) or "(子代理无文本输出)"
        except Exception as e:
            return f"task 子代理失败: {e}"
        finally:
            active.discard(sub_model)

    def kill() -> None:
        """打断废弃钩子(loop 打断路径调用): abort 所有在途子代理的流。"""
        for m in list(active):
            m.abort()

    return Tool(
        name="task",
        description="派 general-purpose 子代理独立跑子任务(检索、批量评测等), prompt 写清目标和约束, 只返回结论文本。",
        input_schema={
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "子任务完整描述"}},
            "required": ["prompt"],
        },
        run=run,
        kill=kill,
    )
