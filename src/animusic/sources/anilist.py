"""AniList —— 仅作辅助（番单 + 热度）。

对应 DESIGN.md §5.4.3。

⚠️ 实测确认：AniList 的 `Media` 类型**没有** `openings` / `endings` 字段
（API 直接报 `Cannot query field "openings" on type "Media"`）。
它**不能**提供曲目，只能：

    · 给当季番单补热度（可按热度过滤冷门番）
    · 交叉校验 AnimeThemes 的季度番剧是否漏了
    · 提供 romaji / native 双语标题，用于跨源标题桥接

因此不使用它做任何兜底曲目来源。
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import constants
from ..http import HttpClient
from ..models import Season

_QUERY = """
query ($season: MediaSeason, $year: Int, $page: Int) {
  Page(page: $page, perPage: 50) {
    media(season: $season, seasonYear: $year, type: ANIME, sort: POPULARITY_DESC) {
      id
      title { romaji native english }
      popularity
      episodes
      format
      startDate { year month day }
    }
  }
}
"""


@dataclass
class AlAnime:
    anilist_id: int
    title_romaji: str = ""
    title_native: str = ""
    title_english: str = ""
    popularity: int = 0
    episodes: int | None = None
    format: str = ""


class AniListSource:
    def __init__(self, client: HttpClient | None = None,
                 cache_ttl: float = 3600.0,
                 timeout: float = constants.FETCH_HTTP_TIMEOUT,
                 retries: int = constants.FETCH_HTTP_RETRIES,
                 slow_seconds: float = constants.SLOW_REQUEST_SECONDS,
                 on_event=None,
                 rate_limit_wait_budget: float = constants.RATE_LIMIT_WAIT_BUDGET,
                 rate_limit_retries: int = constants.RATE_LIMIT_RETRIES,
                 rate_limit_breaker: int = constants.RATE_LIMIT_BREAKER):
        #: 缓存：这里以前写死 `cache_ttl=0`，理由是「先看清事实再决定要不要开」。
        #: 事实现在清楚了：AniList 每次跑**固定 2 次请求**（`max_pages=2`）、
        #: 从没失败过，而一季的番单一天里基本不动 —— 缓存 1 小时
        #: （与 AnimeThemes 同档），省掉每天这 2 次。
        #:
        #: ⚠️ 开它的**前提**是 `HttpClient` 的缓存键已经带上请求体：
        #: AniList 是 GraphQL，同一个 URL、不同 body（第 1/2 页、不同季度）。
        #: 先修 `http.py` 的缓存键、再开这个缓存，顺序不能反（见那边的注释）。
        self.http = client or HttpClient(
            rps=1.0, cache_ttl=cache_ttl, timeout=timeout, retries=retries,
            slow_seconds=slow_seconds, on_event=on_event,
            rate_limit_wait_budget=rate_limit_wait_budget,
            rate_limit_retries=rate_limit_retries,
            rate_limit_breaker=rate_limit_breaker,
        )

    def fetch_season(self, season: Season, max_pages: int = 3) -> list[AlAnime]:
        out: list[AlAnime] = []
        for page in range(1, max_pages + 1):
            payload = {
                "query": _QUERY,
                "variables": {
                    "season": constants.SEASON_EN[season.quarter].upper(),
                    "year": season.year,
                    "page": page,
                },
            }
            data = self.http.post_json(constants.ANILIST_API, payload, use_cache=True)
            if data.get("errors"):
                raise RuntimeError(f"AniList GraphQL 错误：{data['errors']}")
            media = ((data.get("data") or {}).get("Page") or {}).get("media") or []
            if not media:
                break
            for m in media:
                t = m.get("title") or {}
                out.append(
                    AlAnime(
                        anilist_id=m.get("id"),
                        title_romaji=(t.get("romaji") or "").strip(),
                        title_native=(t.get("native") or "").strip(),
                        title_english=(t.get("english") or "").strip(),
                        popularity=m.get("popularity") or 0,
                        episodes=m.get("episodes"),
                        format=m.get("format") or "",
                    )
                )
        return out
