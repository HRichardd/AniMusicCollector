r"""量化别名自学习的收益。

做法（**不重新搜索**，直接对已有候选重新打分，所以对比是干净的）：

    1. 用 AnimeThemes 当季真实数据构造输入，跑一次真实匹配
    2. 找出「算法其实已经找对了、只是分数不够进自动区」的条目 ——
       判据是**艺人对得上且曲名也像**（`artist >= 0.8 and title >= 0.6`）
    3. 把这一步模拟成「用户在向导里确认了它们」，据此学别名
    4. 用同一批候选、同一个打分器，只多一张别名表，重新打分
    5. 对比前后分布

**诚实声明**：第 3 步是**模拟**，不是真人操作。判据是我定的启发式，
所以这个数字应当读作「如果用户确认了这些，下一轮会怎样」，
而不是「用户实际会确认多少」。

用法：
    $env:PYTHONPATH = "<工作区>\src;<工作区>\vendor"
    python tools/measure_learning.py --season 2025Q2 --limit 12
"""

from __future__ import annotations

import argparse
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

# 幂等处理 Windows 控制台的 GBK 问题（见 measure_accuracy.py 里的说明）
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

# 「算法已经找对了、只是分不够」的判据
CONFIRMABLE_ARTIST = 0.8
CONFIRMABLE_TITLE = 0.6


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="2025Q2")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    season = Season.parse(args.season)
    db = args.db or os.path.join(constants.DATA_DIR, "learning_test.db")
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(db + ext):
            os.remove(db + ext)

    store = Store(db)
    sid = store.get_season_id(season.year, season.quarter)

    print(f"=== 1. 构造输入并跑真实匹配（{season.key}）===")
    n = seed(store, sid, season, args.limit, verbose=False)
    print(f"  {n} 条 theme")

    orch = Orchestrator(store=store, search=ApiSearchCollector(),
                        progress=lambda m: None, aliases=AliasTable())
    orch.match_season(season)

    # ---- 2. 取每条的最佳候选，看它「是不是其实已经对了」
    rows = []
    decisions = store.decision_map(sid)
    for t in store.theme_list(sid):
        cands = store.candidate_list(t["id"])
        if not cands:
            rows.append((t, None, None, None))
            continue
        best = cands[0]
        import json

        detail = json.loads(best["score_detail"] or "{}")
        rows.append((t, best, detail, decisions.get(t["id"])))

    def bucket(sc: float) -> str:
        return scorer.classify(sc) if sc is not None else "none"

    before = {"auto": 0, "pending_review": 0, "low": 0, "none": 0}
    for _t, best, _d, _dec in rows:
        before[bucket(best["score"] if best else None)] += 1

    # ---- 3. 模拟用户确认
    table = AliasTable()
    confirmable = []
    for t, best, detail, _dec in rows:
        if best is None or not detail:
            continue
        if (detail.get("artist", 0) >= CONFIRMABLE_ARTIST
                and detail.get("title", 0) >= CONFIRMABLE_TITLE):
            confirmable.append((t, best))
            table.learn_title(t["song_title"], best["name"])
            table.learn_artists(artists_from_json(t["artists"]),
                                artists_from_json(best["artists"]))
            table.learn_anime([x for x in (t["anime_cn"], t["anime_jp"]) if x],
                              best["anime_alias"])

    print(f"\n=== 2. 按阈值推定可确认的条目（未经人工核验） ===")
    print(f"  判据: artist >= {CONFIRMABLE_ARTIST} 且 title >= {CONFIRMABLE_TITLE}")
    print(f"  命中 {len(confirmable)} / {len([r for r in rows if r[1]])} 条有候选的")
    print(f"  学到别名: {table.stats()}")

    # ---- 4. 同一批候选重新打分（只多一张别名表）
    after = {"auto": 0, "pending_review": 0, "low": 0, "none": 0}
    changed = []
    for t, best, detail, _dec in rows:
        if best is None:
            after["none"] += 1
            continue
        artists = artists_from_json(t["artists"])
        target = scorer.Target(
            song_title=t["song_title"], artists=artists,
            anime_names=[x for x in (t["anime_cn"], t["anime_jp"]) if x],
            kind=t["kind"], aliases=table,
        )
        import json as _json

        s = scorer.score({
            "name": best["name"], "artists": artists_from_json(best["artists"]),
            "alias": [best["anime_alias"]] if best["anime_alias"] else [],
            "duration_ms": best["duration_ms"],
            "tags": _json.loads(best["tags"] or "[]"),
            "search_rank": best["search_rank"],
        }, target)
        after[bucket(s.score)] += 1
        if bucket(s.score) != bucket(best["score"]):
            changed.append((t["song_title"], best["name"], best["score"], s.score))

    # ---- 5. 对比
    print(f"\n{'=' * 62}")
    print("  分布对比（同一批候选，只多一张别名表）")
    print("=" * 62)
    print(f"  {'档位':<18}{'学习前':>8}{'学习后':>8}{'变化':>8}")
    total = sum(before.values())
    for k, label in (("auto", "🟢 自动通过"), ("pending_review", "🟡 待人工审核"),
                     ("low", "🔴 匹配不上"), ("none", "⬜ 无候选")):
        d = after[k] - before[k]
        mark = f"{d:+d}" if d else "—"
        print(f"  {label:<18}{before[k]:>8}{after[k]:>8}{mark:>8}")
    print(f"  {'合计':<18}{total:>8}{sum(after.values()):>8}")

    a0 = before["auto"] / max(1, total)
    a1 = after["auto"] / max(1, total)
    print(f"\n  ★ 自动通过率  {a0:.1%}  →  {a1:.1%}")

    u0 = (before["auto"] + before["pending_review"]) / max(1, total)
    u1 = (after["auto"] + after["pending_review"]) / max(1, total)
    print(f"  ★ 可用率      {u0:.1%}  →  {u1:.1%}")

    if changed:
        print(f"\n  档位发生变化的 {len(changed)} 条（前 12）：")
        for title, chosen, s0, s1 in changed[:12]:
            print(f"    {title[:30]:<32} {chosen[:22]:<24} {s0:.2f} → {s1:.2f}")

    # 落一份别名表供检视
    out_yaml = os.path.join(constants.DATA_DIR, "learned_aliases_demo.yaml")
    table.path = out_yaml
    table.save()
    print(f"\n  学到的别名表已写出：{out_yaml}")

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
