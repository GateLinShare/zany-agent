# 部署指南

新环境从零到能用：clone → `deploy.sh` →（可选）git 代理。飞书网关的容器化部署见 `docs/feishu-integration.md`。

## 一键部署

```bash
git clone <仓库地址> && cd miniagent
bash deploy.sh
```

脚本（仓库根 `deploy.sh`）依次做四件事，幂等，重复跑无副作用：

| 步骤 | 内容 | 说明 |
|---|---|---|
| 1. uv | 没有才装 | 官方安装器，落 `~/.local/bin` |
| 2. uv sync | 按本分支 pyproject/lock 装依赖 | develop 自动带 `--extra feishu`（gateway 依赖），master 不需要；python 版本由 uv 自动配 |
| 3. 浏览器内核 | 调 `install_browser.sh` | assets 自带 zip，离线可装；`.pw-browsers` 已有就跳过 |
| 4. 全局命令 `miniagent` | 生成 shim | 有写权限写 `/usr/local/bin`，否则写 `~/.local/bin`（需在 PATH，脚本会提示） |

装完即用（任意目录）：

```bash
miniagent                    # TUI
miniagent -p "..."           # 无头单次
<仓库>/gateway/run.sh start  # 飞书网关（develop 分支功能，需 .env 配 FEISHU_*）
uv run eval/main.py [name]   # 评测
```

## 全局命令 miniagent 的语义

`miniagent` 是个 bash shim，由 `deploy.sh` 第 4 步按部署路径生成：

```bash
unset VIRTUAL_ENV  # 残留 venv 变量会让 uv run 每次告警
exec uv run --project <部署路径> python <部署路径>/tui.py "$@"
```

- 代码路径钉死在部署时的 checkout：换目录重部署后要重跑 `deploy.sh`（或手改 shim）
- 调用时的 **cwd 就是 agent 工作目录**（AGENTS.md、文件工具、execute 全锚定它）

## .env

仓库约定 `.env`（含密钥）随 git 提交，clone 即得、开箱能用；只有换飞书 bot / 白名单时才要改（改完网关需重启）。

## git 走代理拉 GitHub

内网直连 GitHub 时好时坏。给 git 配"只对 github.com 走 socks5"，不影响内网私有仓库的收发：

```bash
git config --global http.https://github.com/.proxy socks5://127.0.0.1:10808
```

- `127.0.0.1:10808` 是 v2rayN/xray 的 socks 入口；其他机器部署时换成对应能翻墙的地址
- 验证：`git ls-remote https://github.com/octocat/Hello-World.git`
- SSH 协议（`git@github.com:...`）不走这条配置，需要时在 `~/.ssh/config` 给 github.com 配 `ProxyCommand`（当前全用 https，未配）
