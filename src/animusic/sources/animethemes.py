"""AnimeThemes —— OP/ED 曲目来源（主力）。

对应 DESIGN.md §5.4.1。**实测可用**，且 Python 直连不需要浏览器。

    GET /anime?filter[year]=2026&filter[season]=Fall
              &include=animethemes.song.artists
              &page[size]=100

返回结构（实测）：
    anime[].name / slug / year / season / media_format
    anime[].animethemes[].type ("OP"/"ED") / sequence
    anime[].animethemes[].song.title / song.artists[].name

注意：
    · 必须带自定义 User-Agent（Cloudflare 会挑战默认 UA）
    · 这是**唯一**能直接给出 OP/ED 曲名 + 歌手的源
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field

from .. import constants
from ..http import HttpClient
from ..models import Season, ThemeKind


@dataclass
class AtTheme:
    kind: ThemeKind
    song_title: str
    artists: list[str] = field(default_factory=list)
    sequence: int | None = None


@dataclass
class AtAnime:
    name: str
    slug: str = ""
    year: int | None = None
    season: str = ""
    media_format: str = ""
    themes: list[AtTheme] = field(default_factory=list)


class AnimeThemesError(RuntimeError):
    pass


class AnimeThemesSource:
    def __init__(self, client: HttpClient | None = None, cache_ttl: float = 0,
                 timeout: float = constants.FETCH_HTTP_TIMEOUT,
                 retries: int = constants.FETCH_HTTP_RETRIES,
                 slow_seconds: float = constants.SLOW_REQUEST_SECONDS,
                 on_event=None,
                 rate_limit_wait_budget: float = constants.RATE_LIMIT_WAIT_BUDGET,
                 rate_limit_retries: int = constants.RATE_LIMIT_RETRIES,
                 rate_limit_breaker: int = constants.RATE_LIMIT_BREAKER):
        # 超时/重试与维基同一档（见 `constants.FETCH_HTTP_TIMEOUT`）。
        # 这里的证据弱一些：真机那次的 6 段停顿里，我**没能分辨**哪一段是
        # AnimeThemes/AniList 卡的（缓存只记成功，失败不留痕）。统一降下来，
        # 再由日志里的「慢请求/失败」行去指认到底是谁。
        # 限流策略（429）也统一 —— 实测维基那个配额是按出口 IP 的窗口，
        # 别的源哪天开始限流，这套策略照样管用。
        self.http = client or HttpClient(
            user_agent=constants.ANIMETHEMES_UA,
            rps=1.5,
            cache_ttl=cache_ttl,
            timeout=timeout,
            retries=retries,
            slow_seconds=slow_seconds,
            on_event=on_event,
            rate_limit_wait_budget=rate_limit_wait_budget,
            rate_limit_retries=rate_limit_retries,
            rate_limit_breaker=rate_limit_breaker,
        )
        #: 最近一次 search_anime 的失败原因（成功则 None）。
        #: 存在的意义：区分「数据源确实没有这部番」与「我们没问到数据源」——
        #: `search_anime` 失败时返回空列表，调用方无法分辨，于是会把
        #: 网络故障当成「未收录」报给用户（同类错误见附录 G.11）。
        self.last_search_error: str | None = None

    # ---------------------------------------------------------------- 季度列表

    def fetch_season(self, season: Season) -> list[AtAnime]:
        """拉当季全部番剧及其 OP/ED。"""
        season_en = constants.SEASON_EN[season.quarter]
        url = (
            f"{constants.ANIMETHEMES_API}/anime"
            f"?filter%5Byear%5D={season.year}"
            f"&filter%5Bseason%5D={season_en}"
            f"&include=animethemes.song.artists"
            f"&page%5Bsize%5D=100"
        )
        data = self.http.get_json(url, use_cache=True)
        anime = data.get("anime") or []
        return [self._to_anime(a) for a in anime]

    def fetch_season_with_themes(self, season: Season) -> list[AtAnime]:
        return [a for a in self.fetch_season(season) if a.themes]

    # ---------------------------------------------------------------- 标题搜索

    def search_anime(self, title: str, limit: int = 5) -> list[AtAnime]:
        """按标题搜（跨季番兜底用）。

        ⚠️ 实测确认的正确用法（试过三种写法）：

            `/anime?q=Kusuriya`          ✅ 3 条 —— 模糊搜索，用这个
            `/anime?filter[name]=...`    ✗ 精确匹配，部分词返回 0 条
            `/search?q=...`              ✗ 返回的是 {"search": {...}} 结构，不是 anime 列表

        另外：它主要存**罗马音/英文名**，日文原名命中率差。
        所以调用方应在失败后落到别名表（aliases.yaml）。
        """
        if not title:
            return []
        self.last_search_error = None
        q = urllib.parse.quote(title)
        url = (
            f"{constants.ANIMETHEMES_API}/anime?q={q}"
            f"&include=animethemes.song.artists"
            f"&page%5Bsize%5D={max(1, min(limit, 20))}"
        )
        try:
            data = self.http.get_json(url, use_cache=True)
        except Exception as e:  # noqa: BLE001
            # 记下来供调用方判断 —— 不能静默变成「没搜到」
            self.last_search_error = f"{type(e).__name__}: {e}"
            return []
        return [self._to_anime(x) for x in (data.get("anime") or [])][:limit]

    # ---------------------------------------------------------------- 解析

    @staticmethod
    def _to_anime(a: dict) -> AtAnime:
        themes: list[AtTheme] = []
        for t in a.get("animethemes") or []:
            song = t.get("song") or {}
            # 实测：song 是 to-one 关系，序列化为对象；带上 include 后仍是对象
            if isinstance(song, list):
                song = song[0] if song else {}
            title = (song.get("title") or "").strip()
            artists = [x.get("name") for x in (song.get("artists") or []) if x.get("name")]
            seq = t.get("sequence")
            themes.append(
                AtTheme(
                    kind=ThemeKind(t["type"]) if t.get("type") in ("OP", "ED") else ThemeKind.OP,
                    song_title=title,
                    artists=artists,
                    sequence=seq if seq else 1,
                )
            )
        return AtAnime(
            name=(a.get("name") or "").strip(),
            slug=a.get("slug") or "",
            year=a.get("year"),
            season=a.get("season") or "",
            media_format=a.get("media_format") or "",
            themes=themes,
        )
