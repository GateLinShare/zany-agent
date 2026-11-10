# eval/terminal-bench — Terminal-Bench 判分链路

Stanford/Laude 的 Terminal-Bench（榜单口径 86 题，数据集 branch `dataset/terminal-bench-core/v0.1.x`），
容器内纯终端真实任务，可执行脚本判分。这里放的是**本地环境从零跑通的配方**，
踩坑与 oracle 战绩详见全局技能 `~/.zany/skills/agent-quality-eval/SKILL.md`。

## 文件

| 文件 | 作用 |
|---|---|
| `setup.sh` | 环境准备：clone 数据集（走本机 xray 代理）→ 装 harness（uv tool）→ 本地重建两个 base 镜像 |
| `run.sh` | 跑评测：tb run 薄封装，agent/task-id 全参数传，结果落 `/tmp/tb-runs/run-<时间戳>/` |
| `base-python-3-13.dockerfile` | ghcr 官方 base `python-3-13` 的本地替身（覆盖 37 题） |
| `base-ubuntu-24-04.dockerfile` | ghcr 官方 base `ubuntu-24-04` 的本地替身（覆盖 29 题） |
| `astropy.dockerfile` | swe-bench-astropy-1/2 任务镜像预构建（build 阶段要 git clone github） |

## 怎么跑

```bash
# 1. 环境准备（幂等，可重复跑）
bash eval/terminal-bench/setup.sh

# 2. oracle 冒烟（标准答案，验证判分链路；先通这个再接真 agent）
bash eval/terminal-bench/run.sh --agent oracle --task-id hello-world

# 3. 批量/接真 agent
bash eval/terminal-bench/run.sh --agent oracle --task-id hello-world git-multibranch fix-permissions
bash eval/terminal-bench/run.sh --agent <zany-adapter> --task-id ...
```

结果看 `/tmp/tb-runs/run-*/<task>/` 下 trial 目录（`--no-cleanup` 保留现场）。

## 环境依赖的几个硬前提

- **ghcr.io 官方镜像拉不动**：两个 base 用 `docker.1ms.run` 基础镜像本地重建，打回原 tag（`ghcr.io/laude-institute/t-bench/<name>:latest`），tb run 按 tag 找镜像，感知不到差异。
- **base 必配清华 pip/uv 源 + 预装 uv**：每题测试脚本 `setup-uv-pytest.sh` 现场装 uv+pytest，不配源必超时（harness 侧配 `--global-test-timeout-sec 600`）。
- **litellm 坑**：装 harness 带 `--with "httpx[socks]"`，跑时 `LITELLM_LOCAL_MODEL_COST_MAP=True`（run.sh 已带）。
- **13 题需预构建任务镜像**（build 内 clone github，如 swe-bench-astropy-1/2）：按 `astropy.dockerfile` 头注释 build 后 `run.sh` 加 `--no-rebuild`；任务镜像名规律 `tb__<task-id>__client:latest`。
- **代理 IP 会漂**：astropy.dockerfile 的 `PROXY` build-arg 每次构建要传宿主侧当前 IP。

## 已知限制（oracle 验证阶段结论，2026-11-10）

- 17 题 oracle 全过 = 判分链路可信（hello-world、astropy×2、git、localstack create-bucket 等）。
- `broken-networking`：容器内直连 example.com 的外网出口题，oracle 也过不了，环境豁免。
- conda-env-conflict-resolution / simple-sheets-put / tmux-advanced-workflow 三题 trial 目录全空（agent 没起来），待细查。
- qemu×2、eval-mteb×2、build-linux-kernel 等重题需大外网下载/编译，未跑。
- 本地数据集在 `/tmp/tb-dataset`（52M 不进 git，setup.sh 可重拉）；跑分结果全在 `/tmp/tb-runs/`，重启即失，要留档先拷走。
