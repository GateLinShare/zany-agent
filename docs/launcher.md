# zany 启动器

`/usr/local/bin/zany` 是一个 bash wrapper（非软链接），装好后任意目录敲
`zany` 即以当前目录为 workdir 启动本项目：

```bash
#!/usr/bin/env bash
exec uv run --project /data01/tsn/code/tsn_files/zany-agent \
     python /data01/tsn/code/tsn_files/zany-agent/tui.py "$@"
```

三个要点：

| 要点 | 说明 |
|---|---|
| `--project` | 强制用本项目的 uv 环境（`pyproject.toml`/`uv.lock`），与 cwd 无关 |
| 绝对路径跑 `tui.py` | 代码永远用本项目这份，多机部署时改脚本里的路径即可 |
| cwd 不动 | `tui.py` 拿进程当前目录当 workdir —— 在哪敲，操作的就是哪个项目 |

为什么不用软链接：`uv run` 需要真实项目路径，symlink 解析后 cwd 语义易踩坑。
