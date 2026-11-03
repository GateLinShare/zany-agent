"""本地浏览器工具(navigate/click/type), playwright + 自带 chrome-headless-shell。

与 hermes browser 工具同思路的极简版:
- 页面表示用 aria snapshot(YAML 文本, 元素带 [ref=eN]), 无视觉也能定位交互
- 浏览器进程惰性启动、全程复用一个 page; 崩了下次调用自动重启
- 错误即文本约定: 工具不抛异常, 崩溃转 "错误: ..." 让模型自愈

安装: bash install_browser.sh (解压 assets/ 的 zip + uv sync 装 playwright)
"""

import os
import re
from pathlib import Path

# .env(若 agent.py 已加载)优先; 否则指向项目内 .pw-browsers(install_browser.sh 的解压目标)
# 插件文件在 core/plugins/ 下, 上溯两级才是仓库根(别用 parent.parent)
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path(__file__).resolve().parents[2] / ".pw-browsers"),
)

_SNAP_MAX = 8000  # 快照上限(字符), 大页面防刷爆上下文
_NAV_TIMEOUT = 30_000  # playwright 毫秒

# 可交互元素选择器 + 打 ref 标记的 JS: 给每个元素挂 data-maref=eN,
# click/type 按属性选择器定位(自控、导航后重打即可), 不依赖 playwright 内部 aria-ref API
_TAG_JS = """
() => {
  const sel = 'a, button, input, textarea, select, [role=\\"button\\"], [role=\\"link\\"], [role=\\"textbox\\"], [onclick], [contenteditable]';
  const els = [...document.querySelectorAll(sel)];
  els.forEach((el, i) => el.setAttribute('data-maref', 'e' + (i + 1)));
  return els.map((el, i) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return null;  // 不可见(隐藏菜单等)不列出, 标记留着无害
    const tag = el.tagName.toLowerCase();
    const name = (el.getAttribute('aria-label') || el.getAttribute('placeholder')
      || (el.innerText || el.value || '').trim().replace(/\\s+/g, ' ').slice(0, 40) || '').slice(0, 60);
    const href = tag === 'a' ? ' -> ' + (el.getAttribute('href') || '') : '';
    return `e${i + 1} ${tag}${name ? ' "' + name + '"' : ''}${href}`;
  }).filter(Boolean);
}
"""

_TEXT_MAX = 3000  # 正文文本上限

# ---- 安全护栏(学 hermes browser_tool) ----

# URL 里带密钥直接拒绝: 防 prompt injection 诱导访问 evil.com/?key=sk-ant-... 外泄
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|sk-ant-|Bearer\s+[A-Za-z0-9._-]{8,}|"
    r"(?i:api[_-]?key|token|secret|password)=[^&\s]{8,})"
)
# SSRF: 环回/链路本地/云元数据地址不导航(内网工具仍保留这条, prompt injection 的主要外跳面)
_SSRF_RE = re.compile(
    r"^(?:https?://)?(?:localhost|127\.|0\.0\.0\.0|\[::1\]|169\.254|10\.|192\.168|172\.(1[6-9]|2\d|3[01])\.)",
    re.I,
)


def _url_guard(url: str) -> str | None:
    """返回拒绝原因, None = 放行。URL 解码后再查一遍, 防 %2D 编码绕过。"""
    from urllib.parse import unquote

    for u in (url, unquote(url)):
        if _SSRF_RE.match(u.strip()):
            return "错误: 已拦截对本地/内网地址的导航(SSRF 防护)"
        if _SECRET_RE.search(u):
            return "错误: URL 疑似携带密钥/token, 已拦截(密钥不能进 URL)"
    return None


class _Session:
    """单例 page: 惰性启动, 复用; 任何一步崩了标记 _dead, 下次调用重建。"""

    def __init__(self) -> None:
        self._pw = self._browser = self._page = None
        self._dead = False

    def _start(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # ponytail: 代理走 .env 的 BROWSER_PROXY(如 socks5://127.0.0.1:10808), 未设=直连
        px = os.environ.get("BROWSER_PROXY")
        self._browser = self._pw.chromium.launch(
            headless=True, proxy={"server": px} if px else None
        )
        self._page = self._browser.new_page()
        self._dead = False

    def page(self):
        if self._dead or self._page is None:
            if self._pw is not None:  # 清掉上一具尸体
                try:
                    self._pw.stop()
                except Exception:
                    pass
            self._start()
        return self._page

    def crash(self):
        self._dead = True

    def close(self):
        for closer in (lambda: self._browser.close(), lambda: self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        self._pw = self._browser = self._page = None


_SESS: _Session | None = None


def _session() -> _Session:
    global _SESS
    if _SESS is None:
        _SESS = _Session()
    return _SESS


def _snapshot(page) -> str:
    """重打 ref 标记 + 拼快照: 标题/URL、可交互元素表(eN ref 内联)、正文截断。"""
    try:
        page.evaluate(_TAG_JS)
        rows = page.evaluate(_TAG_JS)  # 打标 + 取行信息一次完成
    except Exception:
        rows = []
    els = "\n".join(rows) if rows else "(无可交互元素)"
    try:
        body = page.evaluate("() => document.body ? document.body.innerText : ''").strip()
        if len(body) > _TEXT_MAX:
            body = body[:_TEXT_MAX] + f"\n…(正文过长, 共 {len(body)} 字符)"
    except Exception:
        body = "(正文不可用)"
    snap = f"--- 可交互元素 ---\n{els}\n--- 正文 ---\n{body or '(空)'}"
    if len(snap) > _SNAP_MAX:
        snap = snap[: int(_SNAP_MAX * 0.6)] + "\n…(快照过长, 截断)\n" + snap[-int(_SNAP_MAX * 0.4):]
    return snap or "(页面无可访问元素)"


def _wrap(page, extra: str = "") -> str:
    head = f"[{page.title()}]  {page.url}"
    return head + ("\n" + extra if extra else "") + "\n\n" + _snapshot(page)


def _navigate(args: dict) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "错误: url 必填"
    if blocked := _url_guard(url):
        return blocked
    if "://" not in url:
        url = "https://" + url
    try:
        page = _session().page()
        page.goto(url, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
        page.wait_for_timeout(500)  # JS 渲染余量
        return _wrap(page)
    except Exception as e:
        _session().crash()
        return f"错误: 导航失败 {e}"


def _click(args: dict) -> str:
    ref = str(args.get("ref", "")).lstrip("@")
    if not ref:
        return "错误: ref 必填(来自最近一次快照的元素行, 如 e1)"
    try:
        page = _session().page()
        page.locator(f'[data-maref="{ref}"]').click(timeout=10_000)
        page.wait_for_timeout(500)
        return _wrap(page, f"已点击 @{ref}")
    except Exception as e:
        _session().crash()
        return f"错误: 点击失败 {e}"


def _type(args: dict) -> str:
    ref = str(args.get("ref", "")).lstrip("@")
    text = str(args.get("text", ""))
    if not ref:
        return "错误: ref 必填(来自最近一次快照的元素行, 如 e2)"
    try:
        page = _session().page()
        loc = page.locator(f'[data-maref="{ref}"]')
        loc.fill(text, timeout=10_000)
        if args.get("submit"):
            loc.press("Enter", timeout=10_000)
            page.wait_for_timeout(800)
        return _wrap(page, f"已在 @{ref} 输入 {len(text)} 字符" + (" 并回车" if args.get("submit") else ""))
    except Exception as e:
        _session().crash()
        return f"错误: 输入失败 {e}"


SNAP_SCHEMA = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "元素 ref, 如 e12(来自上次 snapshot 的 [ref=eN])"},
        "text": {"type": "string", "description": "输入文本(type 工具用)"},
        "submit": {"type": "boolean", "description": "输入后按回车(默认 false)"},
    },
    "required": ["ref"],
}


def browser_tools() -> list:
    from core.tools import Tool

    return [
        Tool(
            "browser_navigate",
            "无头浏览器打开 URL, 返回标题 + 可交互元素表(eN ref 供 browser_click/browser_type 定位) + 正文。"
            "JS 渲染页、需登录/点击才能拿的内容用它; 纯静态抓取用 execute curl 更快。",
            {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            _navigate,
        ),
        Tool(
            "browser_click",
            "点击快照里的元素(eN), 返回点击后的新快照。",
            SNAP_SCHEMA,
            _click,
        ),
        Tool(
            "browser_type",
            "向快照里的输入框(eN)输入文本; submit=true 时回车提交(搜索/登录)。返回新快照。",
            SNAP_SCHEMA,
            _type,
        ),
    ]


if __name__ == "__main__":  # 自检: install_browser.sh 最后一步跑这个
    r = _navigate({"url": "https://example.com"})
    assert "Example Domain" in r, r
    print(r.split("\n\n")[0])
    print("selftest OK")


def register(workdir: str) -> list:
    return browser_tools()
