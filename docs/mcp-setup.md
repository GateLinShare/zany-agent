# MCP 工具接入

zany-agent 通过 MCP（Model Context Protocol）挂外部工具进程。配置一处、所有 checkout 共享，
代码在仓库 `mcp-servers/` 下，克隆即用。

## 通用机制

- 配置文件：`~/.zany/mcp_servers.json`（HOME 级共享，不入 git）。格式为标准的
  `{"mcpServers": {<名字>: {command, args, env, timeout}}}`。
- 工具名带 `mcp__<server>__<tool>` 前缀防跨服务器重名。
- 快速认输：单服务器握手/list_tools 超时 15s（uvx 拉包卡 pypi 的环境不会卡启动），
  工具调用默认 60s、可按服务器 `"timeout"` 覆盖。
- TUI 先起、MCP 后台补挂：失败的服务器每 60s 重试，5 分钟窗口；失败原因落盘
  `~/.zany/mcp-errors.log`（屏幕上那行打印会被 TUI 帧搅碎，以日志为准）。
- 即开即用：每次工具调用重连一次，无连接池状态，服务器崩了不拖垮 agent。

## 内置：rea-dispatcher（逆向工具集）

把 [REA](https://github.com/morluto/rea)（npm 包 `rea-agents`，~207 个逆向工具）折叠成 3 个
瘦工具（`rea_tools` / `rea_help` / `rea_call`），避免全量工具 schema（约 20 万 token）常驻上下文。

代码在 [`mcp-servers/rea-dispatcher/`](../mcp-servers/rea-dispatcher/README.md)，README 里有
从克隆到可用的完整步骤（uv sync → npm i -g rea-agents → 写 mcp_servers.json → 自测脚本）。

本机已配置时的最简验证：

```bash
# 无头跑一轮, 回答里应有 mcp__rea_dispatcher__* 三个工具
uv run core/tui.py -p "你现在有哪些工具? 有 mcp__ 开头的吗?"
```
