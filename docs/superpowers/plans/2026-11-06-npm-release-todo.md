# npm 分发待办清单（goal 模式执行用）

> 详细计划（每步完整代码/命令/预期输出）: 同目录 `2026-11-06-npm-release.md`，开工先读它。
> 执行分支: **master**（开工先 `git branch --show-current` 确认，别跑到 develop）。
> 范围: Task 1-6。**Task 7（首发演练/分支同步/push）用户明确说先不做，别做。**

## 当前状态（2026-11-06）

- checkout: `/data01/tsn/code/tsn_files/zany-agent`，已在 master，tracked 全干净
- `tests/test_frozen.py` 已写好，未跑过、预期失败（缺 `core.frozen`）
- 其余全部未动

## Task 1 — frozen 路径层

- `uv run pytest tests/test_frozen.py -v` 确认先失败（ModuleNotFoundError: core.frozen）
- 新建 `core/frozen.py`: `IS_FROZEN` / `app_root()`（frozen→`sys._MEIPASS`，源码→仓库根）/ `data_dir()`（frozen→`~/.zany`，源码→仓库根），代码照计划 Task 1 Step 3
- 收口三处（照计划 Task 1 Step 4）:
  - `agent.py` .env 段: `ENV_PATH = data_dir() / ".env"`，frozen 且缺失时打印提示继续跑，源码缺失照旧 raise
  - `agent.py` `prompt_packs()` 的 styles 根: `app_root() / "styles"`
  - `cron_jobs.py:22`: `CRON_FILE = data_dir() / "cron_jobs.json"`
- 验证: `uv run pytest tests/ -q` 全绿（源码形态行为必须零变化）
- 提交: `frozen 路径层: 源码/PyInstaller 双形态收口(.env/styles/cron)`

## Task 2 — PyInstaller 本地冒烟（go/no-go 闸门）

- `pyproject.toml` 加 `[dependency-groups] dev = ["pyinstaller>=6.10"]`，`uv sync`
- 构建: `uv run pyinstaller --onefile --name zany --collect-all textual --collect-all croniter --add-data "styles:styles" --add-data "skills:skills" tui.py`
- 冒烟: `cp .env ~/.zany/.env`（frozen 读 `~/.zany/.env`，开发机首跑要铺），然后 `./dist/zany -p "只回复 ok"` 必须返回模型回复；`./dist/zany` TUI 能起不崩
- **闸门: 冒烟不过，停，全部后续任务不做，报告卡点**
- 过了提交: pyproject.toml + uv.lock，`dev 依赖加 pyinstaller`

## Task 3 — 内置 skills 进包

- 先写 `tests/test_skills_bundled.py`（计划有全文）跑失败
- `core/skills.py`: import `app_root`；`skill_section` 开头追加内置源 `app_root() / "skills"`（排 sources 最后，项目/全局同名先到先得）；`skill_md_path` 改为 `src` 以 `~` 开头**或绝对路径**时返回 `str(p)`
- 验证: `uv run pytest tests/ -q` 全绿
- 提交: `内置 skills 打进二进制: app_root()/skills 兜底源, 绝对路径 source 全文可直读`

## Task 4 — npm 包骨架（发布相关统一放 npm/ 目录）

- `npm/main/package.json`: name `zany-agent`，bin `bin/zany.js`，optionalDependencies `zany-agent-linux-x64` / `zany-agent-darwin-arm64`，license `Apache-2.0`（照计划 Task 4 Step 1）
- `npm/main/bin/zany.js`: spawn 平台二进制（stdio inherit、退出码透传）、平台缺失给清晰报错、首跑铺 `~/.zany/.env`（照计划全文）
- 验证（照计划 Task 4 Step 3）: `npm pack` → 全局装 → 无平台包时报"平台包缺失"exit 1；把 Task 2 的 `dist/zany` 手工拷进全局 node_modules 的 `zany-agent-linux-x64/bin/zany` → `zany -p "只回复 ok"` 全链路通
- 验完 `npm uninstall -g zany-agent` 清理，提交: `npm 主包骨架`

## Task 5 — 发布脚本 + CI

- `npm/release.sh`: 参数 `linux-x64|darwin-arm64|main`，版本单源 pyproject.toml（tomllib 读），平台包 package.json heredoc 生成（os/cpu 字段），最后 `npm publish`（照计划全文）
- `.github/workflows/release.yml`: 矩阵 ubuntu-22.04(linux-x64, glibc 2.35 下限) + macos-14(darwin-arm64)，平台包先发，main job `needs: platform`（照计划全文）
- 验证: release.sh 最后一行临时换 `npm publish "$DIR" --dry-run` 跑一遍看 tarball 清单，验完还原
- 提交: `npm 发布: release.sh 单平台构建发布 + Actions 矩阵(平台包先发、主包后发)`

## Task 6 — 文档

- `npm/main/README.md`（npm 包页英文安装说明，计划 Task 6 Step 1 全文）
- `docs/npm-release.md`（发版手册，计划 Task 6 Step 2 全文）
- `AGENTS.md` 约定表加"npm 分发"一行（计划 Task 6 Step 3 有现成文本）
- 提交: `npm 分发文档: 发版手册 + 包 README + AGENTS 约定`

## 现场注意

- 本地 master 与 origin/master 分叉（107 vs 102 commits），本地提交不受影响，push 留给用户
- 临时产物只进 `tmp/`（release.sh 已如此），不污染仓库根
- 提交后不 push、不 merge develop（Task 7 范围，等用户发话）
