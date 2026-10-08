"""日文维基百科「主題歌」栏 —— OP/ED 曲目的**主数据源**。

## 为什么换源（用户提议，直击要害）

AnimeThemes 只提供**罗马音**曲名，而网易云索引的是**日文原名**。
这个错配是当前最大的准确率损失来源（附录 G.9/G.10 实测）：

    罗马音（AnimeThemes）   网易云实际收录      pykakasi 读音
    Kienai Riyuu        ↔  消えない理由        读不出来
    Kento Nakajima      ↔  中島健人            读成 taketo（错）
    Umeda Cypher        ↔  梅田サイファー        sifa vs cypher
    Zutomayo            ↔  ずっと真夜中でいいのに。  读不出来

之前的对策是「罗马音兜底 + 别名表自学习」—— 那是**事后补救**。
维基的主题歌栏**一开始就给日文原名 + 艺人**：

    ; 「花になって」
    : [[緑黄色社会]]によるオープニングテーマ。

所以这是从源头消掉问题，而不是猜。

## 实测覆盖率（2025Q4 抽样 12 部）

    拿到日文原名           12/12   （来自 AniList）
    找到词条且有主题歌栏    9/12    （精确 8 · 搜索校验后 1）
    解析出的主题歌条目      26 条   （与 AnimeThemes 同批持平）

## 两种格式（都实测见过）

**格式 A** 定义列表（逃げ上手の若君）::

    ; 「プランA」{{R|...}}
    : [[DISH//]]による第一期のオープニングテーマ。

**格式 B** wikitable（薬屋のひとりごと (アニメ)）::

    {| class="wikitable"
    |+ オープニングテーマ
    ! 使用話 !! 曲名 !! 歌手 !! 作詞 !! 作曲
    ! 1 - 12
    | [[花になって]] || [[緑黄色社会]] || 長屋晴子 || ...
    |}

## ⚠️ 三个必须注意的点

**① 维基会限流，而且比网易云严格得多。**
实测：网易云搜索连发 20 次全 200；维基连发一百多次就 429。
所以默认 1 req/s，并对 429 退避重试。**对维基要有礼貌**。

**② 搜索兜底必须校验词条名。**
实测踩到的坑：`元祖！バンドリちゃん` 搜到并接受了
`ひみつのアッコちゃん` —— 完全无关的另一部动画。
这比「没找到」更糟：没找到只是缺数据，**找错了会静默写入错误数据**。

**③ 正文是 CC BY-SA 授权。**
界面上需要标注来源。见 `ATTRIBUTION`。

## 解析的可靠性边界

解析器处理**这两种**实测见过的格式。维基编辑风格不统一，
遇到没见过的格式会**返回空**而不是猜 —— 调用方据此回落到 AnimeThemes。
宁可少拿数据，不可拿错数据。
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field

from .. import constants
from ..http import HttpClient, RateLimited
from ..models import ThemeKind

WIKI_HOST = "ja.wikipedia.org"
WIKI_API = f"https://{WIKI_HOST}/w/api.php"

#: 维基要求 User-Agent 里带联系方式，否则容易被限流。
#: ⚠️ 这里**刻意不写占位地址**：原来写的是 `https://github.com/local/animusic;
#: contact: local-user` —— 一个不存在的仓库 + 不存在的联系人。假联系方式比不写更糟
#: （真出问题时限流封的是一整个 UA，而对方找不到人沟通）。
#: 要对外发布/大量抓取时，把下面的字符串换成真实地址或邮箱。
USER_AGENT = "AniMusicCollector/0.1"

#: 正文授权要求署名
ATTRIBUTION = "主題歌データ出典: ウィキペディア（CC BY-SA 4.0）"

#: 对维基的最小请求间隔。实测连发会吃 429，1 秒对个人工具足够保守。
WIKI_RPS = 1.0

#: 词条消歧后缀（按优先级试）
PAGE_SUFFIXES = (" (アニメ)", " (テレビアニメ)", "")

#: 一次 `action=query&prop=revisions` 最多放几个候选名。
#: 服务端的硬上限是 50（匿名），但**真正的限制是 URL 长度**（见下一个常量）。
WIKI_BATCH_TITLES = 50

#: `titles=` 里所有候选名 percent-encode 之后的**字节上限**。
#:
#: 为什么按字节而不按个数：候选名基本都是日文，一个 15 字的标题编码后
#: 约 150 字节，50 个就是 7.5KB。Apache 的请求行上限是 8KB，
#: 而用户走的是代理 —— 不少代理/CDN 在 4KB 甚至 2KB 就回 414 了。
#: 1800 是个保守值：宁可多两次请求，也不要一次 414 把整块候选名丢掉。
WIKI_BATCH_URL_MAX = 1800

#: `find_article` 试候选名时一批放几个 —— 也就是「一次请求能试几个名字」。
#: 12 = 一个名字的全部候选（`title_variants` × `PAGE_SUFFIXES` 最多 4×3）。
#: 少了会退化成一次一个名字（真机那 62 次调用就是这么来的），
#: 多了则第一批就带上大量注定 404 的猜测。
WIKI_TRY_BATCH = 12

#: 匹配标题的最小相似度 —— 宁可漏，不可错
TITLE_MIN_SIM = 0.72


@dataclass
class WikiTheme:
    kind: ThemeKind
    song_title: str
    artists: list[str] = field(default_factory=list)
    sequence: int | None = None
    note: str = ""              # 「第一期」「第14話の挿入歌」之类
    raw: str = ""
    fmt: str = ""               # list | table
    page: str = ""

    @property
    def is_interlude(self) -> bool:
        """插入歌 / 角色歌 —— 不属于 OP/ED，默认不纳入。"""
        return "挿入歌" in self.note or "劇中歌" in self.note


# ---------------------------------------------------------------- 清理

_REF = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.S)
_TMPL = re.compile(r"\{\{[^{}]*\}\}")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_LINK = re.compile(r"\[\[([^\[\]|]*)\|([^\[\]]*)\]\]")
_LINK1 = re.compile(r"\[\[([^\[\]]+)\]\]")
_TAG = re.compile(r"<[^>]+>")


def clean_wikitext(s: str) -> str:
    s = _COMMENT.sub("", s)
    s = _REF.sub("", s)
    for _ in range(6):                  # 模板可能嵌套
        s2 = _TMPL.sub("", s)
        if s2 == s:
            break
        s = s2
    s = _LINK.sub(r"\2", s)             # [[A|B]] → B
    s = _LINK1.sub(r"\1", s)            # [[A]] → A
    s = _TAG.sub("", s)
    s = s.replace("'''", "").replace("''", "")
    return " ".join(s.split()).strip()


#: wikitable 单元格属性前缀：`colspan="2"| `、`rowspan="3" style="..."| `
#: 实测：不做这步会把 `colspan="2"` 当成艺人名的一部分
#: （真实数据里 `colspan="2"| [[Uru]]` 被解析成艺人 `colspan="2"| Uru`）。
_CELL_ATTR = re.compile(r'^\s*(?:[a-zA-Z-]+\s*=\s*"[^"]*"\s*)+\|\s*')


def _table_cell(s: str) -> str:
    """清理一个 wikitable 单元格：先剥属性前缀，再做常规清理。"""
    s = _CELL_ATTR.sub("", s)
    return clean_wikitext(s)


# ---------------------------------------------------------------- 解析

_SECTION_RX = re.compile(
    r"^=+\s*(主題歌|オープニングテーマ|エンディングテーマ|テーマソング|音楽)"
    r"[^=\n]*\s*=+\s*$",
    re.M,
)
_KIND_PATTERNS = (
    (re.compile(r"オープニング|OPテーマ"), ThemeKind.OP),
    (re.compile(r"エンディング|EDテーマ"), ThemeKind.ED),
)
_QUOTE = re.compile(r"[「『\"](.+?)[」』\"]")
_SEQ = re.compile(r"第([一二三四五六七八九十\d]+)(期|クール|シリーズ)")
_KANJI_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _kind_of(text: str) -> ThemeKind | None:
    for rx, k in _KIND_PATTERNS:
        if rx.search(text):
            return k
    return None


def _seq_of(text: str) -> int | None:
    m = _SEQ.search(text)
    if not m:
        return None
    t = m.group(1)
    if t.isdigit():
        return int(t)
    return _KANJI_NUM.get(t)


#: 标题里的**季数后缀**。维基词条通常不带这些，而 Bangumi 的名字经常带。
#: 实测踩过：`氷の城壁 第2期` 查不到词条，但 `氷の城壁` **主词条就有 4 条主题歌** ——
#: 纯粹是被后缀挡住了。
_SEASON_SUFFIX = re.compile(
    r"[\s　]*("
    r"第[0-9一二三四五六七八九十]+(期|クール|シリーズ|部作?)"
    r"|[0-9]+(st|nd|rd|th)\s+Season"
    r"|Season\s*[0-9]+"
    r"|シーズン[0-9]+"
    r"|[ⅡⅢⅣⅤ]+"
    r")[\s　]*$",
    re.I,
)


def title_variants(name: str) -> list[str]:
    """同一个番剧的几种可能词条名。

    两个实测踩过的坑，都在这里处理：

    **① 季数后缀**：Bangumi 给 `氷の城壁 第2期`，而维基主词条叫 `氷の城壁`
    —— 里面就有 4 条主题歌，纯粹被后缀挡住了。

    **② 全角/半角标点**：Bangumi 给 `名探偵プリキュア！`（全角 U+FF01），
    而维基词条叫 `名探偵プリキュア!`（半角 U+0021）。
    全角半角不匹配 → 精确查找失败 → 搜索兜底又返回「名探偵コナン」之类
    无关条目，于是被正确拒绝，结果是**明明有词条却找不到**。

    所以每个名字都同时产出 NFKC 归一化版本（全角 → 半角）。
    只用于**生成候选标题**，不影响匹配判定，所以 NFKC 的副作用无所谓。
    """
    out: list[str] = []

    def add(s: str) -> None:
        s = (s or "").strip()
        if s and s not in out:
            out.append(s)

    for base in (name, unicodedata.normalize("NFKC", name or "")):
        add(base)
        add(_SEASON_SUFFIX.sub("", base).strip())
    return out


def candidate_titles(names: list[str]) -> list[str]:
    """一部番要试的**全部**候选词条名，按优先级去重。

    顺序**必须**与 `find_article` 逐个试时的顺序完全一致
    （name → title_variants → PAGE_SUFFIXES）：批量取回正文之后，
    仍然是「先命中的先用」，判定结果一个字都不变。
    真机那个例子：`野生のラスボスが現れた！` 解析出 6 个候选名
    （`第2期` 前后各 3 个后缀），而名字里带英文/中文的番会翻倍。
    """
    out: list[str] = []
    for n in names:
        if not n:
            continue
        for base in title_variants(n):
            for suffix in PAGE_SUFFIXES:
                t = base + suffix
                if t and t not in out:
                    out.append(t)
    return out


def _batch_chunks(titles: list[str]) -> list[list[str]]:
    """把候选名切成几次请求（按个数 + 按编码后的字节数）。见 `WIKI_BATCH_URL_MAX`。"""
    out: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for t in titles:
        n = len(_quote(t)) + 1          # +1 = 分隔用的 `%7C`
        if cur and (len(cur) >= WIKI_BATCH_TITLES or size + n > WIKI_BATCH_URL_MAX):
            out.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += n
    if cur:
        out.append(cur)
    return out


def _revision_content(page: dict) -> str | None:
    """从 `prop=revisions` 的一个 page 里取正文。

    两种形状都要认：带 `rvslots=main` 的是 `revisions[0].slots.main.content`，
    没带的（老版本/别的 wiki）是 `revisions[0].content`，
    而且后者可能是 `{"*": "..."}`。宁可多写三行，也不要在某个 wiki 上
    静默拿到 `None` 而以为「这个词条没有正文」。
    """
    revs = page.get("revisions") or []
    if not revs:
        return None
    r = revs[0]
    slots = r.get("slots")
    if isinstance(slots, dict) and slots:
        main = slots.get("main") or next(iter(slots.values()), None)
        return (main or {}).get("content")
    c = r.get("content")
    if isinstance(c, dict):
        return c.get("*")
    return c


def theme_sections(wikitext: str) -> list[str]:
    """**所有**可能的主题歌相关段落，按出现顺序。

    为什么不只取第一个：段落名匹配天然会误命中。
    实测 `薬屋のひとりごと (アニメ)` 的第一候选段落是
    「音楽」—— 讲的是**劇伴（配乐）作曲家**，一个主题歌都没有。
    真正的主题歌栏在后面。所以调用方必须依次试，取第一个有结果的。
    """
    out: list[str] = []
    for m in _SECTION_RX.finditer(wikitext):
        start = m.end()
        nxt = re.search(r"^=+[^=\n]+=+\s*$", wikitext[start:], re.M)
        out.append(wikitext[start:start + (nxt.start() if nxt else 5000)])
    return out


def theme_section(wikitext: str) -> str | None:
    """兼容旧接口：返回第一个候选段落（**可能不含主题歌**）。

    新代码请用 `parse_from_wikitext()`，它会依次试所有候选段落。
    """
    secs = theme_sections(wikitext)
    return secs[0] if secs else None


def parse_from_wikitext(wikitext: str, page: str = "",
                        include_interludes: bool = False) -> list[WikiTheme]:
    """从整篇 wikitext 抽主题歌 —— 依次试每个候选段落，取第一个有结果的。

    这是**推荐的入口**：单个段落名不可靠，靠「解析出东西」来判定才对。
    """
    for seg in theme_sections(wikitext):
        themes = parse_themes(seg, page=page)
        if not include_interludes:
            themes = [t for t in themes if not t.is_interlude]
        if themes:
            return themes
    return []


def _artist_from_desc(desc: str) -> str:
    m = re.search(r"^(.+?)による", desc)
    if m:
        return m.group(1).strip(" 、,")
    m = re.search(r"^(.+?)[のが]", desc)
    return m.group(1).strip() if m else ""


def parse_themes(segment: str, page: str = "") -> list[WikiTheme]:
    """解析主题歌段 —— 支持实测见到的两种格式。

    **遇到不认识的格式就返回空**，不猜。调用方据此回落到 AnimeThemes。
    """
    out: list[WikiTheme] = []

    # ---- 格式 A：定义列表
    for m in re.finditer(r"^;\s*(.+?)\s*$\n:\s*(.+?)\s*$", segment, re.M):
        head, desc = clean_wikitext(m.group(1)), clean_wikitext(m.group(2))
        q = _QUOTE.search(head)
        title = (q.group(1) if q else head).strip()
        kind = _kind_of(desc) or _kind_of(head)
        if not title or kind is None:
            continue
        artist = _artist_from_desc(desc)
        out.append(WikiTheme(
            kind=kind, song_title=title,
            artists=[artist] if artist else [],
            sequence=_seq_of(desc) or _seq_of(head),
            note=desc[:160], raw=desc[:200], fmt="list", page=page,
        ))

    # ---- 格式 B：wikitable
    #
    # ⚠️ 真实结构里，「使用話」在 `!` 行、**「曲名/歌手」在下一行 `|` 行**：
    #
    #     ! 1 - 12
    #     | [[花になって]] || [[緑黄色社会]] || 長屋晴子
    #
    # 一开始我假设它们在同一个单元格组里（`! num | title || artist`），
    # 于是表格格式**一直解析出 0 条**——列表格式的表掩盖了这个 bug。
    # 现在把 `!` 行的话数记下来，供紧随其后的 `|` 行使用。
    if "{|" in segment:
        cur_kind: ThemeKind | None = None
        pending_num = ""
        for line in segment.split("\n"):
            s = line.strip()
            if s.startswith("|+"):
                # ⚠️ 必须**直接赋值**（不能 `or cur_kind` 保留旧值）。
                # 实测：主題歌段里 エンディングテーマ 表之后还有一张
                # 「挿入歌」表，保留旧 kind 会把 7 首**插入歌**当成 ED ——
                # 那会污染歌单（插入歌不是 OP/ED）。
                # 新标题解析不出 OP/ED 时就置空，让该表的行被跳过。
                caption = clean_wikitext(s[2:])
                cur_kind = _kind_of(caption)
                if cur_kind is None and "挿入歌" not in caption and "劇中歌" not in caption:
                    # 标题不属于主题歌类，也明确置空
                    cur_kind = None
                continue

            if s.startswith("!"):
                head = clean_wikitext(s[1:].strip())
                if "曲名" in head:              # 表头行
                    continue
                # 纯话数行（「1 - 12」「第3話」）→ 记为 pending
                if head and re.fullmatch(r"[\d\s\-–~〜第話]*", head):
                    pending_num = head
                continue

            if not s.startswith("|") or s.startswith("|-"):
                continue
            cells = [c for c in (_table_cell(c)
                                 for c in re.split(r"\|\||!!", s[1:].strip())) if c]
            if len(cells) < 2:
                continue
            title, artist = cells[0], cells[1]
            if not title:
                continue
            kind = cur_kind or _kind_of("".join(cells))
            if kind is None:
                continue
            out.append(WikiTheme(
                kind=kind, song_title=title,
                artists=[artist] if artist else [],
                sequence=None,
                note=(f"使用話 {pending_num}" if pending_num else "wikitable"),
                raw=" | ".join(cells)[:200], fmt="table", page=page,
            ))
            pending_num = ""

    # 去重（同一首歌可能在 OP/ED 表里各出现一次）
    seen: set[tuple] = set()
    uniq: list[WikiTheme] = []
    for t in out:
        key = (t.kind, t.song_title, tuple(t.artists))
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    return uniq


# ---------------------------------------------------------------- 标题校验

def canon_title(s: str) -> str:
    """激进规范化，用于判断「词条名与番剧名是不是同一部」。

    实测踩过的两个坑都体现在这里：
      · **误收**：`元祖！バンドリちゃん` 接受了 `ひみつのアッコちゃん`
      · **误杀**：`キャッツ♥アイ (2025)` 被拒掉了正确的 `キャッツ・アイ`
        —— 因为 `♥` 没被清掉、年份也没剥

    ⚠️ 注意 `・`(U+30FB) 和 `゠`(U+30A0) **落在片假名区段里**，
    但它们其实是标点 —— 只用区段过滤会把它们留下，
    于是 `キャッツ・アイ` 与 `キャッツアイ` 就比不上了（实测踩过）。
    长音符 `ー`(U+30FC) 则必须保留，它是假名的一部分。
    """
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"\s*\((アニメ|テレビアニメ|漫画|小説|曖昧さ回避)\)\s*$", "", s)
    s = s.replace("\u30fb", "").replace("\u30a0", "")   # ・ ゠
    # 只留字母、数字、假名、汉字 —— 其余符号一律丢弃
    s = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", "", s)
    # 剥掉结尾年份（「...2025」通常是同名新作）
    s = re.sub(r"(19|20)\d{2}$", "", s)
    return s


def title_matches(anime_name: str, page_title: str) -> bool:
    """词条名和番剧名是否**真的是同一部**。宁可漏，不可错。"""
    a, b = canon_title(anime_name), canon_title(page_title)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio() >= TITLE_MIN_SIM


# ---------------------------------------------------------------- 数据源

class WikipediaSource:
    """日文维基的主题歌读取。

    自带**进程内 + 磁盘缓存**：同一个词条在一次运行里只读一次 ——
    既省请求（维基会限流），也避免重复解析。
    """

    def __init__(self, client: HttpClient | None = None, rps: float = WIKI_RPS,
                 cache_ttl: float = 0.0,
                 timeout: float = constants.FETCH_HTTP_TIMEOUT,
                 retries: int = constants.FETCH_HTTP_RETRIES,
                 slow_seconds: float = constants.SLOW_REQUEST_SECONDS,
                 on_event=None,
                 rate_limit_wait_budget: float = constants.RATE_LIMIT_WAIT_BUDGET,
                 rate_limit_retries: int = constants.RATE_LIMIT_RETRIES,
                 rate_limit_breaker: int = constants.RATE_LIMIT_BREAKER):
        # ⚠️ 超时/重试**刻意从 30 秒 / 4 次降下来**（见 `constants.FETCH_HTTP_TIMEOUT`）。
        # ⚠️ 429（限流）另有自己的一套策略，见 `constants` 里「HTTP 429」那一节。
        self.http = client or HttpClient(
            user_agent=USER_AGENT, rps=rps, retries=retries,
            cache_ttl=cache_ttl, timeout=timeout,
            slow_seconds=slow_seconds, on_event=on_event,
            rate_limit_wait_budget=rate_limit_wait_budget,
            rate_limit_retries=rate_limit_retries,
            rate_limit_breaker=rate_limit_breaker,
        )
        self._lock = threading.Lock()
        self._wt_cache: dict[str, str | None] = {}
        #: 诊断计数 —— 覆盖率报告与「本步时间花在哪」的日志都用它。
        #:
        #: ⚠️ `not_found` 与 `api_failed` **必须分开**：前者是「维基真的没有这个
        #: 词条」，后者是「我们没问到」。合成一个数就会把梯子故障报成
        #: 「维基没收录」，用户会照着错的结论去手动补数据（§6 不许伪造指标）。
        #:
        #: ⚠️ 换成批量接口（`_revisions`）之后 `api_calls` 的含义变了：
        #: 它现在是**批量请求**的次数，不再是「一个候选名一次」。
        #: 于是另有一组**候选名级**的计数专门用来对账（日志里那一行）::
        #:
        #:     候选名 = titles_cached + not_found + page_hits + titles_unanswered
        #:     titles_asked = not_found + page_hits + titles_unanswered
        #:
        #: 两条都是恒等式，有测试钉着。少了任何一项，用户就没法把手算的结果
        #: 和日志对上 —— 对不上账的汇总行比不报更糟，因为它看着像证据。
        self.stats = {"api_calls": 0, "page_hits": 0, "search_used": 0,
                      "rejected_title": 0, "no_section": 0,
                      "not_found": 0, "api_failed": 0,
                      #: 批量请求里**发出去问过**的候选名个数（不含命中缓存的）
                      "titles_asked": 0,
                      #: 连问都没问到的候选名个数（请求失败 / 被限流熔断）
                      "titles_unanswered": 0,
                      #: 靠进程内缓存答上的候选名个数（0 次网络开销，是好事）
                      "titles_cached": 0,
                      #: 最近一次失败的原因（**只留状态码/异常类型，不留 URL**）——
                      #: 自检与日志要能一眼说出「是 429 被限流，还是超时」。
                      "last_error": "",
                      #: 因为**连续限流熔断**而中止查询的次数（>0 = 本步被中断）
                      "rate_limit_stopped": 0}
        #: 被限流熔断的原因（空 = 没被限流）。编排层据此停止查询并告诉用户。
        self.blocked_reason = ""

    # ---- 底层
    def _api(self, params: dict) -> dict | None:
        """返回 `None` = **请求失败**（网络/超时）；返回 dict = 服务端真的答了。

        ⚠️ 为什么要把这两者分开（原来都返回 `{}`）：一次真机排查里，
        「拉取番单 7 分钟」的日志**无法回答**「维基是真的没收录，还是梯子断了」
        —— 因为两种情况的返回值一模一样。

        ⚠️ **HTTP 429（限流）的熔断必须往外抛**，不能像普通失败那样吞成
        `None`：吞掉之后调用方只会把它当成「这个候选名没搞定」，
        接着去试下一个 —— 真机那一次就这样把 62 个候选名全撞了一遍，
        28 个失败、84 次白发的请求、44.2 秒退避，一个都没成功。
        """
        with self._lock:
            self.stats["api_calls"] += 1
        url = WIKI_API + "?" + "&".join(
            f"{k}={_quote(v)}" for k, v in params.items()
        )
        try:
            return self.http.get_json(url, use_cache=True)
        except RateLimited as e:
            with self._lock:
                self.stats["api_failed"] += 1
                self.stats["last_error"] = "HTTP 429（限流）"
                if e.breaker:
                    self.stats["rate_limit_stopped"] += 1
                    self.blocked_reason = (
                        f"HTTP 429，服务端要求等待 {e.retry_after:.0f} 秒"
                        if e.retry_after is not None else "HTTP 429（限流）")
            if e.breaker:
                raise
            return None
        except Exception as e:  # noqa: BLE001 - 单个词条失败不该中断整季
            with self._lock:
                self.stats["api_failed"] += 1
                st = getattr(e, "status", None)
                self.stats["last_error"] = (f"HTTP {st}" if st
                                            else f"{type(e).__name__}: {e}")
            return None

    def wikitext(self, page: str) -> str | None:
        if page in self._wt_cache:
            return self._wt_cache[page]
        d = self._api({"action": "parse", "page": page, "prop": "wikitext",
                       "format": "json", "redirects": 1, "formatversion": 2})
        if d is None:
            # 没问到 —— 这不是「词条不存在」，别记进 not_found
            self._wt_cache[page] = None
            return None
        if "error" in d:
            with self._lock:
                self.stats["not_found"] += 1
            self._wt_cache[page] = None
            return None
        if not d:
            # 服务端答了但内容为空 —— 既不是「词条不存在」也不是「没问到」，
            # 两个计数器都不加（宁可少报，也不把一件事塞进错的桶里）
            self._wt_cache[page] = None
            return None
        wt = (d.get("parse") or {}).get("wikitext")
        self._wt_cache[page] = wt
        if wt:
            with self._lock:
                self.stats["page_hits"] += 1
        return wt

    # ---- 批量取正文（必修 2）

    def _revisions(self, chunk: list[str]) -> dict[str, str | None]:
        """一次请求问多个词条的正文。返回 `{候选名: wikitext 或 None}`。

        **没问到的候选名不出现在返回的字典里**（既不是 `None` 也不是 `""`）
        —— 调用方据此知道「这些还没问到」，而不是「这些不存在」。
        这条区分是整个项目的红线：把限流/断网说成「维基没收录」，
        用户会照着错的结论去手动补数据（§6）。

        为什么要有这个方法（真机实测）：一部番要试 7~13 个候选名，
        一次一个请求 = 一个季度 62 次调用 / 120 次网络请求，
        并且**撞上了维基的配额墙**（28 次 429、44.2 秒退避、一个词条都没问到）。
        换成 `action=query&prop=revisions` 之后，12 个候选名 = 1 次请求，
        而**判定顺序一个字没改**（见 `candidate_titles`）。
        """
        params = {
            "action": "query", "prop": "revisions",
            "rvslots": "main", "rvprop": "content",
            "titles": "|".join(chunk),
            "redirects": 1, "format": "json", "formatversion": 2,
        }
        with self._lock:
            self.stats["titles_asked"] += len(chunk)
        try:
            d = self._api(params)
        except RateLimited:
            # 熔断必须往外抛（见 `_api` 的说明），但这批候选名确实**没问到**
            # —— 不记的话「候选名 = 查无 + 取到 + 没问到」立刻对不上账。
            with self._lock:
                self.stats["titles_unanswered"] += len(chunk)
            raise
        if d is None:
            with self._lock:
                self.stats["titles_unanswered"] += len(chunk)
            return {}

        q = d.get("query") or {}
        pages = {p.get("title"): p for p in (q.get("pages") or []) if p.get("title")}
        # 请求名 →（归一化）→（重定向）→ 真正的词条名。
        # 归一化那一步不能省：`A_(アニメ)` 和 `A (アニメ)` 在服务端是同一个
        # 词条，而我们的候选名是拼出来的。重定向同理 —— `redirects=1` 之前
        # 是 `action=parse` 自己跟的，换成 query 就得自己跟。
        norm = {m.get("from"): m.get("to") for m in (q.get("normalized") or [])}
        redir = {m.get("from"): m.get("to") for m in (q.get("redirects") or [])}
        out: dict[str, str | None] = {}
        for t in chunk:
            final = t
            for _ in range(5):          # 最多跟 5 跳；有环也能出去
                nxt = norm.get(final) or redir.get(final)
                if not nxt or nxt == final:
                    break
                final = nxt
            p = pages.get(final)
            if p is None:
                # 服务端答了，但没给这一条 —— 既不是「有」也不是「没有」，
                # 两个桶都不进（宁可少报，也不把一件事塞进错的桶里）
                with self._lock:
                    self.stats["titles_unanswered"] += 1
                continue
            if p.get("missing"):
                with self._lock:
                    self.stats["not_found"] += 1
                self._wt_cache[t] = None
                out[t] = None
                continue
            content = _revision_content(p)
            if content is None:
                # 词条**存在**却读不出正文 —— 那就只可能是返回形状和我们预期的
                # 不一样（接口示例 / 参数组合 / 版本差异）。这种情况**绝不能**
                # 记成「取到了，只是没有主题歌栏」：那等于把「我们没读懂返回」
                # 说成「维基没收录」，方向正好反了（§6）。
                # 记成「没问到」且**不写缓存**，账也照样平。
                with self._lock:
                    self.stats["titles_unanswered"] += 1
                continue
            self._wt_cache[t] = content
            if content:
                with self._lock:
                    self.stats["page_hits"] += 1
            out[t] = content
        return out

    def wikitext_many(self, titles: list[str]) -> dict[str, str | None]:
        """批量取正文（自动切块 + 命中进程内缓存的不再请求）。"""
        out: dict[str, str | None] = {}
        todo: list[str] = []
        for t in dict.fromkeys(x for x in titles if x):
            if t in self._wt_cache:
                with self._lock:
                    self.stats["titles_cached"] += 1
                out[t] = self._wt_cache[t]
            else:
                todo.append(t)
        for chunk in _batch_chunks(todo):
            out.update(self._revisions(chunk))
        return out

    def search(self, query: str, limit: int = 5) -> list[str]:
        d = self._api({"action": "query", "list": "search", "srsearch": query,
                       "srlimit": limit, "format": "json", "formatversion": 2})
        if d is None:
            return []
        return [h["title"] for h in (d.get("query") or {}).get("search", [])]

    # ---- 找词条
    def find_article(self, names: list[str]) -> tuple[str | None, str]:
        """返回 (词条名, 怎么找到的)。`how` ∈ {exact, search, none}。

        顺序：先试 **(アニメ) 子词条** —— 动画的主题歌栏通常在那里，
        正文常常只有原作信息（实测：薬屋のひとりごと 正文没有主题歌栏，
        但 薬屋のひとりごと (アニメ) 有）。

        判定标准是**真的解析出主题歌**，而不是「有个叫主題歌的段落」——
        段落名会误命中（实测「音楽」段讲的是配乐作曲家）。

        ⚠️ 候选名是**一批一批**问的（`WIKI_TRY_BATCH` 个一次），
        但判定顺序与「一个一个试」**完全一致**：同一批里先命中的先用，
        一批都没命中才问下一批。所以结果不会因为批量而改变
        （`tests/test_wikipedia.py` 里有一条「批量与逐个结果相同」的对照测试）。
        """
        cands = candidate_titles(names)
        for i in range(0, len(cands), WIKI_TRY_BATCH):
            chunk = cands[i:i + WIKI_TRY_BATCH]
            got = self.wikitext_many(chunk)
            for c in chunk:
                wt = got.get(c)
                if wt and parse_from_wikitext(wt, page=c):
                    return c, "exact"

        # 搜索兜底 —— **必须校验词条名**，否则会静默拿到别部番的主题歌
        for n in names[:1]:
            if not n:
                continue
            self.stats["search_used"] += 1
            hits = self.search(f"{n} アニメ", 5)[:3]
            keep = []
            for hit in hits:
                if title_matches(n, hit):
                    keep.append(hit)
                else:
                    self.stats["rejected_title"] += 1
            got = self.wikitext_many(keep)      # 命中的几个也批量问，别一个一个来
            for hit in keep:
                wt = got.get(hit)
                if wt and parse_from_wikitext(wt, page=hit):
                    return hit, "search"
        return None, "none"

    # ---- 主入口
    def fetch_themes(self, names: list[str], *, include_interludes: bool = False
                     ) -> tuple[list[WikiTheme], str]:
        """按番剧名抓主题歌。返回 (主题歌列表, 词条名或空)。

        拿不到就返回空列表 —— 调用方应当回落到 AnimeThemes。
        """
        page, how = self.find_article(names)
        if not page:
            return [], ""
        wt = self.wikitext(page)
        if not wt:
            return [], ""
        themes = parse_from_wikitext(wt, page=page,
                                     include_interludes=include_interludes)
        if not themes:
            self.stats["no_section"] += 1
        return themes, page


def _quote(v) -> str:
    import urllib.parse

    return urllib.parse.quote(str(v))
