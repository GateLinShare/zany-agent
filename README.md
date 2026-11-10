# zany-agent

[中文](README-ZH.md)

A minimal coding agent: five built-in tools (`ls` / `read_file` / `write_file` / `edit_file` / `execute`) plus `task` subagent dispatch, with skills and MCP tools loaded on demand. Kernel inspired by pi, TUI modeled after Claude Code, faster conversation output; project memory (AGENTS.md) and personal memory (USER.md) are loaded every turn — memory stays intact after context compression. Full architecture doc: [docs/architecture.md](docs/architecture.md).

## The master branch: minimal kernel

Exactly 6 built-in tools: 5 basic operations (`ls`, `read_file`, `write_file`, `edit_file`, `execute`) + 1 `task` subagent (returns conclusions only, no recursive spawning). Everything else is optional and stays out of the kernel:
- Skills with progressive disclosure (manifest always in context, SKILL.md read on demand)
- MCP tools (connect once, use and discard)

### Install & Run

**Option 1: from source** (repo checkout, Python env managed by uv):

```bash
uv run core/tui.py             # run directly, zero setup
uv run core/tui.py -p "..."    # headless single run

bash core/deploy/deploy.sh     # one-shot deploy: uv + deps + global `zany` command (skips browser kernel automatically when absent; works when invoked via sh too)
```

Windows PowerShell:

```powershell
.\core\deploy\deploy.cmd       # same as deploy.sh, use .cmd on Windows
```

After deploy, run `zany` from any directory (cwd is the agent's working directory):

```bash
zany             # TUI
zany -p "..."    # headless single run
```

**Option 2: frozen binary (local build, zero Python deps)**:

```bash
bash release/build.sh        # build single-file binary for current platform → release/dist/zany-<target> (pass a target to build cross-platform)
bash release/install.sh      # install into system PATH (zany command, removes any source-installed version)
```

On Windows PowerShell use the .ps1 twins (.sh won't run in PS):

```powershell
.\release\build.ps1     # build → release\dist\zany-win32-x64.exe
.\release\install.ps1   # install to ~\.local\bin\zany.exe
```

First run auto-generates `~/.zany/.env`; fill in `ANTHROPIC_API_KEY`, then from any directory:

```bash
zany             # TUI
cd project-dir && zany -p "..."    # headless single run (cwd is the agent's working directory)
zany             # sessions/memory live in ~/.zany/, shared across projects
```

Optional: MCP server config at `~/.zany/mcp_servers.json` (uv must be installed on the target machine).

> Both deploy methods write the same `zany` command slot; reinstalling one removes the other. Sessions/memory live in `~/.zany/`, so switching loses nothing. The develop branch deploys as `zany-dev`, coexisting with master's `zany`.

### Building npm packages (maintainers)

PyInstaller onefile binary + npm per-platform packages (codex-style), single version source in pyproject.toml:

Official releases go through GitHub Actions (workflow_dispatch), see [docs/npm-release.md](docs/npm-release.md).

## The develop branch: master plus

Tools and runtime modes:

- **Browser tool** (`core/browser.py`): a local headless browser the agent drives directly (navigate/click/type)
- **Peer multi-instance collaboration** (`core/peers.py`): `tui.py --serve` starts a resident service for cross-machine task dispatch / progress checks / code-swap restart (A2A semantics)
- **Feishu gateway** (`gateway/`): Feishu bot DM / group @ remote coding control, WebSocket long connection + cron jobs, see [docs/feishu-integration.md](docs/feishu-integration.md)
- **Containerized deployment** (`docker/`): two-layer images (zany-base / zany-gateway) + docker-compose, gateway runs in a container

Run `bash core/deploy/deploy.sh` to deploy (uv + deps + browser kernel); the global command is **zany-dev** (coexists with master's `zany`, no clobbering).

### Manual browser download

`assets/chrome-headless-shell-linux64.zip` (115MB) exceeds GitHub's 100MB limit and is not in the repo. Download it manually into `assets/` before deploying:

```bash
mkdir -p assets
curl -L -o assets/chrome-headless-shell-linux64.zip \
  https://storage.googleapis.com/chrome-for-testing-public/154.0.8037.0/linux64/chrome-headless-shell-linux64.zip
bash core/deploy/deploy.sh    # extracts to .pw-browsers/ + uv sync + headless self-check
```

## License

[Apache-2.0](LICENSE)

## Contact

- WeChat: `wxid_22la5az0x9k322` <img src="assets/wechat-qrcode.png" width="180" />
- Email: [tsnninghao@gmail.com](mailto:tsnninghao@gmail.com) / [tsnninghao@163.com](mailto:tsnninghao@163.com)
