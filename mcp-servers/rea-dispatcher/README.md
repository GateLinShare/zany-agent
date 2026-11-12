# rea-dispatcher

把 [REA](https://github.com/morluto/rea)（npm 包 `rea-agents`，~141-207 个逆向工程工具：
Ghidra/Hopper native、APK/JADX、JS/Electron、固件、网站）折叠成 3 个瘦 MCP 工具，
避免全量工具 schema（数百 KB ≈ 20 万 token）常驻 MCP 宿主上下文。纯本地：REA 与引擎都在本机起子进程。

## 工具（模型侧只亮这三个）

| 工具 | 作用 |
|---|---|
| `rea_tools(filter="")` | 列 REA 工具名 + 一句话说明（每条截 120 字符） |
| `rea_help(name)` | 单工具的参数 schema 与完整说明 |
| `rea_call(name, arguments)` | 调用真工具，参数原样透传；REA 错误原样返回 |

## 接入（克隆 zany-agent 后）

```bash
# 1. 克隆仓库并建环境（uv sync 会装好 mcp SDK）
git clone <repo> zany-agent && cd zany-agent && git checkout develop
cp env.example .env   # 填 API key
uv sync

# 2. REA 引擎（一次性）
npm i -g rea-agents          # JS/Electron/网站类工具靠它
# native/APK 类另需 Ghidra 12.1.x + JDK 21, 装好后宿主 env 传
# GHIDRA_INSTALL_DIR / JAVA_HOME 即可, 不装则那部分工具报 REA 自己的错

# 3. 配 MCP 宿主 ~/.zany/mcp_servers.json
```

```json
{
  "mcpServers": {
    "rea-dispatcher": {
      "command": "<克隆目录>/.venv/bin/python",
      "args": ["<克隆目录>/mcp-servers/rea-dispatcher/server.py"],
      "env": { "REA_MJS": "<npm root -g>/rea-agents/scripts/rea.mjs" },
      "timeout": 600
    }
  }
}
```

`REA_MJS` 通常可省（server.py 会自己 `npm root -g` 找）；`timeout: 600` 是宿主侧单工具调用上限（core/mcp_loader.py 支持单服务器覆盖），深度逆向调用是分钟级。

# 4. 重启 zany 即挂上；验证：
cd <克隆目录>/mcp-servers/rea-dispatcher && ../.venv/bin/python test_dispatch.py

## 行为

- 无状态：每次工具调用现拉 REA 子进程（慢 3-5s），对齐"单点可验证查询"定位；深挖长会话场景再加常驻。
- 返回超 `REA_MAX_BYTES`（默认 20000B）截断：头 40% + 尾 60%，全文落盘 `REA_SPILL_DIR`（默认 `~/.zany/rea-spill/`）。
- call 超时 540s / 列目录 120s（都低于宿主 600s，留头）。

## 自测

```bash
cd 本目录 && <python> test_dispatch.py    # rea_tools → rea_help → rea_call 链路 + 错误透传
```
