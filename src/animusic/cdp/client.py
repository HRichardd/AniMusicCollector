"""CDP（Chrome DevTools Protocol）客户端封装。

两个 CDP 目标共用这一份实现：
    · 浏览器      :9223   —— 用于读 Bangumi
    · 网易云客户端 :9222   —— 用于搜索与写入歌单

实现选择说明：
    打开标签页走 **HTTP 端点**（`PUT /json/new`），不走 `Target.createTarget`。
    因为这样可以直接拿到新标签页自己的 `webSocketDebuggerUrl`，
    不需要 `Target.attachToTarget` + `sessionId` 那套扁平会话，实现简单得多，
    而且本轮 M0 探测里已经用这条路验证通过。

    页面求值走 **页面级 WebSocket**，只用到 `Runtime.evaluate` 一个域。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import constants
from ..models import BrowserInfo, HealthStatus, Tab

try:
    from websockets.sync.client import connect as ws_connect
except ImportError:  # pragma: no cover
    ws_connect = None


class CDPError(RuntimeError):
    pass


def _http(port: int, path: str, method: str = "GET", timeout: float = 5.0) -> Any:
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        raise CDPError(f"CDP HTTP {e.code} {e.reason} for {path}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise CDPError(f"CDP 端点不可达：{path} —— {e}") from None


def port_open(port: int, timeout: float = constants.CDP_CONNECT_TIMEOUT) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _same_page(href: str, expect: str) -> bool:
    """当前 href 是否就是目标页（比对 host + path，忽略 query/hash）。

    只比 host+path 是刻意的：有些站点会改 query，但 path 是稳的。
    """
    if not href or href == "about:blank":
        return False
    try:
        a = urllib.parse.urlsplit(href)
        b = urllib.parse.urlsplit(expect)
    except ValueError:
        return False
    return a.netloc == b.netloc and a.path.rstrip("/") == b.path.rstrip("/")


# ---------------------------------------------------------------- 页面会话

class CDPPage:
    """一个标签页的 CDP 会话（页面级 WebSocket）。"""

    def __init__(self, ws_url: str, timeout: float = constants.CDP_CALL_TIMEOUT):
        if ws_connect is None:
            raise CDPError("缺少 websockets 依赖： pip install websockets")
        self.ws_url = ws_url
        self.timeout = timeout
        self._seq = 0
        self._lock = threading.Lock()
        try:
            self._ws = ws_connect(ws_url, max_size=64 * 1024 * 1024,
                                  open_timeout=timeout, close_timeout=2)
        except Exception as e:
            raise CDPError(f"无法连接页面 WebSocket：{e}") from None

    def call(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            self._seq += 1
            mid = self._seq
            payload = {"id": mid, "method": method, "params": params or {}}
            try:
                self._ws.send(json.dumps(payload))
            except Exception as e:
                raise CDPError(f"发送失败：{method} —— {e}") from None

            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                try:
                    raw = self._ws.recv(timeout=max(0.1, deadline - time.monotonic()))
                except TimeoutError:
                    raise CDPError(f"等待响应超时：{method}") from None
                except Exception as e:
                    raise CDPError(f"接收失败：{method} —— {e}") from None
                msg = json.loads(raw)
                if msg.get("id") != mid:
                    continue  # 事件或别的响应，忽略
                if "error" in msg:
                    raise CDPError(f"{method} 返回错误：{msg['error']}")
                return msg.get("result", {})
            raise CDPError(f"等待响应超时：{method}")

    def evaluate(self, expression: str, await_promise: bool = True) -> Any:
        res = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
        )
        if "exceptionDetails" in res:
            d = res["exceptionDetails"]
            raise CDPError(f"JS 抛错：{d.get('text') or d}")
        return res.get("result", {}).get("value")

    def wait_ready(self, timeout: float = 15.0, poll: float = 0.25,
                   expect_url: str | None = None) -> bool:
        """等待 `document.readyState == 'complete'`。

        ⚠️ **新建标签页时必须传 `expect_url`。**

        实测踩到的坑：`open_tab()` 刚建出来的标签页是 `about:blank`，
        而它的 `readyState` **本来就是 `complete`** ——
        只看 readyState 会立刻返回，然后读到一张空白页。
        症状很隐蔽：函数"成功"了，但内容是空的（0.1 秒就返回）。

        所以要看**两件事**：导航真的到了目标 URL，且页面加载完成。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    if expect_url is None:
                        return True
                    if _same_page(self.evaluate("location.href") or "", expect_url):
                        return True
            except CDPError:
                pass
            time.sleep(poll)
        return False

    def is_hidden(self) -> bool:
        try:
            return bool(self.evaluate("document.hidden"))
        except CDPError:
            return False

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 浏览器

class CDPBrowser:
    """一个 CDP 端点的浏览器级操作。"""

    def __init__(self, port: int, timeout: float = constants.CDP_CALL_TIMEOUT):
        self.port = port
        self.timeout = timeout

    # ---- 元信息
    def version(self) -> BrowserInfo:
        v = _http(self.port, "/json/version")
        return BrowserInfo(
            browser=v.get("Browser", ""),
            protocol=v.get("Protocol-Version", ""),
            ws_url=v.get("webSocketDebuggerUrl", ""),
        )

    def list_tabs(self) -> list[Tab]:
        raw = _http(self.port, "/json/list") or []
        out = []
        for t in raw:
            if t.get("type") != "page":
                continue
            out.append(
                Tab(
                    id=t.get("id", ""),
                    url=t.get("url", ""),
                    title=t.get("title", ""),
                    ws_url=t.get("webSocketDebuggerUrl", ""),
                )
            )
        return out

    def find_tab(self, url_pattern: str) -> Tab | None:
        import re

        rx = re.compile(url_pattern)
        for t in self.list_tabs():
            if rx.search(t.url or ""):
                return t
        return None

    def health(self) -> HealthStatus:
        if not port_open(self.port):
            return HealthStatus(False, f"端口 {self.port} 未监听")
        try:
            info = self.version()
        except CDPError as e:
            return HealthStatus(False, str(e))
        tabs = self.list_tabs()
        return HealthStatus(
            True,
            f"{info.browser}，{len(tabs)} 个标签页",
            {"browser": info.browser, "tabs": len(tabs),
             "urls": [t.url for t in tabs]},
        )

    # ---- 标签页操作
    def open_tab(self, url: str, timeout: float = 15.0) -> Tab:
        """新建标签页并导航（用 HTTP 端点，免去 Target 会话）。"""
        q = urllib.parse.quote(url, safe="")
        raw = _http(self.port, f"/json/new?{q}", method="PUT", timeout=timeout)
        if not raw or not raw.get("id"):
            raise CDPError(f"新建标签页失败：{raw!r}")
        return Tab(
            id=raw.get("id", ""),
            url=raw.get("url", url),
            title=raw.get("title", ""),
            ws_url=raw.get("webSocketDebuggerUrl", ""),
        )

    def close_tab(self, tab: Tab) -> None:
        try:
            _http(self.port, f"/json/close/{tab.id}", timeout=5)
        except CDPError:
            pass

    def navigate(self, page: CDPPage, url: str) -> None:
        page.call("Page.navigate", {"url": url})

    def page(self, tab: Tab) -> CDPPage:
        if not tab.ws_url:
            raise CDPError(f"标签页没有 webSocketDebuggerUrl：{tab.id}")
        return CDPPage(tab.ws_url, self.timeout)
