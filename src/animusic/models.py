"""领域数据模型。

用 dataclass 而非 pydantic：这些对象在向导流程里会被频繁原地修改
（手工编辑、状态流转），dataclass 更直接。配置校验另见 `config.py`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any


# ---------------------------------------------------------------- 枚举

class ThemeKind(StrEnum):
    OP = "OP"
    ED = "ED"


class Origin(StrEnum):
    """数据来源 —— 决定重跑时能否被覆盖（§8.3 的硬约束）。"""

    BANGUMI = "bangumi"
    ANIMETHEMES = "animethemes"
    SEARCH = "search"
    MANUAL = "manual"          # 手工数据：重跑拉取时绝不覆盖
    PENDING = "pending"        # 占位行，等用户手填


class DecisionStatus(StrEnum):
    """三态缺失见 §5.12：区分「暂时没有」与「确实没有」。"""

    AUTO = "auto"
    PENDING_REVIEW = "pending_review"
    CONFIRMED = "confirmed"
    SKIPPED = "skipped"
    MISSING_SOURCE = "missing_source"     # 数据源未收录 → 下次重跑可能补上
    MISSING_NETEASE = "missing_netease"   # 网易云未上架 → 等上架
    MISSING_MATCH = "missing_match"       # 匹配不上 → 需要人工


class SeasonBucket(StrEnum):
    TARGET = "target"
    CROSS_SEASON = "cross_season"
    OTHER = "other"
    UNKNOWN = "unknown"


class StageStatus(StrEnum):
    PENDING = "pending"
    FETCHED = "fetched"
    CONFIRMED = "confirmed"
    DIRTY = "dirty"


class Stage(StrEnum):
    ANIME = "anime"
    THEME = "theme"
    MATCH = "match"
    PUSH = "push"


# ---------------------------------------------------------------- 季度

@dataclass(frozen=True)
class Season:
    year: int
    quarter: int

    @property
    def label(self) -> str:
        from .constants import SEASON_CN

        return f"{self.year} {SEASON_CN[self.quarter]}"

    @property
    def key(self) -> str:
        return f"{self.year}Q{self.quarter}"

    @property
    def start_month(self) -> int:
        from .constants import QUARTER_MONTH

        return QUARTER_MONTH[self.quarter]

    @classmethod
    def parse(cls, s: str) -> "Season":
        """接受 '2026Q4' / '2026q4' / '2026-4'。"""
        t = s.strip().upper().replace("-", "Q")
        if "Q" not in t:
            raise ValueError(f"无法解析季度：{s!r}（应形如 2026Q4）")
        y, q = t.split("Q", 1)
        return cls(int(y), int(q))

    @classmethod
    def current(cls, today: date | None = None) -> "Season":
        """**今天所在的季度** —— 所有 `--season` 默认值的唯一来源。

        为什么必须有它（实测踩过）：季度原来硬编码成 `2026Q4`，散落在 7 个 CLI
        子命令、`run_gui.py` 和 `gui/session.py` 里共 9 处。后果不是「可能会错」，
        而是**下个季度一定会静默做错事** —— 2027 年 1 月跑，程序照旧处理 2026Q4，
        而且看起来一切正常。

        分季规则与整个项目一致（`constants.quarter_of_month`）：
        1~3 月 = Q1（冬）· 4~6 月 = Q2（春）· 7~9 月 = Q3（夏）· 10~12 月 = Q4（秋）。

        `today` 可注入，于是「跨年边界」这种行为能被离线钉住，不用改系统时钟。
        """
        from .constants import quarter_of_month

        d = today or date.today()
        return cls(d.year, quarter_of_month(d.month))


# ---------------------------------------------------------------- 番剧 / 曲目

@dataclass
class BangumiSubject:
    bgm_id: int
    title_cn: str | None = None
    title_jp: str | None = None
    air_date: date | None = None
    url: str = ""

    def all_names(self) -> list[str]:
        return [n for n in (self.title_cn, self.title_jp) if n]


@dataclass
class Anime:
    season_key: str
    title_cn: str | None = None
    title_jp: str | None = None
    title_romaji: str | None = None
    bgm_id: int | None = None
    animethemes_slug: str | None = None
    anilist_id: int | None = None
    popularity: int | None = None
    air_date: date | None = None
    season_bucket: SeasonBucket = SeasonBucket.UNKNOWN
    origin: Origin = Origin.BANGUMI
    included: bool = True
    note: str = ""
    id: int | None = None

    def all_names(self) -> list[str]:
        return [n for n in (self.title_cn, self.title_jp, self.title_romaji) if n]


@dataclass
class Theme:
    anime_id: int
    kind: ThemeKind
    song_title: str
    artists: list[str] = field(default_factory=list)
    sequence: int | None = None
    source: str = "animethemes"
    origin: Origin = Origin.ANIMETHEMES
    included: bool = True
    note: str = ""
    raw: dict[str, Any] | None = None
    id: int | None = None


@dataclass
class Candidate:
    theme_id: int
    netease_song_id: str
    name: str
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    cover: str | None = None
    duration_ms: int | None = None
    tags: list[str] = field(default_factory=list)
    anime_alias: str | None = None
    trans_names: list[str] = field(default_factory=list)
    fee: int | None = None
    mvid: int | None = None
    query_used: str = ""
    search_rank: int | None = None
    score: float = 0.0
    score_detail: dict[str, Any] = field(default_factory=dict)
    origin: Origin = Origin.SEARCH
    id: int | None = None

    @property
    def alias_list(self) -> list[str]:
        if not self.anime_alias:
            return []
        return [self.anime_alias]


@dataclass
class Decision:
    theme_id: int
    status: DecisionStatus = DecisionStatus.PENDING_REVIEW
    chosen_candidate_id: int | None = None
    decided_by: str = "auto"      # auto | human
    note: str = ""
    retry_count: int = 0
    last_checked_at: datetime | None = None
    id: int | None = None


@dataclass
class PlaylistRef:
    name: str
    netease_id: str | None = None
    id: int | None = None


@dataclass
class HealthStatus:
    ok: bool
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Tab:
    id: str
    url: str
    title: str = ""
    ws_url: str = ""


@dataclass
class BrowserInfo:
    browser: str = ""
    protocol: str = ""
    ws_url: str = ""


# ---------------------------------------------------------------- 辅助

def artists_to_json(artists: list[str]) -> str:
    return json.dumps(artists or [], ensure_ascii=False)


def artists_from_json(s: str | None) -> list[str]:
    if not s:
        return []
    try:
        v = json.loads(s)
        return [str(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []
