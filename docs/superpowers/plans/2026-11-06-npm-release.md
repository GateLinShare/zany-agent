# zany-agent npm 分发（zany-agent@npmjs）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 zany-agent（master 基座）打包为 PyInstaller 单文件二进制，经 npm 裸包名 `zany-agent` + 平台分包（`zany-agent-linux-x64` / `zany-agent-darwin-arm64`）发布公网，实现 `npm i -g zany-agent` 秒装、目标机零 Python。

**Architecture:** core/frozen.py 统一"源码运行 vs PyInstaller 冻结"双形态路径（只读资源 → `_MEIPASS`，可写数据 → `~/.zany`）；GitHub Actions 三 runner 矩阵各构建一份平台二进制并先发平台包，再发主包（bin 启动器 spawn 二进制）；版本号单源于 pyproject.toml。

**Tech Stack:** PyInstaller 6、uv（dev dependency-groups）、npm optionalDependencies 平台分包模式（codex/esbuild 同款）、GitHub Actions workflow_dispatch。

**工作目录:** `/data01/tsn/code/tsn_files/zany-agent`（当前 checkout 在 develop，**本计划的全部改动落在 master**——npm 分发是基座能力。开工先 `git checkout master`，收尾 Task 7 同步 develop）。

---

## 前置条件（用户手动，不进任务）

1. npmjs.com 注册账号 + 邮箱验证（发布人身份，包名 `zany-agent` 已确认空闲）。
2. 生成 Automation 类型 publish token（npmjs.com → Access Tokens），配到 GitHub 仓库 `GateLinShare/zany-agent` 的 Actions Secret `NPM_TOKEN`。
3. GitHub 推送一律走 `publish.sh`（2026-11-06 约定，严禁从开发 checkout 直推 github remote）。

## 风险与已知边界

| 风险 | 处置 |
|---|---|
| textual 在 PyInstaller 下缺隐式导入 | `--collect-all textual`；Task 2 是 go/no-go 闸门，冒烟不过则停 |
| macOS 二进制未签名 | npm 下载链路无 quarantine 属性，可直接跑；codesign/notarize 后续再说 |
| linux 产物 glibc 下限 | 用 ubuntu-22.04 runner（glibc 2.35），文档注明 |
| onefile 每次启动解包 1-2s | TUI/无头均可接受；嫌慢再换 --onedir |
| MCP stdio 走外部 uvx，目标机无 uv 时 MCP 不可用 | probe 快速认输降级，文档注明"可选能力需自装 uv" |
| 内置 skills 的全文路径在冻结后必须绝对 | Task 3 改 `skill_md_path` 对绝对路径 source 返回绝对路径 |

---

### Task 1: 冻结路径层 core/frozen.py + 三处收口

**Files:**
- Create: `core/frozen.py`
- Modify: `agent.py:21-30`（.env 加载）、`agent.py:63`（prompt_packs 的 styles 根）
- Modify: `cron_jobs.py:22`（CRON_FILE）
- Test: `tests/test_frozen.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_frozen.py
"""frozen 路径层: 源码运行锚仓库根, 冻结运行资源在 _MEIPASS、数据在 ~/.zany。"""
import sys
from pathlib import Path

import core.frozen as frozen

ROOT = Path(__file__).resolve().parent.parent


def test_source_mode_anchors_repo_root():
    assert frozen.app_root() == ROOT
    assert frozen.data_dir() == ROOT


def test_frozen_mode(monkeypatch):
    monkeypatch.setattr(frozen, "IS_FROZEN", True)
    monkeypatch.setattr(sys, "_MEIPASS", "/fake/meipass", raising=False)
    assert frozen.app_root() == Path("/fake/meipass")
    assert frozen.data_dir() == Path.home() / ".zany"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_frozen.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'core.frozen'`

- [ ] **Step 3: 实现 core/frozen.py**

```python
"""源码运行 vs PyInstaller 冻结运行的双形态路径解析, 三处收口:

- agent.py   .env 位置 + styles 资源根
- cron_jobs.py cron_jobs.json 落点
- core/skills.py 内置 skills 资源根(Task 3)

约定: 只读资源(打进包的 styles/skills/env.example)看 app_root();
     可写数据(.env/cron/会话)看 data_dir() —— frozen 时跟 ~/.zany 走, 源码时=仓库根(现状不变)。
"""
import sys
from pathlib import Path

IS_FROZEN = bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """只读资源根: frozen → onefile 解包目录, 源码 → 仓库根。"""
    if IS_FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """可写数据根: frozen → ~/.zany, 源码 → 仓库根。"""
    if IS_FROZEN:
        return Path.home() / ".zany"
    return Path(__file__).resolve().parent
```

- [ ] **Step 4: 三处收口**

`agent.py` 的 .env 段（25-30 行）替换为：

```python
from core.frozen import IS_FROZEN, app_root, data_dir

# --- .env (源码=项目目录, frozen=~/.zany/.env) ---
ENV_PATH = data_dir() / ".env"
if ENV_PATH.is_file():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
elif not IS_FROZEN:
    raise FileNotFoundError(f"缺 {ENV_PATH}: cp env.example .env 后填密钥")
# frozen 无 .env 不崩: 环境变量可能外部已给, 缺密钥会在 model.py 明确报
```

`agent.py` 的 `prompt_packs()` 中 `root = ...` 一行改为：

```python
    root = app_root() / "styles"
```

`cron_jobs.py:22` 改为：

```python
from core.frozen import data_dir
CRON_FILE = data_dir() / "cron_jobs.json"
```

（`core/sessions.py` 的 `~/.zany/sessions`、`core/loop.py` 的 `user_memory_path` 本就不锚仓库，不动。）

- [ ] **Step 5: 跑测试 + 全量回归**

Run: `uv run pytest tests/ -q`
Expected: 全 PASS（含既有用例——源码形态行为必须零变化）

- [ ] **Step 6: 提交**

```bash
git add core/frozen.py tests/test_frozen.py agent.py cron_jobs.py
git commit -m "frozen 路径层: 源码/PyInstaller 双形态收口(.env/styles/cron)"
```

### Task 2: PyInstaller 本地冒烟（go/no-go 闸门）

**Files:**
- Modify: `pyproject.toml`（加 dev 依赖组）

- [ ] **Step 1: 加 dev 依赖**

`pyproject.toml` 末尾追加：

```toml
[dependency-groups]
dev = ["pyinstaller>=6.10"]
```

Run: `uv sync && uv run pyinstaller --version`
Expected: 打印 `6.x.x`

- [ ] **Step 2: 构建**

```bash
uv run pyinstaller --onefile --name zany \
  --collect-all textual --collect-all croniter \
  --add-data "styles:styles" --add-data "skills:skills" \
  tui.py
```

Expected: 生成 `dist/zany`，体积 35-60MB。`--add-data` 分隔符 linux/macos 均为 `:`（win 不保，不写）。

- [ ] **Step 3: 冒烟**

```bash
cp .env ~/.zany/.env          # frozen 读 ~/.zany/.env, 开发机首跑要铺
./dist/zany -p "只回复 ok"
./dist/zany 2>&1 | head -5 &  # TUI 能起(3 秒后 kill)
kill %1
```

Expected: `-p` 返回含 ok 的回复（真实跑通模型 + TUI 无 ImportError/资源缺失崩溃）。

- [ ] **Step 4: 提交**

```bash
git add pyproject.toml uv.lock
git commit -m "dev 依赖加 pyinstaller; onefile 构建命令见 npm/release.sh(Task 5)"
```

**闸门：此步不过，后续任务全部停下重新评估。**

### Task 3: 内置 skills 进包 + 绝对路径 source 支持

内置技能（frontend-design/html-ppt/github-repo-search）随二进制分发；用户 cwd / ~/.zany 的同名技能仍先到先得覆盖。skills.py 已有"绝对路径 source 经 pathlib 短路挂对目录"的性质，只需：追加内置源 + 修 `skill_md_path` 对绝对路径返回绝对路径。

**Files:**
- Modify: `core/skills.py:30-40`（_root）、`core/skills.py:37-41`（skill_section 追加内置源）、`core/skills.py:72-75`（skill_md_path）
- Test: `tests/test_skills_bundled.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_skills_bundled.py
"""内置 skills(app_root()/skills) 应进清单且全文路径为绝对路径(read_file 可直用)。"""
from pathlib import Path

import core.skills as skills


def test_bundled_skills_listed_with_abs_path(tmp_path, monkeypatch):
    bundled = tmp_path / "skills" / "demo"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("---\nname: demo\ndescription: 测试技能\n---\n正文")
    monkeypatch.setattr(skills, "app_root", lambda: tmp_path)
    section = skills.skill_section(str(tmp_path / "nowhere"), sources=())
    assert "- demo: 测试技能" in section
    assert str(bundled / "SKILL.md") in section  # 全文路径必须 read_file 可直读(绝对)


def test_bundled_not_found_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "app_root", lambda: tmp_path / "nope")
    assert skills.skill_section(str(tmp_path), sources=()) == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_skills_bundled.py -v`
Expected: FAIL，`AttributeError: module 'core.skills' has no attribute 'app_root'`

- [ ] **Step 3: 实现**

`core/skills.py` import 区加：

```python
from core.frozen import app_root
```

`skill_section` 开头（`items, seen = [], set()` 之前）追加：

```python
    bundled = app_root() / "skills"
    if bundled.is_dir() and str(bundled) not in sources:
        sources = (*sources, str(bundled))  # 内置技能排最后: 项目/全局同名先到先得
```

`_root` 不改（`Path(workdir) / str(绝对路径)` 经 pathlib 短路返回绝对路径本身）。`skill_md_path` 改为：

```python
def skill_md_path(src: str, p: Path) -> str:
    """read_file 可用的路径: 非项目内挂载(~、绝对路径)用真实绝对路径, 其余相对 workdir ——
    不能带前导斜杠: 工具层约定绝对路径按真实文件系统字面解析, 带斜杠必 404。"""
    if src.startswith("~") or Path(src).is_absolute():
        return str(p)
    return f"{src.lstrip('/')}/{p.parent.name}/SKILL.md"
```

- [ ] **Step 4: 跑测试 + 回归**

Run: `uv run pytest tests/ -q`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add core/skills.py tests/test_skills_bundled.py
git commit -m "内置 skills 打进二进制: app_root()/skills 兜底源, 绝对路径 source 全文可直读"
```

### Task 4: npm 包骨架（主包 + 启动器）

**Files:**
- Create: `npm/main/package.json`
- Create: `npm/main/bin/zany.js`
- Create: `npm/main/env.example`（由构建复制，Task 5）
- Create: `npm/main/README.md`（Task 6 写内容，本任务先占位一行）

- [ ] **Step 1: 主包 package.json**

```json
{
  "name": "zany-agent",
  "version": "0.1.0",
  "description": "zany coding agent: TUI + headless coding agent (PyInstaller binaries via npm)",
  "license": "Apache-2.0",
  "repository": { "type": "git", "url": "git+https://github.com/GateLinShare/zany-agent.git" },
  "bin": { "zany": "bin/zany.js" },
  "files": ["bin/", "env.example", "README.md"],
  "optionalDependencies": {
    "zany-agent-linux-x64": "0.1.0",
    "zany-agent-darwin-arm64": "0.1.0"
  },
  "engines": { "node": ">=16" }
}
```

- [ ] **Step 2: 启动器 bin/zany.js**

```js
#!/usr/bin/env node
// zany 启动器: 选平台二进制 spawn, stdio 直通, 退出码透传。零依赖 CommonJS。
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const suffix = process.platform === "win32" ? ".exe" : "";
let bin;
try {
  bin = require.resolve(`zany-agent-${process.platform}-${process.arch}/bin/zany${suffix}`);
} catch {
  console.error(`zany-agent: 平台包缺失 (${process.platform}-${process.arch} 不在支持列表: linux-x64 / darwin-arm64)`);
  process.exit(1);
}

// 首跑: ~/.zany/.env 缺失时从 npm 包里铺一份 env.example, 提示填密钥
const envPath = path.join(os.homedir(), ".zany", ".env");
if (!fs.existsSync(envPath)) {
  const tpl = path.join(__dirname, "..", "env.example");
  if (fs.existsSync(tpl)) {
    fs.mkdirSync(path.dirname(envPath), { recursive: true });
    fs.copyFileSync(tpl, envPath);
    console.error(`zany: 已生成 ${envPath} — 填入 ANTHROPIC_API_KEY 后重跑`);
    process.exit(1);
  }
}

const child = spawn(bin, process.argv.slice(2), { stdio: "inherit" });
child.on("error", (e) => { console.error(`zany-agent: 启动失败: ${e.message}`); process.exit(1); });
child.on("exit", (code) => process.exit(code ?? 1));
```

- [ ] **Step 3: 本地验证（无平台包时的报错 + spawn 链路）**

```bash
cd npm/main && npm pack && cd -
npm i -g ./zany-agent-0.1.0.tgz
zany; echo "exit=$?"
```
Expected: 打印"平台包缺失"清晰报错，exit=1。

```bash
mkdir -p /tmp/fakeplat/zany-agent-linux-x64/bin
cp dist/zany /tmp/fakeplat/zany-agent-linux-x64/bin/zany  # Task 2 的产物
# 用 file: 依赖模拟平台包完整链路
node -e "const p=require('/usr/local/lib/node_modules/zany-agent/package.json');p.optionalDependencies={'zany-agent-linux-x64':'file:/tmp/fakeplat/zany-agent-linux-x64'};require('fs').writeFileSync('/tmp/fakepkg.json',JSON.stringify(p))"
```

（懒验法：直接手动验证 spawn 一行）`node -e "require('/usr/local/lib/node_modules/zany-agent/bin/zany.js')" -- -p "只回复 ok"` 前先手工把平台包塞进全局 node_modules：
```bash
cp -r /tmp/fakeplat/zany-agent-linux-x64 "$(npm root -g)/"
zany -p "只回复 ok"
```
Expected: 返回模型回复（启动器 → 二进制 → 模型全链路通）。

- [ ] **Step 4: 清理与提交**

```bash
npm uninstall -g zany-agent; rm -f zany-agent-0.1.0.tgz
git add npm/
git commit -m "npm 主包骨架: bin 启动器 + optionalDependencies 平台分包 + 首跑铺 env.example"
```

### Task 5: 发布脚本 npm/release.sh + GitHub Actions

**Files:**
- Create: `npm/release.sh`
- Create: `.github/workflows/release.yml`

- [ ] **Step 1: release.sh（本地/CI 通用，单平台或主包）**

```bash
#!/usr/bin/env bash
# 发布一个 npm 包: bash npm/release.sh linux-x64|darwin-arm64|main
# 版本单源 pyproject.toml; CI 由 .github/workflows/release.yml 按矩阵调用。
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET="${1:?usage: release.sh <linux-x64|darwin-arm64|main>}"
VERSION=$(python3 -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
OUT=tmp/npm
mkdir -p "$OUT"

if [ "$TARGET" = main ]; then
  DIR="$OUT/zany-agent"
  mkdir -p "$DIR/bin"
  cp npm/main/package.json "$DIR/"
  cp npm/main/bin/zany.js "$DIR/bin/"
  cp env.example "$DIR/"
  cp npm/main/README.md "$DIR/" 2>/dev/null || echo "zany-agent" > "$DIR/README.md"
else
  uv run pyinstaller --onefile --name zany \
    --collect-all textual --collect-all croniter \
    --add-data "styles:styles" --add-data "skills:skills" \
    tui.py
  DIR="$OUT/zany-agent-$TARGET"
  mkdir -p "$DIR/bin"
  cp dist/zany "$DIR/bin/zany"
  OS="${TARGET%%-*}"; CPU="${TARGET##*-}"
  cat > "$DIR/package.json" <<EOF
{
  "name": "zany-agent-$TARGET",
  "version": "$VERSION",
  "description": "zany binary for $TARGET",
  "license": "Apache-2.0",
  "os": ["$OS"],
  "cpu": ["$CPU"],
  "files": ["bin/"]
}
EOF
fi

# 版本号统一写回(主包 optionalDependencies 与平台包版本必须与 pyproject 一致)
node -e "const fs=require('fs');const f=process.argv[1];const j=JSON.parse(fs.readFileSync(f));j.version='$VERSION';if(j.optionalDependencies)for(const k of Object.keys(j.optionalDependencies))j.optionalDependencies[k]='$VERSION';fs.writeFileSync(f,JSON.stringify(j,null,2)+'\n')" "$DIR/package.json"

npm publish "$DIR"
echo "published zany-agent($TARGET)@$VERSION"
```

- [ ] **Step 2: workflow**

```yaml
# .github/workflows/release.yml
name: release
on:
  workflow_dispatch:
    inputs:
      version:
        description: "version, must equal pyproject.toml"
        required: true
jobs:
  platform:
    strategy:
      matrix:
        include:
          - { os: ubuntu-22.04, target: linux-x64 }     # glibc 2.35 下限
          - { os: macos-14, target: darwin-arm64 }
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync
      - run: npm config set //registry.npmjs.org/:_authToken=$NPM_TOKEN
        env: { NPM_TOKEN: "${{ secrets.NPM_TOKEN }}" }
      - run: bash npm/release.sh ${{ matrix.target }}
  main:
    needs: platform   # 主包 optionalDependencies 引用的平台包必须先存在
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm config set //registry.npmjs.org/:_authToken=$NPM_TOKEN
        env: { NPM_TOKEN: "${{ secrets.NPM_TOKEN }}" }
      - run: bash npm/release.sh main
```

- [ ] **Step 3: 本地 dry-run 验证脚本正确性**

```bash
bash npm/release.sh linux-x64 --dry-run 2>/dev/null || true
# release.sh 里 npm publish 换成临时验证:
```
验证方式：临时把 release.sh 最后一行 `npm publish "$DIR"` 手动换成 `npm publish "$DIR" --dry-run` 跑一遍，Expected: 打印 tarball 内容清单（bin/zany + package.json），不改 registry。验完还原。（npm publish --dry-run 只展示不上传。）

- [ ] **Step 4: 提交**

```bash
git add npm/release.sh .github/
git commit -m "npm 发布: release.sh 单平台构建发布 + Actions 矩阵(平台包先发、主包后发)"
```

### Task 6: 文档与约定

**Files:**
- Create: `docs/npm-release.md`
- Create: `npm/main/README.md`
- Modify: `AGENTS.md`（约定表加一行）

- [ ] **Step 1: npm/main/README.md（npm 包页面展示，英文）**

```markdown
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
```

- [ ] **Step 2: docs/npm-release.md（发布手册）**

```markdown
# npm 发布手册（zany-agent@npmjs）

分发形态: PyInstaller onefile 二进制 + npm 平台分包(codex/esbuild 模式)。
包名: 主包 `zany-agent`, 平台包 `zany-agent-{linux-x64,darwin-arm64}`。
版本单源: pyproject.toml 的 version(npm 侧由 npm/release.sh 同步写回)。

## 发版流程
1. 改 pyproject.toml version → master 提交
2. git push(origin, 走 publish.sh 同步 GitHub 的按既有约定来)
3. GitHub 仓库 Actions → release → Run workflow → 填 version(须与 pyproject 一致)
4. 等两绿: platform(2 平台) → main
5. 验证: npm view zany-agent(公网) + npm view zany-agent --registry https://registry.npmmirror.com(npmmirror 小时级同步)

## 约定
- 平台包先发、主包后发(workflow 的 needs 保证)
- frozen 路径: 只读资源=app()(sys._MEIPASS), 可写数据=~/.zany(core/frozen.py)
- npm 链路问题先看 docs/troubleshooting.md
```

- [ ] **Step 3: AGENTS.md 约定表加一行**

```markdown
| **npm 分发** | master 基座打 PyInstaller onefile 经 npm 裸包 `zany-agent` 分发(平台包 `zany-agent-{linux-x64,darwin-arm64}` optionalDependencies, codex 同款); 版本单源 pyproject.toml; frozen 路径约定: 只读资源 app_root()(_MEIPASS)、可写数据 data_dir()(~/.zany), 收口在 core/frozen.py; 发布 = Actions release(workflow_dispatch), 流程 docs/npm-release.md |
```

- [ ] **Step 4: 提交**

```bash
git add docs/ npm/main/README.md AGENTS.md
git commit -m "npm 分发文档: 发版手册 + 包 README + AGENTS 约定"
```

### Task 7: 首发演练 + 分支同步

- [ ] **Step 1: 推 master 并触发 CI**

```bash
git push origin master          # 内网 origin; GitHub 同步走 publish.sh(既有约定)
# GitHub Actions 页面 → release → Run workflow → version 填 pyproject 当前值
```

- [ ] **Step 2: 公网安装冒烟（干净环境语义）**

```bash
npm i -g zany-agent --registry https://registry.npmjs.org --force
zany -p "只回复 ok"             # 密钥走 ~/.zany/.env 或环境变量
npm view zany-agent dist-tags --registry https://registry.npmmirror.com  # npmmirror 同步后同版本可见
```
Expected: 两个 registry 均返回模型回复 / 同版本号。

- [ ] **Step 3: 分支同步（git 分支约定）**

```bash
git checkout develop && git merge master -m "Merge master: npm 分发(frozen 路径层/skills 内置/npm 发布)" && git push origin master develop
cd /data01/tsn/code/tsn_files/zany-agent-develop && git pull --ff-only
```

- [ ] **Step 4: 机器内收尾**

AGENTS.md 若在 merge 中有冲突按"两分支保持一致"解决；确认 develop 侧 .env 等磁盘资产未被波及（`ls /data01/tsn/code/tsn_files/zany-agent-develop/.env`）。

---

## Self-Review

- 覆盖检查：调研拍板的三件（路线 B / 公网 / 裸包名 zany-agent）→ Task 2（B 的核心）+ Task 4/5（npm）+ 前置条件（账号）；路径冻结风险 → Task 1；内置 skills 资源 → Task 3；npmmirror 速度验证 → Task 7。无缺口。
- 占位符扫描：无 TBD/TODO；Task 4 Step 3 的"懒验法"给了完整命令。
- 类型一致性：`app_root()/data_dir()` 在 Task 1 定义、Task 3 skills.py 引用同名；启动器平台包命名 `zany-agent-<os>-<cpu>` 与 Task 5 heredoc 生成名一致；`IS_FROZEN` 在 agent.py 用 `data_dir()` 分支而非直接判 frozen，测试 monkeypatch 的是模块属性——`core/frozen.py` 函数内引用模块级 `IS_FROZEN`，patch 生效，一致。
