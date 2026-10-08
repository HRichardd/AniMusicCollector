"""测量匹配准确率 —— M1 的关键交付物。

为什么这样测：
    正常流程的 theme 来自「Bangumi 在看」，但那需要浏览器在跑。
    而**要测的是匹配算法本身的质量**，输入集合用什么其实无所谓。
    所以这里直接用 AnimeThemes 当季全量（38 部 × ~2 条 = 76 条 theme）
    构造输入 —— 样本更多，结论更可靠，而且不依赖浏览器。

    因为目标是「AnimeThemes 的曲目 → 网易云的正确歌曲」，
    AnimeThemes 侧的曲名/艺人是**真值**，所以能客观判断匹配对不对。

用法：
    python tools/measure_accuracy.py --season 2026Q4            # 全量
    python tools/measure_accuracy.py --season 2026Q4 --limit 12 # 先跑小样本
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "vendor"))

# ⚠️ 必须是**幂等**的：无条件包一层 TextIOWrapper 时，
# 如果别的模块（或本模块被 import 两次）已经包过，
# 旧 wrapper 会被 GC 掉并**关闭底层 buffer**，接着就 "I/O operation on closed file"。
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from animusic import constants, scorer  # noqa: E402
from animusic.collector.api_search import ApiSearchCollector  # noqa: E402
from animusic.models import (  # noqa: E402
    Anime,
    Origin,
    Season,
    SeasonBucket,
    Theme,
)
from animusic.orchestrator import Orchestrator  # noqa: E402
from animusic.sources.animethemes import AnimeThemesSource  # noqa: E402
from animusic.store import Store  # noqa: E402


def seed(store: Store, sid: int, season: Season, limit: int | None, verbose: bool) -> int:
    """用 AnimeThemes 当季数据构造输入。

    ⚠️ **测量偏差修正**：真实流程里番剧名来自 Bangumi，带中日文名，
    因此 `{番剧中文名} 片头曲` 这类查询是有效的。
    只塞罗马音名会让测量结果**显著偏低**。

    所以这里用 AniList 补上 native（日文）标题 —— AniList 同时给
    romaji 与 native，正好可以和 AnimeThemes 的罗马音名对上。
    """
    at = AnimeThemesSource()
    animes = [a for a in at.fetch_season(season) if a.themes]

    # 用 AniList 补日文名
    native_by_romaji: dict[str, str] = {}
    try:
        from animusic.normalize import name_similarity
        from animusic.sources.anilist import AniListSource

        al = AniListSource().fetch_season(season, max_pages=2)
        for a in animes:
            best, score = None, 0.0
            for x in al:
                s = max(
                    name_similarity(a.name, x.title_romaji),
                    name_similarity(a.name, x.title_english),
                )
                if s > score:
                    best, score = x, s
            if best and score >= 0.7 and best.title_native:
                native_by_romaji[a.name] = best.title_native
        if verbose:
            print(f"    AniList 补名：{len(native_by_romaji)}/{len(animes)} 部拿到日文名")
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"    AniList 补名失败（不影响主流程）：{e}")

    if limit:
        animes = animes[:limit]

    n = 0
    for a in animes:
        anime_id = store.upsert_anime(
            sid,
            Anime(
                season_key=season.key,
                title_romaji=a.name,
                title_jp=native_by_romaji.get(a.name) or None,
                animethemes_slug=a.slug,
                season_bucket=SeasonBucket.TARGET,
                origin=Origin.ANIMETHEMES,
                included=True,
                note="accuracy-test seed",
            ),
        )
        for t in a.themes:
            if not t.song_title:
                continue
            store.upsert_theme(
                anime_id,
                Theme(
                    anime_id=anime_id,
                    kind=t.kind,
                    song_title=t.song_title,
                    artists=t.artists,
                    sequence=t.sequence,
                    source="animethemes",
                    origin=Origin.ANIMETHEMES,
                    included=True,
                ),
            )
            n += 1
        if verbose:
            jp = native_by_romaji.get(a.name) or "—"
            print(f"    seed {a.name[:42]:<44} jp={jp[:18]:<20} {len(a.themes)} themes")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="2026Q4")
    ap.add_argument("--limit", type=int, default=None, help="只取前 N 部番（小样本快跑）")
    ap.add_argument("--db", default=None, help="DB 路径，默认 data/accuracy_test.db")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    season = Season.parse(args.season)
    db_path = args.db or os.path.join(constants.DATA_DIR, "accuracy_test.db")
    if os.path.exists(db_path):
        os.remove(db_path)
        for ext in ("-wal", "-shm"):
            p = db_path + ext
            if os.path.exists(p):
                os.remove(p)

    store = Store(db_path)
    sid = store.get_season_id(season.year, season.quarter)

    print(f"=== 构造输入：AnimeThemes {season.key} ===")
    t0 = time.time()
    n_themes = seed(store, sid, season, args.limit, not args.quiet)
    print(f"  {n_themes} 条 theme，耗时 {time.time() - t0:.1f}s")
    print()

    print("=== 匹配（真实搜索 + 打分）===")
    search = ApiSearchCollector()

    def prog(msg: str) -> None:
        if not args.quiet:
            print(f"  {msg}")

    orch = Orchestrator(store=store, search=search, progress=prog)
    t0 = time.time()
    rep = orch.match_season(season)
    elapsed = time.time() - t0

    print()
    print("=" * 68)
    print("  匹配结果")
    print("=" * 68)
    print(f"  theme 总数            {rep.themes_total}")
    print(f"  参与匹配              {rep.searched}")
    print(f"  搜索次数              {rep.queries_used}")
    print(f"  耗时                  {elapsed:.0f}s")
    print()
    print(f"  🟢 自动通过           {rep.auto:>4}   {rep.auto / max(1, rep.searched):>6.1%}")
    print(f"  🟡 待人工审核         {rep.pending_review:>4}   {rep.pending_review / max(1, rep.searched):>6.1%}")
    print(f"  ⬜ 数据源未收录       {rep.missing_source:>4}")
    print(f"  ⬜ 网易云未上架       {rep.missing_netease:>4}")
    print(f"  🔴 匹配不上           {rep.missing_match:>4}   {rep.missing_match / max(1, rep.searched):>6.1%}")
    print()
    print(f"  ★ 自动通过率          {rep.auto_rate:>6.1%}")
    print(f"  ★ 可用率(自动+待审)   {rep.usable_rate:>6.1%}")
    print()

    # 分档明细
    print("=" * 68)
    print("  分数分布")
    print("=" * 68)
    buckets = [(0.9, 1.01), (0.85, 0.9), (0.7, 0.85), (0.55, 0.7), (0.3, 0.55), (0.0, 0.3)]
    scores = [d.get("score", 0) for d in rep.details]
    for lo, hi in buckets:
        n = len([s for s in scores if lo <= s < hi])
        bar = "█" * min(40, n)
        label = f"{lo:.2f}-{hi:.2f}" if hi <= 1 else f"{lo:.2f}-1.00"
        print(f"  {label:>10}  {n:>4}  {bar}")
    print()

    # 抽样对照
    print("=" * 68)
    print("  抽样对照（前 20 条）")
    print("=" * 68)
    for d in rep.details[:20]:
        st = d.get("status", "")
        mark = {"auto": "🟢", "pending_review": "🟡", "confirmed": "🔵"}.get(st, "🔴")
        det = d.get("detail") or {}
        print(
            f"  {mark} {d.get('score', 0):.2f}  {str(d.get('title'))[:34]:<36}"
            f"-> {str(d.get('chosen') or '—')[:30]:<32}"
            f"曲{det.get('title', 0):.2f} 人{det.get('artist', 0):.2f} "
            f"番{det.get('anime_alias', 0):.2f} 时{det.get('duration', 0):.2f}"
        )

    paths = orch.export(season)
    print()
    print("报告已写出：")
    for k, v in paths.items():
        print(f"  {k:<8} {v}")

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
