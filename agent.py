#!/usr/bin/env python3
"""Mini coding agent —— 无框架版, 核心组件都在 core/ 目录:

    core/model.py     模型薄封装(流式/非流式, anthropic 兼容端点)
    core/tools.py     内置文件工具 + execute(路径不做重写: 绝对直用/相对挂 workdir)
    core/loop.py      agent loop(流式聚合 → 工具派发 → 循环)
    core/subagent.py  task 子代理
    core/summarize.py 上下文压缩
    core/skills.py    skills 渐进披露
    core/mcp.py       MCP 工具加载
    core/sessions.py  会话持久化
    core/messages.py  消息格式小工具

本文件只负责组装: .env 加载 + SYSTEM_PROMPT + build_agent(workdir)。
入口在 tui.py:

    uv run tui.py            # interactive TUI
    uv run tui.py -p "问题"  # headless single shot
"""

import os
import sys
from pathlib import Path

from core.frozen import IS_FROZEN, app_root, data_dir

if IS_FROZEN:  # Windows 控制台默认 GBK, 遇 utf-8 字符 print 崩 → 错误降级为替换符
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass

from core.frozen import IS_FROZEN, app_root, data_dir

# --- .env (源码=项目目录, frozen=~/.zany/.env) ---
ENV_PATH = data_dir() / ".env"
if ENV_PATH.is_file():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
elif not IS_FROZEN:
    raise FileNotFoundError(f"缺 {ENV_PATH}: cp env.example .env 后填密钥")
# frozen 无 .env 不崩: 环境变量可能外部已给, 缺密钥会在 model.py 明确报

from core.loop import Agent, user_memory_path
from core.mcp import load_mcp_tools
from core.messages import text_of  # noqa: F401  (tui/eval 从这取)
from core.model import ChatModel, make_client
from core.plugins import load_plugins
from core.skills import skill_section
from core.subagent import make_task_tool
from core.tools import builtin_tools

SYSTEM_PROMPT = f"""你是一个编码助手, 在当前工作目录内完成用户的编码任务。
回答保持简洁,操作文件时展示清晰路径。
最终回复只给结论和结果: 做了什么一句话带过, 不复述工具调用过程, 不贴大段命令输出;
说明性文字几句话内说清, 不堆段落, 不写"首先/接下来"式施工流水账;
收尾不加"注意/建议"式叮嘱段落, 结论给完即止。
简单的问候、闲聊、测试性消息直接简短回应即可, 不要推断用户意图、
不要联想之前的对话过程, 除非用户明确在问问题。
长期记忆分两份, 每轮开工自动载入:
项目约定/结构/规范写进 /AGENTS.md(不存在就创建, 随项目 git 提交);
个人协作偏好、用户纠正过你的事写进真实路径 {user_memory_path()}
(不存在就创建, 每条一行带日期, 不进任何项目仓库),
用真实绝对路径 write_file/edit_file 写。
task 工具派 general-purpose 子代理独立跑子任务(检索、批量评测等),
适合量大或可并行的活, 主对话保留结论即可。"""


def prompt_packs() -> str:
    """常驻风格包: styles/<name>.md 全文拼进 system prompt, .env 里 PROMPT_PACKS=ponytail,x 选用。

    与 skills 的渐进披露不同, 人格/规则类文本必须每轮都在场, 不能等模型按需读。
    插 = 丢一个 md 文件进 styles/, 拔 = PROMPT_PACKS 里删名字。
    """
    root = app_root() / "styles"
    names = [n.strip() for n in os.environ.get("PROMPT_PACKS", "").split(",") if n.strip()]
    out = []
    for n in names:
        p = root / f"{n}.md"
        if not p.is_file():
            print(f"prompt pack 不存在, 跳过: {p}")
            continue
        out.append(p.read_text(encoding="utf-8"))
    return "\n\n".join(out)


STYLE_PROMPT = SYSTEM_PROMPT + "\n\n" + prompt_packs()


def build_agent(workdir: str, with_mcp: bool = True):
    model = ChatModel(os.environ["ANTHROPIC_MODEL"])
    max_turns = int(os.environ.get("MAX_TURNS", "40"))  # 单轮工具轮数上限, 防打转烧钱
    tools = builtin_tools(workdir)
    tools += load_plugins(workdir)  # .env 的 ZANY_PLUGINS 选装能力插件(core/plugins/<name>.py)
    if with_mcp:
        tools += load_mcp_tools()  # 阻塞(每台最多 PROBE_TIMEOUT); TUI 传 False 后台补
    interrupt_flag: dict = {}  # 主/子代理共享打断标志(见 core/subagent.py)
    tools.append(make_task_tool(model, STYLE_PROMPT, workdir, [],
                                shared_interrupt=interrupt_flag))  # ponytail: 子代理不载 MCP, 够用再说
    return Agent(model, STYLE_PROMPT + skill_section(workdir), tools, workdir,
                 max_turns=max_turns, shared_interrupt=interrupt_flag)


if __name__ == "__main__":
    from tui import main
    main()
