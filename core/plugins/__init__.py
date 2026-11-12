"""能力插件 loader:master 基座的可插拔工具缝(docs/plugin-research.md §3.2)。

约定:一个插件 = 本目录下一个 <name>.py,暴露 register(workdir) -> list[Tool]。
没有基类 / manifest / registry —— .env 的 ZANY_PLUGINS 开关 + Python import 就是全部仪式。

- 开关:ZANY_PLUGINS=browser,peers(逗号分隔;空/不设 = 纯基座)
- 降级:import 或 register 失败 → 打印跳过,不崩启动(与 core/mcp_loader.py 同款风格)
- 依赖:插件的重依赖走 pyproject extras(如 browser = playwright),master 默认不装,
  import 失败即降级为该工具缺席
- 热更(档 1):ZANY_PLUGINS_RELOAD=1 时每次 load_plugins 重新 reload 模块,供
  gateway 等长驻宿主"改插件文件 → 新会话即用新代码" —— 会话本来逐个 build_agent,
  进行中的会话拿旧代码跑完本轮自然结束,无双版本冲突。reload 不递归重载依赖,
  所以插件保持单文件、只依赖 stdlib 与 core 稳定模块。
"""

import importlib
import importlib.util
import os
from pathlib import Path

from core.tools import Tool


def _load(name: str):
    mod = importlib.import_module(f"core.plugins.{name}")
    if os.environ.get("ZANY_PLUGINS_RELOAD") == "1":
        # 先清 pyc 再 reload: pyc 校验键是秒级 mtime+size, 同秒内改文件且大小不变会拿到旧字节码
        Path(importlib.util.cache_from_source(mod.__file__)).unlink(missing_ok=True)
        importlib.reload(mod)  # 开发模式:每次会话重建重读插件文件
    return mod


def load_plugins(workdir: str) -> list[Tool]:
    tools: list[Tool] = []
    names = [n.strip() for n in os.environ.get("ZANY_PLUGINS", "").split(",") if n.strip()]
    for name in names:
        try:
            tools += _load(name).register(workdir)
        except Exception as e:  # 依赖没装/文件写坏 → 该工具缺席,不崩启动
            print(f"(插件 {name} 加载失败, 跳过: {e!r})")
    return tools
