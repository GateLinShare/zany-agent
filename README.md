# zany-agent

无框架手写的 mini coding agent：Anthropic 原生消息单协议贯穿（模型输入 = 会话落盘 = 回放格式），单循环、单工具形态，零编排框架。架构全量文档见 [docs/architecture.md](docs/architecture.md)。

## master 分支：极简内核

内置工具只有 5 个：`ls`、`read_file`、`write_file`、`edit_file`、`execute`。其余能力一律外置，不进内核：

- `task` 子代理（只回传结论，不递归派生）
- skills 渐进披露（清单常驻，SKILL.md 按需读）
- MCP 工具（单次连接即用即走）

```bash
uv run tui.py              # TUI
uv run tui.py -p "..."     # 无头单次
uv run eval/main.py [name] # 评测（用例在 evals/cases.jsonl）
```

## develop 分支：在 master 之上增加

工具与运行形态：

- **浏览器工具**（`core/browser.py`）：agent 可直接驱动的本地无头浏览器（navigate/click/type）
- **peer 多实例协作**（`core/peers.py`）：`tui.py --serve` 起常驻服务，跨机派任务/查进度/换码重启（A2A 语义）
- **飞书 gateway**（`gateway/`）：飞书 bot 私聊 / 群 @ 远程控制编码，WebSocket 长连接收事件 + cron 定时任务，见 [docs/feishu-integration.md](docs/feishu-integration.md)
- **容器化部署**（`docker/`）：两层镜像（zany-base / zany-gateway）+ docker-compose，gateway 可跑容器

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
