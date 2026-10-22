#!/usr/bin/env python3
"""Mini conversational agent on deepagents, mirroring pi's default 4 tools:
read_file / edit_file / write_file / execute  (pi: read / edit / write / bash).

本文件只负责构建 agent（模型/工具/skills/MCP）；跑对话的入口在 chat.py:

    uv run chat.py            # interactive REPL
    uv run chat.py "问题"     # single shot

Run with the dcode venv (deepagents is importable from the repo checkout):
    cd /data01/tsn/code/deepagents/libs/code && .venv/bin/python \
        /data01/tsn/code/tsn_files/deepagents-mini-agent/chat.py
"""

import os
from pathlib import Path

# --- .env (项目目录下的模型配置) ---
ENV_PATH = Path(__file__).resolve().parent / ".env"
for line in ENV_PATH.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

from langchain.agents.middleware import SummarizationMiddleware
from langchain.chat_models import init_chat_model

import deepagents
from deepagents import (
    FilesystemMiddleware,
    MemoryMiddleware,
    create_deep_agent,
)
from deepagents.backends import LocalShellBackend

from mcp_tools import load_mcp_tools

# --- Skills(可选)---
# pi: 项目 .claude/skills + 用户级 ~/.pi/agent/skills;这里最简:只认工作目录下两个位置。
# 每个 skill 是一个子目录,含 SKILL.md(frontmatter name/description + 正文指令)。
SKILL_SOURCES = ["/skills", "/.claude/skills"]  # backend 路径,映射到 workdir 下

# --- MCP(可选)---
# 服务器声明在同目录 mcp_servers.json(格式同 Claude Code),加载逻辑见 mcp_tools.py

# deepagents FilesystemMiddleware 默认自带 8 个文件工具(FsToolName):
#
#   ls         列目录。path 必须是绝对路径;不能读文件内容。
#   read_file  读文件,默认 offset=0、limit=100 行,大文件靠 offset/limit 分页;
#              单次结果超 ~20000 token 会被截断/外置到文件系统,需继续翻页读取;
#              也能读图片/视频(视频有解码时长、帧数、字节上限)。
#   write_file 写文件(整文件覆盖),content 必填;不能追加、不能局部修改。
#   edit_file  精确字符串替换;old_string 必须在文件中唯一,否则要 replace_all=True;
#              文件不存在或匹配失败直接报错。
#   delete     删除文件;文件不存在报错。只能删单个文件路径,不能递归删目录。
#   glob       按 glob 模式找文件;超时上限 10 秒,模式太宽会超时报错。
#   grep       内容搜索;pattern 是字面字符串(不是正则),默认最多返回 1000 条;
#              装了 ripgrep 时用 rg(快、自动遵循 .gitignore),否则退化为 Python 慢速实现。
#   execute    执行 shell 命令;默认超时 120 秒,可按命令覆盖,但上限 3600 秒;
#              只有 backend 实现 SandboxBackendProtocol(如 LocalShellBackend)才有,
#              默认的 StateBackend(内存态)下调用只会返回错误。
#
# 之外还有第 9 个 `task`(派子代理跑独立子任务),由 SubAgentMiddleware 注入,
# create_deep_agent 默认开启 —— 用于并行评测/大范围检索等场景。
#
# 与 pi 默认四个工具对齐: read→read_file, bash→execute, edit→edit_file, write→write_file
TOOLS = ["ls", "read_file", "edit_file", "write_file", "execute", "task"]

SYSTEM_PROMPT = """你是一个运行在 deepagents 框架里的编码助手,
在当前工作目录内完成用户的编码任务。回答保持简洁,操作文件时展示清晰路径。
路径约定: ls/read_file/edit_file/write_file/glob 的路径以当前工作目录为根,
读本项目文件用 "/agent.py" 或 "agent.py", 不要拼真实绝对路径;
execute 是真实 shell, 用什么路径都行。
长期记忆: 项目约定、用户偏好、重要结论写进 /AGENTS.md(不存在就创建),
每轮开工时你会自动读到它, 用户纠正过你的事情也要记进去。
task 工具派 general-purpose 子代理独立跑子任务(检索、批量评测等),
适合量大或可并行的活, 主对话保留结论即可。"""


def build_agent(workdir: str):
    model = init_chat_model(
        f"anthropic:{os.environ['ANTHROPIC_MODEL']}",
        max_tokens=8192,
        # 代理只认 Authorization: Bearer,anthropic SDK 默认发 x-api-key
        default_headers={"Authorization": f"Bearer {os.environ['ANTHROPIC_API_KEY']}"},
    )
    backend = LocalShellBackend(root_dir=workdir)
    # skills: backend 路径以 root_dir 为根,只传实际存在的目录
    skills = [s for s in SKILL_SOURCES if (Path(workdir) / s).is_dir()]
    mcp_tools = load_mcp_tools()
    return create_deep_agent(
        model=model,
        backend=backend,
        system_prompt=SYSTEM_PROMPT,
        tools=mcp_tools,          # MCP 工具混入(SDK 内置文件工具不受影响)
        skills=skills or None,
        middleware=[
            FilesystemMiddleware(backend=backend, tools=TOOLS),
            # 长期记忆: 每轮把工作目录下 /AGENTS.md 注入 system prompt(缺文件静默跳过);
            # 写入由模型用 edit_file 维护, 不用 dcode 的 ManagedMemoryGuard(无托管块需求)
            MemoryMiddleware(backend=backend, sources=["/AGENTS.md"]),
            # 换掉 deepagents 默认的非破坏性压缩(同名替换): langchain 原版直接
            # 改写 messages 为 summary+尾部, sessions 落盘/恢复天然不爆上下文。
            # ponytail: 丢了 deepagents 的历史 offload 到 conversation_history/;
            # 全量原始记录仍在我们自己的 sessions/*.json(压缩前各轮已存盘)。
            SummarizationMiddleware(
                model=model, trigger=("tokens", 80 * 1024), keep=("messages", 20)
            ),
        ],
    )


def text_of(message) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))
