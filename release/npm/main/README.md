# zany-agent

zany coding agent — TUI + headless coding agent, packaged as prebuilt binaries.

    npm i -g @gatelinshare/zany-agent

GitHub Packages requires an `.npmrc` entry and a GitHub classic token with
`read:packages`:

    @gatelinshare:registry=https://npm.pkg.github.com

First run creates `~/.zany/.env` — put your `ANTHROPIC_API_KEY` there (see the
generated file), then run `zany` in any project directory.

- `zany` — interactive TUI
- `zany -p "..."` — headless single shot
- Optional: MCP servers via `~/.zany/mcp_servers.json` (requires `uv` on PATH)
- Sessions/memory live in `~/.zany/`

The npm installer downloads the matching binary from the GitHub Release with the
same version. Linux x86-64 (glibc 2.35+), macOS Apple Silicon and Windows x64
are shipped.
