# AGENTS.md

## 项目环境约定
- 工作目录（agent 文件工具的根）= execute shell 中的 `/data01/tsn/code/tsn_files/deepagents-mini-agent`。文件工具写入的 `/xxx.py` 在 shell 中对应 `/data01/tsn/code/tsn_files/deepagents-mini-agent/xxx.py`。
- shell 里没有 `python` 命令，只有 `/usr/bin/python3`；`.venv/bin/python` 不存在。
- 项目内有 test/、docs/、skills/ 等目录；核心文件: agent.py, tui.py, sessions.py, cron_jobs.py。
