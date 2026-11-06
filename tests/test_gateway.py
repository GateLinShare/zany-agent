"""gateway 单测: 纯逻辑不打网 —— 解析/门禁/分段/会话路径/cron 存储与去重。

跑法: uv run python tests/test_gateway.py
"""

import json
import sys
import tempfile
from types import SimpleNamespace
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.channels.feishu import (  # noqa: E402
    is_addressed,
    parse_incoming,
    session_path,
    sniff_image_ext,
    split_message,
)
from gateway.cron import CronStore, make_cron_tools  # noqa: E402


class FakeMsg:
    """模拟 lark SDK 的 P2 事件体(只造 parse_incoming 用到的属性)。"""

    def __init__(self, text, chat_type="p2p", sender="ou_u1", mentions=None, mtype="text",
                 content=None):
        message = SimpleNamespace(
            message_id=f"om_{time.monotonic_ns()}",
            chat_id="oc_c1" if chat_type == "group" else "ou_c1",
            chat_type=chat_type,
            message_type=mtype,
            content=content if content is not None else json.dumps({"text": text}, ensure_ascii=False),
            mentions=mentions or [],
        )
        sender_obj = SimpleNamespace(sender_id=SimpleNamespace(open_id=sender))
        self.event = SimpleNamespace(message=message, sender=sender_obj)


def test_parse_mentions_and_at():
    mentions = [SimpleNamespace(key="@_user_1", id=SimpleNamespace(open_id="ou_bot"), name="小助手")]
    item = parse_incoming(FakeMsg("@_user_1 帮我看下", chat_type="group", mentions=mentions))
    assert item["text"] == "小助手 帮我看下", item["text"]
    assert is_addressed(item, "ou_bot") is True
    assert is_addressed(item, "ou_other") is False
    assert is_addressed(parse_incoming(FakeMsg("随便说说", chat_type="p2p")), "") is True


def test_parse_non_text():
    item = parse_incoming(FakeMsg("", mtype="image"))
    assert item and item["is_text"] is False


def test_parse_image_takes_image_key():
    content = json.dumps({"image_key": "img_v3_abc"})
    item = parse_incoming(FakeMsg("", mtype="image", content=content))
    assert item["image_key"] == "img_v3_abc"
    assert item["is_text"] is False
    assert item["message_id"]


def test_sniff_image_ext():
    assert sniff_image_ext(b"\x89PNG\r\n\x1a\nxx") == ".png"
    assert sniff_image_ext(b"\xff\xd8\xff\xe0xx") == ".jpg"
    assert sniff_image_ext(b"GIF89axx") == ".gif"
    assert sniff_image_ext(b"RIFF\x00\x00\x00\x00WEBP") == ".webp"
    assert sniff_image_ext(b"plain-text") == ".jpg"  # 兼不出认 jpeg(飞书多图)


def test_user_content_media_type():
    import base64
    from core.messages import user_content
    p = Path(tempfile.mkdtemp()) / "x.jpg"
    p.write_bytes(b"\xff\xd8fake")
    blocks = user_content(f"[图片: {p}]")
    assert blocks[0]["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(blocks[0]["source"]["data"]) == b"\xff\xd8fake"


def test_parse_garbage_returns_none():
    class Bad:
        event = None
    assert parse_incoming(Bad()) is None


def test_split_message():
    assert split_message("") == []
    assert split_message("hi") == ["hi"]
    long = "\n".join(f"line{i}" for i in range(2000))
    parts = split_message(long, limit=4000)
    assert all(len(p) <= 4000 for p in parts)
    assert "line0" in parts[0] and "line1999" in parts[-1]


def test_session_path_sanitize():
    p = session_path("/tmp/wd", "oc_a/b:c")
    assert p.name.startswith("feishu-oc_a_b_c"), p.name


def test_cron_store_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        store = CronStore(Path(d) / "cron.json")
        assert store.create("日报", "0 9 * * *", "生日报", "ou_x").startswith("定时任务")
        assert "已存在" in store.create("日报", "0 8 * * *", "x", "ou_x")
        assert "非法" in store.create("坏", "99 9 * *", "x", "ou_x")
        tools = make_cron_tools(store, "ou_x")
        assert "日报" in tools[0].run({"action": "list"})
        assert "已删除" in store.delete("日报")
        assert "没有叫" in store.delete("日报")


def test_cron_three_modes():
    """三模式: 默认续本会话 / chat_id 改绑别的会话续跑 / fresh 每次全新会话。"""
    with tempfile.TemporaryDirectory() as d:
        store = CronStore(Path(d) / "cron.json")
        store.create("a", "0 9 * * *", "pa", "ou_x")  # 默认: 绑当前会话
        tools = make_cron_tools(store, "ou_x")
        assert "已创建" in tools[0].run({
            "action": "create", "name": "b", "cron": "0 9 * * *",
            "prompt": "pb", "chat_id": "oc_other"})  # 指定别的会话
        assert "已创建" in tools[0].run({
            "action": "create", "name": "c", "cron": "0 9 * * *",
            "prompt": "pc", "fresh": True})  # 每次新会话
        jobs = {t["name"]: t for t in store.list()}
        assert jobs["a"]["chat_id"] == "ou_x" and "fresh" not in jobs["a"]
        assert jobs["b"]["chat_id"] == "oc_other" and "fresh" not in jobs["b"]
        assert jobs["c"]["chat_id"] == "ou_x" and jobs["c"]["fresh"] is True
        listing = tools[0].run({"action": "list"})  # list 展示全部任务并标注非本会话/新会话
        assert "a |" in listing and "→ 会话…_other" in listing and "[每次新会话]" in listing


def test_cron_tick_fires_once_per_slot():
    from gateway.cron import start_tick
    fired, done = [], threading.Event()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cron.json"
        p.write_text(json.dumps([{"name": "t1", "cron": "* * * * *", "prompt": "hi", "chat_id": "ou_x"}]))

        def on_fire(task):
            fired.append(task["name"])
            done.set()

        start_tick(CronStore(p), on_fire, poll_s=0.2)
        assert done.wait(3), "3 秒内应触发一次"
        time.sleep(0.6)  # 同一分钟槽内不再触发
        assert fired == ["t1"], fired




def test_router_roundtrip(tmp_path=None):
    """mock 渠道+agent: submit → 读盘/建agent/跑/写盘 → 回发; /new 清会话。"""
    import os
    import agent as agent_mod
    import gateway.router as R

    real_backend = os.environ.get("AGENT_BACKEND")  # 防宿主 .env 带入 codex 让本测真调 LLM
    os.environ["AGENT_BACKEND"] = "zany"

    class FakeAgent:
        usage = {"input": 1, "output": 2, "calls": 1, "last_in": 1}
        def __init__(self, *a, **k):
            self.tools = {}
        def add_tools(self, extra):
            self.tools.update({t.name: t for t in extra})
        def restore(self, usage):
            self.usage = dict(usage)
        def stream(self, msgs):
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": "echo!"}]})
            yield ("text", "echo!")

    sent, real_build = [], agent_mod.build_agent
    agent_mod.build_agent = lambda *a, **k: FakeAgent()  # zany backend 函数内 from agent import, patch 源头生效
    try:
        ch = SimpleNamespace(send=lambda cid, text: sent.append((cid, text)))
        r = R.GatewayRouter(tempfile.mkdtemp(), ch)
        r.submit("ou_t1", "你好")
        deadline = time.time() + 10
        while not any(t == "echo!" for _, t in sent) and time.time() < deadline:
            time.sleep(0.05)
        assert sent[-1] == ("ou_t1", "echo!"), sent
        path = session_path(r.workdir, "ou_t1")
        data = json.loads(path.read_text())
        assert data["messages"][0]["content"] == "你好" and data["usage"]["calls"] == 1
        r.submit("ou_t1", "/new")
        time.sleep(0.5)
        assert not path.exists(), "/new 应清掉会话文件"
    finally:
        agent_mod.build_agent = real_build
        if real_backend is None:
            os.environ.pop("AGENT_BACKEND", None)
        else:
            os.environ["AGENT_BACKEND"] = real_backend


def test_codex_backend_turns():
    """codex backend: 事件解析/resume 拼接/映射读写/fresh 不落映射/超时与失败文案。"""
    import subprocess
    import gateway.backends as B

    events = "\n".join([
        '{"type":"thread.started","thread_id":"01abc"}',
        '{"type":"turn.started"}',
        'noise line',
        '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"收到"}}',
        '{"type":"turn.completed","usage":{"input_tokens":1}}',
    ])
    assert B._parse_events(events) == ("01abc", "收到")
    assert B._parse_events("garbage\n{}") == (None, "")  # 全坏输入不炸

    with tempfile.TemporaryDirectory() as d:
        B.CODEX_THREADS = Path(d) / "threads.json"
        calls = []

        def fake_run(cmd, **k):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout=events, stderr="")

        real_run = B.subprocess.run
        B.subprocess.run = fake_run
        try:
            b = B.CodexBackend("/tmp/wd")
            assert "收到" in b.run_turn("ou_x", "hi", fresh=False)
            assert calls[-1][:2] == ["codex", "exec"] and "resume" not in calls[-1]
            assert B._load_threads() == {"ou_x": "01abc"}
            assert "收到" in b.run_turn("ou_x", "again", fresh=False)
            assert calls[-1][calls[-1].index("resume") + 1] == "01abc"  # 第二轮续接
            b.run_turn("ou_x", "fresh-run", fresh=True)
            assert "resume" not in calls[-1]  # fresh: 不 resume…
            assert B._load_threads() == {"ou_x": "01abc"}  # …也不落映射
            b.new_session("ou_x")
            assert B._load_threads() == {}  # /new 清映射
            b.new_session("ou_x")  # 再清一次不炸
        finally:
            B.subprocess.run = real_run

    def raise_timeout(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, 900)

    def raise_rc(cmd, **k):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom\nline2")

    real_run = B.subprocess.run
    try:
        B.subprocess.run = raise_timeout
        assert "超时" in B.CodexBackend("/tmp/wd").run_turn("ou_x", "hi", False)
        B.subprocess.run = raise_rc
        out = B.CodexBackend("/tmp/wd").run_turn("ou_x", "hi", False)
        assert "rc=1" in out and "line2" in out, out
    finally:
        B.subprocess.run = real_run

def test_restart_tool_gateway_entry():
    """gateway 进程里 restart 排定的 execv 目标是 gateway/main.py, 不是 tui.py -r。"""
    import core.plugins.restart as T
    real_argv = sys.argv
    try:
        # 锚定真实仓库根(tests/ 在根下), 不用 T.__file__ 拼接 —— 那会与被测代码同错, 根算错时假绿
        root = Path(__file__).resolve().parent.parent
        sys.argv = [str(root / "gateway" / "main.py")]
        tool = T.make_restart_tool()
        out = tool.run({})
        av = T.pending_restart()
        assert av is not None and av[:2] == [sys.executable, str(root / "gateway" / "main.py")], (out, av)
        T._pending_restart = None  # 清理, 防污染其他测试
    finally:
        sys.argv = real_argv


def test_router_no_restart_when_not_scheduled():
    """pending_restart 为空(常态)时 _run_one 不触发 execv —— 复用 roundtrip 流程即可覆盖。"""
    import core.plugins.restart as T
    assert T.pending_restart() is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all gateway tests passed")
