"""浏览器桥 —— 附着到用户的浏览器，借它的网络栈与登录态取数据。

对应 DESIGN.md §5.2 / 原则 6。

要解决的问题：
    `bgm.tv` / `api.bgm.tv` 在本机被 DNS 污染，Python 直连超时；
    而浏览器能正常访问（Chrome 默认启用 Secure DNS）。

    实测证据：系统解析 `api.bgm.tv` 得到 `108.160.166.49`（Dropbox 段，投毒）；
    阿里 DoH 返回 `157.240.8.41`（Facebook 段，同样是投毒）；
    腾讯 DoH 能解出真实 Cloudflare IP，但仍需自己实现 DoH 与连通性验证。

    于是不如**把浏览器当成数据通道**：零新增网络代码，顺带拿到登录态。
"""

from __future__ import annotations

import re
from typing import Any

from .. import constants
from ..cdp import CDPBrowser, CDPError, CDPPage, port_open
from ..models import HealthStatus, Tab


class BrowserBridgeError(RuntimeError):
    pass


class BrowserBridge:
    def __init__(self, port: int = constants.BROWSER_DEBUG_PORT, timeout: float | None = None):
        self.port = port
        self.browser = CDPBrowser(port, timeout or constants.CDP_CALL_TIMEOUT)

    # ---------------------------------------------------------------- 健康

    def health(self) -> HealthStatus:
        if not port_open(self.port):
            return HealthStatus(
                False,
                f"浏览器调试端口 {self.port} 未监听 —— 请用带调试参数的方式启动浏览器",
            )
        try:
            return self.browser.health()
        except CDPError as e:
            return HealthStatus(False, str(e))

    def require(self) -> None:
        st = self.health()
        if not st.ok:
            raise BrowserBridgeError(st.detail)

    # ---------------------------------------------------------------- 标签页

    def list_tabs(self) -> list[Tab]:
        return self.browser.list_tabs()

    def find_tab(self, url_pattern: str) -> Tab | None:
        return self.browser.find_tab(url_pattern)

    def open_tab(self, url: str) -> Tab:
        return self.browser.open_tab(url)

    def close_tab(self, tab: Tab) -> None:
        self.browser.close_tab(tab)

    def page(self, tab: Tab) -> CDPPage:
        return self.browser.page(tab)

    # ---------------------------------------------------------------- 求值

    def eval_in_tab(self, tab: Tab, expression: str, await_promise: bool = True) -> Any:
        page = self.page(tab)
        try:
            return page.evaluate(expression, await_promise=await_promise)
        finally:
            page.close()

    def read_text(self, tab: Tab) -> str:
        """读 `document.body.innerText` —— 对 JSON 响应页就是原文。"""
        return self.eval_in_tab(tab, "document.body ? document.body.innerText : ''") or ""

    def navigate_and_wait(self, page: CDPPage, url: str, timeout: float = 20.0) -> bool:
        """导航并等待**真的到了那一页**。

        `expect_url` 不能省 —— 新建/刚导航的标签页是 about:blank，
        它的 readyState 本来就是 complete（见 `CDPPage.wait_ready`）。
        """
        self.browser.navigate(page, url)
        return page.wait_ready(timeout, expect_url=url)

    def open_tab_and_wait(self, url: str, timeout: float = 20.0) -> tuple[Tab, CDPPage]:
        """新建标签页并等它加载好。返回 (tab, page) —— 调用方负责关掉。"""
        tab = self.open_tab(url)
        page = self.page(tab)
        if not page.wait_ready(timeout, expect_url=url):
            # 没到目标页就别让调用方拿到一个空页面
            page.close()
            self.close_tab(tab)
            raise BrowserBridgeError(f"标签页没有加载到目标地址（超时 {timeout:.0f}s）：{url}")
        return tab, page

    # ---------------------------------------------------------------- 常用组合

    def open_read_close(self, url: str, wait: float = 1.5) -> str:
        """开一个后台标签页、读文本、关闭。

        这是绕开 CORS 的通用手法：**顶层导航不受同源策略约束**，
        因此任何「浏览器能到但页内 fetch 被 CORS 拦」的站点都能这样取数。
        """
        import time

        tab, page = self.open_tab_and_wait(url)
        try:
            time.sleep(wait)  # 给 SPA / 延迟渲染留点时间
            return self.read_text(tab)
        finally:
            page.close()
            self.close_tab(tab)

    def fetch_json_in_tab(self, tab: Tab, url: str, timeout_ms: int = 15000) -> Any:
        """页内 fetch，继承该页的 Cookie 与浏览器 DNS。

        注意：**受 CORS 限制**。实测 `api.bgm.tv` 不认 `https://bgm.tv` 这个源，
        所以 Bangumi 不靠这条路（见 §5.3.2 的实测结论）。
        保留此方法供其它同源场景使用。
        """
        js = f"""
        (async () => {{
          const ctl = new AbortController();
          const t = setTimeout(() => ctl.abort(), {timeout_ms});
          try {{
            const r = await fetch({url!r}, {{credentials: 'include', signal: ctl.signal}});
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return await r.text();
          }} finally {{ clearTimeout(t); }}
        }})()
        """
        return self.eval_in_tab(tab, js)

    # ---------------------------------------------------------------- Bangumi 专用

    BGM_URL_RE = re.compile(r"^https?://(?:www\.)?(bgm\.tv|bangumi\.tv)")

    def find_bangumi_tab(self) -> Tab | None:
        for t in self.list_tabs():
            if self.BGM_URL_RE.match(t.url or ""):
                return t
        return None

    def bangumi_uid(self, tab: Tab | None = None) -> str | None:
        """从导航栏徽章区读登录用户的 UID。

        必须限定在 `.idBadgerNeue` 内 —— 宽泛的 `a[href*="/user/"]`
        会误命中好友链接（实测踩过）。
        """
        js = f"""
        (function() {{
          var a = document.querySelector({constants.SEL_BGM_USER!r});
          if (!a) return null;
          var m = (a.getAttribute('href') || '').match(/\\/user\\/(\\d+)/);
          return m ? m[1] : null;
        }})()
        """
        if tab is not None:
            return self.eval_in_tab(tab, js)
        found = self.find_bangumi_tab()
        if found is None:
            # 没有 bgm 标签页时，临时开一个首页读一次
            text = self.open_read_close("https://bgm.tv/", wait=0.8)
            m = re.search(r'href="/user/(\d+)"', text)
            return m.group(1) if m else None
        return self.eval_in_tab(found, js)
