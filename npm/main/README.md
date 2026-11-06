# zany-agent

zany coding agent — TUI + headless coding agent, packaged as prebuilt binaries.

    npm i -g zany-agent

First run creates `~/.zany/.env` — put your `ANTHROPIC_API_KEY` there (see the
generated file), then run `zany` in any project directory.

- `zany` — interactive TUI
- `zany -p "..."` — headless single shot
- Optional: MCP servers via `~/.zany/mcp_servers.json` (requires `uv` on PATH)
- Sessions/memory live in `~/.zany/`

Linux x86-64 (glibc 2.35+) and macOS Apple Silicon are shipped.
