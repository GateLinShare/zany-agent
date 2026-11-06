"""serve/peer 全链路自检: 假 agent + 真 HTTP, send → check → 401 → list。

    uv run python test_serve.py
"""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ["PEER_TOKEN"] = "test-token"

from core.plugins.peers import make_peer_tool
from core.serve import make_server


class FakeAgent:
    usage = {"input": 0, "output": 0, "calls": 1}

    def invoke(self, msgs):
        msgs.append({"role": "assistant",
                     "content": [{"type": "text", "text": f"done: {msgs[0]['content']}"}]})


def main() -> None:
    workdir = tempfile.mkdtemp()
    httpd = make_server(lambda wd: FakeAgent(), workdir, port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    peers_dir = Path(workdir) / "peers"
    peers_dir.mkdir()
    (peers_dir / "test.json").write_text(json.dumps(
        {"url": f"http://127.0.0.1:{port}", "token": "test-token"}), encoding="utf-8")

    tool = make_peer_tool(workdir)

    # list
    out = tool.run({"action": "list"})
    assert "test:" in out and "url" in out, out
    # send → task_id
    out = tool.run({"action": "send", "name": "test", "task": "干点活"})
    assert "task_id=" in out and "working" in out, out
    tid = out.split("task_id=")[1].split()[0]
    # check 轮询到 completed
    for _ in range(50):
        out = tool.run({"action": "check", "name": "test", "task_id": tid})
        if "completed" in out:
            break
        time.sleep(0.1)
    assert "completed" in out and "done: 干点活" in out, out
    # 会话落盘: .sessions/<对端workdir桶>/peer-<task_id>.json
    from core.sessions import project_dir
    sess = project_dir(workdir) / f"peer-{tid}.json"
    assert sess.exists() and "干点活" in sess.read_text(encoding="utf-8"), sess
    # 错 token → 错误文本(不抛异常, 项目约定)
    (peers_dir / "bad.json").write_text(json.dumps(
        {"url": f"http://127.0.0.1:{port}", "token": "wrong"}), encoding="utf-8")
    out = tool.run({"action": "send", "name": "bad", "task": "x"})
    assert out.startswith("错误:") and "401" in out, out
    # 未知 task → 错误文本
    out = tool.run({"action": "check", "name": "test", "task_id": "nope"})
    assert out.startswith("错误:"), out
    # 未知 peer → 错误文本
    out = tool.run({"action": "send", "name": "ghost", "task": "x"})
    assert "peer 不存在" in out, out

    # restart 工具: 排定 + 轮末生效(不真 execv)
    from core.plugins.restart import make_restart_tool, pending_restart
    import core.plugins.restart as _t
    _t._pending_restart = None
    old_argv = sys.argv
    try:
        sys.argv = ["tui.py"]
        out = make_restart_tool().run({})
        assert "重启已排定" in out, out
        av = pending_restart()
        assert av and av[1].endswith("tui.py") and av[-1] == "-r", av  # TUI 自动接最近会话
        _t._pending_restart = None
        sys.argv = ["tui.py", "-p", "任务"]
        out = make_restart_tool().run({})
        assert "无需重启" in out and pending_restart() is None, out  # 无头模式拒绝
    finally:
        sys.argv = old_argv
        _t._pending_restart = None

    httpd.server_close()
    print("自检通过: send/check 全链路 + 鉴权 401 + 错误即文本")


if __name__ == "__main__":
    main()
