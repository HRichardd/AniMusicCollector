"""打分器 —— 实现 DESIGN.md §6。

多信号加权 + 乘性修正，输出 0~1 的分数与可解释的分项明细。

    base = 0.35·曲名相似 + 0.25·艺人匹配 + 0.30·番剧标注 + 0.10·时长合理
    final = clamp(base × Π降权 × Π加权, 0, 1)

两个来自实测的关键处理（§6.1 / §6.2）：
    · **跨书写系统**：AnimeThemes 存罗马音、网易云存日文原名，
      字符集不相交导致相似度恒为 0 → 补一次罗马音比较
    · **搜索排名**：用对关键词时网易云的排序本身是信号，
      在罗马音失效时能打破「同歌手多首歌同分」的僵局
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import constants
from .normalize import (
    artist_romaji_similarity,
    expand_artists,
    is_cross_script,
    normalize_text,
    romanize,
    romanize_compare,
    title_ratio,
)

_NOISE = {k: re.compile(v, re.I) for k, v in constants.NOISE_PATTERNS.items()}


@dataclass
class Target:
    """要匹配的目标：一条 theme。

    `aliases` 是可选注入的别名表（`normalize.AliasTable`）。
    注入后，曲名/艺人/番剧名都会先**展开成所有已知等价写法**再比较 ——
    这是「确认一次、永久生效」的落点：用户在上一步人工确认过的对应关系
    会在这里直接把原本匹配不上的组合救回来。
    """

    song_title: str
    artists: list[str]
    anime_names: list[str]
    kind: str = "OP"          # OP / ED
    aliases: Any = None

    _titles: list[str] | None = field(default=None, repr=False, compare=False)
    _artist_names: list[str] | None = field(default=None, repr=False, compare=False)
    _anime_names: list[str] | None = field(default=None, repr=False, compare=False)

    def _expand(self, kind: str, names: list[str]) -> list[str]:
        out = [n for n in (names or []) if n]
        if self.aliases is not None:
            out = self.aliases.expand_names(kind, out)
        return out

    def title_variants(self) -> list[str]:
        if self._titles is None:
            self._titles = self._expand("title", [self.song_title])
        return self._titles

    def anime_variants(self) -> list[str]:
        if self._anime_names is None:
            self._anime_names = self._expand("anime", self.anime_names)
        return self._anime_names

    def artist_set(self) -> set[str]:
        if self._artist_names is None:
            self._artist_names = self._expand("artist", self.artists)
        return expand_artists(self._artist_names)


@dataclass
class Scored:
    score: float
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- 分项

def title_similarity(cand_name: str | None, target_title: str | None) -> tuple[float, float | None]:
    """返回 (相似度, 罗马音相似度或 None)。

    主比较交给 `normalize.title_ratio`（它内部已含跨书写系统的罗马音兜底）。
    这里额外把罗马音那一项单独取出来，供审核界面展示分项得分。
    """
    base = title_ratio(cand_name, target_title)
    rom = None
    if is_cross_script(cand_name, target_title):
        r = romanize_compare(cand_name, target_title)
        rom = r if r > 0 else None
    return base, rom


def artist_match(cand_artists: list[str] | None, target: Target) -> float:
    """艺人匹配。

    三层，从严到宽：
      1. 归一化后直接相等            → 1.0 / 0.8
      2. **别名表里的等价关系**      → 1.0（人工确认过的事实，给满分）
      3. 罗马音兜底                  → 0.8

    第 2 层是关键：有了它，「确认一次 → 以后自动通过」才真正成立。
    没有它时实测只能从 0.47 抬到 0.79，差 0.01 卡在自动门槛外，
    用户每次重跑都要再确认一遍。
    """
    cs = expand_artists(cand_artists or [])
    ts = target.artist_set()
    if not cs or not ts:
        return 0.5

    # ⚠️ 别名检查必须放在**直接比较之前**。
    # 因为 `artist_set()` 已经被别名表展开过，直接比较会先命中并返回 0.8，
    # 于是「人工确认过的等价关系」拿不到它应得的 1.0。
    # 实测：放后面时分数停在 0.786，差 0.014 卡在自动门槛外。
    if target.aliases is not None:
        for c in cand_artists or []:
            for t in target.artists or []:
                if target.aliases.same_group("artist", c, t):
                    return 1.0

    if cs & ts:
        return 1.0 if cs == ts else 0.8

    for c in cs:
        for t in ts:
            if c and t and (c in t or t in c):
                return 0.8
    # 跨书写系统兜底（实测必需）：
    #   花冷え。    ↔ HANABIE.
    #   中島健人    ↔ Kento Nakajima   （词序相反）
    #   奥井雅美    ↔ Masami Okui      （词序相反）
    #   女王蜂      ↔ Ziyoou-vachi     （罗马音方案不同）
    # 不处理这一项，上面这些「曲名完全正确、艺人其实是同一个」的会被判成人0.00。
    for c in cs:
        for t in ts:
            # 阈值 0.75（而非 0.88）：实测 pykakasi 对片假名外来语与
            # 人名读音经常不准，卡太死会把「同一个艺人」判成不同。
            # 风险可控 —— 艺人项最高只给 0.8，最终分还要靠曲名撑。
            if artist_romaji_similarity(c, t) >= 0.75:
                return 0.8
    return 0.0


def anime_alias_match(cand_alias: list[str] | str | None, target: Target) -> float:
    """设计的核心信号：网易云自带的「TV动画《XXX》片头曲」。

    番剧名会先经别名表展开，所以用户确认过一次的对应关系能立刻复用。
    """
    if not cand_alias:
        return 0.0
    if isinstance(cand_alias, str):
        cand_alias = [cand_alias]
    blob = " ".join(str(x) for x in cand_alias).strip()
    if not blob:
        return 0.0
    names = target.anime_variants()
    if not any(n and n in blob for n in names):
        return 0.0
    kind_ok = (target.kind == "OP" and "片头" in blob) or (
        target.kind == "ED" and "片尾" in blob
    )
    return 1.0 if kind_ok else 0.4


def duration_plausible(ms: int | None) -> float:
    if not ms:
        return 0.5
    lo, hi = constants.DURATION_FULL_MIN, constants.DURATION_FULL_MAX
    if lo <= ms <= hi:
        return 1.0
    if ms <= constants.DURATION_TV_MAX:
        return 0.15
    return 0.5


# ---------------------------------------------------------------- 主入口

def score(cand: dict, target: Target, prefer_full_size: bool = True) -> Scored:
    """`cand` 至少含 name / artists / alias / duration_ms / tags。"""
    name = cand.get("name") or ""
    artists = cand.get("artists") or []
    tags = cand.get("tags") or []
    duration = cand.get("duration_ms")
    alias = cand.get("alias")
    norm_name = normalize_text(name)

    s_title, s_rom = 0.0, None
    # 曲名按「所有已知等价写法」取最高分 —— 别名表学过的关系在这里生效
    for variant in target.title_variants():
        t, r = title_similarity(name, variant)
        if t > s_title:
            s_title, s_rom = t, r
    s_artist = artist_match(artists, target)
    s_alias = anime_alias_match(alias, target)
    s_dur = duration_plausible(duration)

    base = (
        constants.W_TITLE * s_title
        + constants.W_ARTIST * s_artist
        + constants.W_ANIME_ALIAS * s_alias
        + constants.W_DURATION * s_dur
    )

    # ---- 乘性修正
    blob = name + " " + " ".join(alias if isinstance(alias, list) else ([alias] if alias else []))
    penalties: dict[str, float] = {}
    for key, rx in _NOISE.items():
        if not rx.search(blob):
            continue
        if key == "tv_size" and not prefer_full_size:
            continue
        if key == "cover" and s_artist >= 0.8:
            continue  # 艺人对得上就不当翻唱处理
        penalties[key] = constants.NOISE_PENALTY[key]

    bonuses: dict[str, float] = {}
    if constants.TAG_ORIGINAL in (tags or []):
        bonuses["original_tag"] = constants.BONUS_ORIGINAL_TAG

    # 「曲名完全相等」在跨书写系统下应等价于「罗马音完全相等」。
    # 实测动机：target「Mahiru no Tsuki」vs 网易云「真昼の月」，
    # 归一化后永不相等，于是拿不到这项加权，正确结果被压低。
    # 别名表里学过的写法也算「完全相等」。
    exact = bool(norm_name) and any(
        norm_name == normalize_text(v) for v in target.title_variants()
    )
    if exact:
        bonuses["exact_title"] = constants.BONUS_EXACT_TITLE
    elif s_rom is not None and s_rom >= 0.98:
        bonuses["exact_title_romaji"] = constants.BONUS_EXACT_TITLE

    rank = cand.get("search_rank", cand.get("rank"))
    if isinstance(rank, int):
        rb = constants.BONUS_SEARCH_RANK.get(rank, 1.0)
        if rb > 1.0:
            bonuses["search_rank"] = rb

    factor = 1.0
    for v in penalties.values():
        factor *= v
    for v in bonuses.values():
        factor *= v

    final = max(0.0, min(1.0, base * factor))

    return Scored(
        score=round(final, 4),
        detail={
            "title": round(s_title, 3),
            "title_romaji": None if s_rom is None else round(s_rom, 3),
            "artist": round(s_artist, 3),
            "anime_alias": round(s_alias, 3),
            "duration": round(s_dur, 3),
            "base": round(base, 3),
            "cross_script": is_cross_script(name, target.song_title),
            "penalties": penalties,
            "bonuses": bonuses,
        },
    )


def classify(value: float) -> str:
    """阈值分档（§6.3）。"""
    if value >= constants.AUTO_THRESHOLD:
        return "auto"
    if value >= constants.REVIEW_THRESHOLD:
        return "pending_review"
    return "low"


def pick_best(hits: list[dict]) -> dict | None:
    """同曲多版本收敛（§6.4）：只留一个最佳版本。

    `hits` 中每项需含 score / duration_ms / netease_song_id。
    调用方应已按 score 降序排好；此处只做平局打破。
    """
    if not hits:
        return None

    def sort_key(h: dict) -> tuple:
        dur = h.get("duration_ms") or 0
        # 时长越接近 240s 越好；再退化到 song_id 小的（网易云老 ID 通常是原版）
        return (
            -float(h.get("score") or 0),
            abs(dur - 240_000) if dur else 10**9,
            int(h.get("netease_song_id") or 0),
        )

    return sorted(hits, key=sort_key)[0]


def dedupe_key(cand: dict) -> tuple[str, frozenset]:
    """判定「同一首歌」的键 —— 用曲名+艺人，不用 song_id。

    因为同一首歌的不同音质/不同专辑在网易云是不同的 song_id。
    """
    return (
        normalize_text(cand.get("name")),
        frozenset(expand_artists(cand.get("artists") or [])),
    )
