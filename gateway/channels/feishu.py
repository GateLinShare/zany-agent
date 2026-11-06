"""飞书渠道: ws 长连接收消息(SDK 官方事件) + 消息发送/回复 + 门禁与去重。

SDK(lark-oapi) 延迟到 start()/send() 才 import —— 没装 extra 时不炸模块,
gateway/main.py 能给出清晰的安装提示。纯函数(解析/分段/门禁)放这里, 单测不打 SDK。

门禁(与 docs/feishu-integration.md §1.3 一致):
- FEISHU_ALLOWED_USERS 为空/未命中 = 回 open_id 配置引导(陌生人可自助拿 id 加白), 敲门日志同步留痕
- 群聊必须 @bot 才算正对 bot 说话; 私聊天然全归 bot
- 非文本消息: 明确回"仅支持文本"(用户能感知, 与白名单拒绝的沉默区分开)
"""

import json
import re
import time
from collections import deque
from pathlib import Path

from core.sessions import project_dir

SEND_CHUNK = 4000  # 发送分段上限(字符): 飞书 text 消息舒适区, 超长切多段
IMG_DIR = Path("/tmp/feishu-images")  # 图片消息落盘处, 会话文件里存路径占位符


def sniff_image_ext(data: bytes) -> str:
    """magic bytes 推图片扩展名(下载响应不保证带文件名); 兼不全认 .jpg(飞书图片多为 jpeg)。"""
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


# ---- 纯函数(可单测) ----

def parse_incoming(data) -> dict | None:
    """P2ImMessageReceiveV1 -> {message_id, chat_id, chat_type, sender_id, text, mentions, is_text}。

    非文本/解析失败返回带 is_text=False 或 None(调用方决定沉默还是提示)。
    """
    try:
        ev = data.event
        msg = ev.message
        mentions = [
            {"key": m.key, "open_id": getattr(getattr(m, "id", None), "open_id", ""), "name": m.name}
            for m in (getattr(msg, "mentions", None) or [])
        ]
        out = {
            "message_id": msg.message_id,
            "chat_id": msg.chat_id,
            "chat_type": msg.chat_type,
            "sender_id": getattr(getattr(ev.sender, "sender_id", None), "open_id", ""),
            "mentions": mentions,
        }
        if msg.message_type == "image":
            out["image_key"] = json.loads(msg.content).get("image_key", "")
            out["is_text"] = False
            return out
        if msg.message_type != "text":
            out["is_text"] = False
            return out
        text = json.loads(msg.content).get("text", "")
        # mention 占位(@_user_1)替换成真人名, 再剥首尾 @bot 名(@你是 bot 时 SDK 常带 name)
        for m in mentions:
            if m["key"]:
                text = text.replace(m["key"], m["name"] or "")
        out["text"] = text.strip()
        out["is_text"] = True
        return out
    except Exception:
        return None


def is_addressed(item: dict, bot_open_id: str) -> bool:
    """群聊必须 @bot(mentions 里有 bot 的 open_id); 私聊(p2p)天然通过。
    bot_open_id 未配时群聊宽松放行(docs §1.3) —— 严格 @ 检测要填 FEISHU_BOT_OPEN_ID。"""
    if item.get("chat_type") == "p2p" or not bot_open_id:
        return True
    return any(m["open_id"] == bot_open_id for m in item.get("mentions", []))


def split_message(text: str, limit: int = SEND_CHUNK) -> list[str]:
    """超长分段: 优先按换行边界切, 单行超长再硬切。"""
    if len(text) <= limit:
        return [text] if text else []
    out, buf = [], ""
    for para in text.split("\n"):
        while len(para) > limit:  # 单行超长硬切
            out.append(para[:limit])
            para = para[limit:]
        if len(buf) + len(para) + 1 > limit:
            out.append(buf)
            buf = para
        else:
            buf = f"{buf}\n{para}" if buf else para
    if buf:
        out.append(buf)
    return out


def session_path(workdir: str, chat_id: str) -> Path:
    """chat -> 会话文件: <桶>/feishu-<chat_id>.json, chat_id 直接推导(无 state.json)。"""
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in chat_id)
    return project_dir(workdir) / f"feishu-{safe}.json"


class FeishuChannel:
    """收(阻塞 start, 由调用方放线程/主流程)+ 发(独立可测)。"""

    def __init__(self, on_message, allowed_users: set[str], bot_open_id: str):
        """on_message(item: dict): 解析+门禁后的消息回调, 在 SDK 的回调线程执行。"""
        self._on_message = on_message
        self._allowed = allowed_users
        self._bot_open_id = bot_open_id
        self._seen: deque[str] = deque(maxlen=2000)  # message_id 去重(飞书会重推)
        # ponytail: 去重表落盘 —— gateway 重启内存表清空, 重推的旧消息会被再回一遍(用户看到"没问却回了")
        self._seen_file = Path(__file__).resolve().parent / ".seen_msgids"
        if self._seen_file.exists():
            self._seen.extend(self._seen_file.read_text(encoding="utf-8").split()[-2000:])
        self._client = None  # lazy: import 才建

    def start(self) -> None:
        """ws 长连接, 阻塞直到进程退出。"""
        import os

        try:
            from lark_oapi import Client
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws import Client as WsClient
        except ImportError as e:
            raise SystemExit(
                f"缺少飞书 SDK({e}); 运行: uv sync --extra feishu"
            ) from e
        self._client = Client.builder().app_id(os.environ["FEISHU_APP_ID"]).app_secret(
            os.environ["FEISHU_APP_SECRET"]).build()
        handler = (
            # ponytail: ws 长连接模式事件不走加密, 两个 key 传空串(SDK 签名必填)
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_event)
            .register_p2_im_message_message_read_v1(lambda data: None)  # 已读回执: 不处理, 不注册会刷 processor not found 错误
            .build()
        )
        WsClient(
            app_id=os.environ["FEISHU_APP_ID"],
            app_secret=os.environ["FEISHU_APP_SECRET"],
            event_handler=handler,
            log_level=__import__("lark_oapi").LogLevel.WARNING,
        ).start()

    # ---- 收 ----

    def _handle_event(self, data) -> None:
        item = parse_incoming(data)
        if item is None:
            return
        mid = item["message_id"]
        if mid in self._seen:
            return
        self._seen.append(mid)
        with self._seen_file.open("a", encoding="utf-8") as f:
            f.write(mid + "\n")  # ponytail: 追加式, 每千行截到 maxlen, 防无限涨
        if self._seen_file.stat().st_size > 40_000:  # ~2000 行 id
            self._seen_file.write_text("\n".join(self._seen) + "\n", encoding="utf-8")
        if not is_addressed(item, self._bot_open_id):
            return  # 群里没 @bot: 沉默, 不打扰群成员
        if item["sender_id"] not in self._allowed:
            # 敲门日志保留(运维留痕/收集新作用域 id) + 引导回复: 告诉请求者自己的 open_id 和加白方法
            print(f"(白名单外消息: sender={item['sender_id']} chat={item['chat_id'][:12]}…)", flush=True)
            self.send(item["chat_id"],
                      "⛔ 你还没有使用本 bot 的权限。\n"
                      f"你的 open_id: {item['sender_id']}\n"
                      "加白方法: 把上述 id 添加到网关 .env 的 FEISHU_ALLOWED_USERS"
                      "(多个用逗号分隔), 然后重启 gateway 即可。")
            return
        if not item["is_text"]:
            if item.get("image_key") and self._download_image(item):
                self._on_message(item)  # 图片已落盘, text 是 [图片: 路径] 占位符
            elif item.get("image_key"):
                self.send(item["chat_id"], "⚠️ 图片下载失败, 请重发一次。")
            else:
                self.send(item["chat_id"], "抱歉, 我目前只认文本消息和图片。")
            return
        if item["text"]:
            self._on_message(item)

    def _download_image(self, item: dict) -> bool:
        """飞书图片消息 → 落盘 IMG_DIR, item["text"] 填 [图片: 路径](core/messages 转 image 块)。"""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        req = (GetMessageResourceRequest.builder()
               .message_id(item["message_id"])
               .file_key(item["image_key"])
               .type("image")
               .build())
        try:
            resp = self._client.im.v1.message_resource.get(req)
        except Exception as e:
            print(f"(图片下载异常: {e})", flush=True)
            return False
        if not resp.success() or not resp.file:
            print(f"(图片下载失败: {resp.code} {resp.msg})", flush=True)
            return False
        data = resp.file.read()
        IMG_DIR.mkdir(parents=True, exist_ok=True)
        path = IMG_DIR / f"{item['message_id']}{sniff_image_ext(data)}"
        path.write_bytes(data)
        item["text"] = f"[图片: {path}]"
        item["is_text"] = True
        return True

    # ---- 发 ----

    def send(self, chat_id: str, text: str) -> None:
        """发到 chat(群/私聊同参)。每段独立 create, 顺序依赖 SDK 同步调用天然保序。"""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        for chunk in split_message(text):
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": chunk}, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            resp = None
            for attempt in range(3):  # DNS/网络秒级抖动重试(3 次 × 3s), 全败才抛 —— 回复丢了用户看不见也没法重发
                resp = self._client.im.v1.message.create(req)
                if resp.success():
                    break
                time.sleep(3)
            if not resp.success():
                raise RuntimeError(f"feishu 发送失败(重试 3 次后) chat={chat_id[:12]}…: "
                                   f"{resp.code} {resp.msg}")
