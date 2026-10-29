"""必修 1：HTTP 429（限流）要按**服务端说的**办。

## 这一套改动要挡住的，是真机上量到过的那件事

    本步 163.0 秒 · HTTP 请求 135 次（维基 120 · AnimeThemes 13 · AniList 2）
    维基 62 次调用 → 120 次网络请求（重试 58 · 缓存命中 0）
    失败 28 次 · 退避等待 44.2 秒 · **最慢一次只有 2.9 秒**
    最近一次失败：HTTP 429 Too Many Requests

而服务端在响应里说得一清二楚（实测抄录）：

    HTTP/1.1 429 Too Many Requests
    server: envoy
    x-envoy-ratelimited: 1
    retry-after: 25
    You are making too many requests to the API. …

**服务端说等 25 秒，代码等 0.5 秒** —— 于是 28 个失败的 URL 各撞 3 次
= 84 次请求（占维基请求的 70%）+ 44.2 秒退避，换来 **0 个成功**。

所以这里钉住四件事：

  ① `Retry-After` 两种形式（秒数 / HTTP 日期）都能解析，大小写无关；
  ② 429 的等待**独立记账**（`rate_limit_wait`），不混进我们自己的 `backoff`，
     且**不占用**网络抖动的重试预算；
  ③ 同一域名**连续** 429 到阈值就熔断，之后**一个请求都不再发**；
     而中间夹着成功则清零（间歇限流不该熔断，靠减速兜）；
  ④ 熔断后**不许说「维基没收录」** —— 那是把我们被限流说成数据源没有，
     用户会照着错的结论去手动补数据。

全部离线：monkeypatch `urlopen`，不联网、不碰账号、不写用户库。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import types
import unittest
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

from animusic import constants, http as http_mod  # noqa: E402
from animusic.config import Config  # noqa: E402
from animusic.http import (  # noqa: E402
    HttpClient, RateLimited, header_value, parse_retry_after,
)
from animusic.models import Season, ThemeKind  # noqa: E402
from animusic.orchestrator import Orchestrator  # noqa: E402
from animusic.sources.animethemes import AtAnime, AtTheme  # noqa: E402
from animusic.sources.wikipedia import WikipediaSource  # noqa: E402

_TMP = os.path.join(_ROOT, "data", "_test_tmp", "rate_limit")


def _tmpdir(case: unittest.TestCase, name: str) -> str:
    p = os.path.join(_TMP, f"{name}_{id(case)}")
    shutil.rmtree(p, ignore_errors=True)
    os.makedirs(p, exist_ok=True)
    case.addCleanup(shutil.rmtree, p, True)
    return p


# ==================================================================== ① 解析

class TestRetryAfterParsing(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(parse_retry_after("25"), 25.0)
        self.assertEqual(parse_retry_after(" 3.5 "), 3.5)
        self.assertEqual(parse_retry_after("0"), 0.0)

    def test_http_date(self):
        """RFC 允许 HTTP 日期形式。不处理的话会被当成「没给」而走默认值。"""
        import email.utils

        future = time.time() + 30
        v = email.utils.formatdate(future, usegmt=True)
        got = parse_retry_after(v)
        self.assertIsNotNone(got)
        self.assertGreater(got, 20)
        self.assertLess(got, 35)

    def test_past_date_is_zero_not_negative(self):
        self.assertEqual(parse_retry_after("Thu, 01 Jan 1970 00:00:00 GMT"), 0.0)

    def test_missing_or_garbage(self):
        for v in (None, "", "   ", "soon", "12 monkeys"):
            self.assertIsNone(parse_retry_after(v), f"{v!r} 应当算「没给」")

    def test_header_value_is_case_insensitive(self):
        """真实 urllib 给的是大小写无关的 `Message`，而测试常喂 dict。

        第一版只用 `headers.get("Retry-After")` —— 测试喂 dict 时取不到，
        真机却拿得到，于是「测试过了但线上拿不到」会潜伏下来。
        """
        self.assertEqual(header_value({"Retry-After": "25"}, "Retry-After"), "25")
        self.assertEqual(header_value({"retry-after": "25"}, "Retry-After"), "25")
        self.assertEqual(header_value({"RETRY-AFTER": "25"}, "Retry-After"), "25")
        self.assertIsNone(header_value({}, "Retry-After"))
        self.assertIsNone(header_value(None, "Retry-After"))


# ==================================================================== HTTP 层

class HttpCase(unittest.TestCase):
    """把 `urlopen` / `time.sleep` / `time.monotonic` 换掉，跑真实的 `request()`。"""

    def setUp(self):
        self._saved = (http_mod.urllib.request.urlopen,
                       http_mod.time.monotonic,
                       http_mod.time.sleep)
        self.clock = [1000.0]
        self.sleeps: list[float] = []
        self.hits: list[str] = []
        http_mod.time.monotonic = lambda: self.clock[0]
        http_mod.time.sleep = lambda s: self.sleeps.append(s)
        self.addCleanup(self._restore)

    def _restore(self):
        (http_mod.urllib.request.urlopen, http_mod.time.monotonic,
         http_mod.time.sleep) = self._saved

    def raises_429(self, retry_after: str | None = 25):
        """让每个请求都返回 429（同一个异常对象重复抛，测完 close 掉）。"""
        hdrs = {} if retry_after is None else {"retry-after": str(retry_after)}
        err = urllib.error.HTTPError("https://ja.wikipedia.org/w/api.php",
                                     429, "Too Many Requests", hdrs, None)
        self.addCleanup(err.close)

        def fake(req, timeout=None, context=None):
            self.hits.append(getattr(req, "full_url", str(req)))
            raise err

        http_mod.urllib.request.urlopen = fake
        return err

    def then_ok(self, retry_after: str = "1"):
        """按 URL 决定：含 `page=OK` 的成功，其余一律 429。

        为什么不用「前 N 次 429、之后成功」：限流重试会消耗掉全部重试预算，
        那样根本走不到成功那一步（第一版就是这么写错的）。
        """
        err = urllib.error.HTTPError("https://ja.wikipedia.org/w/api.php",
                                     429, "Too Many Requests",
                                     {"retry-after": str(retry_after)}, None)
        self.addCleanup(err.close)

        class Resp:
            status = 200
            headers = {"content-type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": 1}'

        def fake(req, timeout=None, context=None):
            u = getattr(req, "full_url", str(req))
            self.hits.append(u)
            if "page=OK" in u:
                return Resp()
            raise err

        http_mod.urllib.request.urlopen = fake


class Test429Policy(HttpCase):
    def test_waits_exactly_what_the_server_asked(self):
        """服务端说 25 秒就等 25 秒 —— 这是本次改动的全部意义。"""
        self.raises_429(25)
        events: list[str] = []
        c = HttpClient(rps=0, retries=2, on_event=events.append)

        with self.assertRaises(RateLimited) as ctx:
            c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=X")

        self.assertEqual(c.stats.rate_limit_wait, 25.0)
        self.assertIn(25.0, self.sleeps)
        self.assertEqual(ctx.exception.retry_after, 25.0)
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(len(self.hits), 2, "429 只重试 1 次（默认），共 2 个请求")

    def test_429_does_not_use_the_network_backoff_budget(self):
        """429 的等待不能混进「网络抖动」的退避，也不能占用它的重试次数。

        混在一起的话，日志里就分不清「网络慢」和「服务端让我们等」——
        而这两种情况的处理方式完全不同。
        """
        self.raises_429(2)
        c = HttpClient(rps=0, retries=3)
        with self.assertRaises(RateLimited):
            c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=X")

        self.assertEqual(c.stats.backoff, 0.0, "429 不该走指数退避")
        self.assertEqual(c.stats.retried, 0, "429 不该占用网络重试预算")
        self.assertEqual(c.stats.rate_limit_wait, 2.0)
        self.assertEqual(c.stats.rate_limited, 2, "429 响应计 2 次（两次尝试）")

    def test_missing_retry_after_uses_the_default_wait(self):
        self.raises_429(None)
        c = HttpClient(rps=0)
        with self.assertRaises(RateLimited):
            c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=X")
        self.assertEqual(c.stats.rate_limit_wait, constants.RATE_LIMIT_DEFAULT_WAIT)

    def test_wait_is_limited_by_the_step_budget(self):
        """预算不足就直接放弃，不能截短服务端等待后提前重试。"""
        self.raises_429(600)
        c = HttpClient(rps=0, rate_limit_wait_budget=30.0)
        with self.assertRaises(RateLimited):
            c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=X")
        self.assertEqual(c.stats.rate_limit_wait, 0.0)
        self.assertEqual(len(self.hits), 1)
        self.assertEqual(c.stats.rate_limit_retries, 0)

    def test_budget_stops_runaway_waiting_when_429s_are_intermittent(self):
        """⚠️ 这条钉的是一个**比原问题更糟**的坑。

        真机的形态是成功与 429 **交替**（62 次调用里 34 次成功、28 次 429）。
        成功会把「连续计数」清零 ⇒ 熔断几乎不触发 ⇒ 如果每个失败的 URL 都
        老实按 `Retry-After: 25` 等，就是 28 × 25 ≈ **700 秒** ——
        比要修的 163 秒还糟 4 倍。预算用完必须停止等待，而不是继续老实等。
        """
        self.then_ok(25)                    # page=OK 成功，其余 429（要等 25 秒）
        events: list[str] = []
        c = HttpClient(rps=0, rate_limit_wait_budget=10.0, rate_limit_retries=1,
                       rate_limit_breaker=99, on_event=events.append)

        with self.assertRaises(RateLimited):
            c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=A")

        # 预算只有10秒，服务端要求25秒：不发送提前重试，也不浪费10秒等待。
        self.assertEqual(c.stats.rate_limit_wait, 0.0)
        self.assertEqual(self.sleeps, [])

        hits_before = len(self.hits)
        with self.assertRaises(RateLimited):
            c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=B")
        self.assertEqual(len(self.hits) - hits_before, 1,
                         "预算用完后再遇到 429 应当**直接放弃**，不再重试")
        self.assertEqual(c.stats.rate_limit_wait, 0.0, "不该再增加等待")
        self.assertEqual(len([e for e in events if "不足" in e]), 1,
                         "预算用完只提示一次，不刷屏")

    def test_zero_retries_means_one_request(self):
        """`rate_limit_retries=0` = 一次都不等，直接放弃（给特别差的节点用）。"""
        self.raises_429(1)
        c = HttpClient(rps=0, rate_limit_retries=0)
        with self.assertRaises(RateLimited):
            c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=X")
        self.assertEqual(len(self.hits), 1)
        self.assertEqual(c.stats.rate_limit_wait, 0.0)


class TestBreaker(HttpCase):
    def test_consecutive_429s_trip_the_breaker(self):
        self.raises_429(1)
        events: list[str] = []
        c = HttpClient(rps=0, rate_limit_breaker=3, on_event=events.append)

        seen: list[RateLimited] = []
        for i in range(3):
            try:
                c.get(f"https://ja.wikipedia.org/w/api.php?action=parse&page=P{i}")
            except RateLimited as e:
                seen.append(e)

        self.assertEqual(len(seen), 3, "每个 URL 各失败一次")
        self.assertFalse(seen[0].breaker, "第 1 次不该熔断")
        self.assertFalse(seen[1].breaker, "第 2 次不该熔断")
        self.assertTrue(seen[2].breaker, "第 3 次连续 429 应当熔断")
        self.assertTrue(any("熔断" in e for e in events), "熔断要打日志")

    def test_after_tripping_no_more_requests_are_sent(self):
        """⚠️ 熔断之后**一个请求都不该再发**。

        真机那一次：一次「找不到词条」的番要试 7~13 个候选名，
        不拦住就会把 62 个候选名全撞一遍 —— 明知对方在限流还硬撞，
        只会让我们的配额更晚恢复。
        """
        self.raises_429(1)
        c = HttpClient(rps=0, rate_limit_breaker=2)
        for i in range(2):
            with self.assertRaises(RateLimited):
                c.get(f"https://ja.wikipedia.org/w/api.php?action=parse&page=P{i}")
        sent_before = len(self.hits)

        with self.assertRaises(RateLimited) as ctx:
            c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=LATER")

        self.assertEqual(len(self.hits), sent_before, "熔断后不该再发请求")
        self.assertTrue(ctx.exception.breaker)
        self.assertEqual(c.stats.rate_limit_skipped, 1)
        self.assertIn("ja.wikipedia.org", c.tripped_hosts)

    def test_a_success_in_between_resets_the_streak(self):
        """**间歇**限流不该熔断 —— 否则「偶然抖一下」就会让整步作废。

        那种情况靠自适应减速兜（见下一条），不靠熔断。
        """
        self.then_ok()          # page=OK 成功，其余 429
        c = HttpClient(rps=0, rate_limit_breaker=2, rate_limit_retries=0)

        # ① 一个失败的 URL → 计数 1（阈值 2，不该熔断）
        with self.assertRaises(RateLimited) as first:
            c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=A")
        self.assertFalse(first.exception.breaker)

        # ② 中间夹一次成功 → 计数清零
        r = c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=OK")
        self.assertEqual(r.status, 200)

        # ③ 再来一个失败的 URL → 计数应当是 1 而不是 2，所以**仍然不该熔断**
        with self.assertRaises(RateLimited) as second:
            c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=B")
        self.assertFalse(second.exception.breaker,
                         "那次成功没有把连续计数清零")
        self.assertEqual(c.tripped_hosts, frozenset())

    def test_requests_identity_accounts_for_skipped_calls(self):
        """恒等式要带上「熔断跳过」那一项，否则日志里的数对不上账：

            网络请求 == (调用次数 − 缓存命中 − 熔断跳过) + 重试 + 限流重试
        """
        self.raises_429(1)
        c = HttpClient(rps=0, rate_limit_breaker=1, rate_limit_retries=0)

        calls = 0
        for i in range(4):
            calls += 1
            try:
                c.get(f"https://ja.wikipedia.org/w/api.php?action=parse&page=P{i}")
            except RateLimited:
                pass

        s = c.stats
        self.assertEqual(s.rate_limit_skipped, 3, "熔断后那 3 次调用应当一次都没发")
        self.assertEqual(
            s.requests,
            (calls - s.cache_hits - s.rate_limit_skipped) + s.retried
            + s.rate_limit_retries)

    def test_requests_identity_accounts_for_rate_limit_retries(self):
        """⚠️ 这一项是**被真机日志逼出来的**，别删。

        用户那次重跑（`retry-after: 50`）的日志打出

            维基调用 20 次 → 网络请求 21 次（重试 0 · 缓存命中 0）

        —— 差 1，对不上账。差的正是「按服务端要求等过 30 秒再重发」那一条：
        它刻意不计入 `retried`（那是给指数退避用的），但它确实又发了一次请求。
        这一行的全部价值就是能逐项复算，对不上账等于自证这行在骗人（§6）。
        """
        self.raises_429(1)
        c = HttpClient(rps=0, rate_limit_breaker=99, rate_limit_retries=1,
                       rate_limit_wait_budget=60.0)

        calls = 1
        with self.assertRaises(RateLimited):
            c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=A")

        s = c.stats
        self.assertEqual(s.requests, 2, "第一次 + 等过之后的重发")
        self.assertEqual(s.rate_limit_retries, 1)
        self.assertEqual(s.retried, 0, "限流重试**不算**退避重试")
        self.assertEqual(
            s.requests,
            (calls - s.cache_hits - s.rate_limit_skipped) + s.retried
            + s.rate_limit_retries)

    def test_limiter_interval_is_deliberately_not_slowed_down(self):
        """⚠️ 这条钉的是一个**试过又去掉**的做法，别再改回去。

        曾经在收到 429 时把最小请求间隔翻倍（自适应减速，想靠它对付
        「间歇限流」）。离线模拟（`.tmp/sim_429.py`）量出来是**净亏**：
        请求数没降、每条请求却从 1 秒变 5 秒，交替形态下 330.7 秒 vs 约 95 秒。
        真正管用的是熔断与等待预算。
        """
        self.raises_429(1)
        c = HttpClient(rps=4.0, rate_limit_breaker=99)
        before = c.limiter.min_interval
        with self.assertRaises(RateLimited):
            c.get("https://ja.wikipedia.org/w/api.php?action=parse&page=A")
        self.assertEqual(c.limiter.min_interval, before,
                         "限速间隔不该因为 429 被改（试过，净亏，见 sim_429.py）")


# ==================================================================== 维基源

class FakeWikiClient:
    """顶替 `HttpClient`：按脚本决定「抛什么」。"""

    def __init__(self, breaker: bool):
        from animusic.http import RequestStats

        self.breaker = breaker
        self.stats = RequestStats()

    def get_json(self, url, **kw):
        raise RateLimited("被限流", retry_after=25.0, host="ja.wikipedia.org",
                          breaker=self.breaker, url=url)


class TestWikipediaSource(unittest.TestCase):
    def test_single_429_is_swallowed_as_not_answered(self):
        """单次 429（没熔断）：记成「没问到」，不中断整季。"""
        src = WikipediaSource(client=FakeWikiClient(breaker=False), rps=0)
        self.assertIsNone(src.wikitext("何か"))
        self.assertEqual(src.stats["api_failed"], 1)
        self.assertEqual(src.stats["not_found"], 0, "被限流不是「词条不存在」")
        self.assertIn("429", src.stats["last_error"])
        self.assertEqual(src.blocked_reason, "")

    def test_breaker_propagates_instead_of_being_swallowed(self):
        """⚠️ 熔断必须往外抛。吞掉的话调用方只会去试下一个候选名 ——
        真机那次就是这样把 62 个候选名全撞了一遍。"""
        src = WikipediaSource(client=FakeWikiClient(breaker=True), rps=0)
        with self.assertRaises(RateLimited) as ctx:
            src.wikitext("何か")
        self.assertTrue(ctx.exception.breaker)
        self.assertEqual(src.stats["rate_limit_stopped"], 1)
        self.assertIn("25", src.blocked_reason)

    def test_breaker_stops_the_search_fallback_too(self):
        src = WikipediaSource(client=FakeWikiClient(breaker=True), rps=0)
        with self.assertRaises(RateLimited):
            src.search("何か アニメ")


# ==================================================================== 编排层

def _pooled_at(name: str = "X", slug: str = "x") -> AtAnime:
    return AtAnime(name=name, slug=slug,
                   themes=[AtTheme(ThemeKind.OP, "回退曲", ["回退艺人"], 1)])


class TestOrchestratorFallback(unittest.TestCase):
    def _orch(self, wikipedia) -> tuple[Orchestrator, list[str]]:
        logs: list[str] = []
        orch = Orchestrator.__new__(Orchestrator)
        orch.progress = logs.append
        orch.wikipedia = wikipedia
        orch._wiki_blocked = ""
        return orch, logs

    def test_breaker_switches_to_fallback_and_says_why(self):
        orch, logs = self._orch(WikipediaSource(client=FakeWikiClient(True), rps=0))
        themes, source = orch._collect_themes(["X"], _pooled_at())

        self.assertTrue(source.startswith("animethemes:"), "应当改用回退源")
        self.assertEqual([t["song_title"] for t in themes], ["回退曲"])
        self.assertTrue(orch._wiki_blocked)
        joined = "\n".join(logs)
        self.assertIn("停止", joined)
        self.assertIn("不是「维基没收录」", joined,
                      "⚠️ 不许把我们被限流说成数据源没有 —— "
                      "用户会照着错的结论去手动补数据")

    def test_after_blocking_no_further_wiki_requests(self):
        """熔断之后第二次调用**不该再去碰维基**（否则又会白撞一轮）。"""
        src = WikipediaSource(client=FakeWikiClient(True), rps=0)
        calls = {"n": 0}
        orig = src.fetch_themes

        def counting(names, **kw):
            calls["n"] += 1
            return orig(names, **kw)

        src.fetch_themes = counting
        orch, _ = self._orch(src)
        orch._collect_themes(["X"], _pooled_at())
        first = calls["n"]
        orch._collect_themes(["Y"], _pooled_at())

        self.assertEqual(calls["n"], first, "熔断后不该再调用维基")
        self.assertEqual(first, 1)

    def test_fetch_season_reports_the_abort_and_uses_fallback(self):
        """端到端：整步收尾必须写明「被限流中止」+ 用了回退源。"""
        from animusic.models import BangumiSubject
        from animusic.sources.bangumi import ReadResult
        from animusic.store import Store

        tmp = _tmpdir(self, "fetch")
        store = Store(os.path.join(tmp, "s.db"))
        self.addCleanup(store.close)
        logs: list[str] = []

        class B:
            def read_watching(self):
                return ReadResult([BangumiSubject(
                    bgm_id=1, title_cn="番A", title_jp="ばんA",
                    air_date=__import__("datetime").date(2026, 10, 1), url="u")],
                    "api_tab", "1 条")

        class AT:
            def fetch_season(self, season):
                # 名字要与 Bangumi 的番名对得上，否则 `_match_anime` 匹配不上、
                # `at_anime` 为 None，回退路径压根不会走（第一版就是这么假失败的）
                return [_pooled_at("番A", "fan_a")]

            def search_anime(self, title, limit=5):
                return []

        orch = Orchestrator.__new__(Orchestrator)
        orch.store = store
        orch.progress = logs.append
        orch.bangumi = B()
        orch.animethemes = AT()
        orch.wikipedia = WikipediaSource(client=FakeWikiClient(True), rps=0)
        orch._at_pool_by_season = {}
        orch._anilist_source = None
        orch._anilist_pool = lambda season: []
        orch._wiki_blocked = ""
        orch._slow_seconds = 3.0
        from animusic.normalize import AliasTable

        orch.aliases = AliasTable()
        orch.config = types.SimpleNamespace(bangumi=types.SimpleNamespace(
            include_cross_season=True))

        rep = orch.fetch_season(Season.parse("2026Q4"))
        joined = "\n".join(logs)

        self.assertEqual(rep.subjects_with_themes, 1)
        self.assertIn("被限流中止", joined)
        self.assertIn("回退源", joined)
        self.assertNotIn("维基与 AnimeThemes 都没有收录", joined,
                         "⚠️ 曲目其实拿到了（来自回退源），不许写成「都没收录」")
        rows = store.theme_list(store.get_season_id(2026, 4))
        self.assertEqual([r["source"] for r in rows], ["animethemes"])
        self.assertEqual(rows[0]["song_title"], "回退曲")


# ==================================================================== 汇总行

class TestTimingLinesShowRateLimiting(unittest.TestCase):
    def _lines(self, wst, wstats) -> str:
        from animusic.http import RequestStats

        orch = Orchestrator.__new__(Orchestrator)
        orch.progress = lambda m: None
        orch._slow_seconds = 3.0
        orch.wikipedia = types.SimpleNamespace(http=types.SimpleNamespace(stats=wst),
                                              stats=wstats)
        orch.animethemes = types.SimpleNamespace(
            http=types.SimpleNamespace(stats=RequestStats()))
        orch._anilist_source = None
        return "\n".join(orch._timing_lines(10.0))

    def test_rate_limit_is_visible_and_explained(self):
        from animusic.http import RequestStats

        wst = RequestStats(requests=6, rate_limited=6, rate_limit_wait=25.0,
                           rate_limit_skipped=3, failed=6)
        out = self._lines(wst, {"api_calls": 10, "not_found": 1, "page_hits": 0,
                                "search_used": 0, "api_failed": 6,
                                "last_error": "HTTP 429（限流）",
                                "rate_limit_stopped": 1})
        self.assertIn("被限流（HTTP 429）6 次", out)
        self.assertIn("按服务端要求等待 25.0 秒（429）", out)
        self.assertIn("熔断跳过 3", out)
        self.assertIn("已因限流熔断，本步停止查询", out)
        self.assertIn("换一个出口节点", out)

    def test_no_rate_limit_no_noise(self):
        from animusic.http import RequestStats

        out = self._lines(RequestStats(requests=3),
                          {"api_calls": 3, "not_found": 3})
        self.assertNotIn("429", out)
        self.assertNotIn("熔断", out)


# ==================================================================== 配置

class TestConfig(unittest.TestCase):
    def test_defaults(self):
        f = Config().fetch
        self.assertEqual(f.rate_limit_retries, constants.RATE_LIMIT_RETRIES)
        self.assertEqual(f.rate_limit_breaker, constants.RATE_LIMIT_BREAKER)
        self.assertGreater(f.rate_limit_wait_budget, 0)
        self.assertGreaterEqual(f.rate_limit_wait_budget,
                                constants.RATE_LIMIT_DEFAULT_WAIT)

    def test_breaker_triggers_well_before_a_full_season_of_candidates(self):
        """熔断阈值必须**远小于**一部番的候选名数量（7~13 个），
        否则「熔断」还没来得及生效，候选名就已经撞完了。"""
        self.assertLess(Config().fetch.rate_limit_breaker, 7)

    def test_template_has_the_knobs(self):
        import yaml

        with open(os.path.join(_ROOT, "config", "settings.example.yaml"),
                  encoding="utf-8") as f:
            ex = yaml.safe_load(f)
        for k in ("rate_limit_wait_budget", "rate_limit_retries", "rate_limit_breaker"):
            self.assertIn(k, ex["fetch"], f"模板缺少 fetch.{k}")

    def test_sources_accept_the_rate_limit_budget(self):
        from animusic.sources.anilist import AniListSource
        from animusic.sources.animethemes import AnimeThemesSource

        for src in (WikipediaSource(rate_limit_breaker=2, rate_limit_retries=0,
                                    rate_limit_wait_budget=7.0),
                    AnimeThemesSource(rate_limit_breaker=2, rate_limit_retries=0,
                                      rate_limit_wait_budget=7.0),
                    AniListSource(rate_limit_breaker=2, rate_limit_retries=0,
                                  rate_limit_wait_budget=7.0)):
            self.assertEqual(src.http.rate_limit_breaker, 2)
            self.assertEqual(src.http.rate_limit_retries, 0)
            self.assertEqual(src.http.rate_limit_wait_budget, 7.0)


if __name__ == "__main__":
    unittest.main()
