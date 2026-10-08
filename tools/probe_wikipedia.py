r"""可行性探针：能不能用日文维基百科的「主題歌」栏替代 AnimeThemes 的罗马音？

动机（用户的提议，直击要害）：
    当前最大的准确率损失来自**跨书写系统** —— AnimeThemes 只给罗马音，
    而网易云索引的是日文原名。实测失败案例：

        罗马音（AnimeThemes）   网易云实际收录      pykakasi 读音
        Kienai Riyuu        ↔  消えない理由        读不出来
        MONTAGE             ↔  MONTAGE             —
        Kento Nakajima      ↔  中島健人            读成 taketo（错）
        梅田サイファー        ↔  Umeda Cypher        sifa vs cypher

    这些靠算法永远猜不准（见附录 G.9/G.10）。
    而日文维基的主题歌栏**直接给出日文原名 + 艺人**：

        ; 「花になって」
        : [[緑黄色社会]]によるオープニングテーマ。

    如果覆盖率够，这条路比「罗马音 + 别名表自学习」根本得多 ——
    不是事后补救，而是一开始就拿到正确的名字。

本工具只做**测量**，不写库：
    · 找出每部番的维基词条（正文 or 「(アニメ)」子词条 or 搜索）
    · 解析「主題歌」段（至少两种格式）
    · 报告覆盖率与抽样对照

用法：
    $env:PYTHONPATH = "<工作区>\src;<工作区>\vendor"
    python tools/probe_wikipedia.py --season 2026Q2 --limit 20
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from animusic.models import Season  # noqa: E402
from animusic.sources.animethemes import AnimeThemesSource  # noqa: E402

CTX = ssl.create_default_context()
# ⚠️ 维基百科**会限流**（实测连发一百多次请求就吃 429），而且它明确要求
# User-Agent 里带联系方式。所以：全局限速 + 429 退避重试。
# 这也是本轮的一个实测发现：**维基比网易云搜索严格得多**
# （网易云连发 20 次全 200；维基连发就 429）。
# ⚠️ UA 与程序本体保持一致（`sources/wikipedia.py:USER_AGENT`）：**不写占位联系方式** ——
# 假地址比不写更糟（真被限流时对方找不到人）。要大量抓取时换成真实地址。
UA = "AniMusicCollector/0.1"
HOST = "ja.wikipedia.org"

#: 维基请求的最小间隔（秒）。1 秒对个人工具足够保守。
WIKI_MIN_INTERVAL = 1.0
_last_call = [0.0]
_stats = {"calls": 0, "429": 0}

# ⚠️ 维基百科正文是 CC BY-SA 授权。真要用这份数据，
# 界面上需要标注来源（本工具只做测量，不落库）。

_REF = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.S)
_TMPL = re.compile(r"\{\{[^{}]*\}\}")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_LINK = re.compile(r"\[\[([^\[\]|]*)\|([^\[\]]*)\]\]")
_LINK1 = re.compile(r"\[\[([^\[\]]+)\]\]")
_TAG = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------- 维基抓取

def _api(params: dict, timeout: float = 30.0, retries: int = 4) -> dict:
    """带全局限速与 429 退避的维基 API 调用。"""
    q = urllib.parse.urlencode(params)
    url = f"https://{HOST}/w/api.php?{q}"
    delay = 4.0
    for attempt in range(retries + 1):
        # 全局限速
        gap = time.monotonic() - _last_call[0]
        if gap < WIKI_MIN_INTERVAL:
            time.sleep(WIKI_MIN_INTERVAL - gap)
        _last_call[0] = time.monotonic()
        _stats["calls"] += 1

        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"__notfound__": True}
            if e.code == 429 and attempt < retries:
                _stats["429"] += 1
                wait = float(e.headers.get("Retry-After") or delay)
                time.sleep(min(wait, 30.0))
                delay *= 1.8
                continue
            raise
    raise RuntimeError("维基 API 重试耗尽")


def wiki_wikitext(title: str) -> str | None:
    try:
        d = _api({"action": "parse", "page": title, "prop": "wikitext",
                  "format": "json", "redirects": 1, "formatversion": 2})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if d.get("__notfound__") or "error" in d:
        return None
    return d.get("parse", {}).get("wikitext")


def wiki_search(query: str, limit: int = 5) -> list[str]:
    try:
        d = _api({"action": "query", "list": "search", "srsearch": query,
                  "srlimit": limit, "format": "json", "formatversion": 2})
    except Exception:  # noqa: BLE001
        return []
    return [h["title"] for h in d.get("query", {}).get("search", [])]


def find_article(names: list[str]) -> tuple[str | None, str]:
    """按候选名找词条，返回 (标题, 怎么找到的)。

    顺序很讲究：**先试 (アニメ) 子词条** —— 动画的主题歌栏通常在子词条里，
    正文常常只有原作信息（实测：薬屋のひとりごと 正文没有主题歌栏，
    但 薬屋のひとりごと (アニメ) 有）。

    请求数要克制（维基会限流）：每个名字最多试 3 个后缀，
    而且**只在正文没找到时才走搜索**，搜到后最多再读 3 个候选。
    """
    for n in names:
        if not n:
            continue
        for suffix in (" (アニメ)", " (テレビアニメ)", ""):
            wt = wiki_wikitext(n + suffix)
            if wt and _theme_section(wt):
                return n + suffix, "exact"

    # 正文/子词条都没有主题歌栏 → 搜一次（不是每个名字都搜）
    #
    # ⚠️ **搜索兜底必须校验词条名**。实测踩到的坑：
    #     「元祖！バンドリちゃん」搜到并接受了「ひみつのアッコちゃん」——
    #     完全无关的另一部动画，主题歌也完全不对。
    # 这比「没找到」更糟：没找到只是缺数据，**找错了会静默写入错误数据**。
    for n in names[:1]:
        if not n:
            continue
        hits = wiki_search(f"{n} アニメ", 5)
        for hit in hits[:3]:
            if not _title_matches(n, hit):
                continue
            wt = wiki_wikitext(hit)
            if wt and _theme_section(wt):
                return hit, "search"
    return None, "none"


def _title_matches(anime_name: str, page_title: str) -> bool:
    """词条名和番剧名是否**真的是同一部**。

    判据要偏保守 —— 宁可漏，不可错（找错了会静默写入错误数据）。

    实测踩过的两个坑，都体现在下面的规范化里：
      · **误收**：「元祖！バンドリちゃん」接受了「ひみつのアッコちゃん」
      · **误杀**：「キャッツ♥アイ (2025)」被拒掉了正确的「キャッツ・アイ」
        —— 因为 `♥` 不在标点表里、年份也没剥

    所以规范化要够狠：只留字母/数字/假名/汉字，并剥掉结尾的年份。
    """
    from animusic.normalize import normalize_text

    def canon(s: str) -> str:
        s = normalize_text(s)
        s = re.sub(r"\s*\((アニメ|テレビアニメ|漫画|小説|曖昧さ回避)\)\s*$", "", s)
        # 只保留字母、数字、假名、汉字 —— 符号（♥・！等）一律丢弃
        s = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", "", s)
        # 剥掉结尾的年份（「...2025」这种，通常是同名新作）
        s = re.sub(r"(19|20)\d{2}$", "", s)
        return s

    a, b = canon(anime_name), canon(page_title)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True

    # 再退一步：用规范化后的串做字符相似度
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio() >= 0.72


# ---------------------------------------------------------------- 解析

def _clean(s: str) -> str:
    s = _COMMENT.sub("", s)
    s = _REF.sub("", s)
    for _ in range(6):                      # 模板可能嵌套
        s2 = _TMPL.sub("", s)
        if s2 == s:
            break
        s = s2
    s = _LINK.sub(r"\2", s)
    s = _LINK1.sub(r"\1", s)
    s = _TAG.sub("", s)
    s = s.replace("'''", "").replace("''", "")
    return " ".join(s.split()).strip()


def _theme_section(wt: str) -> str | None:
    """取出「主題歌」段落（含 主題歌/オープニングテーマ 等等级标题）。"""
    m = re.search(r"^=+\s*(主題歌|オープニングテーマ|エンディングテーマ|テーマソング)"
                  r"[^=\n]*\s*=+\s*$", wt, re.M)
    if not m:
        return None
    start = m.end()
    nxt = re.search(r"^=+[^=\n]+=+\s*$", wt[start:], re.M)
    return wt[start:start + (nxt.start() if nxt else 4000)]


_KIND = [
    (re.compile(r"オープニング|OPテーマ|オープニングテーマ"), "OP"),
    (re.compile(r"エンディング|EDテーマ|エンディングテーマ"), "ED"),
]


def _kind_of(text: str) -> str | None:
    for rx, k in _KIND:
        if rx.search(text):
            return k
    return None


def parse_themes(seg: str) -> list[dict]:
    """解析主题歌段 —— 支持实测见到的两种格式。

    格式 A（逃げ上手の若君）：定义列表
        ; 「プランA」
        : [[DISH//]]による第一期のオープニングテーマ。

    格式 B（薬屋のひとりごと (アニメ)）：wikitable
        |+ オープニングテーマ
        ! 使用話 !! 曲名 !! 歌手 !! 作詞 !! 作曲
        ! 1 - 12
        | [[花になって]] || [[緑黄色社会]] || ...
    """
    out: list[dict] = []

    # ---- 格式 A
    for m in re.finditer(r"^;\s*(.+?)\s*$\n:\s*(.+?)\s*$", seg, re.M):
        head, desc = _clean(m.group(1)), _clean(m.group(2))
        title = _first_quote(head) or head
        kind = _kind_of(desc) or _kind_of(head)
        artist = _artist_from_ja(desc)
        if title and (kind or artist):
            out.append({"title": title, "artist": artist, "kind": kind,
                        "raw": desc[:160], "fmt": "list"})

    # ---- 格式 B
    if "{|" in seg:
        cur_kind = None
        for line in seg.split("\n"):
            s = line.strip()
            if s.startswith("|+"):
                cur_kind = _kind_of(_clean(s[2:])) or cur_kind
                continue
            if not s.startswith("!") and not s.startswith("|"):
                continue
            if s.startswith("!") and "曲名" in s:
                continue
            body = s[1:].strip()
            cells = [_clean(c) for c in re.split(r"\|\||!!", body)]
            cells = [c for c in cells if c]
            if len(cells) >= 2:
                # wikitable 的列序是 使用話|曲名|歌手 → 第二列是曲名，第三列是歌手
                num, title, artist = cells[0], cells[1], (cells[2] if len(cells) > 2 else "")
                if re.fullmatch(r"[\d\s\-–~〜第話]*", num) and title:
                    k = cur_kind or _kind_of("".join(cells))
                    out.append({"title": title, "artist": artist, "kind": k,
                                "raw": " | ".join(cells)[:160], "fmt": "table"})

    # 去重
    seen, uniq = set(), []
    for t in out:
        key = (t["title"], t["artist"], t["kind"])
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    return uniq


def _first_quote(s: str) -> str | None:
    m = re.search(r"[「『\"](.+?)[」』\"]", s)
    return m.group(1).strip() if m else None


def _artist_from_ja(desc: str) -> str:
    """从「[[艺人]]による…テーマ」里取艺人。"""
    m = re.search(r"^(.+?)による", desc)
    if m:
        return m.group(1).strip(" 、,")
    m = re.search(r"^(.+?)[のが]", desc)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="2026Q2")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--sleep", type=float, default=0.3, help="每部番之间额外等待")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    season = Season.parse(args.season)
    at = AnimeThemesSource()
    animes = [a for a in at.fetch_season(season) if a.themes][: args.limit]
    print(f"=== 该季 {len(animes)} 部番（AnimeThemes）===")

    # 用 AniList 补日文原名 —— 搜索维基需要日文名
    native: dict[str, str] = {}
    try:
        from animusic.normalize import name_similarity
        from animusic.sources.anilist import AniListSource

        al = AniListSource().fetch_season(season, max_pages=2)
        for a in animes:
            best, score = None, 0.0
            for x in al:
                s = max(name_similarity(a.name, x.title_romaji),
                        name_similarity(a.name, x.title_english))
                if s > score:
                    best, score = x, s
            if best and score >= 0.7 and best.title_native:
                native[a.name] = best.title_native
    except Exception as e:  # noqa: BLE001
        print(f"  （AniList 补名失败，只能用罗马音搜维基，覆盖率会偏低：{e}）")
    print(f"  拿到日文原名 {len(native)}/{len(animes)}")

    rows = []
    hit_exact = hit_search = miss = 0
    total_themes = 0
    for i, a in enumerate(animes, 1):
        names = [native.get(a.name), a.name]
        title, how = find_article([n for n in names if n])
        n_themes = 0
        sample = []
        if title:
            wt = wiki_wikitext(title) or ""
            seg = _theme_section(wt) or ""
            themes = parse_themes(seg)
            n_themes = len(themes)
            total_themes += n_themes
            sample = themes[:3]
            if how == "exact":
                hit_exact += 1
            else:
                hit_search += 1
        else:
            miss += 1
        at_n = len(a.themes)
        flag = "✓" if n_themes else ("△" if title else "✗")
        print(f"  [{i:>2}/{len(animes)}] {flag} {a.name[:38]:<40} "
              f"AT={at_n} 维基={n_themes}  {title or '（没找到词条）'}")
        for t in sample:
            print(f"          {t['kind'] or '??'}  {t['title'][:32]:<34} / {t['artist'][:24]}")
        rows.append({"anime": a.name, "title_jp": native.get(a.name),
                     "wiki_page": title, "found_via": how,
                     "at_themes": at_n, "wiki_themes": n_themes, "sample": sample})
        time.sleep(args.sleep)

    n = len(animes)
    print()
    print("=" * 74)
    print("  覆盖率统计")
    print("=" * 74)
    print(f"  有日文原名           {len(native)}/{n}")
    print(f"  找到词条且有主题歌栏  {hit_exact + hit_search}/{n}  "
          f"（精确 {hit_exact} · 搜索 {hit_search}）")
    print(f"  没找到               {miss}/{n}")
    print(f"  解析出的主题歌条目    {total_themes} 条")
    print(f"  AnimeThemes 同批      {sum(len(a.themes) for a in animes)} 条")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(f"\n  明细：{args.json}")
    print(f"  维基 API 调用 {_stats['calls']} 次，其中 429 退避 {_stats['429']} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
