"""搜索词构造 —— 实现 DESIGN.md §5.6。

按优先级依次尝试，命中高分即提前终止：

    1  {曲名} {主艺人}        最干净
    2  {曲名} {番剧中文名}     网易云大量「曲名(番剧《X》OP)」的命名习惯
    3  {番剧中文名} 片头曲/片尾曲  曲名不标准时（AnimeThemes 里有些只写 "OP1"）
    4  {曲名}                 兜底

上限 `MAX_QUERIES_PER_THEME`，避免把额度浪费在冷门番上。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import constants
from .normalize import normalize_text, script_of, split_artists


@dataclass
class QueryPlan:
    queries: list[str]
    reasons: list[str]      # 与 queries 一一对应，便于日志与审核界面解释

    def __iter__(self):
        return iter(self.queries)

    def __len__(self):
        return len(self.queries)

    def __getitem__(self, i):
        return self.queries[i]


def _clean(s: str | None) -> str:
    return " ".join((s or "").split()).strip()


def primary_artist(artists: list[str] | None) -> str:
    """取主艺人：多艺人字段里第一个，并剥掉 feat. 尾巴。"""
    if not artists:
        return ""
    for raw in artists:
        parts = split_artists(raw)
        if parts:
            # split_artists 返回集合，顺序不可靠；这里回到原串切
            import re

            first = re.split(r"\s*(?:/|／|&|＆|、|,|，|×|\bwith\b|\bfeat\.?\b|\bft\.?\b)\s*",
                             raw, flags=re.I)[0]
            first = _clean(first)
            if first:
                return first
    return _clean(artists[0])


def pick_anime_name(anime_names: list[str] | None) -> str:
    """优先中文名（网易云上中文命中率最高），否则退到第一个可用名。"""
    names = [_clean(n) for n in (anime_names or []) if _clean(n)]
    if not names:
        return ""
    for n in names:
        if any("\u4e00" <= ch <= "\u9fff" for ch in n):
            return n
    return names[0]


def kind_cn(kind: str) -> str:
    return "片头曲" if (kind or "").upper() == "OP" else "片尾曲"


def build_queries(
    song_title: str,
    artists: list[str] | None,
    anime_names: list[str] | None,
    kind: str = "OP",
    limit: int = constants.MAX_QUERIES_PER_THEME,
    aliases=None,
) -> QueryPlan:
    """构造搜索词。

    ⚠️ 实测得出的关键顺序（§5.6 修正）：

        AnimeThemes 只有**罗马音**曲名，而网易云索引的是**日文原名**。
        于是「{罗马音曲名} {艺人}」这种看起来最自然的查询反而搜不到：

            查询 "Kienai Riyuu WANIMA"  -> 0 条
            查询 "WANIMA"（仅艺人）      -> 第 0 条就是 消えない理由(WANIMA)  ✅

        原因是罗马音曲名对网易云是**噪声**，会把结果全部滤掉。
        而网易云能处理罗马音**艺人**名（存了艺人别名，Yorushika → ヨルシカ）。

        所以：曲名是拉丁字母时，**把「仅艺人」提到最前**，
        再靠打分器的罗马音比较把正确的那条挑出来。

    `aliases` 是可选注入的别名表。注入后曲名/艺人/番剧名都会先展开成
    所有已知等价写法，并**优先挑 CJK 写法当主查询词** ——
    因为用户确认过一次的对应关系（例如 Kienai Riyuu ↔ 消えない理由）
    往往正好把「罗马音噪声」这个问题直接消掉。
    """
    raw_titles = [t for t in [song_title] if t]
    raw_artists = list(artists or [])
    raw_anime = list(anime_names or [])
    if aliases is not None:
        raw_titles = aliases.expand_names("title", raw_titles)
        raw_artists = aliases.expand_names("artist", raw_artists)
        raw_anime = aliases.expand_names("anime", raw_anime)

    # 有 CJK/混排写法就优先用它当主查询词 —— 网易云索引的是原名
    def _prefer_native(names: list[str]) -> str:
        cleaned = [_clean(n) for n in names if _clean(n)]
        if not cleaned:
            return ""
        for n in cleaned:
            if script_of(n) in ("cjk", "mixed"):
                return n
        return cleaned[0]

    title = _prefer_native(raw_titles)
    artist = _prefer_native(raw_artists) or primary_artist(raw_artists)
    anime = pick_anime_name(raw_anime)
    kind_label = kind_cn(kind)
    title_is_latin = script_of(title) == "latin"

    candidates: list[tuple[str, str]] = []
    if title_is_latin:
        # 罗马音曲名是噪声 → 先按艺人找，再用罗马音打分挑
        if artist and anime:
            candidates.append((f"{artist} {anime}", "艺人+番剧名"))
        if artist:
            candidates.append((artist, "仅艺人（罗马音曲名对网易云是噪声）"))
        if artist and title:
            candidates.append((f"{title} {artist}", "曲名+艺人"))
        if anime:
            candidates.append((f"{anime} {kind_label}", f"番剧名+{kind_label}"))
        if title:
            candidates.append((title, "仅曲名"))
    else:
        # 曲名本身是中日文，与网易云索引一致，按原优先级
        if title and artist:
            candidates.append((f"{title} {artist}", "曲名+主艺人"))
        if title and anime:
            candidates.append((f"{title} {anime}", "曲名+番剧名"))
        if artist:
            candidates.append((artist, "仅艺人"))
        if anime:
            candidates.append((f"{anime} {kind_label}", f"番剧名+{kind_label}"))
        if title:
            candidates.append((title, "仅曲名"))

    queries: list[str] = []
    reasons: list[str] = []
    seen: set[str] = set()
    for q, why in candidates:
        key = normalize_text(q)
        if not key or key in seen:
            continue
        seen.add(key)
        queries.append(q)
        reasons.append(why)
        if len(queries) >= max(1, limit):
            break

    return QueryPlan(queries, reasons)
