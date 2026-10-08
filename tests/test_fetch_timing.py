"""方向 D（可见性）+ B1（缩短超时/重试）的回归测试。

## 这两项改动的来源（真机实测，不是推测）

用户报「拉取番单耗时非常长」。他那次跑留在 `data/cache/` 里的 71 个文件是
完整证据链：6 部番 71 次请求、总长 429 秒，其中 **6 段停顿合计 355.6 秒
（83%）**，最长一段 **156.2 秒** —— 与当时的预算
「5 次尝试 × 30 秒超时 + 退避 7.5 秒 = 157.5 秒」只差 1.3 秒。
另外 57 次维基请求里有 **51 次是在问不存在的词条**（409 字节的 missingtitle）。

所以这里钉住三件事：

  ① **记账必须准**：请求数、重试、失败、慢请求、缓存命中，各是各的，
     而且 `requests` 与 `retried` 之间要有恒等关系可验（§6 不许伪造指标）。
  ② **「查无此词条」与「请求失败」必须分开**：合成一个数就会把梯子故障
     报成「维基没收录」，用户会照着错的结论去手动补数据。
  ③ **超时/重试必须是可调的配置**（`fetch.http_timeout` / `http_retries`），
     默认值要对得上那 156.2 秒的教训。

全部离线：monkeypatch `urlopen`，不联网、不碰真实配置/数据库。
"""

from __future__ import annotations

import os
import shutil
import sys
import types
import unittest
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

from animusic import constants, http as http_mod  # noqa: E402
from animusic.http import HttpClient, HttpError, RequestStats, brief_url  # noqa: E402
from animusic.orchestrator import Orchestrator  # noqa: E402
from animusic.sources.wikipedia import WikipediaSource  # noqa: E402

#: 测试临时根目录。
#: ⚠️ 为什么不用 `tempfile.mkdtemp()`：这个环境里系统临时目录**写不了**
#: （`_Cache.put` 会把 OSError 静默吞掉，`sqlite3.connect` 直接报
#: "unable to open database file"）。第一次跑本文件时两条测试就是这么假失败的。
_TMP_ROOT = os.path.join(_ROOT, "data", "_test_tmp", "fetch_timing")


def _tmpdir(case: unittest.TestCase, name: str) -> str:
    """给一条测试一个干净的仓库内临时目录。"""
    p = os.path.join(_TMP_ROOT, f"{name}_{id(case)}")
    shutil.rmtree(p, ignore_errors=True)
    os.makedirs(p, exist_ok=True)
    case.addCleanup(shutil.rmtree, p, True)
    return p


# ==================================================================== 假件

class Clock:
    """可控时钟 —— 让「这次请求花了 4 秒」可以被离线构造出来。"""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, on_read=None):
        self.status = status
        self._body = body
        self.headers = {"content-type": "application/json"}
        self._on_read = on_read

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        if self._on_read is not None:
            self._on_read()
        return self._body


class HttpCase(unittest.TestCase):
    """把 `urlopen` / `time.monotonic` / `time.sleep` 换掉，跑真实的 `request()`。"""

    def setUp(self):
        self._saved = (http_mod.urllib.request.urlopen,
                       http_mod.time.monotonic,
                       http_mod.time.sleep)
        self.clock = Clock()
        self.sleeps: list[float] = []
        self.calls: list[str] = []
        http_mod.time.monotonic = self.clock
        http_mod.time.sleep = lambda s: self.sleeps.append(s)
        self.addCleanup(self._restore)

    def _restore(self):
        (http_mod.urllib.request.urlopen, http_mod.time.monotonic,
         http_mod.time.sleep) = self._saved

    def patch_urlopen(self, fn):
        http_mod.urllib.request.urlopen = fn

    def client(self, **kw) -> HttpClient:
        kw.setdefault("rps", 0)          # 关掉限速，让 sleep 只剩退避
        return HttpClient(**kw)


# ==================================================================== ① 记账

class TestRequestAccounting(HttpCase):
    def test_attempts_retries_and_backoff_are_counted(self):
        """全失败时：3 次尝试、2 次重试、1 个失败 URL，退避要单独记。"""

        def boom(req, timeout=None, context=None):
            self.calls.append(req.full_url)
            raise TimeoutError("模拟超时")

        self.patch_urlopen(boom)
        events: list[str] = []
        c = self.client(retries=2, on_event=events.append)

        with self.assertRaises(HttpError):
            c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=X")

        s = c.stats
        self.assertEqual(s.requests, 3, "retries=2 应当是 3 次尝试")
        self.assertEqual(s.retried, 2)
        self.assertEqual(s.failed, 1, "失败的是 URL 数，不是尝试数")
        self.assertEqual(s.cache_hits, 0)
        self.assertGreater(s.backoff, 0.0, "退避要记在 backoff 里，不能混进 seconds")
        self.assertEqual(len(events), 3, "每次失败的尝试都要有一条日志")
        self.assertTrue(all("请求失败" in e for e in events))
        self.assertTrue(any("第 3/3 次" in e for e in events))

    def test_requests_equals_first_attempts_plus_retries(self):
        """恒等关系：`requests == 首次尝试数 + retried`（§6 要求数字可复算）。"""
        state = {"n": 0}

        def sometimes(req, timeout=None, context=None):
            state["n"] += 1
            if state["n"] < 3:
                raise TimeoutError("先抖两下")
            return FakeResponse(b'{"ok": 1}')

        self.patch_urlopen(sometimes)
        c = self.client(retries=4)
        c.get("https://api.animethemes.moe/anime")

        s = c.stats
        self.assertEqual(s.requests, 3)
        self.assertEqual(s.retried, 2)
        self.assertEqual(s.failed, 0)
        self.assertEqual(s.requests, 1 + s.retried)

    def test_slow_request_is_reported_with_its_url(self):
        """4 秒的响应 → 记 1 次慢请求，并给出能定位的 URL。"""

        def slow(req, timeout=None, context=None):
            return FakeResponse(b'{"ok": 1}',
                                on_read=lambda: setattr(self.clock, "t", 1004.0))

        self.patch_urlopen(slow)
        events: list[str] = []
        c = self.client(slow_seconds=3.0, on_event=events.append)
        c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=%E6%B0%B7")

        self.assertEqual(c.stats.slow, 1)
        self.assertAlmostEqual(c.stats.max_seconds, 4.0, places=3)
        self.assertEqual(len(events), 1)
        self.assertIn("慢请求 4.0s", events[0])
        self.assertIn("ja.wikipedia.org", events[0])

    def test_fast_request_is_not_reported(self):
        """快请求不报 —— 否则 70 次请求会把日志淹掉，慢的那几条就看不见了。"""
        self.patch_urlopen(lambda req, timeout=None, context=None:
                           FakeResponse(b'{"ok": 1}'))
        events: list[str] = []
        c = self.client(slow_seconds=3.0, on_event=events.append)
        c.get("https://api.animethemes.moe/anime")
        self.assertEqual(events, [])
        self.assertEqual(c.stats.slow, 0)

    def test_cache_hit_is_not_a_request(self):
        """缓存命中**不计入** requests —— 「请求了 70 次」和「缓存答了 70 次」
        是两件事，混在一起就看不出梯子到底有没有被用到。"""
        tmp = _tmpdir(self, "cache")
        saved = constants.CACHE_DIR
        constants.CACHE_DIR = tmp
        self.addCleanup(lambda: setattr(constants, "CACHE_DIR", saved))

        self.patch_urlopen(lambda req, timeout=None, context=None:
                           FakeResponse(b'{"ok": 1}'))
        c = self.client(cache_ttl=600)
        url = "https://api.animethemes.moe/anime?filter%5Byear%5D=2026"

        first = c.get_json(url, use_cache=True)
        # ⚠️ 先断言「真的落了盘」：`_Cache.put` 会把 OSError 静默吞掉，
        # 不检查的话「缓存没写成」会伪装成「缓存没命中」，排查时白费一轮
        # （本测试第一次跑就踩到了这个 —— 临时目录不可写）。
        self.assertTrue(os.listdir(tmp), "缓存文件没写出来，后面的断言就没意义了")

        second = c.get_json(url, use_cache=True)

        self.assertEqual(first, second)
        self.assertEqual(c.stats.requests, 1, "第二次应当走缓存")
        self.assertEqual(c.stats.cache_hits, 1)

    def test_4xx_is_not_retried_but_is_counted_as_failed(self):
        err = urllib.error.HTTPError("https://api.animethemes.moe/anime",
                                     404, "Not Found", {}, None)
        # 显式 close：不然 GC 会打一条 ResourceWarning，把真正的失败淹没在噪音里
        self.addCleanup(err.close)

        def not_found(req, timeout=None, context=None):
            raise err

        self.patch_urlopen(not_found)
        c = self.client(retries=4)
        with self.assertRaises(HttpError):
            c.get("https://api.animethemes.moe/anime")
        self.assertEqual(c.stats.requests, 1, "404 不该重试")
        self.assertEqual(c.stats.retried, 0)
        self.assertEqual(c.stats.failed, 1)

    def test_broken_callback_does_not_break_fetching(self):
        """日志是观测手段，观测坏了不该把被观测的东西搞坏。"""
        self.patch_urlopen(lambda req, timeout=None, context=None:
                           FakeResponse(b'{"ok": 1}',
                                        on_read=lambda: setattr(self.clock, "t", 1009.0)))

        def explode(_msg):
            raise RuntimeError("日志回调自己炸了")

        c = self.client(slow_seconds=1.0, on_event=explode)
        r = c.get("https://api.animethemes.moe/anime")
        self.assertEqual(r.status, 200)
        self.assertEqual(c.stats.slow, 1)

    def test_requests_identity_holds_with_cache_hits_too(self):
        """恒等关系在**混合**场景下也要成立：

            网络请求 == (调用次数 − 缓存命中) + 重试

        真机那条日志就是靠它才能对上账：62 次调用 → 120 次网络请求，
        差值 58 **全是重试**（缓存命中 0）。第一版措辞写成「含缓存命中」，
        数字没错但指错了方向 —— 这条测试连措辞一起钉住。
        """
        tmp = _tmpdir(self, "identity")
        saved = constants.CACHE_DIR
        constants.CACHE_DIR = tmp
        self.addCleanup(lambda: setattr(constants, "CACHE_DIR", saved))

        state = {"n": 0}

        def flaky(req, timeout=None, context=None):
            state["n"] += 1
            if state["n"] in (2, 3):        # 两次请求失败 → 产生重试
                raise TimeoutError("抖一下")
            return FakeResponse(b'{"ok": 1}')

        self.patch_urlopen(flaky)
        c = self.client(cache_ttl=600, retries=2)

        calls = 0
        for url in ("https://a.test/x", "https://a.test/y"):
            calls += 1
            try:
                c.get_json(url, use_cache=True)
            except HttpError:
                pass
            calls += 1
            c.get_json(url, use_cache=True)      # 第二次应当命中缓存

        s = c.stats
        self.assertGreater(s.cache_hits, 0, "前提：得有缓存命中")
        self.assertGreater(s.retried, 0, "前提：得有重试")
        self.assertEqual(s.requests, (calls - s.cache_hits) + s.retried)

    def test_post_cache_key_includes_the_body(self):
        """⚠️ 缓存键必须带上请求体 —— **不带就会静默串数据**。

        AniList 是 GraphQL：**同一个 URL、不同的 body**（第 1 页 / 第 2 页、
        不同季度）。键只有 `method + url` 时，第 2 页会拿到第 1 页的缓存，
        `Fall` 会拿到 `Summer` 的 —— 表现还特别像「AniList 返回了重复的番」。

        这就是 AniList 那边一直写着 `cache_ttl=0` 的**真实原因**（当时只当它是
        「先别开缓存，看清事实再说」，没人把因果写下来）—— 这条测试把它钉住，
        否则下一个人会先开缓存、再去查那个很难查的数据错误。
        """
        tmp = _tmpdir(self, "post_key")
        saved = constants.CACHE_DIR
        constants.CACHE_DIR = tmp
        self.addCleanup(lambda: setattr(constants, "CACHE_DIR", saved))

        seen: list[str] = []

        def body_echo(req, timeout=None, context=None):
            seen.append(req.data.decode("utf-8"))
            return FakeResponse(b'{"page": ' + str(len(seen)).encode() + b'}')

        self.patch_urlopen(body_echo)
        c = self.client(cache_ttl=600)
        url = "https://graphql.anilist.co"

        a1 = c.post_json(url, {"variables": {"page": 1}}, use_cache=True)
        b1 = c.post_json(url, {"variables": {"page": 2}}, use_cache=True)
        a2 = c.post_json(url, {"variables": {"page": 1}}, use_cache=True)

        self.assertEqual(len(seen), 2, "两个不同的 body 必须是两次请求")
        self.assertNotEqual(a1, b1, "第 2 页不许拿到第 1 页的缓存")
        self.assertEqual(a1, a2, "同一个 body 要命中缓存")
        self.assertEqual(c.stats.cache_hits, 1)

    def test_merge_sums_and_keeps_the_slowest(self):
        a = RequestStats(requests=57, seconds=88.0, slow=4, max_seconds=15.0,
                         slowest="wiki")
        b = RequestStats(requests=13, cache_hits=2, cache_write_failed=1, failed=1,
                         max_seconds=9.0, slowest="at", errors=["x"])
        a.merge(b)
        self.assertEqual(a.requests, 70)
        self.assertEqual(a.cache_hits, 2)
        self.assertEqual(a.cache_write_failed, 1)
        self.assertEqual(a.failed, 1)
        self.assertEqual(a.max_seconds, 15.0)
        self.assertEqual(a.slowest, "wiki")

    def test_cache_write_failure_does_not_fail_the_request(self):
        """⚠️ 这条钉的是一个**实测抓到的真 bug**。

        `_Cache.put` 原来把 `os.makedirs` 放在 try 之外，它抛的 `OSError`
        冒到 `request()` 的 `except (URLError, TimeoutError, OSError)` 分支 ——
        于是一个**完全成功的响应被当成请求失败**，重试到耗尽后整步抓取报错。
        现实触发条件：程序装在只读目录（打包版放 Program Files）。
        """
        tmp = _tmpdir(self, "blocked")
        blocker = os.path.join(tmp, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("我是一个文件，不是目录")
        saved = constants.CACHE_DIR
        # 把一个**文件**当成缓存目录的父路径 → makedirs 必然 OSError
        constants.CACHE_DIR = os.path.join(blocker, "sub")
        self.addCleanup(lambda: setattr(constants, "CACHE_DIR", saved))

        self.patch_urlopen(lambda req, timeout=None, context=None:
                           FakeResponse(b'{"ok": 1}'))
        events: list[str] = []
        c = self.client(cache_ttl=600, retries=2, on_event=events.append)

        r = c.get("https://api.animethemes.moe/anime", use_cache=True)

        self.assertEqual(r.status, 200, "取数成功了，不该因为写不了缓存而报错")
        self.assertEqual(c.stats.requests, 1, "不该重试")
        self.assertEqual(c.stats.failed, 0, "这不是请求失败")
        self.assertEqual(c.stats.cache_write_failed, 1)
        self.assertTrue(any("缓存没写成" in e for e in events),
                        "缓存写失败必须可见 —— 否则用户只会觉得「今天怎么特别慢」")

    def test_limiter_wait_is_counted_separately(self):
        """限速等待必须**单独**记：真机那次「本步用时 163.0 秒」里，
        「网络往返 89.0 + 退避 44.2」只解释了 133 秒 —— 剩下 30 秒就是它，
        当时没量，账对不上。"""
        self.patch_urlopen(lambda req, timeout=None, context=None:
                           FakeResponse(b'{"ok": 1}'))
        c = HttpClient(rps=2.0, retries=0)      # 1 次/0.5 秒
        for _ in range(3):
            c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=X")

        self.assertEqual(c.stats.requests, 3)
        self.assertGreater(c.stats.limiter_seconds, 0.0,
                           "第二次开始就该有 0.5 秒的限速等待")
        # 限速等待不是网络时间，也不是退避
        self.assertEqual(c.stats.backoff, 0.0)

    def test_brief_url_keeps_the_page_name_and_drops_the_noise(self):
        u = ("https://ja.wikipedia.org/w/api.php?action=parse&page=%E6%B0%B7%E3%81%AE"
             "&prop=wikitext&format=json&redirects=1&formatversion=2")
        b = brief_url(u)
        self.assertIn("action=parse", b)
        self.assertIn("page=", b)
        self.assertNotIn("formatversion", b)


# ============================================== ② 查无此词条 ≠ 请求失败

class FakeWikiClient:
    """顶替 `HttpClient`：带一个真的 `RequestStats`，但按模式决定答什么。"""

    def __init__(self, mode: str, error: Exception | None = None):
        self.mode = mode
        self.error = error
        self.stats = RequestStats()

    def get_json(self, url, **kw):
        if self.mode == "missing":
            return {"error": {"code": "missingtitle",
                              "info": "The page you specified doesn't exist."}}
        raise self.error or HttpError("模拟梯子断了")


class TestWikiFailureKinds(unittest.TestCase):
    def test_missing_page_is_not_a_network_failure(self):
        src = WikipediaSource(client=FakeWikiClient("missing"), rps=0)
        self.assertIsNone(src.wikitext("存在しない記事"))
        self.assertEqual(src.stats["not_found"], 1)
        self.assertEqual(src.stats["api_failed"], 0)
        self.assertEqual(src.stats["page_hits"], 0)

    def test_network_failure_is_not_a_missing_page(self):
        """⚠️ 这条是本次改动的核心之一：以前两者都返回 `{}`，
        于是「维基没收录」这句话可能是梯子抖出来的。"""
        src = WikipediaSource(client=FakeWikiClient("boom"), rps=0)
        self.assertIsNone(src.wikitext("何かの記事"))
        self.assertEqual(src.stats["api_failed"], 1)
        self.assertEqual(src.stats["not_found"], 0)

    def test_search_failure_returns_empty_not_a_crash(self):
        src = WikipediaSource(client=FakeWikiClient("boom"), rps=0)
        self.assertEqual(src.search("何か アニメ"), [])

    def test_last_error_records_the_status_code(self):
        """自检要能说出「429 被限流」而不是含糊的「请求失败」。

        所以失败原因里必须带**状态码** —— 而 `HttpError.status` 需要在
        重试耗尽的最终 raise 上活下来（以前丢掉了）。
        """
        err = HttpError("请求失败：…", status=429)
        src = WikipediaSource(client=FakeWikiClient("boom", error=err), rps=0)
        src.wikitext("何か")
        self.assertEqual(src.stats["last_error"], "HTTP 429")

    def test_last_error_falls_back_to_exception_type(self):
        src = WikipediaSource(client=FakeWikiClient("boom"), rps=0)
        src.wikitext("何か")
        self.assertIn("HttpError", src.stats["last_error"])

    def test_retries_zero_means_one_network_request(self):
        """自检用 `retries=0`：45 秒内被限流时，自检自己不该再去撞三次。"""
        class Counting(FakeWikiClient):
            def __init__(self):
                super().__init__("boom")
                self.n = 0

            def get_json(self, url, **kw):
                self.n += 1
                raise TimeoutError("通不了")

        fake = Counting()
        src = WikipediaSource(client=fake, retries=0)
        src.search("鬼滅の刃")
        self.assertEqual(fake.n, 1)

    def test_missing_and_failed_are_both_counted_across_calls(self):
        src = WikipediaSource(client=FakeWikiClient("missing"), rps=0)
        src.wikitext("A")
        src.wikitext("B")
        self.assertEqual(src.stats["api_calls"], 2)
        self.assertEqual(src.stats["not_found"] + src.stats["api_failed"], 2)


# ============================================== ③ 本步时间花在哪

class FakeSource:
    """只有 `http.stats` 的假数据源 —— 用来验汇总行的算术。"""

    def __init__(self, stats: RequestStats, wiki: dict | None = None):
        self.http = types.SimpleNamespace(stats=stats)
        if wiki is not None:
            self.stats = wiki


def bare_orch(*, wikipedia=None, animethemes=None, anilist=None) -> tuple[Orchestrator, list[str]]:
    logs: list[str] = []
    orch = Orchestrator.__new__(Orchestrator)
    orch.progress = logs.append
    orch._slow_seconds = 3.0
    if wikipedia is not None:
        orch.wikipedia = wikipedia
    if animethemes is not None:
        orch.animethemes = animethemes
    orch._anilist_source = anilist
    return orch, logs


class TestTimingLines(unittest.TestCase):
    def _wiki(self) -> FakeSource:
        return FakeSource(
            RequestStats(requests=57, slow=4, failed=3, retried=6,
                         seconds=88.3, backoff=7.5, max_seconds=15.0,
                         slowest="https://ja.wikipedia.org/w/api.php?action=parse&page=X",
                         errors=["ja.wikipedia.org/w/api.php?action=parse&page=X —— TimeoutError: x"]),
            {"api_calls": 6, "titles_asked": 60, "titles_cached": 0,
             "not_found": 51, "page_hits": 6, "titles_unanswered": 3,
             "search_used": 1, "api_failed": 3},
        )

    def test_summary_splits_by_source(self):
        """按源分开数 —— 三个源走的出口不一样（维基在大陆必须过代理）。"""
        orch, logs = bare_orch(wikipedia=self._wiki(),
                               animethemes=FakeSource(RequestStats(requests=13)),
                               anilist=FakeSource(RequestStats(requests=2)))
        for line in orch._timing_lines(429.3):
            logs.append(line)
        joined = "\n".join(logs)

        self.assertIn("本步用时 429.3 秒", joined)
        self.assertIn("HTTP 请求 72 次", joined)      # 57 + 13 + 2
        self.assertIn("维基 57", joined)
        self.assertIn("AnimeThemes 13", joined)
        self.assertIn("AniList 2", joined)
        self.assertIn("慢请求 ≥3s 4 次", joined)
        self.assertIn("失败 3 次（重试 6 次）", joined)
        self.assertIn("网络往返合计 88.3 秒", joined)
        self.assertIn("退避等待 7.5 秒", joined)

    def test_wiki_breakdown_separates_missing_from_failed(self):
        orch, logs = bare_orch(wikipedia=self._wiki(),
                               animethemes=FakeSource(RequestStats()))
        joined = "\n".join(orch._timing_lines(10.0))
        self.assertIn("查无此词条 51", joined)
        self.assertIn("取到正文 6", joined)
        self.assertIn("搜索兜底 1", joined)
        self.assertIn("请求失败 3", joined)
        self.assertIn("不是「维基没有」", joined)

    def test_wiki_breakdown_says_where_the_gap_comes_from(self):
        """⚠️ 这一行必须能**逐项对账**：

            网络请求 = 调用 − 缓存命中 − 熔断跳过 + 重试 + 限流重试

        第一版写的是「维基查询 44 次（含缓存命中）」。真机数据立刻打脸：
        120 网络请求 / 62 调用，差值 58 **全是重试**，缓存命中是 0。
        措辞指错方向，用户就会去怀疑缓存没生效（实测就是这么发生的）。

        后来又漏了一项：真机重跑打出「维基调用 20 次 → 网络请求 21 次
        （重试 0 · 缓存命中 0）」，差 1 对不上账 —— 那 1 次是
        **按服务端要求等过再重发**的那条（`rate_limit_retries`）。
        """
        wiki = self._wiki()          # requests=57, retried=6, cache_hits=0
        wiki.stats["api_failed"] = 0
        wiki.stats["last_error"] = ""
        orch, _ = bare_orch(wikipedia=wiki)
        line = "\n".join(orch._timing_lines(1.0))
        self.assertIn("维基调用 6 次", line)
        self.assertIn("网络请求 57 次", line)
        self.assertIn("重试 6", line)
        self.assertIn("限流重试 0", line)
        self.assertIn("缓存命中 0", line)
        self.assertNotIn("含缓存命中", line)

    def test_wiki_breakdown_shows_the_last_error(self):
        wiki = self._wiki()
        wiki.stats["api_failed"] = 28
        wiki.stats["last_error"] = "HTTP 429"
        orch, _ = bare_orch(wikipedia=wiki)
        line = "\n".join(orch._timing_lines(1.0))
        self.assertIn("请求失败 28", line)
        self.assertIn("最近一次失败：HTTP 429", line)

    def test_failed_requests_are_listed_with_reasons(self):
        orch, _ = bare_orch(wikipedia=self._wiki())
        joined = "\n".join(orch._timing_lines(10.0))
        self.assertIn("失败的请求", joined)
        self.assertIn("TimeoutError", joined)

    def test_no_lines_without_real_sources(self):
        """测试里用 `__new__` 组装的假源没有 `http.stats` —— 不许编数字。"""
        orch, _ = bare_orch(wikipedia=object(), animethemes=object())
        self.assertEqual(orch._timing_lines(5.0), [])

    def test_only_failed_requests_are_counted_when_no_backoff(self):
        orch, _ = bare_orch(wikipedia=FakeSource(RequestStats(requests=3)))
        joined = "\n".join(orch._timing_lines(1.0))
        self.assertNotIn("退避等待", joined, "没有重试就不该出现退避那一项")
        self.assertNotIn("慢请求", joined)
        self.assertNotIn("失败", joined)

    def test_cache_hits_are_shown_but_kept_out_of_requests(self):
        orch, _ = bare_orch(wikipedia=FakeSource(RequestStats(requests=6, cache_hits=51)))
        joined = "\n".join(orch._timing_lines(1.0))
        self.assertIn("HTTP 请求 6 次", joined)
        self.assertIn("缓存命中 51 次", joined)

    def test_cache_write_failure_is_surfaced(self):
        orch, _ = bare_orch(wikipedia=FakeSource(
            RequestStats(requests=6, cache_write_failed=6)))
        joined = "\n".join(orch._timing_lines(1.0))
        self.assertIn("缓存没写成 6 次", joined)
        self.assertIn("下次重跑会重新请求", joined)

    def test_limiter_wait_closes_the_time_account(self):
        """三项相加要能解释总用时（剩下的才是本地处理）。

        真机：163.0 = 网络往返 89.0 + 退避 44.2 + **限速等待 ~30** —— 最后一项
        当时没量，所以那 30 秒在日志里是"消失"的。
        """
        orch, _ = bare_orch(wikipedia=FakeSource(
            RequestStats(requests=62, seconds=89.0, backoff=44.2,
                         limiter_seconds=29.6)))
        joined = "\n".join(orch._timing_lines(163.0))
        self.assertIn("网络往返合计 89.0 秒", joined)
        self.assertIn("限速等待 29.6 秒", joined)
        self.assertIn("退避等待 44.2 秒", joined)

    def test_limiter_wait_hidden_when_zero(self):
        orch, _ = bare_orch(wikipedia=FakeSource(RequestStats(requests=3)))
        self.assertNotIn("限速等待", "\n".join(orch._timing_lines(1.0)))


# ============================================== ④ 接线：步骤里真的会打

class TestStepWiring(unittest.TestCase):
    """`fetch_season` / `refresh_themes` 收尾必须真的打出这几行。

    光有 `_timing_lines` 不算数 —— 用户看不到就等于没做（这次改动的全部意义
    就是「下一次重跑能自己说清时间去哪了」）。
    """

    def setUp(self):
        from animusic.models import BangumiSubject
        from animusic.sources.bangumi import ReadResult
        from animusic.store import Store

        self.tmp = _tmpdir(self, "step")
        self.store = Store(os.path.join(self.tmp, "s.db"))
        self.addCleanup(self.store.close)
        self.BangumiSubject = BangumiSubject
        self.ReadResult = ReadResult

    def _orch(self, logs: list[str]) -> Orchestrator:
        from datetime import date

        from animusic.models import Season
        from animusic.normalize import AliasTable

        subjects = [self.BangumiSubject(bgm_id=1, title_cn="番A", title_jp="ばんA",
                                        air_date=date(2026, 10, 1), url="u")]

        class B:
            def read_watching(self_inner):
                return self.ReadResult(list(subjects), "api_tab", "1 条")

        class W:
            stats = {"api_calls": 7, "titles_asked": 7, "not_found": 5, "page_hits": 2,
                     "search_used": 0, "api_failed": 0}

            def __init__(self):
                self.http = types.SimpleNamespace(
                    stats=RequestStats(requests=7, slow=1, max_seconds=4.0,
                                       slowest="https://ja.wikipedia.org/x"))

            def fetch_themes(self, names):
                return [], ""

        class AT:
            def __init__(self):
                self.http = types.SimpleNamespace(stats=RequestStats(requests=1))

            def fetch_season(self, season):
                return []

            def search_anime(self, title, limit=5):
                return []

        orch = Orchestrator.__new__(Orchestrator)
        orch.store = self.store
        orch.aliases = AliasTable()
        orch.progress = logs.append
        orch.bangumi = B()
        orch.wikipedia = W()
        orch.animethemes = AT()
        orch._at_pool_by_season = {}
        orch._anilist_source = None
        orch._anilist_pool = lambda season: []
        orch._slow_seconds = 3.0
        orch.config = types.SimpleNamespace(bangumi=types.SimpleNamespace(
            include_cross_season=True))
        self.season = Season.parse("2026Q4")
        return orch

    def test_fetch_season_prints_the_timing_summary(self):
        logs: list[str] = []
        self._orch(logs).fetch_season(self.season)
        joined = "\n".join(logs)
        self.assertIn("本步用时", joined)
        self.assertIn("HTTP 请求 8 次", joined)          # 维基 7 + AnimeThemes 1
        self.assertIn("查无此词条 5", joined)
        self.assertIn("慢请求 ≥3s 1 次", joined)


# ============================================== ⑤ 配置旋钮（B1）

class TestFetchBudgetIsConfigurable(unittest.TestCase):
    def test_defaults_are_shorter_than_the_generic_timeout(self):
        """默认值必须显著短于 30 秒 / 4 次 —— 那正是 156.2 秒的来源。"""
        from animusic.config import Config

        f = Config().fetch
        self.assertLess(f.http_timeout, constants.HTTP_TIMEOUT)
        self.assertEqual(f.http_timeout, constants.FETCH_HTTP_TIMEOUT)
        self.assertLess(f.http_retries, 4)
        self.assertEqual(f.http_retries, constants.FETCH_HTTP_RETRIES)

    def test_worst_case_per_url_is_bounded(self):
        """单个 URL 的最坏代价 = (retries+1) × timeout + 退避上限。

        旧值 157.5 秒（真机实测到 156.2）；新值必须明显更小，
        否则「一个词条名烧 2.5 分钟」这件事没有修。
        """
        from animusic.config import Config

        f = Config().fetch
        backoff_max = sum(min(8.0, (2.0 ** i) * 0.5) * 1.3
                          for i in range(f.http_retries))
        worst = (f.http_retries + 1) * f.http_timeout + backoff_max
        self.assertLess(worst, 60.0, f"单个 URL 最坏仍有 {worst:.1f} 秒")
        self.assertLess(worst, 157.5 / 2)

    def test_editorial_template_matches_defaults(self):
        """`settings.example.yaml` 必须带上这三个旋钮（否则用户不知道能调）。"""
        import yaml

        path = os.path.join(_ROOT, "config", "settings.example.yaml")
        with open(path, encoding="utf-8") as f:
            example = yaml.safe_load(f)
        fetch = example["fetch"]
        for k in ("http_timeout", "http_retries", "slow_request_seconds"):
            self.assertIn(k, fetch, f"模板缺少 fetch.{k}")

    def test_sources_accept_the_budget(self):
        """三个源都要能接住这几个参数 —— 编排层是按统一一套传下去的。"""
        from animusic.sources.anilist import AniListSource
        from animusic.sources.animethemes import AnimeThemesSource

        for src in (WikipediaSource(timeout=7.0, retries=1, slow_seconds=2.0),
                    AnimeThemesSource(timeout=7.0, retries=1, slow_seconds=2.0),
                    AniListSource(timeout=7.0, retries=1, slow_seconds=2.0)):
            self.assertEqual(src.http.timeout, 7.0)
            self.assertEqual(src.http.retries, 1)
            self.assertEqual(src.http.slow_seconds, 2.0)
            self.assertIsInstance(src.http.stats, RequestStats)


# ============================================== ⑥ AniList 的缓存（顺手项）

class TestAniListCache(unittest.TestCase):
    """AniList 每次跑**固定 2 次请求**（`max_pages=2`），而一季的番单一天里
    基本不动 —— 给它开缓存（1 小时，与 AnimeThemes 同档）。

    ⚠️ 顺序不能反：先修 `HttpClient` 的缓存键（带上请求体），再开这个缓存。
    AniList 是 GraphQL，同一个 URL、不同 body（第 1/2 页、不同季度），
    键里不带 body 就会串页/串季度 —— 见 `test_post_cache_key_includes_the_body`。
    这正是它以前写死 `cache_ttl=0` 的真实原因。
    """

    def test_source_caches_by_default(self):
        from animusic.sources.anilist import AniListSource

        src = AniListSource()
        self.assertTrue(src.http.cache.enabled, "默认应当开缓存（以前是关的）")
        self.assertGreater(src.http.cache.ttl, 0)

    def test_ttl_is_configurable_and_disableable(self):
        from animusic.sources.anilist import AniListSource

        self.assertFalse(AniListSource(cache_ttl=0).http.cache.enabled,
                         "0 = 不缓存（调试用，与另外两个源一致）")
        self.assertEqual(AniListSource(cache_ttl=123.0).http.cache.ttl, 123.0)

    def test_config_and_template_have_the_knob(self):
        import yaml

        from animusic.config import Config

        c = Config().fetch
        self.assertEqual(c.anilist_cache_ttl, 3600.0)
        self.assertEqual(c.anilist_cache_ttl, c.animethemes_cache_ttl,
                         "与 AnimeThemes 同档：两者都是「一季基本不动的番单」")
        with open(os.path.join(_ROOT, "config", "settings.example.yaml"),
                  encoding="utf-8") as f:
            ex = yaml.safe_load(f)
        self.assertIn("anilist_cache_ttl", ex["fetch"], "模板缺 fetch.anilist_cache_ttl")

    def test_orchestrator_passes_the_ttl_through(self):
        """配了却不传下去 = 没配。编排层是懒建 AniList 源的那个地方。"""
        from unittest import mock

        from animusic.models import Season

        orch = Orchestrator.__new__(Orchestrator)
        orch._http_kw = {}
        orch._anilist_ttl = 123.0
        orch._anilist_source = None
        orch._at_pool_by_season = {}
        orch.progress = lambda m: None

        with mock.patch("animusic.sources.anilist.AniListSource") as cls:
            cls.return_value.fetch_season.return_value = []
            orch._anilist_pool(Season.parse("2026Q4"))
        self.assertEqual(cls.call_args.kwargs.get("cache_ttl"), 123.0)


if __name__ == "__main__":
    unittest.main()
