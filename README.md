# zany-agent

无框架手写的 mini coding agent：Anthropic 原生消息单协议贯穿（模型输入 = 会话落盘 = 回放格式），单循环、单工具形态，零编排框架。架构全量文档见 [docs/architecture.md](docs/architecture.md)。

## 项目特点：双层记忆不遗忘

- **项目记忆 `AGENTS.md`**——架构/约定/规范随仓库走，每轮开工自动全文载入
- **个人记忆 `~/.zany/USER.md`**（路径可用 `ZANY_USER_MEMORY` 改）——协作偏好、被纠正过的决策，逐条带日期累积，同样每轮自动载入

新会话、恢复旧会话、隔多天继续，agent 都带着完整上下文开工；改记忆文件即时生效，无需重启。

## master 分支：极简内核

内置工具只有 6 个：5 个基础操作（`ls`、`read_file`、`write_file`、`edit_file`、`execute`）+ 1 个 `task` 子代理（只回传结论，不递归派生）。其余能力按需外挂，不占内核：
- skills 渐进披露（清单常驻，SKILL.md 按需读）
- MCP 工具（单次连接即用即走）

### 安装与启动

**npm 安装（推荐，零 Python 依赖）**：

```bash
npm i -g zany-agent        # 预编译二进制, 内置 skills 随包分发
```

首跑自动生成 `~/.zany/.env`，填入 `ANTHROPIC_API_KEY` 后在任意目录：

```bash
zany             # TUI
cd 项目目录 && zany -p "..."    # 无头单次(cwd 就是 agent 工作目录)
zany             # 会话/记忆在 ~/.zany/, 跨项目共用
```

可选：MCP 服务器配置 `~/.zany/mcp_servers.json`（需目标机自装 uv）。

**源码部署**（仓库 checkout，走 uv 管理 Python 环境）：

```bash
bash deploy.sh   # 一键部署: uv + 依赖 + 全局命令 zany(无浏览器模块, 自动跳过内核)
```

部署后任意目录 `zany` 即启动（cwd 就是 agent 工作目录）：

```bash
zany             # TUI
zany -p "..."    # 无头单次
```

> npm 版与源码版都读 `~/.zany/` 下的会话与记忆，但装了 deploy.sh 的 `zany` 命令后再 `npm i -g zany-agent` 会撞名，看 PATH 优先级。

### 构建 npm 包（维护者）

PyInstaller onefile 二进制 + npm 平台分包（codex 同款），版本单源 pyproject.toml：

```bash
bash npm/release.sh linux-x64     # 构建并发布平台包
bash npm/release.sh main          # 发布主包
```

正式发版走 GitHub Actions（workflow_dispatch），流程见 [docs/npm-release.md](docs/npm-release.md)。

### 开发模式（不装全局命令，仓库内直接跑）

```bash
uv run tui.py              # TUI
uv run tui.py -p "..."     # 无头单次
uv run eval/main.py [name] # 评测（用例在 eval/cases.jsonl）
```

## develop 分支：在 master 之上增加

工具与运行形态：

- **浏览器工具**（`core/browser.py`）：agent 可直接驱动的本地无头浏览器（navigate/click/type）
- **peer 多实例协作**（`core/peers.py`）：`tui.py --serve` 起常驻服务，跨机派任务/查进度/换码重启（A2A 语义）
- **飞书 gateway**（`gateway/`）：飞书 bot 私聊 / 群 @ 远程控制编码，WebSocket 长连接收事件 + cron 定时任务，见 [docs/feishu-integration.md](docs/feishu-integration.md)
- **容器化部署**（`docker/`）：两层镜像（zany-base / zany-gateway）+ docker-compose，gateway 可跑容器

跑 `bash deploy.sh` 完成部署（uv + 依赖 + 浏览器内核），全局命令为 **zany-dev**（与 master 的 zany 并存，互不覆盖）。

### 浏览器包手动下载

`assets/chrome-headless-shell-linux64.zip`（115MB）超 GitHub 100MB 限制不入库，部署前手动下载，放到仓库根 `assets/` 下再跑部署脚本：

```bash
mkdir -p assets
curl -L -o assets/chrome-headless-shell-linux64.zip \
  https://storage.googleapis.com/chrome-for-testing-public/154.0.8037.0/linux64/chrome-headless-shell-linux64.zip
bash deploy.sh    # 解压到 .pw-browsers/ + uv sync + headless 自检
```

## License

[Apache-2.0](LICENSE)
