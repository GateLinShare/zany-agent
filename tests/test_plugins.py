"""plugins loader 自检: python3 tests/test_plugins.py

覆盖: 空开关不加载 / 坏插件名降级不崩 / 正常注册 / RELOAD=1 热更 / 默认缓存不热更。
插件文件动态写进 core/plugins/(用 _test_ 前缀 + uuid 防撞), finally 清理。
"""
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.plugins import load_plugins

PLUGINS_DIR = Path(__file__).resolve().parent.parent / "core" / "plugins"

PLUGIN_V1 = '''"""动态生成的测试插件 v1。"""
from core.tools import Tool

def register(workdir):
    def _run(args):
        return f"v1@{workdir}"
    return [Tool(name="fake_probe", description="fake v1",
                 input_schema={"type": "object"}, run=_run)]
'''

PLUGIN_V2 = PLUGIN_V1.replace("v1@{workdir}", "v2@{workdir}").replace("v1", "v2")


def _make_plugin(body: str) -> str:
    name = f"_test_{uuid.uuid4().hex[:8]}"
    (PLUGINS_DIR / f"{name}.py").write_text(body, encoding="utf-8")
    return name


def _cleanup(name: str):
    import importlib
    (PLUGINS_DIR / f"{name}.py").unlink(missing_ok=True)
    sys.modules.pop(f"core.plugins.{name}", None)
    importlib.invalidate_caches()


def _with_env(**kv):
    """临时设环境变量, 返回恢复函数。"""
    saved = {k: os.environ.get(k) for k in kv}
    os.environ.update({k: v for k, v in kv.items()})
    return lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                    for k, v in saved.items()]


def main():
    # 0. 插件文件的仓库根锚定: restart/browser 都用 __file__ 上溯定位仓库根, 搬目录层级会算错
    #    (教训: root 算成 core/ 后 execv 指向 core/tui.py, execv 失败直接死进程)
    from core.plugins.restart import make_restart_tool
    repo_root = Path(__file__).resolve().parent.parent
    old_argv = sys.argv
    try:
        sys.argv = [str(repo_root / "core" / "tui.py")]
        av = make_restart_tool().run({}) and None
        import core.plugins.restart as _r
        av = _r.pending_restart()
        assert av and Path(av[1]) == repo_root / "core" / "tui.py", (av, repo_root)
        _r._pending_restart = None
    finally:
        sys.argv = old_argv

    # 1. 空开关 → 纯基座, 不加载任何东西
    restore = _with_env(ZANY_PLUGINS="")
    try:
        assert load_plugins("/tmp") == [], "空开关应返回空列表"
    finally:
        restore()

    # 2. 不存在的插件 → 降级打印, 不抛异常, 返回空
    restore = _with_env(ZANY_PLUGINS="_test_no_such_plugin")
    try:
        assert load_plugins("/tmp") == [], "坏插件名应降级为空"
    finally:
        restore()

    # 3. 正常插件 → 注册成功, run 可调; 4/5 延续同一插件验证热更语义
    name = _make_plugin(PLUGIN_V1)
    try:
        restore = _with_env(ZANY_PLUGINS=name)
        try:
            tools = load_plugins("/data/proj")
            assert len(tools) == 1 and tools[0].name == "fake_probe"
            assert tools[0].run({}) == "v1@/data/proj"

            # 4. 默认(无 RELOAD) → 模块缓存, 文件改了也拿旧代码
            (PLUGINS_DIR / f"{name}.py").write_text(PLUGIN_V2, encoding="utf-8")
            tools = load_plugins("/tmp")
            assert tools[0].run({}) == "v1@/tmp", "默认应走 import 缓存, 不热更"

            # 5. RELOAD=1 → 同进程内重新 load, 新会话即拿新代码
            os.environ["ZANY_PLUGINS_RELOAD"] = "1"
            tools = load_plugins("/tmp")
            assert tools[0].run({}) == "v2@/tmp", "RELOAD=1 应拿到改后的代码"
            assert tools[0].description == "fake v2"
        finally:
            os.environ.pop("ZANY_PLUGINS_RELOAD", None)
            restore()
    finally:
        _cleanup(name)

    # 6. 混合列表: 坏的降级, 好的照常加载
    name2 = _make_plugin(PLUGIN_V1)
    try:
        restore = _with_env(ZANY_PLUGINS=f"_test_missing,{name2}")
        try:
            tools = load_plugins("/tmp")
            assert [t.name for t in tools] == ["fake_probe"], "坏插件不应拖垮好插件"
        finally:
            restore()
    finally:
        _cleanup(name2)

    print("test_plugins: 全部通过")


if __name__ == "__main__":
    main()
