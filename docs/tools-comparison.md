# 工具层对比：我们 vs pi / hermes

> 对比对象：本项目 `core/tools.py`（五内置工具，241 行）vs pi（`packages/coding-agent/src/core/tools/`，8 内置默认激活 4）vs hermes（`tools/`，核心 40+，窄腰哲学）。
> 姊妹篇：`docs/compression-comparison.md`（压缩逻辑对比）。

## 一、总览定位

| | 我们 | pi | hermes |
|---|---|---|---|
| 一句话 | 5 工具极简内核，错误即文本 | 8 内置(默认开 4)，检索工具默认关、教模型用 bash+rg | 40+ 核心工具 + Footprint Ladder(新能力最后才做成工具) |
| 文件形态 | 单文件 241 行 | 每工具一文件 + TypeBox schema | 每工具一文件自注册 + registry |
| 错误处理 | `错误: ...` 文本回填 | `isError: true` + 明确错误信息 | `{"error": ...}` JSON，从不抛到模型层 |
| 共同点 | **错误即文本、不抛异常到模型层——三家完全一致**，这是行业收敛的答案 | | |

## 二、逐工具对比

### read

| 维度 | 我们 | pi | hermes |
|---|---|---|---|
| 分页 | offset/limit，默认 100 行 | offset/limit，默认 2000 行/50KB 双限 | offset/limit，默认 500 行上限 2000，另有 100k 字符预算 |
| 截断提示 | "用 offset 继续读"(无具体值) | **可操作**：`Use offset=Z to continue` 带精确值 | 回传 `next_offset` + hint |
| 二进制/图片 | `errors="replace"` 硬读 | 魔数嗅探图片 → 自动缩放转 image block；不支持图片时返回提示不丢弃 | 二进制扩展名拒绝；docx/xlsx/ipynb 自动抽文本 |
| 重复读 | 无处理 | — | **同文件重读返回"unchanged"stub 指回早前结果**，write 后失效、压缩后清表 |

### write / edit

| 维度 | 我们 | pi | hermes |
|---|---|---|---|
| 批量编辑 | edits[] 对同一原文定位、排序、重叠检测、逆序套用 ✓ | 同 ✓ | patch 双模式 |
| 唯一性 | count 校验 + replace_all | 无 count 参数，多匹配直接报错 | 多匹配直接报错 |
| 模糊匹配 | NFKC+智能引号/破折号/行尾空白 + overlay 保原字节(pi 同款) | 归一化空间替换 + 未动行逐字节拷回 | **9 级模糊匹配链**(exact→line_trimmed→…→context_aware)+自动重缩进 |
| 行尾/编码 | 无 BOM/CRLF 处理 | **剥 BOM + CRLF 检测/按原风格回写** | Unicode 保真 |
| 失败升级 | 重试语义靠模型自己悟 | — | **同路径连败 3 次 → 显式提示"停止变体重试，去重读/换 write_file"** |
| 防呆 | — | prepareArguments 兼容畸形参数(edits 发成 JSON 字符串等) | **防回显污染**(内容像 read 的行号输出则拒写)；写后回读校验落盘 |

### execute

| 维度 | 我们 | pi | hermes |
|---|---|---|---|
| 超时 | 1200s 默认(但 description 写"120 秒"，**文案漂移**) | 无默认超时，可配；kill 进程组 | 前台默认 180s 硬顶；background+notify 另算 |
| 截断 | **保头 200k 字符，尾部丢弃**(tools.py:44) | **保尾** 2000 行/50KB + 全量输出落盘临时文件、给模型路径 | **保头 40% + 保尾 60%**，中间注明省略；剥 ANSI、选择性 redact 密钥 |
| 进程树 | `subprocess.run(timeout)` 只 kill shell，孙子进程可能残留孤儿 | `detached` 进程组 + `kill(-pid)` 整组杀 | process 工具统一管理后台会话(list/poll/kill) |
| exit code | 非 0 加 `[exit N]` | 非 0 标 isError | 同款 JSON |

### ls / 检索

| 维度 | 我们 | pi | hermes |
|---|---|---|---|
| ls | 无条目数限制(大目录会撑爆上下文) | 500 条目 + 50KB 截断 | search_files 后端 ripgrep |
| 检索 | 无 grep/glob(教模型用 execute+rg) | grep/find 默认关，系统提示自动加"用 bash 做 ls/rg/find"规则 | search_files 内置(rg 优先) |
| 防循环 | 无 | grep 100 match 即 kill 子进程 | **同参数连搜 4 次直接 BLOCK**("结果没变，停止重搜") |

## 三、优化清单(按性价比排序)

| # | 优化 | 学谁 | 现状问题 | 代价 |
|---|---|---|---|---|
| 1 | **execute 截断改"保头 40% + 保尾 60%"，超长全量落盘临时文件、给模型路径** | hermes(`terminal_tool.py:2710`) / pi(`bash.ts:344`) | 现在**保头丢尾**(tools.py:44)——编译错误/测试失败摘要几乎都在输出尾部，被截掉后模型只能瞎猜或重跑命令；且截断提示"可用 offset/limit 分页"对 execute 输出无意义(没有分页工具) | ~15 行 |
| 2 | **read_file/ls 续读提示可操作化**：`用 offset=100 继续读`带精确参数值 | pi(`read.ts:190`) | 现在只说"用 offset 继续读"，模型要自己算 offset；带值的提示直接降低试错 | 1 行 |
| 3 | **ls 加条目上限**(如 500 条 + "还有 N 项，用更具体的 path") | pi(`ls.ts`) | 大目录(如 node_modules、数据集目录)一次列爆上下文，我们连 MAX_OUTPUT 都没过 | ~5 行 |
| 4 | **edit BOM/CRLF 透明处理**：剥 BOM、统一 LF 匹配、按原行尾风格回写 | pi(`edit-diff.ts:11-25`) | CRLF 仓库里 edit 会把全文行尾改成 LF( diff 噪音爆炸)；BOM 文件首行永远匹配不上 | ~15 行 |
| 5 | **行为熔断**：同文件 read 重复去重、edit 连败 3 次给升级提示、execute 同命令连跑 N 次警告 | hermes(`file_tools.py:866,1877`) | 模型陷入重复调用循环时只能靠 turn_limit 兜底；hermes 用一个计数字典把"你在循环里"这个元信息喂给模型 | ~30 行(计数字典 + 非读调用重置 + 压缩后清空) |
| 6 | execute 超时后 kill 进程组(`start_new_session` + `os.killpg`) | pi(`bash.ts:114`) | `subprocess.run(timeout)` 超时只杀 shell，shell 起的孙子进程(如 `python server.py &`)残留孤儿 | ~8 行 |
| 7 | 修文案漂移：`EXEC_TIMEOUT=1200` 但 description/AGENTS.md 写"120 秒" | — | 文档与实现不一致，模型可能传错误的 timeout 预期 | 1 行 |
| 不抄 | pi 三层结果格式(content/details/structuredContent)——过度设计；hermes 40+ 工具面与浏览器全家桶——违背极简内核；后台任务 process 工具——当前场景用不上；权限审批门——个人本地工具不需要 | | | |

## 四、结论

我们的工具层已经把**最难的部分**(edit 批量定位/模糊匹配/overlay 保原字节)做到了 pi 水准——那是 pi 工具层最有技术含量的 200 行。剩余差距全是"**边角防爆**"：输出截断方向(保头 vs 保尾)、大目录/大输出防线、模型行为循环熔断、CRLF/BOM 透明处理。这些都是几行到几十行的小改动，且每一项都对应真实会话里会撞上的坑(编译错误被截掉、node_modules 列爆、CRLF 全文 diff)。hermes 最值得学的反而是它的**克制**：防呆靠提示文本和计数字典，不靠加工具。
