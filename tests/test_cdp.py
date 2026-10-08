"""CDP 客户端离线测试 —— 用假 CDP 服务器验证整条链路。

为什么要这个：
    `cdp/client.py` + `sources/browser_bridge.py` 是 M2/M3 的地基，
    但它们只能对着真实浏览器验证，而浏览器要么没启动、要么在受限环境里起不来。
    结果就是**最容易出错的 WebSocket 管道长期零覆盖**。

    办法：自己写一个假 CDP 端点 —— HTTP 侧提供 `/json/version`、`/json/list`、
    `PUT /json/new`、`/json/close/<id>`；WebSocket 侧按 CDP 协议回包。
    真实客户端的代码路径**完全不变**，只是对端换成了我们控制的实现。

    这样能在没有 Chrome 的情况下验证：
      · HTTP 端点解析
      · 标签页查找 / 新建 / 关闭
      · WebSocket 握手、请求-响应配对、事件消息跳过
      · `Runtime.evaluate` 往返与异常上报
      · BrowserBridge 的健康检查与降级

注意假服务器用**两个端口**（HTTP 一个、WS 一个）：
真实 CDP 两者同端口，但客户端是从 `/json/*` 返回的
`webSocketDebuggerUrl` 里读 WS 地址的，所以分开端口照样走完全相同的代码路径。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

from animusic.cdp import CDPBrowser, CDPError, CDPPage, port_open  # noqa: E402
from animusic.sources.browser_bridge import BrowserBridge  # noqa: E402

try:
    from websockets.sync.server import serve as ws_serve

    HAS_WS = True
except ImportError:  # pragma: no cover
    HAS_WS = False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ==================================================================== 假 CDP

class FakeCDP:
    """一个最小可用的 CDP 端点。"""

    def __init__(self):
        self.ws_port = _free_port()
        self.http_port = _free_port()
        self.tabs: dict[str, dict] = {}
        self.closed: list[str] = []
        self.eval_log: list[str] = []
        self.expect_exception = False
        #: `location.href` 的应答 —— 新建标签页时由 do_PUT 更新
        self.location_href: str = ""
        # 每条表达式 → 返回值；未命中则回 "echo:<expr>"
        self.responses: dict[str, object] = {
            "document.readyState": "complete",
            "document.hidden": False,
            "1+1": 2,
        }
        self._ws_server = None
        self._http_server = None
        self._threads: list[threading.Thread] = []

    # ---- WS 侧
    def _ws_handler(self, conn):
        for raw in conn:
            try:
                req = json.loads(raw)
            except ValueError:
                continue
            mid = req.get("id")
            method = req.get("method", "")
            if method == "Runtime.evaluate":
                expr = (req.get("params") or {}).get("expression", "")
                self.eval_log.append(expr)
                if self.expect_exception:
                    conn.send(json.dumps({
                        "id": mid,
                        "result": {"exceptionDetails": {"text": "boom"}, "result": {}},
                    }))
                    continue
                # `location.href` 要回**真实位置** —— 否则 `wait_ready(expect_url=...)`
                # 永远等不到。真实浏览器就是这样：新建标签页先是 about:blank，
                # readyState 本来就是 complete，只靠它判断会读到空白页。
                if expr == "location.href":
                    val = self.location_href or "about:blank"
                elif expr in self.responses:
                    val = self.responses[expr]
                else:
                    val = f"echo:{expr}"
                conn.send(json.dumps({
                    "id": mid,
                    "result": {"result": {"type": "object", "value": val}},
                }))
            else:
                conn.send(json.dumps({"id": mid, "result": {}}))

    # ---- HTTP 侧
    def _make_handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静音
                pass

            def _json(self, obj, status=200):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/json/version":
                    self._json({
                        "Browser": "FakeChrome/1.0",
                        "Protocol-Version": "1.3",
                        "webSocketDebuggerUrl":
                            f"ws://127.0.0.1:{fake.ws_port}/devtools/browser/x",
                    })
                elif path == "/json/list":
                    self._json(list(fake.tabs.values()))
                elif path.startswith("/json/close/"):
                    tid = path.rsplit("/", 1)[-1]
                    fake.closed.append(tid)
                    fake.tabs.pop(tid, None)
                    self._json({"ok": True})
                else:
                    self._json({"error": "not found"}, 404)

            def do_PUT(self):
                path = urlparse(self.path).path
                if path.startswith("/json/new"):
                    url = unquote(self.path.split("?", 1)[1]) if "?" in self.path else "about:blank"
                    tid = f"tab{len(fake.tabs) + 1}"
                    tab = {
                        "id": tid,
                        "type": "page",
                        "url": url,
                        "title": url,
                        "webSocketDebuggerUrl":
                            f"ws://127.0.0.1:{fake.ws_port}/devtools/page/{tid}",
                    }
                    fake.tabs[tid] = tab
                    # 导航真的会发生：把「当前位置」更新成新开的这个 URL
                    fake.location_href = url
                    self._json(tab)
                else:
                    self._json({"error": "not found"}, 404)

        return H

    # ---- 生命周期
    def start(self):
        if not HAS_WS:
            raise unittest.SkipTest("缺少 websockets")
        self._ws_server = ws_serve(self._ws_handler, "127.0.0.1", self.ws_port)
        t = threading.Thread(target=self._ws_server.serve_forever, daemon=True)
        t.start()
        self._threads.append(t)

        self._http_server = ThreadingHTTPServer(("127.0.0.1", self.http_port), self._make_handler())
        t2 = threading.Thread(target=self._http_server.serve_forever, daemon=True)
        t2.start()
        self._threads.append(t2)
        _wait_port(self.http_port)
        _wait_port(self.ws_port)
        return self

    def stop(self):
        for srv in (self._http_server, self._ws_server):
            try:
                if srv is not None:
                    srv.shutdown()
            except Exception:
                pass
        for srv in (self._http_server, self._ws_server):
            try:
                if srv is not None:
                    srv.server_close()
            except Exception:
                pass


def _wait_port(port: int, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"端口 {port} 未就绪")


# ==================================================================== 测试

@unittest.skipUnless(HAS_WS, "需要 websockets")
class TestCDPClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeCDP().start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def test_port_open(self):
        self.assertTrue(port_open(self.fake.http_port))
        self.assertFalse(port_open(_free_port(), timeout=0.2))

    def test_version(self):
        info = CDPBrowser(self.fake.http_port).version()
        self.assertEqual(info.browser, "FakeChrome/1.0")
        self.assertEqual(info.protocol, "1.3")
        self.assertTrue(info.ws_url.startswith("ws://"))

    def test_health_reports_tabs(self):
        st = CDPBrowser(self.fake.http_port).health()
        self.assertTrue(st.ok, st.detail)
        self.assertIn("FakeChrome", st.detail)

    def test_health_when_port_closed(self):
        st = CDPBrowser(_free_port()).health()
        self.assertFalse(st.ok)
        self.assertIn("未监听", st.detail)

    def test_open_tab_and_evaluate(self):
        """核心往返：新建标签页 → 连它的 WebSocket → 求值。"""
        br = CDPBrowser(self.fake.http_port)
        tab = br.open_tab("https://bgm.tv/anime/list/1000001/do")
        self.assertEqual(tab.url, "https://bgm.tv/anime/list/1000001/do")
        self.assertTrue(tab.ws_url)

        page = br.page(tab)
        try:
            self.assertEqual(page.evaluate("1+1"), 2)
            self.assertEqual(page.evaluate("hello"), "echo:hello")
            self.assertEqual(page.evaluate("document.readyState"), "complete")
            self.assertFalse(page.is_hidden())
        finally:
            page.close()

    def test_wait_ready(self):
        br = CDPBrowser(self.fake.http_port)
        tab = br.open_tab("about:blank")
        page = br.page(tab)
        try:
            self.assertTrue(page.wait_ready(timeout=3))
        finally:
            page.close()

    def test_js_exception_becomes_cdp_error(self):
        br = CDPBrowser(self.fake.http_port)
        tab = br.open_tab("about:blank")
        page = br.page(tab)
        self.fake.expect_exception = True
        try:
            with self.assertRaises(CDPError):
                page.evaluate("throw new Error('x')")
        finally:
            self.fake.expect_exception = False
            page.close()

    def test_find_tab(self):
        br = CDPBrowser(self.fake.http_port)
        br.open_tab("https://bgm.tv/subject/665390")
        got = br.find_tab(r"bgm\.tv/subject/")
        self.assertIsNotNone(got)
        self.assertIn("665390", got.url)
        self.assertIsNone(br.find_tab(r"never-matches-this"))

    def test_close_tab(self):
        br = CDPBrowser(self.fake.http_port)
        tab = br.open_tab("https://example.com/x")
        br.close_tab(tab)
        self.assertIn(tab.id, self.fake.closed)

    def test_bad_endpoint_raises(self):
        with self.assertRaises(CDPError):
            CDPBrowser(_free_port()).version()

    def test_page_without_ws_url_raises(self):
        from animusic.models import Tab

        with self.assertRaises(CDPError):
            CDPBrowser(self.fake.http_port).page(Tab(id="x", url="u", ws_url=""))


@unittest.skipUnless(HAS_WS, "需要 websockets")
class TestBrowserBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeCDP().start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.br = BrowserBridge(self.fake.http_port)

    def test_health_ok(self):
        st = self.br.health()
        self.assertTrue(st.ok, st.detail)

    def test_health_when_down(self):
        br = BrowserBridge(_free_port())
        st = br.health()
        self.assertFalse(st.ok)
        self.assertIn("未监听", st.detail)
        with self.assertRaises(Exception):
            br.require()

    def test_read_text(self):
        tab = self.br.open_tab("about:blank")
        self.assertEqual(self.br.read_text(tab), "echo:document.body ? document.body.innerText : ''")

    def test_open_read_close_cleans_up(self):
        """临时标签页必须被关掉，否则用户浏览器会堆一堆页签。"""
        before = len(self.br.list_tabs())
        text = self.br.open_read_close("https://api.bgm.tv/whatever", wait=0)
        self.assertTrue(text.startswith("echo:"))
        after = len(self.br.list_tabs())
        self.assertEqual(after, before, "临时标签页没有被关闭")

    def test_bangumi_uid_extraction(self):
        """从导航栏徽章区读 UID —— 必须限定作用域，不能误命中好友链接。"""
        self.fake.responses[
            "document.querySelector('.idBadgerNeue a[href*=\"/user/\"]') "
            "// uid probe"
        ] = None  # 确保没有意外命中
        tab = self.br.open_tab("https://bgm.tv/")
        # 假服务器对未知表达式回 "echo:..."，所以这里只验证「不抛异常且走对了作用域」
        got = self.br.eval_in_tab(tab, "document.querySelector('.idBadgerNeue a[href*=\"/user/\"]')")
        self.assertIn(".idBadgerNeue", got)

    def test_find_bangumi_tab(self):
        self.br.open_tab("https://bgm.tv/anime/list/1000001/do")
        t = self.br.find_bangumi_tab()
        self.assertIsNotNone(t)
        self.assertIn("bgm.tv", t.url)

    def test_find_bangumi_tab_ignores_others(self):
        """清掉所有标签页后不应误判。"""
        for t in list(self.br.list_tabs()):
            self.br.close_tab(t)
        self.assertIsNone(self.br.find_bangumi_tab())


if __name__ == "__main__":
    unittest.main(verbosity=2)
