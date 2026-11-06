"""源码运行 vs PyInstaller 冻结运行的双形态路径解析, 三处收口:

- agent.py   .env 位置 + styles 资源根
- cron_jobs.py cron_jobs.json 落点
- core/skills.py 内置 skills 资源根

约定: 只读资源(打进包的 styles/skills/env.example)看 app_root();
     可写数据(.env/cron/会话)看 data_dir() —— frozen 时跟 ~/.zany 走, 源码时=仓库根(现状不变)。
"""
import sys
from pathlib import Path

IS_FROZEN = bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """只读资源根: frozen → onefile 解包目录, 源码 → 仓库根(本文件在 core/, 要 parent.parent)。"""
    if IS_FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """可写数据根: frozen → ~/.zany, 源码 → 仓库根。"""
    if IS_FROZEN:
        return Path.home() / ".zany"
    return Path(__file__).resolve().parent.parent
