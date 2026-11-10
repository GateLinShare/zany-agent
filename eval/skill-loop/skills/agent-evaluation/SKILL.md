---
name: agent-evaluation
description: 跑一次被测 zany 于一个 benchmark case 并私下打分，返回一份协议 YAML。作为 task 子代理执行；本文件全文由 Builder/Optimizer 拼进 task prompt（子代理不载 skills）。
---

# Agent Evaluation（zany 版）

处理一个评测请求：把指定的被测 zany 在一个 case 上跑恰好一次，私下打分，返回一份协议结果。

只处理请求里的这一个格子：不跑其他 case/run、不派子代理、不改被测仓库、不改 benchmark、**永不写 scoreboard.yaml**。全程静默：不输出叙述/标题/围栏/摘要/评分细节，唯一由你产出的文本是最后的纯协议 YAML。

## 协议（请求）

以下字段缺一、重复或冲突 → 返回 `invalid_request`，不建 workspace 不启动被测：

```text
protocol_version: 1
case_id: <case_id>
run: <1_based_run_index>
expected_version: <git short hash>
repo_path: <被测仓库绝对路径>
benchmark_dir: <benchmark 绝对路径>
model: <冻结的模型名>
```

## 语义

- **scored result**：被测跑过且 rubric 可用即返回。答错、产物缺失、格式坏都是正常打分行为，`status: ok`。
- **evaluation failure**：请求非法（`invalid_request`）、benchmark 不可评（`benchmark_invalid`）、版本不符（`version_changed`）、启动/绑定/评分自身故障（`evaluation_failed`）。失败响应里禁止出现 score、cost、时长、session id、私有评分细节、优化建议。
- `run` 只是调用方给的标签，原样返回；不校验总 run 数。

## 准备

1. 读 `<benchmark_dir>/benchmark_config.toml`，`status = "failed"` → `benchmark_invalid`（校准失败的 benchmark 不评）。
2. 校验路径：`case_id`/`benchmark_dir` 不含路径穿越，解析后仍在 benchmark_dir 下。
3. 读 `<repo_path>/eval/benchmarks/<benchmark_id>/<case_id>/statement/README.md` 与 `rubric/README.md`。要求 rubric 评分项总分恰为 100。
4. 记录 case 目录快照（statement/ 与 rubric/ 的文件清单+md5），跑完对比，变了 → `benchmark_invalid`。
5. 被测版本校验：`git -C <repo_path> rev-parse --short HEAD` 必须等于 `expected_version`，否则 `version_changed`；`git -C <repo_path> status --porcelain` 非空 → version 记 `<hash>+dirty` 并在返回中如实上报。读 `<repo_path>/.env` 的 `ANTHROPIC_MODEL`，与请求 `model` 不一致 → `evaluation_failed`（不得静默换模型评）。
6. 建隔离 workspace：`mktemp -d` 建唯一目录，只把 `statement/` 拷进去。被测 agent 永不接触 rubric、Gold、评分规则。zany 无沙箱——这就是唯一的物理隔离，确认拷贝完整后继续。

## 跑被测

```bash
cd <workspace绝对路径>
uv run <repo_path>/core/tui.py -p "Read README.md in the current directory and complete the task exactly as specified there."
```

- stdout = 被测最终回答；stderr 含 `(session: <id>)` 与 token 用量行；完整会话 Trace 落 `~/.zany/sessions/<workspace名>-<hash>/<id>.json`。
- 启动失败只在"workspace 与 Trace 证据证明被测没启动过"时重试，且每次重试必须带新的具体诊断与修复；不设数值上限；没有新修复可试或无法判断是否启动过 → `evaluation_failed`。
- 跑完核对：statement/rubric 快照未变；Trace 里不得出现对 `eval/` 或 rubric 路径的读取（命中 → 该 run 记 `contaminated`，按 `benchmark_invalid` 处理并在返回中说明）。

## 打分

只看隔离 workspace、该次 session JSON、私有 rubric。rubric 声明的 scorer：

- `rubric`：逐项对照打分，逐项分与理由保密。
- `deepeval:<metric>`：从 session JSON 抽 actual_output（最终回答）/tools_called（tool_use 序列）/context（workspace 产物）组 TestCase，执行 `uv run --offline --with deepeval <repo_path>/eval/scorers/deepeval_score.py --metrics <metrics.yaml 路径> --case-input <组装好的 JSON>`（脚本不存在 → `evaluation_failed`）。deepeval 自身失败 = scoring 失败 ≠ 0 分：重试只重跑打分，被测 run 不动。
- 分数 0..100 两位小数；被测答错/产物差/任务失败都是正常打分。rubric 无法套用 → `benchmark_invalid`；分数非有限或越界 → `evaluation_failed`。

`duration_ms` 用实测墙钟；cost 只从被测 stderr 的 token 行换算或已有记录取，取不到写 `null` 且不影响分数有效性。

## 返回

唯一产出文本，不加围栏：

```yaml
status: ok
protocol_version: 1
case_id: <case_id>
run: <n>
version: <实际被测 version，含 +dirty 后缀若有>
model: <实际模型名>
score: <0..100 两位小数>
cost: <或 null>
duration_ms: <非负整数>
session_id: <Trace 文件 stem>
```

失败响应：`status: invalid_request | benchmark_invalid | version_changed | evaluation_failed` + `reason`，不带任何分数与私有信息。

调用方报格式无效时：用本会话已有结果原样重发干净 YAML，不调工具、不重跑被测、不重打分、不加解释。
