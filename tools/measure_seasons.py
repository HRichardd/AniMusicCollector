r"""跨多个季度测量匹配准确率 —— 让 M1 的关键数字从「单季小样本」变成可靠基线。

为什么需要：
    之前只测了 2025Q2 的 12 部番（28 条 theme）。样本太小，
    既可能是「这一季恰好难」，也可能掩盖只在别的季度才出现的问题。

做法：对多个**已完结且歌曲已上架**的季度各跑一遍完整匹配，
再汇总。顺带把别名自学习的收益也按季度算出来。

用法：
    $env:PYTHONPATH = "<工作区>\src;<工作区>\vendor"
    python tools/measure_seasons.py                        # 默认 3 个季度
    python tools/measure_seasons.py --seasons 2025Q4,2026Q1 --limit 20
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from animusic import constants, scorer  # noqa: E402
from animusic.collector.api_search import ApiSearchCollector  # noqa: E402
from animusic.models import Season, artists_from_json  # noqa: E402
from animusic.normalize import AliasTable  # noqa: E402
from animusic.orchestrator import Orchestrator  # noqa: E402
from animusic.store import Store  # noqa: E402

sys.path.insert(0, _HERE)
from measure_accuracy import seed  # noqa: E402

# 「算法其实已经找对了、只是分不够」的判据（同 measure_learning）
CONFIRMABLE_ARTIST = 0.8
CONFIRMABLE_TITLE = 0.6

DEFAULT_SEASONS = ["2025Q4", "2026Q1", "2026Q2"]


def measure_one(season_key: str, limit: int | None, db_dir: str) -> dict:
    """跑一个季度，返回统计。异常不抛出（单季失败不该毁掉整轮）。"""
    season = Season.parse(season_key)
    db = os.path.join(db_dir, f"measure_{season_key}.db")
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(db + ext):
            os.remove(db + ext)

    t0 = time.time()
    out: dict = {"season": season_key}
    try:
        store = Store(db)
        sid = store.get_season_id(season.year, season.quarter)
        n = seed(store, sid, season, limit, verbose=False)
        out["themes"] = n
        if not n:
            out["error"] = "AnimeThemes 没有该季数据"
            store.close()
            return out

        orch = Orchestrator(store=store, search=ApiSearchCollector(),
                            progress=lambda m: None, aliases=AliasTable())
        rep = orch.match_season(season)

        # 逐条取最佳候选，记录分项，供学习模拟
        rows = []
        for t in store.theme_list(sid):
            cands = store.candidate_list(t["id"])
            best = cands[0] if cands else None
            detail = json.loads(best["score_detail"] or "{}") if best else {}
            rows.append((t, best, detail))

        out.update({
            "searched": rep.searched,
            "queries": rep.queries_used,
            "auto": rep.auto,
            "review": rep.pending_review,
            "miss_src": rep.missing_source,
            "miss_net": rep.missing_netease,
            "miss_match": rep.missing_match,
            "no_candidate": len([1 for _t, b, _d in rows if b is None]),
        })

        # ---- 模拟「用户确认了算法已找对但分不够的那些」，再学一遍
        table = AliasTable()
        confirmable = 0
        for t, best, detail in rows:
            if best is None or not detail:
                continue
            if (detail.get("artist", 0) >= CONFIRMABLE_ARTIST
                    and detail.get("title", 0) >= CONFIRMABLE_TITLE):
                confirmable += 1
                table.learn_title(t["song_title"], best["name"])
                table.learn_artists(artists_from_json(t["artists"]),
                                    artists_from_json(best["artists"]))
                table.learn_anime([x for x in (t["anime_cn"], t["anime_jp"]) if x],
                                  best["anime_alias"])
        out["confirmable"] = confirmable

        after = {"auto": 0, "review": 0, "low": 0, "none": 0}
        for t, best, _d in rows:
            if best is None:
                after["none"] += 1
                continue
            target = scorer.Target(
                song_title=t["song_title"], artists=artists_from_json(t["artists"]),
                anime_names=[x for x in (t["anime_cn"], t["anime_jp"]) if x],
                kind=t["kind"], aliases=table,
            )
            s = scorer.score({
                "name": best["name"], "artists": artists_from_json(best["artists"]),
                "alias": [best["anime_alias"]] if best["anime_alias"] else [],
                "duration_ms": best["duration_ms"],
                "tags": json.loads(best["tags"] or "[]"),
                "search_rank": best["search_rank"],
            }, target)
            cls = scorer.classify(s.score)
            after["auto" if cls == "auto" else
                  "review" if cls == "pending_review" else "low"] += 1
        out["after"] = after

        store.close()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"

    out["seconds"] = round(time.time() - t0, 1)
    return out


def pct(n: int, d: int) -> str:
    return f"{n / d:.1%}" if d else "—"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default=",".join(DEFAULT_SEASONS))
    ap.add_argument("--limit", type=int, default=None, help="每季只取前 N 部番")
    ap.add_argument("--db-dir", default=None)
    args = ap.parse_args()

    seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]
    db_dir = args.db_dir or os.path.join(constants.DATA_DIR, "_measure")
    os.makedirs(db_dir, exist_ok=True)

    results = []
    for sk in seasons:
        print(f"\n{'=' * 72}")
        print(f"  测量 {sk} …")
        print("=" * 72)
        r = measure_one(sk, args.limit, db_dir)
        results.append(r)
        if r.get("error"):
            print(f"  ✗ {r['error']}")
        else:
            print(f"  {r['themes']} 条 theme · {r['queries']} 次搜索 · {r['seconds']}s")
            print(f"  🟢 {r['auto']}  🟡 {r['review']}  ⬜未上架 {r['miss_net']}  "
                  f"🔴 {r['miss_match']}  无候选 {r['no_candidate']}")

    # ---------------- 汇总
    ok = [r for r in results if not r.get("error")]
    print(f"\n{'=' * 72}")
    print("  汇总")
    print("=" * 72)
    if not ok:
        print("  没有任何季度测成功")
        return 1

    print(f"  {'季度':<10}{'theme':>7}{'🟢自动':>8}{'🟡待审':>8}{'⬜未上架':>9}"
          f"{'🔴匹配不上':>11}{'★可用率':>10}")
    print("  " + "-" * 62)

    tot = {"themes": 0, "auto": 0, "review": 0, "miss_net": 0, "miss_match": 0,
           "miss_src": 0, "after_auto": 0, "after_review": 0}
    for r in ok:
        n = r["themes"]
        usable = r["auto"] + r["review"]
        print(f"  {r['season']:<10}{n:>7}{r['auto']:>8}{r['review']:>8}"
              f"{r['miss_net']:>9}{r['miss_match']:>11}{pct(usable, n):>10}")
        tot["themes"] += n
        tot["auto"] += r["auto"]
        tot["review"] += r["review"]
        tot["miss_net"] += r["miss_net"]
        tot["miss_match"] += r["miss_match"]
        tot["miss_src"] += r.get("miss_src", 0)
        tot["after_auto"] += r["after"]["auto"]
        tot["after_review"] += r["after"]["review"]

    print("  " + "-" * 62)
    n = tot["themes"]
    print(f"  {'合计':<10}{n:>7}{tot['auto']:>8}{tot['review']:>8}"
          f"{tot['miss_net']:>9}{tot['miss_match']:>11}"
          f"{pct(tot['auto'] + tot['review'], n):>10}")

    print(f"\n  ★ 自动通过率（学习前）  {pct(tot['auto'], n)}")
    print(f"  ★ 可用率（学习前）      {pct(tot['auto'] + tot['review'], n)}")
    print(f"  ★ 自动通过率（学习后）  {pct(tot['after_auto'], n)}")
    print(f"  ★ 可用率（学习后）      "
          f"{pct(tot['after_auto'] + tot['after_review'], n)}")

    print(f"\n  样本规模：{len(ok)} 个季度 / {n} 条 theme")
    print("  （对比：之前只用 2025Q2 的 28 条，样本偏小）")

    fails = [r for r in results if r.get("error")]
    if fails:
        print(f"\n  ⚠️ {len(fails)} 个季度失败：")
        for r in fails:
            print(f"    {r['season']}: {r['error']}")

    # 落盘供后续引用
    out = os.path.join(constants.DATA_DIR, "measure_seasons.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"results": results, "total": tot}, f, ensure_ascii=False, indent=2)
    print(f"\n  明细已写出：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
