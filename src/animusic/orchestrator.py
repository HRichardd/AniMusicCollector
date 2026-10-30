"""流程编排 —— 把各层串成两条主流程。

    fetch_anime_list  向导第一步：读 Bangumi「在看」→ 按季度筛选 → 只保存番单
    fetch_season   旧 CLI 综合抓取：读 Bangumi → 匹配来源 → 落库番剧与曲目
    match_season   对每条 theme 构造搜索词 → 搜网易云 → 打分 → 落库候选与决策
    export         产出报告（CSV / 导入清单 / 缺失清单）

设计要点：
    · 番剧匹配要处理**跨书写系统**（Bangumi 中日文名 vs AnimeThemes 罗马音），
      因此复用 normalize.name_similarity 的罗马音比较
    · 季度筛选在番单层完成；来源没有单首曲目的季度信息，保留已确认番剧的 OP/ED
    · 增量：默认跳过已有决策的 theme；`retry_missing` 只重试
      missing_source / missing_netease，不碰 missing_match（§5.12）
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from . import constants, scorer
from .collector.base import SearchCollector
from .config import Config
from .http import RateLimited, RequestStats, brief_url
from .models import (
    Anime,
    Candidate,
    DecisionStatus,
    Origin,
    Season,
    SeasonBucket,
    Stage,
    StageStatus,
    Theme,
    ThemeKind,
    artists_from_json,
)
from .normalize import AliasTable, name_similarity, normalize_text
from .query_builder import build_queries
from .sources.animethemes import AnimeThemesSource, AtAnime
from .sources.bangumi import BangumiSource
from .sources.wikipedia import WIKI_TRY_BATCH, WikipediaSource
from .store import Store, ThemeIdentityConflict

Progress = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


# ---------------------------------------------------------------- 报告

@dataclass
class AnimeMatch:
    subject_names: list[str]
    title_cn: str
    bucket: SeasonBucket
    at_anime: AtAnime | None
    score: float
    how: str                      # season_pool | anilist_bridge | title_search | none | search_failed
    search_error: str = ""        # how == "search_failed" 时的原因
    diag: dict = field(default_factory=dict)   # 桥接各跳的分数，便于排查


@dataclass
class FetchReport:
    season: Season
    subjects_total: int = 0
    subjects_in_season: int = 0
    matched: list[AnimeMatch] = field(default_factory=list)
    unmatched: list[AnimeMatch] = field(default_factory=list)
    #: **真正新增的曲目行数** —— 落库前后从库里数差值得到的，不是 upsert 次数。
    #: 为什么不能数次数：两首同曲名会被 `upsert_theme` 合并成一行，那时「新增 N」
    #: 会虚报；重跑一遍（全是更新已有行）也会报成「新增」。
    themes_added: int = 0
    #: upsert 的**次数**（≥ `themes_added`）。只用来在日志里说明差在哪：
    #: 「新增 2 条（另有 13 条是更新已有行）」。
    themes_written: int = 0
    #: **真正拿到曲目的番数** —— 「有没有曲目」这个问题的唯一分子。
    #:
    #: 为什么必须在这里数一遍，而不是让上游反推：上游（`cli.py:501-505`）原本用
    #: `subjects_in_season - len(unmatched)` 与 `len(unmatched)` 反推「有曲目的番」，
    #: 那两式相加再取 min **代数上恒等于 `subjects_in_season`** —— 于是永远打印
    #: 「有曲目的番 N/N 🟢」，哪怕一部番一条曲目都没有。换主数据源之后这个反推
    #: 更没依据：曲目主要来自维基，AnimeThemes 没匹配上**不代表**这部番没曲目
    #: （见 `_collect_themes`）。所以只有落库时数「实际写入了 ≥1 条曲目的番」才是真的。
    #:
    #: ⚠️ `cli.py` 与 GUI 的主结果行都必须打**这个**数，不能打 `len(matched)`
    #: —— 后者是 AnimeThemes 的名称匹配数，只是回退路径的命中率（见 G.18）。
    subjects_with_themes: int = 0
    read_path: str = ""

    @property
    def animethemes_match_rate(self) -> float:
        """AnimeThemes 的**名称匹配率** —— 这是回退路径的命中率，不是成功率。

        ⚠️ 别拿它当「这部番有没有曲目」：维基成为曲目主源之后，一部番完全
        可以「AnimeThemes 没匹配上」却「曲目一条不少」。用那个问题，
        请用 `subjects_with_themes / subjects_in_season`（见 G.18）。
        """
        n = len(self.matched) + len(self.unmatched)
        return (len(self.matched) / n) if n else 0.0

    @property
    def search_failed_unmatched(self) -> list[AnimeMatch]:
        """因为**搜索失败**而没匹配上的（不是「数据源没有」）。

        这两者必须分开报：前者是让用户重跑一次就行，
        后者才是「确实没收录，请手动补」。
        """
        return [m for m in self.unmatched if m.how == "search_failed"]


@dataclass
class MatchReport:
    """本次运行实际保存的匹配结果；themes_total 是当前纳入范围的总数。"""
    season: Season
    themes_total: int = 0
    searched: int = 0
    auto: int = 0
    pending_review: int = 0
    low: int = 0
    missing_source: int = 0
    missing_netease: int = 0
    missing_match: int = 0
    queries_used: int = 0
    details: list[dict] = field(default_factory=list)

    @property
    def decided(self) -> int:
        # low 是 missing_match 的子集，不能再加一次。
        return self.auto + self.pending_review + self.missing_source \
            + self.missing_netease + self.missing_match

    @property
    def auto_rate(self) -> float:
        return (self.auto / self.searched) if self.searched else 0.0

    @property
    def usable_rate(self) -> float:
        """自动 + 待审核（即「不需要人工找歌」的比例）。"""
        n = self.auto + self.pending_review
        return (n / self.searched) if self.searched else 0.0


# ---------------------------------------------------------------- 匹配阈值

@dataclass
class SeasonSnapshot:
    """某一时刻该季度的库内状态 —— 用于算出「这次重跑到底变了什么」。

    直接读库算差集，而不是靠流程里的计数器累加：
    计数器只能反映「这次做了什么」，而快照能反映「**结果**是什么」，
    后者才是用户关心的，而且在流程中途失败时也依然准确。
    """

    anime_ids: set[int] = field(default_factory=set)
    theme_ids: set[int] = field(default_factory=set)
    decisions: dict[int, str] = field(default_factory=dict)   # theme_id -> status

    @classmethod
    def take(cls, store: Store, season_id: int) -> "SeasonSnapshot":
        anime_ids = {r["id"] for r in store.anime_list(season_id, only_included=False)}
        theme_ids = {r["id"] for r in store.theme_list(season_id, only_included=False)}
        decisions = {
            tid: row["status"] for tid, row in store.decision_map(season_id).items()
        }
        return cls(anime_ids, theme_ids, decisions)


@dataclass
class UpdateReport:
    """一次增量重跑的结果 —— 回答「这次有什么新东西」。"""

    season: Season
    new_anime: int = 0
    new_themes: int = 0
    # 之前没有决策、这次刚刚匹配出结果的（最常见的一类变化）
    newly_decided: list[tuple[int, str]] = field(default_factory=list)
    # 之前搜不到、这次搜到了的（missing_netease/missing_source → 其他状态）
    newly_available: list[tuple[int, str, str]] = field(default_factory=list)
    # 状态发生了别的变化
    status_changed: list[tuple[int, str, str]] = field(default_factory=list)
    retried: int = 0
    learned: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)

    @property
    def is_noop(self) -> bool:
        """是否什么都没变 —— 周更时大多数情况应该是这样。"""
        return not (
            self.new_anime or self.new_themes or self.newly_decided
            or self.newly_available or self.status_changed
        )

    def summary(self) -> str:
        if self.is_noop:
            return "无变化"
        bits = []
        if self.new_anime:
            bits.append(f"新增番剧 {self.new_anime}")
        if self.new_themes:
            bits.append(f"新增曲目 {self.new_themes}")
        if self.newly_decided:
            bits.append(f"新匹配 {len(self.newly_decided)}")
        if self.newly_available:
            bits.append(f"新上架 {len(self.newly_available)}")
        if self.status_changed:
            bits.append(f"状态变化 {len(self.status_changed)}")
        return " · ".join(bits)


class OperationCancelled(Exception):
    """在操作边界停止，避免把主动取消当作数据源缺失。"""


@dataclass(frozen=True)
class PushPlanItem:
    theme_id: int
    song_id: str
    playlist: str
    anime: str = ""
    kind: str = ""
    target_title: str = ""
    name: str = ""


@dataclass(frozen=True)
class PushPlan:
    season: Season
    items: tuple[PushPlanItem, ...]


@dataclass
class PushItem:
    """一首歌的推送结果。"""

    theme_id: int
    anime: str
    kind: str
    target_title: str
    song_id: str
    name: str
    status: str = "pending"      # pushed | existed | skipped | failed | pending（未尝试）
    note: str = ""
    error: str = ""
    playlist: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("pushed", "existed", "skipped")


@dataclass
class PushReport:
    season: Season
    playlist: str = ""
    playlist_id: str | None = None
    playlist_created: bool = False
    dry_run: bool = False
    items: list[PushItem] = field(default_factory=list)
    fatal: str = ""              # 整体失败原因（连不上客户端等）
    #: **本次一首都没试过的**曲目（客户端中途没了 → 整批中止）。
    #:
    #: 为什么不塞进 `items`：`items` 的既有语义是「本次真正处理过的」
    #: （GUI 直接拿它刷表格状态、`failed`/`pushed` 都从它数出来），
    #: 把「没试」混进去会让「处理了几首」和「失败几首」两个数字同时失真 ——
    #: 这就是本 bug 的病根：40 首各失败一次，报告看起来像 40 首各自有问题。
    #: 未尝试就是未尝试：单独一个列表，`status="pending"`（`PushItem.ok` 为假）。
    #: GUI 的表格状态取自台账 → 没进 items 的行仍显示「待推送」，这正是事实。
    untried: list[PushItem] = field(default_factory=list)
    stopped: bool = False
    playlists: dict[str, dict] = field(default_factory=dict)

    def _n(self, *statuses: str) -> int:
        return len([i for i in self.items if i.status in statuses])

    @property
    def pushed(self) -> int:
        return self._n("pushed")

    @property
    def existed(self) -> int:
        return self._n("existed")

    @property
    def skipped(self) -> int:
        return self._n("skipped")

    @property
    def failed(self) -> list[PushItem]:
        return [i for i in self.items if i.status == "failed"]

    @property
    def ok(self) -> bool:
        return not self.fatal and not self.failed and not self.stopped

    def summary(self) -> str:
        if self.fatal:
            # 「中止」≠「失败」：中途客户端没了时，前面已经有歌**真的推成功了**，
            # 说「失败」会让人以为这一批白干（甚至去手动重加一遍）。
            # 但一首都没试（客户端一开始就不可用）时就是纯失败，别轻描淡写。
            return f"已中止：{self.fatal}" if self.items else f"失败：{self.fatal}"
        bits = []
        if self.stopped:
            bits.append(f"已停止 · 未尝试 {len(self.untried)}")
        if self.dry_run:
            bits.append(f"（试运行，未写入）计划 {self._n('pending')} 首")
        if self.pushed:
            bits.append(f"新推送 {self.pushed}")
        if self.existed:
            bits.append(f"已存在 {self.existed}")
        if self.skipped:
            bits.append(f"跳过 {self.skipped}")
        if self.failed:
            bits.append(f"失败 {len(self.failed)}")
        return " · ".join(bits) or "没有可推送的曲目"


#: 推送循环里每隔多少首**主动**复检一次客户端（见 `push_season` 的说明）。
#: 10 是开销与灵敏度的折中：一次 health 是 4 次左右 CDP 往返，每首都查会把
#: 「1 首/秒」变成「1 首/好几秒」；10 首查一次在 40 首的批量里多花 3 次探测。
PUSH_HEALTH_RECHECK_EVERY = 10


ANIME_NAME_ACCEPT = 0.72        # 番剧名匹配的接受阈值
ANIME_NAME_ACCEPT_SEARCH = 0.80  # 走标题搜索时要求更高（避免搜到隔壁季）

# AniList 桥接的两跳阈值。
# 桥接比直接匹配多一次跳转，也就多一次出错机会，所以两跳都要够稳。
# 实测参考值（2026Q4 那批）：日文↔日文基本是 1.00，罗马音↔罗马音 0.85~1.00；
# 而误配（把 A 番的主题歌挂到 B 番）通常一跳就掉到 0.5 以下。
ANILIST_HOP1_MIN = 0.70         # Bangumi 名 → AniList（日文↔日文优先）
ANILIST_HOP2_MIN = 0.72         # AniList romaji → AnimeThemes


class Orchestrator:
    def __init__(
        self,
        config: Config | None = None,
        store: Store | None = None,
        bridge=None,
        search: SearchCollector | None = None,
        progress: Progress | None = None,
        aliases: AliasTable | None = None,
    ):
        self.config = config or Config.load()
        self.store = store or Store()
        self.progress = progress or _noop
        # 可注入 —— 测试必须能避开真实的 config/aliases.yaml，
        # 否则跑一次单测就把用户的别名表改了
        self.aliases = aliases if aliases is not None else AliasTable.load()

        if bridge is None:
            from .sources.browser_bridge import BrowserBridge

            bridge = BrowserBridge(self.config.browser.debug_port)
        self.bridge = bridge

        if search is None:
            from .collector.api_search import ApiSearchCollector

            search = ApiSearchCollector()
        self.search = search

        #: 三个外部数据源的超时/重试从配置来（默认已比 `HTTP_TIMEOUT` 短得多，
        #: 依据见 `constants.FETCH_HTTP_TIMEOUT`）。每个源一个 `on_event`，
        #: 让慢请求与失败**当场**出现在日志里，而不是等整步跑完才知道。
        fetch_cfg = self.config.fetch
        self._http_kw = dict(
            timeout=fetch_cfg.http_timeout,
            retries=fetch_cfg.http_retries,
            slow_seconds=fetch_cfg.slow_request_seconds,
            on_event=self._note_http,
            rate_limit_wait_budget=fetch_cfg.rate_limit_wait_budget,
            rate_limit_retries=fetch_cfg.rate_limit_retries,
            rate_limit_breaker=fetch_cfg.rate_limit_breaker,
        )
        self._slow_seconds = fetch_cfg.slow_request_seconds
        #: 维基被**限流熔断**之后就不再查它了（空 = 没被限流）。
        #: 为什么不继续查：实测配额只有约 6~9 次请求/窗口，继续撞只会让
        #: 配额更晚恢复；而且剩下的番用 AnimeThemes 回退源也能出曲目。
        self._wiki_blocked = ""

        self.animethemes = AnimeThemesSource(
            cache_ttl=fetch_cfg.animethemes_cache_ttl, **self._http_kw
        )
        # 维基响应缓存：查询限速 1 请求/秒，而一部番要试多种词条名 ——
        # 不开缓存的话每次重跑都要把这几分钟重新花一遍（用户实测就是这个）。
        self.wikipedia = WikipediaSource(
            cache_ttl=fetch_cfg.wikipedia_cache_ttl, **self._http_kw
        )
        self.animethemes.http.check_cancelled = self._check_cancelled
        self.wikipedia.http.check_cancelled = self._check_cancelled
        #: AniList 的源**懒建但复用** —— 复用才能把它的请求数报进本步汇总。
        #: 它的缓存时长单独存一份：它的构造点在 `_anilist_pool()` 里
        #: （懒建），那边拿不到 `fetch_cfg`。
        self._anilist_ttl = fetch_cfg.anilist_cache_ttl
        self._anilist_source = None
        self.bangumi = BangumiSource(
            bridge=self.bridge,
            uid=self.config.bangumi.uid,
            read_paths=self.config.bangumi.read_paths,
            page_size=self.config.bangumi.page_size,
            collection_type=self.config.bangumi.collection_type,
        )
        self._at_pool_by_season: dict[str, list[AtAnime]] = {}

    # ---- 网络事件的落点

    def _check_cancelled(self) -> None:
        stop = getattr(self, "should_stop", None)
        if stop is not None and stop():
            raise OperationCancelled("已停止后续处理，已完成的数据保留")

    def _note_http(self, msg: str) -> None:
        """单个请求级别的日志 —— 慢请求与失败。

        为什么不打每一次成功请求：真机那次 6 部番就有 70 次请求，
        全打会把日志淹掉，而**成功且快的请求本来就不需要人看**。
        慢和失败才是「时间去哪了」的答案，所以只报这两类。
        """
        self.progress(f"    · {msg}")

    # ---- 「本步时间花在哪」

    def _http_stats_of(self, src) -> "RequestStats | None":
        """取一个源的 `RequestStats`；拿不到（测试的假源）就返回 None。"""
        st = getattr(getattr(src, "http", None), "stats", None)
        return st if isinstance(st, RequestStats) else None

    def _timing_lines(self, elapsed: float) -> list[str]:
        """本步的耗时/请求数汇总 —— 回答「是梯子慢，还是请求太多」。

        为什么要**按源分开**数：这三个源走的出口不一样（维基在大陆必须过代理，
        AnimeThemes/AniList 未必），只报一个总数的话，这个最要紧的问题依然答不出来。

        数字口径（都能从 `RequestStats` 的字段逐条复算）：
            HTTP 请求 = 真的发出去的次数（重试各算一次），**不含**缓存命中
            慢请求     = 单次尝试 ≥ `fetch.slow_request_seconds` 的次数
            失败       = 重试耗尽仍失败的 URL 数
        假源（测试里 `Orchestrator.__new__` 组装的那些）没有 `http.stats`，
        此时返回空列表 —— 不编数字，也不让日志出现 `None 次`。
        """
        groups: list[tuple[str, RequestStats]] = []
        total = RequestStats()
        for label, src in (("维基", getattr(self, "wikipedia", None)),
                           ("AnimeThemes", getattr(self, "animethemes", None)),
                           ("AniList", getattr(self, "_anilist_source", None))):
            st = self._http_stats_of(src)
            if st is None:
                continue
            groups.append((label, st))
            total.merge(st)
        if not groups:
            return []

        span = " · ".join(f"{lab} {st.requests}" for lab, st in groups)
        slow_s = getattr(self, "_slow_seconds", constants.SLOW_REQUEST_SECONDS)
        lines = [
            f"本步用时 {elapsed:.1f} 秒 · HTTP 请求 {total.requests} 次"
            f"（{span}）· 缓存命中 {total.cache_hits} 次"
        ]

        bits = []
        if total.slow:
            bits.append(f"慢请求 ≥{slow_s:g}s {total.slow} 次")
        if total.failed:
            bits.append(f"失败 {total.failed} 次（重试 {total.retried} 次）")
        if total.cache_write_failed:
            # 它不影响本次结果，但会让下次重跑重新付一遍请求的钱 ——
            # 真机上那一次是 71 次请求 / 7 分钟，必须说出来。
            bits.append(f"⚠ 缓存没写成 {total.cache_write_failed} 次"
                        f"（下次重跑会重新请求）")
        bits.append(f"网络往返合计 {total.seconds:.1f} 秒")
        if total.limiter_seconds:
            bits.append(f"限速等待 {total.limiter_seconds:.1f} 秒")
        if total.rate_limit_wait:
            # 与 `backoff` 分开：这个不是我们拍脑袋的退避，是**服务端要求**的时间
            bits.append(f"按服务端要求等待 {total.rate_limit_wait:.1f} 秒（429）")
        if total.backoff:
            bits.append(f"退避等待 {total.backoff:.1f} 秒")
        if total.max_seconds:
            bits.append(f"最慢一次 {total.max_seconds:.1f}s "
                        f"{brief_url(total.slowest) if total.slowest else ''}".rstrip())
        lines.append("  " + " · ".join(bits))

        wsrc = getattr(self, "wikipedia", None)
        w = getattr(wsrc, "stats", None)
        wst = self._http_stats_of(wsrc)
        if isinstance(w, dict) and w.get("api_calls"):
            # ⚠️⚠️ 这两行必须**可以逐项对账**，否则它比不报更糟（§6）。
            #
            #   请求级：网络请求 = 调用 − 缓存命中 − 熔断跳过 + 重试 + 限流重试
            #   候选名：候选名 = 命中缓存 + 查无此词条 + 取到正文 + 没问到
            #
            # 两条都是恒等式，有测试钉着。踩过两次：
            # ① 第一版写「（含缓存命中）」，真机数据立刻打脸 —— 120 网络请求 /
            #    62 调用，差值**全是重试**，缓存命中一个都没有。措辞指错方向，
            #    用户就会去怀疑缓存没生效（实测就是这么发生的）。
            # ② 后来漏了「限流重试」这一项：真机重跑打出
            #    「调用 20 次 → 网络请求 21 次（重试 0 · 缓存命中 0）」，
            #    差 1 对不上账 —— 那 1 次就是按服务端要求等过再重发的那条。
            head = (f"  维基调用 {w.get('api_calls', 0)} 次"
                    f"（批量，每次 ≤{WIKI_TRY_BATCH} 个候选名）")
            if wst is not None:
                head += (f" → 网络请求 {wst.requests} 次（重试 {wst.retried} · 限流重试 "
                         f"{wst.rate_limit_retries} · 缓存命中 {wst.cache_hits}"
                         + (f" · 熔断跳过 {wst.rate_limit_skipped}"
                            if wst.rate_limit_skipped else "") + "）")
            if w.get("api_failed"):
                head += f" · 请求失败 {w['api_failed']} 次"
            lines.append(head)

            cand = (w.get("titles_cached", 0) + w.get("titles_asked", 0))
            lines.append(
                f"      候选名 {cand} 个 = 命中缓存 {w.get('titles_cached', 0)}"
                f" + 查无此词条 {w.get('not_found', 0)}"
                f" + 取到正文 {w.get('page_hits', 0)}"
                f" + 没问到 {w.get('titles_unanswered', 0)}"
                # 「没问到」就是请求失败/被限流的那批候选名。这句话必须留着：
                # 它是「维基真的没有」和「我们没问到」之间唯一的分界线。
                + (f"（没问到的是**我们没问到**，不是「维基没有」）"
                   if w.get("titles_unanswered") else "")
                + f" · 取到正文但没主题歌栏 {w.get('no_section', 0)}"
                + f" · 搜索兜底 {w.get('search_used', 0)}"
                + (f" · 拒绝可疑搜索结果 {w['rejected_title']} 次"
                   if w.get("rejected_title") else "")
                + (f" · 最近一次失败：{w['last_error']}" if w.get("last_error") else "")
                + (" · **已因限流熔断，本步停止查询**"
                   if w.get("rate_limit_stopped") else "")
            )

        if total.rate_limited:
            lines.append(
                f"  ⚠ 被限流（HTTP 429）{total.rate_limited} 次"
                + (f"，其中 {total.rate_limit_skipped} 次请求被熔断拦下（没发出去）"
                   if total.rate_limit_skipped else "")
                + " —— 这是**服务端在限流**，不是网络故障，也不是「数据源没有」。"
                "换一个出口节点（或过几分钟）再跑"
            )
        if total.failed and total.errors:
            lines.append("  失败的请求（最多列 5 条）：")
            for e in total.errors:
                lines.append(f"    ! {e}")
        return lines

    # ---- 曲目来源：维基优先，AnimeThemes 回退

    def _save_fetched_theme(self, anime_id: int, theme: Theme) -> bool:
        try:
            self.store.upsert_theme(anime_id, theme)
        except ThemeIdentityConflict as e:
            self.progress(f"    ! {e}，跳过本条；请人工排除重复项")
            return False
        return True

    def _collect_themes(self, names: list[str], at: AtAnime | None) -> tuple[list[dict], str]:
        """给一部番取 OP/ED。返回 (曲目列表, 来源说明)。

        **维基优先**（用户的提议，实测收益很大）：
            AnimeThemes 只有罗马音，而网易云索引的是日文原名 ——
            这个错配是最大的准确率损失来源（G.9/G.10 实测）。
            维基的主题歌栏直接给日文原名 + 艺人，从源头消掉问题。

        实测对比（2026Q4 那 5 部在看番，只用 Bangumi 的日文名，零罗马音）：
            维基        5/5
            AnimeThemes 2/5（还要靠 AniList 桥接才到 2/5）
        且维基拿到了 AnimeThemes 完全匹配不上的跨季番
        （`欺诈游戏 → LIAR GAME`、`遭到流放的转生重骑士…`）。

        **回退仍然必要**：维基不是每部番都有词条（实测 `名探偵プリキュア！`
        就是靠全角/半角修正才找到的；总有真的没有的）。

        **被限流熔断时**（必修 1）：直接跳到回退源，并且**明确告诉用户
        「是我们被限流了」而不是「维基没收录」** —— 后者会让人去手动补数据，
        补的还是本来能自动拿到的曲目。
        """
        # ---- 主路径：维基（熔断之后不再向它发请求）
        blocked = getattr(self, "_wiki_blocked", "")
        self._check_cancelled()
        if blocked:
            wikis, page = [], ""
        else:
            try:
                wikis, page = self.wikipedia.fetch_themes(names)
            except RateLimited as e:
                # 只有熔断（breaker=True）会冒到这里 —— 单次 429 在
                # `WikipediaSource._api` 里就被记成「没问到」了
                reason = (f"HTTP 429，服务端要求等待 {e.retry_after:.0f} 秒"
                          if e.retry_after is not None else "HTTP 429（限流）")
                self._wiki_blocked = reason
                self.progress(
                    f"    ⚠ 维基开始限流（{reason}）—— **停止**本步对维基的查询，"
                    f"剩下的番改用 AnimeThemes 回退源"
                )
                self.progress(
                    "      ⚠ 注意：这**不是「维基没收录」**，是我们被限流了。"
                    "换一个节点，或过几分钟重跑即可补齐"
                )
                wikis, page = [], ""
            except Exception as e:  # noqa: BLE001 - 维基挂了不该让整季失败
                self.progress(f"    维基查询失败，回退 AnimeThemes：{e}")
                wikis, page = [], ""
        self._check_cancelled()
        if wikis:
            return (
                [{"kind": t.kind, "song_title": t.song_title, "artists": t.artists,
                  "sequence": t.sequence, "note": t.note}
                 for t in wikis],
                f"wikipedia:{page}",
            )

        # ---- 回退：AnimeThemes
        if at is not None:
            return (
                [{"kind": t.kind, "song_title": t.song_title, "artists": t.artists,
                  "sequence": t.sequence, "note": ""}
                 for t in at.themes],
                f"animethemes:{at.slug}",
            )
        return [], "none"

    # ================================================================ 番单

    def fetch_anime_list(self, season: Season) -> FetchReport:
        """向导第一步只确定番单，曲目查询必须由第二步显式启动。"""
        self._check_cancelled()
        self.progress("读取 Bangumi「在看」列表（仅番单）…")
        result = self.bangumi.read_watching()
        self._check_cancelled()
        picked = BangumiSource.filter_by_season(
            result.subjects, season, self.config.bangumi.include_cross_season
        )
        sid = self.store.get_season_id(season.year, season.quarter)
        before = [dict(r) for r in self.store.anime_list(sid, only_included=False)]
        # 取消或保存出错时整体回滚，不能把半份新番单当作拉取完成。
        with self.store.transaction():
            for subject, bucket in picked:
                self._check_cancelled()
                self.store.upsert_anime(sid, Anime(
                    season_key=season.key, title_cn=subject.title_cn,
                    title_jp=subject.title_jp, bgm_id=subject.bgm_id,
                    air_date=subject.air_date, season_bucket=bucket,
                    origin=Origin.BANGUMI, included=True,
                ))
            self._check_cancelled()
            after = [dict(r) for r in self.store.anime_list(sid, only_included=False)]
            if before != after:
                self.store.mark_downstream_dirty(sid, Stage.ANIME)
            self.store.set_stage(sid, Stage.ANIME, StageStatus.FETCHED, fetched=True)
        added = len({r["id"] for r in after} - {r["id"] for r in before})
        self.progress(
            f"番单拉取完成：读取 {len(result.subjects)} 部，季度筛选保留 {len(picked)} 部，"
            f"新增 {added} 部。请确认番单；曲名匹配在第二步手动启动。"
        )
        return FetchReport(season=season, subjects_total=len(result.subjects),
                           subjects_in_season=len(picked), read_path=result.path)

    def fetch_season(self, season: Season, retry_missing: bool = False) -> FetchReport:
        t0 = time.monotonic()
        rep = FetchReport(season=season)
        sid = self.store.get_season_id(season.year, season.quarter)
        self.store.set_stage(sid, Stage.ANIME, StageStatus.FETCHED, fetched=True)
        #: 落库前先拍快照 —— 「新增了多少」必须**从库里数差值**，不能数流程里的
        #: upsert 次数：那会把「更新已有行」也算成新增，两首同曲名合并成一行时
        #: 还会虚报（`update_season` 用的就是这个办法，见 SeasonSnapshot 的注释）。
        before = SeasonSnapshot.take(self.store, sid)
        #: upsert 的**次数**（可能大于新增行数），只用来在日志里说明差在哪
        written = 0

        # ---- 1. 读 Bangumi 在看
        self.progress("读取 Bangumi「在看」列表…")
        result = self.bangumi.read_watching()
        rep.read_path = result.path
        rep.subjects_total = len(result.subjects)
        self.progress(f"  路径={result.path}，共 {len(result.subjects)} 条")

        # ---- 2. 按季度筛选
        picked = BangumiSource.filter_by_season(
            result.subjects, season, self.config.bangumi.include_cross_season
        )
        rep.subjects_in_season = len(picked)
        self.progress(f"  属于 {season.key} 的 {len(picked)} 条")

        # ---- 3. 拉 AnimeThemes 当季池
        #
        # ⚠️ 这个池子是**给番找名字用的**（以及维基没有时回退曲目），
        # **不会**变成番单。日志必须写清这件事 —— 用户看到「当季 49 部」
        # 而自己在看列表只有 5 部，会以为工具背着他把一整季都收进来了。
        self.progress("拉取 AnimeThemes 当季番单（只用于匹配名字与回退曲目，**不会加入番单**）…")
        pool = self._at_pool(season)
        self.progress(f"  当季 {len(pool)} 部（匹配池）")

        # ---- 4. 逐条匹配
        for subject, bucket in picked:
            self._check_cancelled()
            m = self._match_anime(subject, bucket, pool, season)
            if m.at_anime is None:
                rep.unmatched.append(m)
                continue
            rep.matched.append(m)
        self.progress(
            f"  AnimeThemes 名称匹配上 {len(rep.matched)}/{len(picked)} 部"
            + (f"（池子里另外 {len(pool) - len(rep.matched)} 部与你在看的番无关，不会入库）"
               if len(pool) > len(rep.matched) else ""))

        # ---- 5. 落库
        wiki_used = 0
        at_used = 0         # 曲目来自 AnimeThemes 回退源的番数（限流时它会变大）
        with_themes = 0     # 真正写入 ≥1 条曲目的番数（见 subjects_with_themes 的说明）
        # ---- 6. 落库。
        #
        # ⚠️ **两个来源统一处理**：对每一部番都先试维基，再回退 AnimeThemes。
        #
        # 一开始我把维基只放在「AnimeThemes 匹配上」的分支里，结果那 5 部
        # 只看拿到 2 部 —— 因为另外 3 部（欺诈游戏、遭到流放的转生重骑士、
        # 名侦探光之美少女！）**根本不需要 AnimeThemes 匹配**：
        # 维基凭 Bangumi 的日文名就能直接找到它们的主题歌。
        #
        # 教训：换了主数据源之后，「什么算匹配上」的判据也要跟着改 ——
        # AnimeThemes 匹配不上 ≠ 这部番没有曲目。
        for m in list(rep.matched) + list(rep.unmatched):
            self._check_cancelled()
            at = m.at_anime
            themes, source = self._collect_themes(m.subject_names, at)
            if source.startswith("wikipedia:"):
                wiki_used += 1
            elif source.startswith("animethemes:"):
                at_used += 1

            if not themes:
                # 两个源都没有 → 写占位行，等人工补。
                # 搜索**失败**时不要写「未收录」—— 那是我们没问到，
                # 不是数据源没有，写了会让用户误以为要手动补（见附录 G.11）。
                note = ("匹配时搜索失败，未能确认数据源是否收录 —— 建议重跑"
                        if m.how == "search_failed"
                        else "维基与 AnimeThemes 都没有收录 —— 可手动补充曲目")
                self.store.upsert_anime(sid, Anime(
                    season_key=season.key, title_cn=m.title_cn,
                    title_jp=m.subject_names[1] if len(m.subject_names) > 1 else None,
                    bgm_id=self._subject_bgm(m, picked),
                    air_date=self._subject_air_date(m, picked),
                    season_bucket=m.bucket, origin=Origin.BANGUMI,
                    included=True, note=note,
                ))
                continue

            anime_id = self.store.upsert_anime(sid, Anime(
                season_key=season.key,
                title_cn=m.title_cn,
                title_jp=m.subject_names[1] if len(m.subject_names) > 1 else None,
                title_romaji=at.name if at else None,
                animethemes_slug=at.slug if at else None,
                # ⚠️ **必须带 bgm_id**，否则同一部番在不同轮次间合不到一行
                # （上一轮未匹配写 (bgm_id=X, slug=NULL)，这一轮匹配写
                # (bgm_id=NULL, slug=Y)，两个键都不同 → 界面上出现重复行）。
                bgm_id=self._subject_bgm(m, picked),
                air_date=self._subject_air_date(m, picked),
                season_bucket=m.bucket, origin=Origin.BANGUMI, included=True,
                note=f"番剧匹配={m.how}({m.score:.2f}) 曲目源={source}",
            ))
            wrote = 0
            for t in themes:
                if not t.get("song_title"):
                    continue
                if not self._save_fetched_theme(anime_id, Theme(
                    anime_id=anime_id, kind=t["kind"],
                    song_title=t["song_title"],
                    artists=t.get("artists") or [],
                    sequence=t.get("sequence"),
                    source=source.split(":")[0],
                    origin=(Origin.ANIMETHEMES
                            if source.startswith("animethemes") else Origin.SEARCH),
                    included=True, note=t.get("note") or "",
                )):
                    continue
                written += 1
                wrote += 1
            # 数「真的写进去了曲目」的番，而不是「匹配上了的番」：
            # 一部番可以匹配上 AnimeThemes 却两边都没曲目，也可以匹配不上
            # 却从维基拿到整季 OP/ED。只有写入计数不会骗人。
            if wrote:
                with_themes += 1

        # ---- 6. 收尾：数字一律**从库里数差值**，不数流程计数器
        after = SeasonSnapshot.take(self.store, sid)
        added = len(after.theme_ids - before.theme_ids)
        rep.themes_added = added
        rep.themes_written = written
        rep.subjects_with_themes = with_themes
        if wiki_used:
            self.progress(f"  其中 {wiki_used} 部的曲目来自维基百科")

        self.store.set_stage(sid, Stage.ANIME, StageStatus.CONFIRMED, confirmed=True)

        # ⚠️ 这一行曾经打的是「匹配 {len(rep.matched)}/{...}」—— 那是
        # **AnimeThemes 的名称匹配数**，在维基成为曲目主源之后它不再等于成功率：
        # 用户那次 5 部全部从维基拿到了曲目，日志却说「匹配 2/5」，
        # 看起来像 3 部失败了（见 DESIGN.md G.18）。
        # 现在打的是`subjects_with_themes`（真的写入 ≥1 条曲目的番数）。
        self.progress(
            f"完成：{rep.subjects_with_themes}/{rep.subjects_in_season} 部拿到曲目"
            + (f"（维基 {wiki_used} 部）" if wiki_used else "")
            + f"，新增曲目 {added} 条"
            + (f"（另有 {written - added} 条是更新已有行）" if written > added else "")
        )
        matched_total = len(rep.matched) + len(rep.unmatched)
        if matched_total and len(rep.matched) != rep.subjects_with_themes:
            self.progress(
                f"  注：AnimeThemes 只匹配上 {len(rep.matched)}/{matched_total} 部 —— "
                "它现在只是**回退源**，没匹配上不代表这部番没有曲目"
            )
        if getattr(self, "_wiki_blocked", ""):
            self.progress(
                f"⚠ 维基曲目源本步被限流中止（{self._wiki_blocked}）："
                f"有 {at_used} 部的曲目来自 AnimeThemes **回退源**（罗马音，"
                f"到网易云的匹配可用率更低）。换节点或过几分钟重跑一次可以补齐"
            )
        # 「时间花在哪」紧跟主结果行 —— 用户抱怨的正是「慢」，那就要在慢的
        # 那一步把账摊开，而不是等他去翻日志文件。
        for line in self._timing_lines(time.monotonic() - t0):
            self.progress(line)
        return rep

    # ---------------------------------------------------------------- 番单：不经 Bangumi

    def fetch_season_from_animethemes(
        self, season: Season, limit: int | None = None, bridge_names: bool = True
    ) -> FetchReport:
        """直接以 AnimeThemes 当季番单作为输入，**不读 Bangumi**。

        两个正当用途（不是测试专用）：
            ① Bangumi 读不了时的降级 —— 浏览器没接管、未登录、站点抽风
            ② 想「先把整季的 OP/ED 都收进来」再挑要看哪些，
               而不是只收「在看」的番

        用 AniList 的 `title.native` 补日文原名。
        这不是锦上添花：实测缺了日文名会让 `{番剧名} 片头曲` 这类查询失效，
        匹配可用率**显著偏低**（附录 G.9 的测量偏差修正）。
        """
        rep = FetchReport(season=season)
        sid = self.store.get_season_id(season.year, season.quarter)
        rep.read_path = "animethemes"
        self.store.set_stage(sid, Stage.ANIME, StageStatus.FETCHED, fetched=True)
        # 同 `fetch_season`：新增数从库里的差值数出来，不数 upsert 次数
        before = SeasonSnapshot.take(self.store, sid)
        t0 = time.monotonic()

        self.progress("拉取 AnimeThemes 当季番单…")
        animes = [a for a in self.animethemes.fetch_season(season) if a.themes]
        rep.subjects_total = len(animes)
        if limit:
            animes = animes[:limit]
            rep.subjects_total = len(animes)
        rep.subjects_in_season = len(animes)
        self.progress(f"  当季有 OP/ED 的 {len(animes)} 部")

        native: dict[str, str] = {}
        if bridge_names:
            native = self._native_titles(season, animes)
            self.progress(f"  AniList 补到 {len(native)}/{len(animes)} 部的日文原名")

        written = 0
        with_themes = 0
        for a in animes:
            self._check_cancelled()
            anime_id = self.store.upsert_anime(sid, Anime(
                season_key=season.key,
                title_romaji=a.name,
                title_jp=native.get(a.name) or None,
                animethemes_slug=a.slug,
                season_bucket=SeasonBucket.TARGET,
                origin=Origin.ANIMETHEMES,
                included=True,
                note="来自 AnimeThemes 当季番单",
            ))
            wrote = 0
            for t in a.themes:
                if not t.song_title:
                    continue
                if not self._save_fetched_theme(anime_id, Theme(
                    anime_id=anime_id, kind=t.kind, song_title=t.song_title,
                    artists=t.artists, sequence=t.sequence, source="animethemes",
                    origin=Origin.ANIMETHEMES, included=True,
                )):
                    continue
                written += 1
                wrote += 1
            if wrote:
                with_themes += 1

        added = len(SeasonSnapshot.take(self.store, sid).theme_ids - before.theme_ids)
        rep.themes_added = added
        rep.themes_written = written
        rep.subjects_with_themes = with_themes
        self.store.set_stage(sid, Stage.ANIME, StageStatus.CONFIRMED, confirmed=True)
        self.progress(
            f"完成：{len(animes)} 部番，{rep.subjects_with_themes} 部拿到曲目，"
            f"新增曲目 {added} 条"
            + (f"（另有 {written - added} 条是更新已有行）" if written > added else "")
        )
        for line in self._timing_lines(time.monotonic() - t0):
            self.progress(line)
        return rep

    def refresh_themes(self, season: Season) -> FetchReport:
        """**只更新当前番单的曲目** —— 不新增任何番、不碰「当季全季」。

        为什么要有它（用户实测报的问题②）：第②步的「拉取 / 更新 OP/ED」原来接的是
        `fetch_season_from_animethemes`（= 第①步那个"仅从 AnimeThemes 拉当季"）。
        那个按设计会把**整季**番写进番单 —— 于是"更新曲目"实际上变成了"换一季番单"：
        用户点一下，番单里凭空多出几十部没在看的番。

        这个方法的边界很明确：

          · 处理对象 = **库里已纳入的番**（`anime_list(sid)`，即①里勾着的那些）
          · 只 upsert 曲目；**不新增番**、不改 `anime.included`、不删任何东西
          · 手工行照旧受保护：`upsert_theme` 不会覆盖 `origin='manual'` 的字段，
            也不碰 `included`/`note`（用户自己的取舍不会被重跑冲掉）
          · 数字口径见 G.18：新增数从**库里**数差值，不数 upsert 次数
        """
        rep = FetchReport(season=season)
        sid = self.store.get_season_id(season.year, season.quarter)
        before = SeasonSnapshot.take(self.store, sid)
        rep.read_path = "included-anime"
        t0 = time.monotonic()

        animes = self.store.anime_list(sid)
        rep.subjects_total = len(animes)
        rep.subjects_in_season = len(animes)
        self.progress(f"更新曲目：番单里 {len(animes)} 部（只碰这些，不会拉当季全季）")
        if not animes:
            self.progress("  番单是空的 —— 请先在第①步拉取番单")
            return rep

        pool = {a.slug: a for a in self._at_pool(season)}
        wiki_used = 0
        at_used = 0
        written = 0
        with_themes = 0
        for i, a in enumerate(animes, 1):
            self._check_cancelled()
            name = a["title_cn"] or a["title_jp"] or a["title_romaji"] or "?"
            # 每部都报进度：维基限速 1 请求/秒，几部番就是几十秒 ——
            # 不报进度的话用户只会觉得"卡住了"。
            self.progress(f"  第 {i}/{len(animes)} 部：{name}…")
            names = [x for x in (a["title_cn"], a["title_jp"], a["title_romaji"]) if x]
            themes, source = self._collect_themes(names, pool.get(a["animethemes_slug"]))
            if source.startswith("wikipedia:"):
                wiki_used += 1
            elif source.startswith("animethemes:"):
                at_used += 1
            wrote = 0
            for t in themes:
                if not t.get("song_title"):
                    continue
                if not self._save_fetched_theme(a["id"], Theme(
                    anime_id=a["id"], kind=t["kind"],
                    song_title=t["song_title"],
                    artists=t.get("artists") or [],
                    sequence=t.get("sequence"),
                    source=source.split(":")[0],
                    origin=(Origin.ANIMETHEMES
                            if source.startswith("animethemes") else Origin.SEARCH),
                    included=True, note=t.get("note") or "",
                )):
                    continue
                written += 1
                wrote += 1
            if wrote:
                with_themes += 1

        after = SeasonSnapshot.take(self.store, sid)
        rep.themes_added = len(after.theme_ids - before.theme_ids)
        rep.themes_written = written
        rep.subjects_with_themes = with_themes
        self.progress(
            f"完成：{with_themes}/{len(animes)} 部拿到曲目"
            + (f"（维基 {wiki_used} 部）" if wiki_used else "")
            + (f"（AnimeThemes 回退 {at_used} 部）" if at_used else "")
            + f"，新增曲目 {rep.themes_added} 条"
            + (f"（另有 {written - rep.themes_added} 条是更新已有行）"
               if written > rep.themes_added else "")
        )
        if getattr(self, "_wiki_blocked", ""):
            self.progress(
                f"⚠ 维基曲目源本步被限流中止（{self._wiki_blocked}）："
                f"有 {at_used} 部改用 AnimeThemes **回退源**。"
                f"这**不是「维基没收录」** —— 换节点或过几分钟重跑即可补齐"
            )
        for line in self._timing_lines(time.monotonic() - t0):
            self.progress(line)
        return rep

    def _anilist_pool(self, season: Season):
        """该季的 AniList 番单（缓存）。

        用途一：给 AnimeThemes 的罗马音名补日文原名（匹配查询词要用）。
        用途二（更重要）：**当 Bangumi ↔ AnimeThemes 的桥**，见 `_match_via_anilist`。
        """
        key = f"anilist:{season.key}"
        if key not in self._at_pool_by_season:
            from .sources.anilist import AniListSource

            # 源本身也复用：每个源一个 `RequestStats`，重建就等于把它的
            # 请求数/失败数丢了 —— 而「AniList 到底成功过没有」正是
            # 真机那次分辨不出来的两件事之一。
            # `getattr` 而非直接取属性：测试会用 `__new__` 组装最小编排器。
            if getattr(self, "_anilist_source", None) is None:
                self._anilist_source = AniListSource(
                    cache_ttl=self._anilist_ttl, **self._http_kw)
            try:
                self._anilist_source.http.check_cancelled = self._check_cancelled
                self._at_pool_by_season[key] = self._anilist_source.fetch_season(
                    season, max_pages=2
                )
            except Exception as e:  # noqa: BLE001
                self.progress(f"  AniList 不可用：{e}")
                self._at_pool_by_season[key] = []
        return self._at_pool_by_season[key]

    def _native_titles(self, season: Season, animes: list[AtAnime]) -> dict[str, str]:
        """用 AniList 的 native 标题给 AnimeThemes 的罗马音名补日文原名。"""
        al = self._anilist_pool(season)
        if not al:
            return {}
        out: dict[str, str] = {}
        for a in animes:
            best, score = None, 0.0
            for x in al:
                s = max(name_similarity(a.name, x.title_romaji),
                        name_similarity(a.name, x.title_english))
                if s > score:
                    best, score = x, s
            if best is not None and score >= 0.7 and best.title_native:
                out[a.name] = best.title_native
        return out

    # ---- 番剧名匹配

    def _match_via_anilist(
        self, names: list[str], pool: list[AtAnime], season: Season
    ) -> tuple[AtAnime | None, float, dict]:
        """经 AniList 桥接匹配 —— 把一次不可靠的跨语言比对拆成两次可靠的同语言比对。

        为什么需要（实测）：Bangumi 给中文/日文名，AnimeThemes 只给罗马音，
        直接比对要跨**两层**（中文 → 日文读音 → 罗马音），实测命中率只有 20%：

            冰之城墙 第二季   ↔ Koori no Jouheki 2nd Season   0.49
            遭到流放的转生重骑士… ↔ 追放された転生重騎士…          0.40
            名侦探光之美少女！  ↔ 名探偵プリキュア！               0.71（差 0.01）

        而 AniList 同时提供 `native`（日文原名）与 `romaji`，正好当桥：

            Bangumi 氷の城壁 第2期 ──日文↔日文──→ AniList native
                                                    │
            AnimeThemes Koori no Jouheki… ←──罗马音↔罗马音── AniList romaji

        两次都比同语言，可靠得多。

        ⚠️ 但**桥接比直接匹配多一次跳转，也就多一次出错机会**。
        所以只在直接匹配失败时才用，而且要求两跳都达到较高分数。
        """
        al = self._anilist_pool(season)
        if not al or not pool:
            return None, 0.0, {}

        # ---- 第 1 跳：Bangumi 名 → AniList
        # 日文↔日文优先；中文名对不上时退到 romaji/english 比较（弱一些）
        best_al, best_al_score, best_how = None, 0.0, ""
        for x in al:
            s_native = max((name_similarity(n, x.title_native) for n in names), default=0.0)
            s_romaji = max((name_similarity(n, x.title_romaji) for n in names), default=0.0)
            s_eng = max((name_similarity(n, x.title_english) for n in names), default=0.0)
            s = max(s_native, s_romaji, s_eng)
            if s > best_al_score:
                best_al, best_al_score = x, s
                best_how = ("native" if s == s_native and s_native > 0
                            else "romaji" if s == s_romaji else "english")

        # ⚠️ 第 1 跳即使在第 2 跳失败时也有用 —— 它给出的 romaji/native
        # 可以拿去 AnimeThemes **搜索**，覆盖跨季番（那些根本不在当季池里的）。
        al_info = {
            "anilist_native": best_al.title_native if best_al else "",
            "anilist_romaji": best_al.title_romaji if best_al else "",
            "anilist_english": best_al.title_english if best_al else "",
        }

        if best_al is None or best_al_score < ANILIST_HOP1_MIN:
            return None, 0.0, {"hop1": round(best_al_score, 3), "hop1_how": best_how,
                               **al_info}

        # ---- 第 2 跳：AniList 的 romaji → AnimeThemes
        best_at, best_at_score = None, 0.0
        for at in pool:
            s = max(
                name_similarity(best_al.title_romaji, at.name),
                name_similarity(best_al.title_romaji, (at.slug or "").replace("_", " ")),
                name_similarity(best_al.title_native, at.name),
            )
            if s > best_at_score:
                best_at, best_at_score = at, s

        if best_at is None or best_at_score < ANILIST_HOP2_MIN:
            return None, 0.0, {"hop1": round(best_al_score, 3), "hop1_how": best_how,
                               "hop2": round(best_at_score, 3), **al_info}

        # 桥接的可信度取两跳的较小值 —— 两跳都稳才算稳
        return best_at, min(best_al_score, best_at_score), {
            "hop1": round(best_al_score, 3), "hop1_how": best_how,
            "hop2": round(best_at_score, 3), **al_info,
        }

    def _at_pool(self, season: Season) -> list[AtAnime]:
        key = season.key
        if key not in self._at_pool_by_season:
            self._at_pool_by_season[key] = self.animethemes.fetch_season(season)
        return self._at_pool_by_season[key]

    def _subject_air_date(self, m: AnimeMatch, picked) -> date | None:
        for s, _b in picked:
            if s.title_cn == m.title_cn:
                return s.air_date
        return None

    def _subject_bgm(self, m: AnimeMatch, picked) -> int | None:
        for s, _b in picked:
            if s.title_cn == m.title_cn:
                return s.bgm_id
        return None

    def _match_anime(self, subject, bucket: SeasonBucket, pool: list[AtAnime],
                     season: Season) -> AnimeMatch:
        names = self.aliases.expand_names("anime", subject.all_names())
        title_cn = subject.title_cn or subject.title_jp or "?"

        best: AtAnime | None = None
        best_score = 0.0
        for at in pool:
            s = self._anime_sim(names, at)
            if s > best_score:
                best, best_score = at, s

        if best is not None and best_score >= ANIME_NAME_ACCEPT:
            return AnimeMatch(names, title_cn, bucket, best, best_score, "season_pool")

        # 直接匹配不行 → **经 AniList 桥接**。
        # 把「中文/日文 → 罗马音」这一次跨两层的不靠谱比对，
        # 拆成「日文 ↔ 日文」+「罗马音 ↔ 罗马音」两次同语言比对。
        # 实测这条能救回标签法直接匹配不上的番（见 `_match_via_anilist` 的说明）。
        via, via_score, via_diag = self._match_via_anilist(names, pool, season)
        if via is not None and via_score >= ANIME_NAME_ACCEPT:
            m = AnimeMatch(names, title_cn, bucket, via, via_score, "anilist_bridge")
            m.diag = via_diag
            return m

        # 桥接也不行 → 标题搜索（覆盖跨季/半年番）
        #
        # ⚠️ 搜索词里必须**优先放 AniList 给的罗马音/日文名**，而不是 Bangumi 的中文名。
        # 实测：`追放された転生重騎士はゲーム知識で無双する`（7 月开播，跨季）
        # 和 `名探偵プリキュア！`（2 月开播）都不在当季池里，靠池子怎么比都不中；
        # 而 AnimeThemes 的搜索对日文/罗马音都能命中，对中文名基本无效。
        search_names: list[str] = []
        for key in ("anilist_romaji", "anilist_native", "anilist_english"):
            v = (via_diag or {}).get(key)
            if v and v not in search_names:
                search_names.append(v)
        for n in names:
            if n and n not in search_names:
                search_names.append(n)

        search_error: str | None = None
        for nm in search_names[:4]:
            try:
                hits = self.animethemes.search_anime(nm, 5)
            except Exception as e:  # noqa: BLE001
                hits = []
                search_error = f"{type(e).__name__}: {e}"
            else:
                search_error = getattr(self.animethemes, "last_search_error", None) or search_error
            for at in hits:
                # ⚠️ 校验**只能用 Bangumi 的名字**（那才是「这是哪部番」的事实来源）。
                #
                # 这里踩过一个很隐蔽的错：一开始我还拿 AniList 的名字一起 max()，
                # 结果**用 AniList 给 AniList 打分** —— 如果第 1 跳就配错了，
                # 第 2 跳拿错名字去搜，再拿错名字校验，轻松拿到 1.00，
                # 一路"绿灯"把完全无关的番写进库（实测：名侦探光之美少女！
                # 被配成了『探偵はもう、死んでいる。Season 2』，只因都含「探偵」）。
                #
                # AniList 的名字只配用来**构造搜索词**，不配用来**判定结果**。
                s = self._anime_sim(names, at)
                if s > best_score:
                    best, best_score = at, s
            if best is not None and best_score >= ANIME_NAME_ACCEPT_SEARCH:
                break

        if best is not None and best_score >= ANIME_NAME_ACCEPT_SEARCH:
            return AnimeMatch(names, title_cn, bucket, best, best_score, "title_search")

        # ⚠️ 搜索**失败**与搜索**成功但没找到**是两回事。
        # 都报成 "none" 的话，用户会以为 AnimeThemes 确实没收录这部番，
        # 而真相可能只是刚才网络抖了一下（同类错误见附录 G.11）。
        how = "search_failed" if search_error else "none"
        m = AnimeMatch(names, title_cn, bucket, None, max(best_score, via_score), how)
        m.search_error = search_error or ""
        m.diag = via_diag
        return m

    @staticmethod
    def _anime_sim(bangumi_names: list[str], at: AtAnime) -> float:
        at_names = [at.name, at.slug.replace("_", " ") if at.slug else ""]
        best = 0.0
        for a in bangumi_names:
            for b in at_names:
                if not a or not b:
                    continue
                best = max(best, name_similarity(a, b))
        # 带季数的续作容易和第一季混淆，给个轻微惩罚
        joined = normalize_text(at.name)
        if any(tok in joined for tok in ("2nd", "3rd", "4th", "season", "part")):
            best *= 0.98
        return best

    # ================================================================ 匹配

    def match_season(self, season: Season, retry_missing: bool = False) -> MatchReport:
        rep = MatchReport(season=season)
        sid = self.store.get_season_id(season.year, season.quarter)
        themes = self.store.theme_list(sid)
        rep.themes_total = len(themes)

        decisions = self.store.decision_map(sid)
        if retry_missing:
            retry_ids = {r["id"] for r in self.store.retryable_themes(sid)}
            todo = [t for t in themes if t["id"] in retry_ids]
            self.progress(f"增量重试模式：{len(todo)} 条 missing_* 待重试")
        else:
            todo = [t for t in themes if t["id"] not in decisions]
            self.progress(f"待匹配 {len(todo)} / 共 {len(themes)} 条 theme")

        for row in todo:
            self._check_cancelled()
            self._match_one(row, sid, rep)

        # 旧实现把全季度历史结果除以本次 searched，增量运行可能超过100%。
        # 从实际落库结果按本次范围计数；搜索全失败时旧 missing 决策仍在，不能算本次结果。
        failed_ids = {d["theme_id"] for d in rep.details if d.get("status") == "search_failed"}
        decisions = self.store.decision_map(sid)
        summary: dict[str, int] = {}
        for row in todo:
            tid = row["id"]
            if tid not in failed_ids and tid in decisions:
                status = decisions[tid]["status"]
                summary[status] = summary.get(status, 0) + 1
        rep.searched = sum(summary.values())
        rep.auto = summary.get(DecisionStatus.AUTO, 0)
        rep.pending_review = summary.get(DecisionStatus.PENDING_REVIEW, 0)
        rep.missing_source = summary.get(DecisionStatus.MISSING_SOURCE, 0)
        rep.missing_netease = summary.get(DecisionStatus.MISSING_NETEASE, 0)
        rep.missing_match = summary.get(DecisionStatus.MISSING_MATCH, 0)

        self.store.set_stage(sid, Stage.MATCH, StageStatus.CONFIRMED, confirmed=True)
        self.progress(
            f"本次完成：自动 {rep.auto} · 待审核 {rep.pending_review} · "
            f"未收录 {rep.missing_source} · 未上架 {rep.missing_netease} · "
            f"匹配不上 {rep.missing_match}"
        )
        return rep

    def _match_one(self, row, season_id: int, rep: MatchReport) -> None:
        theme_id = row["id"]
        title = row["song_title"] or ""
        artists = artists_from_json(row["artists"])
        anime_names = [x for x in (row["anime_cn"], row["anime_jp"]) if x]
        kind = row["kind"]

        # 占位行（数据源未收录且用户还没填）→ missing_source
        if not title:
            self.store.set_decision(
                theme_id, DecisionStatus.MISSING_SOURCE, None, note="曲目为空（数据源未收录）"
            )
            rep.searched += 1
            return

        plan = build_queries(
            title, artists, anime_names, kind,
            limit=self.config.match.max_queries_per_theme,
            aliases=self.aliases,
        )
        target = scorer.Target(song_title=title, artists=artists,
                               anime_names=anime_names, kind=kind,
                               aliases=self.aliases)

        best: dict | None = None
        best_detail = {}
        best_query = ""
        saw_any_candidate = False
        search_ok = 0
        search_failed = 0

        for q, why in zip(plan.queries, plan.reasons):
            self._check_cancelled()
            try:
                cands = self.search.search(q, limit=self.config.match.search_limit)
            except Exception as e:  # noqa: BLE001
                search_failed += 1
                self.progress(f"    ! 搜索失败 {q!r}: {e}")
                continue
            search_ok += 1
            rep.queries_used += 1
            if not cands:
                continue
            saw_any_candidate = True

            for c in cands:
                payload = {
                    "name": c.name,
                    "artists": c.artists,
                    "alias": c.alias_list,
                    "duration_ms": c.duration_ms,
                    "tags": c.tags,
                    "search_rank": c.search_rank,
                }
                s = scorer.score(payload, target,
                                 prefer_full_size=self.config.match.prefer_full_size)
                c.score = s.score
                c.score_detail = s.detail
                c.theme_id = theme_id
                cid = self.store.upsert_candidate(theme_id, c)
                if best is None or s.score > best["score"]:
                    best = {"score": s.score, "name": c.name,
                            "artists": c.artists, "id": c.netease_song_id, "cid": cid}
                    best_detail = s.detail
                    best_query = q

            if best and best["score"] >= self.config.match.early_accept_score:
                self.progress(f"    ✓ {title[:24]} 提前终止（{best['score']:.2f} via {why}）")
                break

        # ⚠️ **绝不能把「搜索失败」记成「网易云没上架」**。
        #
        # 之前这里没有这个判断：网络断了、接口挂了、被风控了，
        # 所有查询都抛异常 → 走到底部就记成 missing_netease。
        # 后果很严重：那首歌会被当成「等上架就行」，
        # 而且 `--retry-missing` 之外没人会再碰它 —— **等于永久跳过**。
        #
        # 正确做法是什么都不记：保留「未决策」状态，下次重跑自然会再试。
        # 也不计入 `searched` —— 那个数字要和各状态之和对得上。
        if search_ok == 0 and search_failed > 0:
            self.progress(
                f"    ! {search_failed} 次搜索全部失败，**不记录结果**"
                f"（这是网络/接口问题，不是「没有这首歌」）"
            )
            rep.details.append({
                "theme_id": theme_id, "title": title, "status": "search_failed",
                "score": 0.0, "chosen": None,
                "detail": {"search_failed": search_failed},
            })
            return

        rep.searched += 1
        has_artist_target = bool(target.artist_set())

        if best is None:
            # 搜了但一条候选都没有 → 网易云没有
            self.store.set_decision(
                theme_id, DecisionStatus.MISSING_NETEASE, None,
                note=f"0 候选；query={plan.queries[:1]}",
            )
            rep.details.append({"theme_id": theme_id, "title": title,
                                "status": str(DecisionStatus.MISSING_NETEASE), "score": 0.0})
            return

        artist_score = (best_detail or {}).get("artist", 0)
        title_score = (best_detail or {}).get("title", 0)

        # 「未上架」的两种判据：
        #   ① 曲名艺人都对不上          → 找到的全是别人的同名歌
        #   ② **艺人**对上了但曲名很差   → 我们把这位艺人的歌都翻了一遍，没这首
        # 反例（应进人工）：曲名对得上但艺人存疑，例如
        #   "Ballpark de Shake!" ↔ 「ボールパークでShake！」—— 歌确实在，只是署名不同
        not_listed = (
            has_artist_target
            and (
                (artist_score < 0.8 and title_score < 0.6)
                or (artist_score >= 0.8 and title_score < 0.45)
            )
        )
        if not_listed:
            self.store.set_decision(
                theme_id, DecisionStatus.MISSING_NETEASE, None,
                note=f"未找到该曲（候选 {best['name'][:20]}）；query={plan.queries[:1]}",
            )
            rep.details.append({"theme_id": theme_id, "title": title,
                                "chosen": best["name"], "score": best["score"],
                                "status": str(DecisionStatus.MISSING_NETEASE),
                                "detail": best_detail})
            return

        cls = scorer.classify(best["score"],
                              auto_threshold=self.config.match.auto_threshold,
                              review_threshold=self.config.match.review_threshold)
        if cls == "auto":
            status = DecisionStatus.AUTO
            rep.auto += 1
        elif cls == "pending_review":
            status = DecisionStatus.PENDING_REVIEW
            rep.pending_review += 1
        else:
            # 艺人对上了但分数仍低 → 真的需要人工（调阈值 / 补别名）
            status = DecisionStatus.MISSING_MATCH
            rep.low += 1
            rep.missing_match += 1

        self.store.set_decision(
            theme_id, status, best["cid"], decided_by="auto",
            note=f"query={best_query} score={best['score']:.3f}",
        )
        rep.details.append({
            "theme_id": theme_id, "title": title, "chosen": best["name"],
            "status": str(status), "score": best["score"], "detail": best_detail,
        })

    # ================================================================ 别名自学习

    def learn_from_season(self, season: Season, confirm_only: bool = True) -> dict:
        """从已确认的匹配里学别名，回写 `config/aliases.yaml`。

        **只学人工确认过的**（`decided_by='human'`）。这一点很关键：
        从自动结果里学，算法判错时会把错的对应关系**固化**进别名表，
        之后越学越错，而且因为别名几乎是隐式生效的，很难排查。
        人工确认是有成本的信号，正因为它贵，才值得信任。

        学三类关系：
            · 曲名      Kienai Riyuu       ↔ 消えない理由
            · 艺人      ずっと真夜中でいいのに。 ↔ Zutomayo
            · 番剧名    从「TV动画《X》片头曲」标注里抽取

        实测依据（附录 G.9）：这三类正是剩余误差的主要来源 ——
        pykakasi 读不准的片假名外来语、团体简称、人名。
        算法永远猜不准，但用户确认一次就能永久记住。
        """
        sid = self.store.get_season_id(season.year, season.quarter)
        decisions = self.store.decision_map(sid)
        stat = {"checked": 0, "title": 0, "artist": 0, "anime": 0}

        for row in self.store.theme_list(sid):
            self._check_cancelled()
            d = decisions.get(row["id"])
            if not d or not d["chosen_candidate_id"]:
                continue
            if confirm_only and d["decided_by"] != "human":
                continue
            cand = self.store.conn.execute(
                "SELECT * FROM candidate WHERE id=?", (d["chosen_candidate_id"],)
            ).fetchone()
            if cand is None:
                continue

            stat["checked"] += 1
            anime_names = [x for x in (row["anime_cn"], row["anime_jp"]) if x]
            if self.aliases.learn_title(row["song_title"], cand["name"]):
                stat["title"] += 1
            stat["artist"] += self.aliases.learn_artists(
                artists_from_json(row["artists"]), artists_from_json(cand["artists"])
            )
            if self.aliases.learn_anime(anime_names, cand["anime_alias"]):
                stat["anime"] += 1

        if self.aliases.dirty:
            self.aliases.save()
            self.progress(
                f"别名表已更新：曲名 +{stat['title']} · 艺人 +{stat['artist']} · "
                f"番剧 +{stat['anime']} → {self.aliases.path}"
            )
        return stat

    # ================================================================ 季度内增量重跑

    RETRY_STATES = frozenset({"missing_source", "missing_netease"})

    def update_season(
        self,
        season: Season,
        *,
        do_fetch: bool = True,
        do_match: bool = True,
        do_learn: bool = True,
    ) -> UpdateReport:
        """季度内每周重跑 —— 只处理「新的」和「值得重试的」。

        赛季会持续十几周，这期间有三件事在变：
            ① 你在 Bangumi 上把新番加进「在看」
            ② 新番的 OP/ED 在网易云陆续上架
               （**首播时往往还没有**，这是实测确认的最大变量）
            ③ 原本缺曲目信息的番，数据源补上了

        所以增量重跑要做的不是「全部重来」，而是：

            · 拉一遍番单与曲目（幂等 upsert，已有的一律不动）
            · **只给新曲目**做全量匹配
            · 对 `missing_source` / `missing_netease` **重试**
            · 从人工确认里继续学别名
            · 最后报告「这次到底变了什么」

        ⚠️ 拉取失败时**不中断**：浏览器没开、Bangumi 抽风都不该让
        「重试缺歌」这件事做不成 —— 后者恰恰是周更的主要价值。

        ⚠️ 无变化时要明确报告「无变化」。否则用户没法判断
        该不该继续操作，只能自己翻表去数。
        """
        rep = UpdateReport(season=season)
        sid = self.store.get_season_id(season.year, season.quarter)
        before = SeasonSnapshot.take(self.store, sid)

        if do_fetch:
            try:
                self.fetch_season(season)
            except Exception as e:  # noqa: BLE001 - 拉不到就用库里的数据继续
                self.progress(f"番单拉取失败，改用库内已有数据继续：{e}")

        if do_match:
            # ① 新曲目（还没有决策的）
            self.match_season(season, retry_missing=False)
            # ② 之前搜不到的，重试一次
            retryable = self.store.retryable_themes(sid)
            rep.retried = len(retryable)
            if retryable:
                self.progress(f"重试 {len(retryable)} 条之前搜不到的曲目…")
                self.match_season(season, retry_missing=True)

        if do_learn:
            rep.learned = self.learn_from_season(season, confirm_only=True)

        # ---- 算差集（从库读，不靠流程计数器）
        after = SeasonSnapshot.take(self.store, sid)
        rep.new_anime = len(after.anime_ids - before.anime_ids)
        rep.new_themes = len(after.theme_ids - before.theme_ids)

        for tid, now in after.decisions.items():
            was = before.decisions.get(tid)
            if was is None:
                # 之前根本没决策 → 这次刚匹配出来，这是最常见的一类变化
                rep.newly_decided.append((tid, now))
                continue
            if was == now:
                continue
            if was in self.RETRY_STATES and now not in self.RETRY_STATES:
                rep.newly_available.append((tid, was, now))
            else:
                rep.status_changed.append((tid, was, now))

        rep.counts = self.store.counts(sid)
        self.progress(f"增量重跑完成：{rep.summary()}")
        return rep

    def describe_update(self, rep: UpdateReport) -> list[str]:
        """把 UpdateReport 变成可直接打印的行。"""
        lines = [f"季度 {rep.season.key} 增量重跑结果：{rep.summary()}"]
        if rep.new_anime:
            lines.append(f"  ＋ 新增番剧 {rep.new_anime} 部")
        if rep.new_themes:
            lines.append(f"  ＋ 新增曲目 {rep.new_themes} 条")
        if rep.retried:
            lines.append(f"  ↻ 重试了 {rep.retried} 条之前搜不到的")
        if rep.newly_decided:
            from collections import Counter

            dist = Counter(st for _t, st in rep.newly_decided)
            detail = " · ".join(f"{k} {v}" for k, v in sorted(dist.items()))
            lines.append(f"  ✨ 新匹配 {len(rep.newly_decided)} 条（{detail}）")
        for tid, was, now in rep.newly_available[:20]:
            lines.append(f"  ✅ 新上架：theme#{tid}  {was} → {now}")
        for tid, was, now in rep.status_changed[:20]:
            lines.append(f"  · 状态变化：theme#{tid}  {was} → {now}")
        extra = len(rep.newly_available) + len(rep.status_changed) - 40
        if extra > 0:
            lines.append(f"  … 另有 {extra} 条变化未列出")
        lr = rep.learned or {}
        n = lr.get("title", 0) + lr.get("artist", 0) + lr.get("anime", 0)
        if n:
            lines.append(f"  🧠 新学别名 {n} 条")
        c = rep.counts
        if c:
            lines.append(
                f"  库内合计：番剧 {c.get('anime', 0)} · 曲目 {c.get('theme', 0)} · "
                f"候选 {c.get('candidate', 0)}"
            )
        if rep.is_noop:
            lines.append("  （本次没有任何变化 —— 新番歌曲通常开播后 2~8 周才上架，")
            lines.append("    过几天再跑一次即可）")
        return lines

    # ================================================================ 推送到歌单

    def pushable(self, season: Season) -> list[dict]:
        """列出「可以推、也值得推」的曲目（自动通过 + 人工确认）。"""
        sid = self.store.get_season_id(season.year, season.quarter)
        decisions = self.store.decision_map(sid)
        out = []
        for row in self.store.theme_list(sid):
            d = decisions.get(row["id"])
            if not d or not d["chosen_candidate_id"]:
                continue
            if d["status"] not in (DecisionStatus.AUTO, DecisionStatus.CONFIRMED):
                continue
            cand = self.store.conn.execute(
                "SELECT * FROM candidate WHERE id=?", (d["chosen_candidate_id"],)
            ).fetchone()
            if cand is None or not str(cand["netease_song_id"] or "").strip():
                continue
            out.append({
                "theme_id": row["id"],
                "anime": row["anime_cn"] or row["anime_jp"] or "?",
                "kind": row["kind"],
                "target_title": row["song_title"] or "",
                "song_id": str(cand["netease_song_id"]),
                "name": cand["name"] or "",
                "artists": artists_from_json(cand["artists"]),
                "status": d["status"],
            })
        return out

    def build_push_plan(self, season: Season, *, selections=None, limit: int | None = None) -> PushPlan:
        """GUI传当前全量选择，CLI读取保存选择；空列表明确表示不推。"""
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
            raise ValueError("limit 必须是正整数或 None")
        sid = self.store.get_season_id(season.year, season.quarter)
        available = self.pushable(season)
        by_pair = {(r["theme_id"], r["song_id"]): r for r in available}
        default = constants.playlist_name(season.year, season.quarter)
        if selections is None:
            saved = self.store.push_selection_rows(sid)
            by_theme = {(r["theme_id"], r["netease_song_id"]): r for r in saved if r["theme_id"] is not None}
            unbound = {r["netease_song_id"]: r for r in saved if r["theme_id"] is None}
            selections = []
            for r in available:
                choice = by_theme.get((r["theme_id"], r["song_id"])) or unbound.get(r["song_id"]) or {}
                if choice and choice["netease_song_id"] != r["song_id"]:
                    choice = {}
                selections.append({**choice, "theme_id": r["theme_id"], "song_id": r["song_id"]})
        items = []
        seen = set()
        for choice in selections:
            if not choice.get("included", True):
                continue
            song_id = str(choice.get("song_id") or choice.get("netease_song_id") or "").strip()
            tid = choice.get("theme_id")
            row = by_pair.get((tid, song_id))
            if tid is None:
                row = next((r for r in available if r["song_id"] == song_id), None)
            if row is None:
                continue
            target = str(choice.get("playlist") or "").strip() or default
            key = (target, song_id)
            if key in seen:
                continue
            seen.add(key)
            items.append(PushPlanItem(theme_id=row["theme_id"], song_id=song_id, playlist=target,
                                      anime=row["anime"], kind=row["kind"], target_title=row["target_title"],
                                      name=row["name"]))
        if limit is not None:
            items = items[:limit]
        return PushPlan(season=season, items=tuple(items))

    def push_season(self, season: Season, collector=None, *, dry_run: bool = False,
                    limit: int | None = None, progress: Progress | None = None,
                    should_stop=None) -> PushReport:
        plan = self.build_push_plan(season, limit=limit)
        return self.execute_push_plan(plan, collector, dry_run=dry_run,
                                      progress=progress, should_stop=should_stop)

    def execute_push_plan(self, plan: PushPlan, collector=None, *, dry_run: bool = False,
                          should_stop=None, progress: Progress | None = None) -> PushReport:
        """在每首开始前校验并可停止；已经开始的写入必须完成回查与记账。"""
        say = progress or self.progress
        stop = should_stop or (lambda: False)
        targets = list(dict.fromkeys(it.playlist for it in plan.items))
        rep = PushReport(season=plan.season, dry_run=dry_run,
                         playlist=targets[0] if len(targets) == 1 else
                         constants.playlist_name(plan.season.year, plan.season.quarter) if not targets else "")
        sid = self.store.get_season_id(plan.season.year, plan.season.quarter)

        def result(it, status, *, note="", error=""):
            return PushItem(theme_id=it.theme_id, song_id=it.song_id, playlist=it.playlist,
                            anime=it.anime, kind=it.kind, target_title=it.target_title,
                            name=it.name, status=status, note=note, error=error)

        def remaining(start, why, *, stopped=False):
            rep.untried = [result(it, "pending", note=f"未尝试：{why}") for it in plan.items[start:]]
            rep.stopped = stopped
            say(rep.summary())
            return rep

        def current(it):
            return any(r["theme_id"] == it.theme_id and r["song_id"] == it.song_id
                       for r in self.pushable(plan.season))

        def health_error():
            try:
                status = collector.health()
                return "" if status.ok else status.detail or "客户端不可用"
            except Exception as e:
                return f"客户端健康检查失败：{type(e).__name__}: {e}"

        refs = {}
        checked = False
        for index, it in enumerate(plan.items):
            if stop():
                return remaining(index, "用户要求停止", stopped=True)
            if not current(it):
                rep.items.append(result(it, "skipped", note="计划已过期，请重新匹配或纳入曲目"))
                continue
            if dry_run:
                rep.items.append(result(it, "pending", note="试运行，未写入"))
                continue
            if collector is None:
                from .collector.desktop_cdp import DesktopCDPCollector
                collector = DesktopCDPCollector(self.config.netease.debug_port)
            if not checked or index % PUSH_HEALTH_RECHECK_EVERY == 0:
                error = health_error()
                if error:
                    rep.fatal = f"客户端不可用，已中止本批：{error}"
                    return remaining(index, "客户端不可用")
                checked = True
            if stop():
                return remaining(index, "用户要求停止", stopped=True)
            if it.playlist not in refs:
                say(f"准备歌单「{it.playlist}」…")
                try:
                    existed = collector.find_playlist(it.playlist)
                    if stop():
                        return remaining(index, "用户要求停止", stopped=True)
                    ref = collector.ensure_playlist(it.playlist)
                    refs[it.playlist] = ref
                    playlist_id = getattr(ref, "netease_id", None)
                    self.store.save_playlist(it.playlist, playlist_id, sid)
                    rep.playlists[it.playlist] = {"id": playlist_id, "created": existed is None}
                    rep.playlist_created |= existed is None
                    if rep.playlist:
                        rep.playlist_id = playlist_id
                except Exception as e:
                    rep.fatal = f"准备歌单失败：{type(e).__name__}: {e}"
                    return remaining(index, "歌单未准备好")
            if stop():
                return remaining(index, "用户要求停止", stopped=True)
            # 歌单准备过程中决策也可能改变，不得以旧计划替换成另一首后继续写。
            if not current(it):
                rep.items.append(result(it, "skipped", note="计划已过期，请重新匹配或纳入曲目"))
                continue
            was = self.store.is_pushed(it.playlist, it.song_id)
            try:
                ok = collector.add_to_playlist(it.song_id, refs[it.playlist])
            except Exception as e:
                ok, error = False, f"{type(e).__name__}: {e}"
            else:
                error = getattr(collector, "last_write_error", "") or ""
            note = getattr(collector, "last_write_note", "") or ""
            if ok:
                self.store.mark_pushed(it.playlist, it.song_id, it.theme_id)
                action = getattr(collector, "last_write_action", None)
                from .collector.desktop_cdp import WRITE_ACTION_ADDED
                status = ("existed" if was else "pushed") if action is None else (
                    "pushed" if action == WRITE_ACTION_ADDED else "existed")
            else:
                status = "failed"
            rep.items.append(result(it, status, note=note, error="" if ok else error))
            say(f"  [{index + 1}/{len(plan.items)}] {it.playlist} · {it.name[:28]}：{status}"
                + (f"  {error[:100]}" if not ok else ""))
            if not ok:
                error = health_error()
                if error:
                    rep.fatal = f"客户端不可用，已中止本批：{error}"
                    return remaining(index + 1, "客户端不可用")
        if dry_run:
            say(f"试运行：将推送 {len([i for i in rep.items if i.status == 'pending'])} 首到 {len(targets)} 个歌单（未写入）")
        else:
            say(f"推送完成：{rep.summary()}")
        return rep

    def describe_push(self, rep: PushReport) -> list[str]:
        """把 PushReport 变成可直接打印的行。"""
        if rep.stopped:
            return [rep.summary()] + [f"  未尝试：{it.playlist} · {it.name}" for it in rep.untried]
        if rep.fatal:
            lines = [f"推送中止：{rep.fatal}"]
            if rep.untried and rep.items:
                # 只有**中途中止**才逐条列出「还剩哪些」：一首都没试过时，
                # 清单等于 `pushable()`，用试运行看更清楚 ——
                # 报告里塞 40 行会把「为什么停下」淹没。
                lines.append(
                    f"  未尝试的 {len(rep.untried)} 首（客户端恢复后重跑即可，"
                    f"已成功的不会重复）："
                )
                for it in rep.untried[:20]:
                    lines.append(f"    · {it.anime} {it.kind} — {it.target_title} → {it.name}")
                if len(rep.untried) > 20:
                    lines.append(f"    … 另有 {len(rep.untried) - 20} 首")
            elif rep.untried:
                lines.append(f"  {len(rep.untried)} 首一首都没试（客户端不可用时不发任何写入）")
            return lines
        # ⚠️ 试运行时**从未检查过**歌单是否存在，就不能说「复用已有」——
        # 声称自己知道实际不知道的事，比不说更糟。
        targets = set(rep.playlists) | {it.playlist for it in rep.items + rep.untried if it.playlist}
        head = f"歌单「{rep.playlist}」" if rep.playlist else f"多个目标歌单（{len(targets)}个）"
        if rep.playlist_id:
            head += f"（id={rep.playlist_id}）"
        if rep.dry_run:
            head += "　【试运行，未检查歌单是否存在】"
        elif rep.playlist:
            head += "，本次新建" if rep.playlist_created else "，复用已有"
        lines = [
            head,
            f"  结果：{rep.summary()}",
        ]
        if rep.dry_run:
            for it in rep.items:
                lines.append(f"  · {it.playlist} · {it.anime} {it.kind} — {it.target_title} → {it.name}")
            return lines

        fails = rep.failed
        if fails:
            lines.append("")
            lines.append(f"  失败的 {len(fails)} 首（可在客户端手动加，或稍后重跑）：")
            for it in fails[:20]:
                lines.append(f"    ✗ {it.anime} {it.kind} — {it.target_title} → {it.name}")
                if it.error:
                    lines.append(f"        {it.error[:160]}")
            if len(fails) > 20:
                lines.append(f"    … 另有 {len(fails) - 20} 首")
        return lines

    # ================================================================ 导出

    def export(self, season: Season, out_dir: str | None = None) -> dict[str, str]:
        from .collector.export import ExportCollector

        sid = self.store.get_season_id(season.year, season.quarter)
        ex = ExportCollector(season.key)
        decisions = self.store.decision_map(sid)

        for row in self.store.theme_list(sid):
            d = decisions.get(row["id"])
            status = d["status"] if d else "pending_review"
            chosen_name = chosen_artists = ""
            song_id = ""
            score = 0.0
            query_used = ""
            if d and d["chosen_candidate_id"]:
                c = self.store.conn.execute(
                    "SELECT * FROM candidate WHERE id=?", (d["chosen_candidate_id"],)
                ).fetchone()
                if c:
                    chosen_name = c["name"]
                    chosen_artists = "、".join(artists_from_json(c["artists"]))
                    song_id = c["netease_song_id"]
                    score = c["score"]
                    query_used = c["query_used"] or ""
            if not score and d:
                for c in self.store.candidate_list(row["id"])[:1]:
                    score = c["score"]
                    chosen_name = chosen_name or c["name"]
                    chosen_artists = chosen_artists or "、".join(artists_from_json(c["artists"]))
                    song_id = song_id or c["netease_song_id"]
                    query_used = query_used or (c["query_used"] or "")

            dur = ""
            ex.record(
                anime=row["anime_cn"] or row["anime_jp"] or "",
                kind=row["kind"],
                seq=row["sequence"] or "",
                song_title=row["song_title"],
                artists=artists_from_json(row["artists"]),
                chosen_name=chosen_name,
                chosen_artists=chosen_artists,
                netease_song_id=song_id,
                duration=dur,
                score=f"{score:.3f}" if score else "",
                status=status,
                query_used=query_used,
                note=(d["note"] if d else "") or "",
                # 曲目**从哪来**必须跟着数据一起走：维基是主源、正文 CC BY-SA 4.0，
                # 导出/分发时不带出处就等于丢了署名（许可证义务，不是礼貌）。
                # 取值来自 `Theme.source`（落库时写的是 `wikipedia` /
                # `animethemes`，见 `_collect_themes`），不是猜的。
                source=row["source"] or "",
            )
        return ex.export(out_dir)
