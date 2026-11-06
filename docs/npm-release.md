# npm 发布手册（zany-agent@npmjs）

分发形态: PyInstaller onefile 二进制 + npm 平台分包(codex/esbuild 模式)。
包名: 主包 `zany-agent`, 平台包 `zany-agent-{linux-x64,darwin-arm64}`。
版本单源: pyproject.toml 的 version(npm 侧由 npm/release.sh 同步写回)。

## 发版流程
1. 改 pyproject.toml version → master 提交
2. git push(origin, 走 publish.sh 同步 GitHub 的按既有约定来)
3. GitHub 仓库 Actions → release → Run workflow → 填 version(须与 pyproject 一致)
4. 等两绿: platform(2 平台) → main
5. 验证: npm view zany-agent(公网) + npm view zany-agent --registry https://registry.npmmirror.com(npmmirror 小时级同步, 急用 curl -X PUT https://registry-direct.npmmirror.com/-/package/zany-agent/syncs 手动催)

## 约定
- 平台包先发、主包后发(workflow 的 needs 保证)
- frozen 路径: 只读资源=app_root()(sys._MEIPASS), 可写数据=data_dir()(~/.zany), 收口在 core/frozen.py
- 发布脚本显式 --registry https://registry.npmjs.org(防本机/CI 默认镜像 registry)
- 本机冒烟注意: deploy.sh 装的 /usr/local/bin/zany 会撞名, 测 npm 启动器用 node <npm prefix -g>/lib/node_modules/zany-agent/bin/zany.js
- npm 链路问题先看 docs/troubleshooting.md
