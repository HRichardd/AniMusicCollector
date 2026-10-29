"""HTTP 客户端 —— 基于标准库 `urllib.request`。

为什么不用 httpx（设计文档 v0.9 的调整）：
    httpx 会带来 idna / httpcore / anyio / sniffio / certifi 一串传递依赖。
    这个工具只需要「带重试和限速的 GET/POST + JSON」，标准库完全够用，
    而**零依赖意味着安装几乎不会失败**。

    实测动机：本机 Python 3.14 + 受限环境下 pip 本身就不可用（§G.2），
    依赖越少越稳。

功能：
    · 统一 UA / 超时
    · 指数退避重试（对网络抖动与 5xx）
    · 按域名限速（保护网易云这类有风控的接口）
    · 可选的磁盘缓存（写进 data/cache/）
    · **每次请求的耗时/失败记账**（见 `RequestStats`）

为什么要有最后那条：用户报「拉取番单要 7 分钟」。当时手上唯一的证据是
`data/cache/` 里每个文件的 mtime，靠间隔反推既费劲，又分不清「请求太多」
「梯子慢」「重试烧时间」这三件完全不同的事。现在它们分开计数。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from . import constants

_CTX = ssl.create_default_context()


class HttpError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, url: str = ""):
        super().__init__(msg)
        self.status = status
        self.url = url


class RateLimited(HttpError):
    """被服务端**限流**（HTTP 429）。

    为什么单独一个类型、而不是混在 `HttpError` 里：429 和「网络抖了一下」
    要用完全不同的策略对付（服务端会通过 `Retry-After` 明说要等多久），
    而且它需要一个**能中止整步**的信号 —— 继续撞只会让窗口更晚恢复。
    真机那一次 28 个失败的 URL 各撞 3 次 = 84 次请求、0 个成功。

    `breaker=True` 表示已熔断（同一域名连续多次被限流）：调用方**应当停止**
    向该域名继续查询，而不是把它当成「这次没拿到」轻轻放过。
    """

    def __init__(self, msg: str, *, retry_after: float | None = None,
                 host: str = "", breaker: bool = False, url: str = ""):
        super().__init__(msg, status=429, url=url)
        #: 服务端要求等待的秒数（`Retry-After`）；没给就是 None
        self.retry_after = retry_after
        self.host = host
        self.breaker = breaker


def header_value(headers, name: str) -> str | None:
    """大小写无关地取一个响应头。

    为什么要自己写：真实 `urllib` 给的是 `email.message.Message`（本来就
    大小写无关），但测试里常常喂普通 `dict`（**大小写敏感**）——
    只用 `headers.get("Retry-After")` 的话，测试和现实的取法不一致，
    「测试过了但真机拿不到 Retry-After」这种坑就会潜伏下来。
    """
    if headers is None:
        return None
    try:
        v = headers.get(name)
    except Exception:  # noqa: BLE001
        v = None
    if v is not None:
        return v
    try:
        items = headers.items()
    except Exception:  # noqa: BLE001
        return None
    for k, val in items:
        if str(k).lower() == name.lower():
            return val
    return None


def parse_retry_after(value: str | None) -> float | None:
    """解析 `Retry-After` 响应头。两种合法形式都支持。

    实测拿到的是秒数（`retry-after: 25`），但 RFC 也允许 HTTP 日期
    （`Retry-After: Thu, 08 Oct 2026 03:10:00 GMT`）—— 不处理的话，
    遇到日期形式就会当成「没给」而走默认值，白等或白撞。

    返回 None 表示**没给或看不懂**（调用方据此用 `RATE_LIMIT_DEFAULT_WAIT`）。
    """
    v = (value or "").strip()
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(v)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def brief_url(url: str, limit: int = 88) -> str:
    """把 URL 压成日志里能看的一行 —— 只留**能定位问题**的那几个查询参数。

    为什么不能原样打整条 URL：维基的 `parse` 请求带一串 formatversion 之类的
    参数，AniList 的 POST 更是整段 GraphQL 语句；原样打出来会把
    「到底是哪个词条名卡住了」这个唯一有用的信息淹掉。
    """
    p = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(p.query)
    keep = [f"{k}={q[k][0]}" for k in
            ("action", "page", "srsearch", "titles", "q",
             "filter[year]", "filter[season]")
            if k in q and q[k]]
    s = p.netloc + p.path + (("?" + "&".join(keep)) if keep else "")
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ---------------------------------------------------------------- 计量

@dataclass
class RequestStats:
    """一个 `HttpClient` 的网络事实。**只在真的发出请求时记账。**

    ⚠️ 口径写死在这里，因为日志里的每个数字都要能被逐条复算（§6）：

      · `requests`   —— 实际发出的 HTTP 请求次数（**重试各算一次**）
      · `cache_hits` —— 命中磁盘缓存、一次网络都没发的次数
      · `cache_write_failed` —— 响应拿到了但**没写成缓存**的次数（不影响本次
                        结果，但会让下次重跑重新请求一遍；以前是静默的）
      · `failed`     —— 重试耗尽仍然失败的 **URL 数**（不是尝试数）
      · `retried`    —— 因为失败而**额外**发出的尝试次数
      · `seconds`    —— 所有尝试的网络往返时间合计（**不含**退避睡眠）
      · `backoff`    —— 重试之间的退避睡眠合计（与 `seconds` 分开，否则
                        分不清「网络慢」和「我们自己规定要等」）
      · `limiter_seconds` —— **我们自己规定的限速等待**合计（维基 1 请求/秒）。
                        它和 `backoff` 都要单列，否则「耗时对不上账」——
                        真机那次 163 秒里就有 30 秒是它，而当时没量。
      · `slow`       —— 单次尝试耗时 ≥ `slow_seconds` 的次数
      · `max_seconds` / `slowest` —— 最慢的那一次及其 URL
      · `errors`     —— 失败原因（最多留 5 条，给日志用）
      · `rate_limited` —— 收到 **HTTP 429** 的次数（限流，不是网络故障）
      · `rate_limit_wait` —— 为 429 按服务端要求等待的秒数。**与 `backoff`
                        分开**：`backoff` 是我们自己拍脑袋的退避，这个是服务端
                        明确要求的时间，混在一起就分不清谁在吃时间
      · `rate_limit_skipped` —— 熔断之后**没有发出**的请求数（0 次网络开销）
      · `rate_limit_retries` —— 为 429 **额外**发出的尝试次数

    恒等关系（有测试盯着）：
        `requests == 首次尝试数 + retried + rate_limit_retries`
        `requests == (调用次数 − 缓存命中 − 熔断跳过) + retried + rate_limit_retries`
    `cache_hits` 与 `rate_limit_skipped` 都 **不计入** `requests`。

    ⚠️ `rate_limit_retries` 是**后补的**，而且是被真机日志逼出来的：
    429 的重试刻意不计入 `retried`（那一项是给我们自己拍脑袋的指数退避用的），
    但它确实又发了一次请求。于是用户那次重跑的日志打出
    「维基调用 20 次 → 网络请求 21 次（重试 0 · 缓存命中 0）」——
    差 1 项对不上账。这一行的**全部价值**就是能逐项复算（§6），
    对不上账等于自证这行在骗人。
    """

    requests: int = 0
    cache_hits: int = 0
    cache_write_failed: int = 0
    failed: int = 0
    retried: int = 0
    seconds: float = 0.0
    backoff: float = 0.0
    limiter_seconds: float = 0.0
    slow: int = 0
    max_seconds: float = 0.0
    slowest: str = ""
    errors: list[str] = field(default_factory=list)
    rate_limited: int = 0
    rate_limit_wait: float = 0.0
    rate_limit_skipped: int = 0
    rate_limit_retries: int = 0

    def merge(self, other: "RequestStats") -> None:
        """把另一个客户端的记账并进来（汇总多个数据源时用）。"""
        self.requests += other.requests
        self.cache_hits += other.cache_hits
        self.cache_write_failed += other.cache_write_failed
        self.failed += other.failed
        self.retried += other.retried
        self.seconds += other.seconds
        self.backoff += other.backoff
        self.limiter_seconds += other.limiter_seconds
        self.rate_limited += other.rate_limited
        self.rate_limit_wait += other.rate_limit_wait
        self.rate_limit_skipped += other.rate_limit_skipped
        self.rate_limit_retries += other.rate_limit_retries
        self.slow += other.slow
        if other.max_seconds > self.max_seconds:
            self.max_seconds = other.max_seconds
            self.slowest = other.slowest
        for e in other.errors:
            if len(self.errors) < 5:
                self.errors.append(e)


# ---------------------------------------------------------------- 限速

class RateLimiter:
    """按 key（通常是域名）限速，简单令牌桶。线程安全。"""

    def __init__(self, rps: float):
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, key: str) -> float:
        """必要时睡到下一个允许的时刻，返回**实际睡了多少秒**。

        为什么要返回这个数：真机那次「本步用时 163.0 秒」里，
        「网络往返 89.0」+「退避等待 44.2」只解释了 133 秒 —— 剩下 30 秒
        没有归属，用户看不出那是什么。把限速等待也计上，这笔账才闭合
        （剩下的就是本地处理，本来就该很小）。
        """
        if self.min_interval <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            earliest = self._last.get(key, 0.0) + self.min_interval
            slept = 0.0
            if now < earliest:
                slept = earliest - now
                time.sleep(slept)
                now = time.monotonic()
            self._last[key] = now
            return slept


# ---------------------------------------------------------------- 缓存

@dataclass
class _Cache:
    enabled: bool = False
    ttl: float = 3600.0

    def path(self, key: str) -> str:
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return os.path.join(constants.CACHE_DIR, f"{h}.json")

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        p = self.path(key)
        if not os.path.exists(p):
            return None
        try:
            if time.time() - os.path.getmtime(p) > self.ttl:
                return None
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def put(self, key: str, value: Any) -> bool | None:
        """写缓存。返回 `None` = 缓存功能没开；`True`/`False` = 写成/没写成。

        ⚠️ **整个函数体都在 try 里**，包括 `os.makedirs`。这不是洁癖，是一个
        实测抓到的真 bug：`makedirs` 原来在 try 之外，它抛的 `OSError` 会一路
        冒到 `request()` 的 `except (URLError, TimeoutError, OSError)` 分支 ——
        于是**一个完全成功的响应被当成请求失败**，重试到耗尽后整步抓取报错。
        触发条件很现实：程序装在只读目录（打包版放 `Program Files` 就是），
        缓存目录建不出来 → 每一次请求都"失败"。
        （来源：`.tmp/profile_fetch.py` 把运行根指到一个不可写目录时复现。）

        返回值存在的理由同上：**「缓存没写成」必须能被上层看见** ——
        它本身不影响本次结果，但会让下一次重跑重新付一遍全部请求的钱。
        """
        if not self.enabled:
            return None
        try:
            os.makedirs(constants.CACHE_DIR, exist_ok=True)
            with open(self.path(key), "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False)
            return True
        except (OSError, TypeError, ValueError):
            return False


# ---------------------------------------------------------------- 客户端

@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""

    @property
    def text(self) -> str:
        ct = self.headers.get("content-type", "")
        enc = "utf-8"
        if "charset=" in ct:
            enc = ct.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        return self.body.decode(enc, errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class HttpClient:
    def __init__(
        self,
        user_agent: str = "AniMusicCollector/0.1",
        timeout: float = constants.HTTP_TIMEOUT,
        rps: float = 2.0,
        retries: int = 3,
        cache_ttl: float = 0.0,
        slow_seconds: float = constants.SLOW_REQUEST_SECONDS,
        on_event: Callable[[str], None] | None = None,
        rate_limit_wait_budget: float = constants.RATE_LIMIT_WAIT_BUDGET,
        rate_limit_retries: int = constants.RATE_LIMIT_RETRIES,
        rate_limit_breaker: int = constants.RATE_LIMIT_BREAKER,
    ):
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.limiter = RateLimiter(rps)
        self.cache = _Cache(enabled=cache_ttl > 0, ttl=cache_ttl)
        #: 单次请求超过这个秒数就报一条「慢请求」；0 = 不报。
        self.slow_seconds = slow_seconds
        #: 记账。**不重置** —— 想知道「本步花了多少请求」，调用方在步骤开始时
        #: 自己取快照差值，或者直接用一个新的客户端（编排层是后者）。
        self.stats = RequestStats()
        self._on_event = on_event
        self.check_cancelled = None

        # ---- HTTP 429（限流）的策略，见 `constants` 里那一节
        #: **本步为限流等待的总预算（秒）**。为什么必须有它而不是「单次上限」：
        #: 真机的形态是成功与 429 交替，成功会把连续计数清零 ⇒ 熔断不触发
        #: ⇒ 每条失败的 URL 都老实等 25 秒 ⇒ 28 × 25 ≈ 700 秒。
        #: 预算是唯一能挡住这个的东西。
        self.rate_limit_wait_budget = rate_limit_wait_budget
        #: 同一个 URL 为 429 最多等几次（服务端已经说了等多久，还不成就没戏）
        self.rate_limit_retries = rate_limit_retries
        #: 同一域名**连续**这么多次 429（只数「彻底没过去」的）就熔断
        self.rate_limit_breaker = rate_limit_breaker
        #: 预算用完只提示一次，不刷屏
        self._rl_budget_spent = False
        #: host -> 连续 429 次数（任何一次成功都清零）
        self._rl_streak: dict[str, int] = {}
        #: 已熔断的 host —— 之后不再向它发请求（`RateLimited.breaker=True`）
        self._rl_tripped: set[str] = set()

    # ---- 记账

    @property
    def tripped_hosts(self) -> frozenset[str]:
        """已熔断的域名（编排层据此决定「换回退源」并告诉用户）。"""
        return frozenset(self._rl_tripped)

    def _see_429(self, host: str) -> bool:
        """记**一次被限流事件**（一条 URL 彻底没过去）；返回是否因此熔断。

        ⚠️ 只在「放弃这条 URL」时调用一次 —— 不按响应数计。
        按响应数计会让「老实重试」变成「更快熔断」，正好反了。
        """
        n = self._rl_streak.get(host, 0) + 1
        self._rl_streak[host] = n
        if n >= self.rate_limit_breaker and host not in self._rl_tripped:
            self._rl_tripped.add(host)
            self._emit(f"⚠ {host} 连续 {n} 次被限流 —— **已熔断**，本步不再向它发请求"
                       f"（继续撞只会让配额更晚恢复；换节点或过几分钟重跑）")
            return True
        return host in self._rl_tripped

    def _clear_429(self, host: str) -> None:
        """一次成功就清零**连续**计数 —— 间歇限流不该触发熔断（靠减速兜）。"""
        if self._rl_streak.get(host):
            self._rl_streak[host] = 0

    def _emit(self, msg: str) -> None:
        """把一条网络事件交给上层。

        ⚠️ 回调抛异常**绝不能**影响抓取本身：日志是观测手段，观测坏了不该
        把被观测的东西搞坏（这个工具跑一次要几分钟，不能因为打印失败重来）。
        """
        if self._on_event is None:
            return
        try:
            self._on_event(msg)
        except Exception:  # noqa: BLE001
            pass

    def _note_attempt(self, url: str, dt: float, err: str | None,
                      attempt: int, attempts: int) -> None:
        """记一次**尝试**（不是一次请求）的耗时与结果。"""
        s = self.stats
        s.seconds += dt
        if dt > s.max_seconds:
            s.max_seconds = dt
            s.slowest = url
        if self.slow_seconds and dt >= self.slow_seconds:
            s.slow += 1
            self._emit(f"慢请求 {dt:.1f}s（第 {attempt + 1}/{attempts + 1} 次）"
                       f"{brief_url(url)}")
        if err is not None:
            if len(s.errors) < 5:
                s.errors.append(f"{brief_url(url)} —— {err}")
            self._emit(f"请求失败 {dt:.1f}s（第 {attempt + 1}/{attempts + 1} 次）"
                       f"{brief_url(url)} —— {err}")

    # ---- 核心请求
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        json_body: Any | None = None,
        timeout: float | None = None,
        use_cache: bool = False,
        retries: int | None = None,
    ) -> Response:
        if self.check_cancelled is not None:
            self.check_cancelled()
        h = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip",
        }
        if headers:
            h.update(headers)
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            h.setdefault("Content-Type", "application/json")

        # ⚠️ 缓存键**必须带上请求体**。原来只有 `method + url`，而 AniList 是
        # GraphQL：**同一个 URL、不同的 body**（第 1 页 / 第 2 页、不同季度）。
        # 键里不带 body 的话，第 2 页会拿到第 1 页的缓存、`Fall` 会拿到 `Summer`
        # 的 —— 静默串数据，表现还特别像「AniList 返回了重复的番」。
        # 这就是 AniList 那边一直写着 `cache_ttl=0` 的真实原因：当时只当它是
        # 「先别开缓存，看清事实再说」，**没人把因果关系写下来** ——
        # 差一点就把缓存打开了（那会是个很难查的数据错误）。
        cache_key = f"{method} {url}"
        if data:
            cache_key += " #" + hashlib.sha256(data).hexdigest()[:16]
        if use_cache:
            hit = self.cache.get(cache_key)
            if hit is not None:
                # ⚠️ 缓存命中**不计入** requests —— 「请求了 71 次」和
                # 「缓存答了 71 次」是两件事，混在一起就看不出梯子有没有被用到。
                self.stats.cache_hits += 1
                return Response(200, json.dumps(hit).encode("utf-8"), {}, url)

        host = urllib.parse.urlparse(url).netloc
        # ⚠️ 熔断之后**一个请求都不再发**。为什么这条不能省：一次「找不到词条」
        # 的番要试 7~13 个候选名，不拦住就会把几十个候选名全撞一遍 ——
        # 明知对方在限流还硬撞，只会让我们的配额更晚恢复。
        if host in self._rl_tripped:
            self.stats.rate_limit_skipped += 1
            raise RateLimited(
                f"{host} 已熔断（连续多次被限流），本步不再请求：{brief_url(url)}",
                host=host, breaker=True, url=url,
            )

        attempts = self.retries if retries is None else retries
        last: Exception | None = None
        #: 这个 URL 已经为 429 等过几次。**与 `attempts` 分开计**：
        #: 429 是服务端在限流，不该和「网络抖动」共用同一份重试预算。
        rl_used = 0
        i = 0
        retry_kind = None
        while True:
            self.stats.limiter_seconds += self.limiter.wait(host)
            if self.check_cancelled is not None:
                self.check_cancelled()
            # 等待完成且真正开始下一次发送时才计重试，不能把「准备重试」当作请求。
            if retry_kind == "rate_limit":
                self.stats.rate_limit_retries += 1
            elif retry_kind == "network":
                self.stats.retried += 1
            self.stats.requests += 1
            t0 = time.monotonic()
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            try:
                with urllib.request.urlopen(
                    req, timeout=timeout or self.timeout, context=_CTX
                ) as r:
                    raw = r.read()
                    hdrs = {k.lower(): v for k, v in r.headers.items()}
                    if hdrs.get("content-encoding") == "gzip":
                        try:
                            raw = gzip.decompress(raw)
                        except OSError:
                            pass
                    self._note_attempt(url, time.monotonic() - t0, None, i, attempts)
                    self._clear_429(host)
                    resp = Response(r.status, raw, hdrs, url)
                    if use_cache:
                        wrote = None
                        try:
                            wrote = self.cache.put(cache_key, resp.json())
                        except Exception:  # noqa: BLE001 - 缓存永远不该影响取数
                            wrote = False
                        if wrote is False:
                            # 不静默：缓存写不成不影响**这次**结果，但会让
                            # **下次**重跑重新付一遍全部请求的钱（真机上那一次
                            # 是 71 次请求 / 7 分钟）。以前这里是彻底无声的。
                            self.stats.cache_write_failed += 1
                            self._emit(f"缓存没写成（临时目录/磁盘不可写？）"
                                       f"{brief_url(url)} —— 不影响本次结果，"
                                       f"但下次重跑要重新请求")
                    return resp

            except urllib.error.HTTPError as e:
                body = b""
                try:
                    body = e.read()
                except Exception:
                    pass
                dt = time.monotonic() - t0

                # ---- 429：按**服务端说的**办，不走那套拍脑袋的指数退避
                if e.code == 429:
                    self.stats.rate_limited += 1
                    wait = parse_retry_after(
                        header_value(getattr(e, "headers", None), "Retry-After"))
                    self._note_attempt(url, dt, "HTTP 429（限流，不是网络故障）",
                                       i, attempts)
                    # ⚠️ 重试**不计入**「连续限流」的计数。这是第一版写错的地方：
                    # 那次把「同一个 URL 的限流重试」也算成新的一次，于是
                    # **越老实按服务端要求重试，熔断触发得越快** —— 完全反了。
                    # 一条 URL 只在**彻底没过去**时才算一次事件。
                    left = max(0.0, self.rate_limit_wait_budget
                               - self.stats.rate_limit_wait)
                    nap = constants.RATE_LIMIT_DEFAULT_WAIT if wait is None else wait
                    if rl_used < self.rate_limit_retries \
                            and host not in self._rl_tripped and nap <= left:
                        rl_used += 1
                        self.stats.rate_limit_wait += nap
                        asked = (f"服务端 Retry-After={wait:.0f} 秒"
                                 if wait is not None else "服务端没给 Retry-After")
                        self._emit(f"被限流（{asked}）—— 等 {nap:.1f} 秒后重试"
                                   f"（本 URL 最多为限流重试 "
                                   f"{self.rate_limit_retries} 次；本步剩余预算 "
                                   f"{left - nap:.1f} 秒）")
                        time.sleep(nap)
                        retry_kind = "rate_limit"
                        continue
                    if nap > left and not self._rl_budget_spent:
                        self._rl_budget_spent = True
                        self._emit(
                            f"本步为限流等待的预算（{self.rate_limit_wait_budget:.0f} 秒）"
                            f"不足以满足服务端等待要求 —— 放弃本请求，不截短等待后提前重试")

                    tripped = self._see_429(host)
                    self.stats.failed += 1
                    raise RateLimited(
                        f"被限流（HTTP 429）：{brief_url(url)}"
                        + (f"，服务端要求等待 {wait:.0f} 秒" if wait is not None
                           else "（服务端没给 Retry-After）")
                        + ("；**已熔断**：同一域名连续多次被限流" if tripped else ""),
                        retry_after=wait, host=host, breaker=tripped, url=url,
                    ) from None

                last = HttpError(
                    f"HTTP {e.code} {e.reason} for {url}", status=e.code, url=url
                )
                self._note_attempt(url, dt, f"HTTP {e.code} {e.reason}", i, attempts)
                # 4xx 除了 429 都不重试
                if e.code < 500:
                    self.stats.failed += 1
                    raise last from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                self._note_attempt(url, time.monotonic() - t0,
                                   f"{type(e).__name__}: {e}", i, attempts)

            if i < attempts:
                # 指数退避 + 抖动（只管网络抖动与 5xx；429 在上面单独处理）
                nap = min(8.0, (2.0**i) * 0.5) * (0.7 + random.random() * 0.6)
                self.stats.backoff += nap
                time.sleep(nap)
                i += 1
                retry_kind = "network"
                continue
            break

        self.stats.failed += 1
        # ⚠️ `status` 必须带上来：`last` 是最后一次的异常，而重试耗尽时
        # 调用方最需要的恰恰是**状态码**（429 还是 503 决定了该怎么办）。
        # 丢掉它的话，自检/日志只能说「请求失败」，说不出「被限流了」。
        raise HttpError(
            (f"请求失败（重试 {attempts} 次）：" if attempts else "请求失败：")
            + f"{url} —— {last}",
            status=getattr(last, "status", None),
            url=url,
        )

    # ---- 便捷方法
    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def get_json(self, url: str, **kw) -> Any:
        return self.get(url, **kw).json()

    def post_json(self, url: str, payload: Any, **kw) -> Any:
        return self.request("POST", url, json_body=payload, **kw).json()


# ---------------------------------------------------------------- 全局实例

_default: HttpClient | None = None


def default_client() -> HttpClient:
    """进程内共享的客户端（复用限速状态）。"""
    global _default
    if _default is None:
        _default = HttpClient()
    return _default
