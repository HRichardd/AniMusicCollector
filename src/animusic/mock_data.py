"""UI 原型的测试数据。

**数据全部来自实测，不是编造的**：

  番单  → 真实 Bangumi「在看」列表（实测于 2026-10；UID 已匿名，这里写 <UID>）
          https://api.bgm.tv/v0/users/<UID>/collections?subject_type=2&type=3
  曲目  → 真实 AnimeThemes 2026 Fall 数据（38 部）
  候选  → 真实网易云搜索结果（music.163.com/api/search/get）

数据里**刻意保留了三种真实情况**，用来演示三态缺失设计（§5.12）：
  · 正常命中         冰之城墙 ED「花蔭 / Superfly」
  · 网易云未上架     FX战士久留美 OP（搜不到）
  · 数据源未收录     名侦探光之美少女（年番，AnimeThemes 未标 Q4）
  · 多版本 + TV size 光之美少女 OP（含 90s 的 TV サイズ 版）
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import scorer
from .scorer import Target

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_MOCK_DIR = os.path.join(_ROOT, "data", "mock")


# ------------------------------------------------------------------ 真实番单
# 来自实测的 Bangumi「在看」（5 条），外加 1 条手工新增用于演示「手动补充」
ANIME = [
    {
        "bgm_id": "665390", "title_cn": "冰之城墙 第二季", "title_jp": "氷の城壁 第2期",
        "air_date": "2026-10-01", "bucket": "target", "origin": "bangumi",
        "note": "", "included": True,
    },
    {
        "bgm_id": "622288", "title_cn": "FX战士久留美", "title_jp": "FX戦士くるみちゃん",
        "air_date": "2026-10-01", "bucket": "target", "origin": "bangumi",
        "note": "", "included": True,
    },
    {
        "bgm_id": "511177", "title_cn": "遭到流放的转生重骑士凭借游戏知识大开无双",
        "title_jp": "追放された転生重騎士はゲーム知識で無双する",
        "air_date": "2026-07-02", "bucket": "cross_season", "origin": "bangumi",
        "note": "7 月开播，仍在「在看」", "included": True,
    },
    {
        "bgm_id": "580133", "title_cn": "欺诈游戏", "title_jp": "LIAR GAME",
        "air_date": "2026-04-06", "bucket": "other", "origin": "bangumi",
        "note": "已完结，未标「看过」", "included": False,
    },
    {
        "bgm_id": "611077", "title_cn": "名侦探光之美少女！", "title_jp": "名探偵プリキュア！",
        "air_date": "2026-02-01", "bucket": "cross_season", "origin": "bangumi",
        "note": "年番，全年在播，OP 随季度更换", "included": True,
    },
    {
        "bgm_id": "", "title_cn": "鬼灭之刃", "title_jp": "鬼滅の刃",
        "air_date": "2019-04-06", "bucket": "other", "origin": "manual",
        "note": "演示：手动新增的番（用于展示多版本候选面板）", "included": True,
    },
]

# 中文名 → AnimeThemes 里的番剧名（mock 用具，真实项目靠标题匹配 + 别名表）
_AT_MAP = {
    "冰之城墙 第二季": "Koori no Jouheki 2nd Season",
    "FX战士久留美": "FX Senshi Kurumi-chan",
    # 其余在 Fall 2026 里没有 —— 正好演示「数据源未收录」
}

# 手工补充的曲目（原番不在 AnimeThemes，或数据源还没收录）
MANUAL_THEMES = [
    {
        "anime": "鬼灭之刃", "kind": "OP", "seq": 1,
        "song_title": "紅蓮華", "artists": ["LiSA"],
        "origin": "animethemes", "included": True, "note": "多版本候选演示",
    },
    {
        "anime": "鬼灭之刃", "kind": "ED", "seq": 1,
        "song_title": "from the edge", "artists": ["FictionJunction", "LiSA"],
        "origin": "animethemes", "included": True, "note": "",
    },
    {
        "anime": "名侦探光之美少女！", "kind": "OP", "seq": 1,
        "song_title": "いつかわかる☆きっとあえる",
        "artists": ["熊田茜音", "増井優花"],
        "origin": "manual", "included": True,
        "note": "数据源未收录，手动补充（网易云有 TVサイズ 版，注意区分）",
    },
    {
        "anime": "名侦探光之美少女！", "kind": "ED", "seq": 1,
        "song_title": "バイバイライアー", "artists": ["名探偵プリキュア！"],
        "origin": "manual", "included": True, "note": "手动补充",
    },
]

# 无候选（模拟未上架 / 搜不到）
_PUSH_RESULTS: dict[str, tuple[str, str]] = {}   # 由 build() 填充


def _load(name: str) -> Any:
    path = os.path.join(_MOCK_DIR, name)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _bucket_label(b: str) -> str:
    return {"target": "当季", "cross_season": "跨季", "other": "其他", "unknown": "未知"}.get(b, b)


# ------------------------------------------------------------------ build

def build() -> dict[str, list[dict]]:
    """组装四个步骤的表格数据。"""
    animethemes = {a["name"]: a for a in _load("animethemes_2026fall.json")}
    cand_groups = _load("netease_candidates.json")
    by_key = {(g["anime"], g["kind"]): g for g in cand_groups}

    # ---------------- 步骤① 番单
    anime_rows = []
    for a in ANIME:
        row = dict(a)
        row["bucket_label"] = _bucket_label(a["bucket"])
        row["origin_label"] = "手工" if a["origin"] == "manual" else "Bangumi"
        anime_rows.append(row)

    included = {r["title_cn"] for r in anime_rows if r["included"]}

    # ---------------- 步骤② 曲目
    theme_rows: list[dict] = []
    missing_source: list[str] = []

    for cn in [r["title_cn"] for r in anime_rows if r["included"]]:
        at_name = _AT_MAP.get(cn)
        entry = animethemes.get(at_name) if at_name else None
        if not entry:
            missing_source.append(cn)
            # 已经有手工补充的曲目就不再加占位行
            if not any(m["anime"] == cn for m in MANUAL_THEMES):
                theme_rows.append(
                    {
                        "anime": cn, "kind": "OP", "seq": 1,
                        "song_title": "", "artists": [],
                        "origin": "manual", "origin_label": "待补",
                        "included": True,
                        "note": "数据源未收录 —— 请手动填写曲名与艺人",
                    }
                )
            continue
        for t in entry["themes"]:
            theme_rows.append(
                {
                    "anime": cn, "kind": t["kind"], "seq": t.get("sequence") or 1,
                    "song_title": t["song_title"], "artists": t["artists"],
                    "origin": "animethemes", "origin_label": "数据源",
                    "included": True, "note": "",
                }
            )

    for m in MANUAL_THEMES:
        if m["anime"] not in included:
            continue
        theme_rows.append(
            {
                "anime": m["anime"], "kind": m["kind"], "seq": m["seq"],
                "song_title": m["song_title"], "artists": m["artists"],
                "origin": m["origin"],
                "origin_label": "手工" if m["origin"] == "manual" else "数据源",
                "included": m["included"], "note": m["note"],
            }
        )

    # 排序：番剧 → 类型 → 序号
    theme_rows.sort(key=lambda r: (r["anime"], r["kind"], r["seq"]))

    # ---------------- 步骤③ 匹配
    match_rows: list[dict] = []
    push_rows: list[dict] = []

    for th in theme_rows:
        if not th["included"]:
            continue
        cn = th["anime"]
        anime_jp = next((r["title_jp"] for r in anime_rows if r["title_cn"] == cn), "")
        target = Target(
            song_title=th["song_title"],
            artists=th["artists"],
            anime_names=[cn, anime_jp],
            kind=th["kind"],
        )

        group = by_key.get((cn, th["kind"]))
        scored: list[dict] = []
        if group:
            for i, c in enumerate(group["candidates"]):
                s = scorer.score({**c, "rank": i}, target)
                scored.append({**c, "rank": i, "score": s.score, "detail": s.detail})
            scored.sort(key=lambda x: -x["score"])

        if scored:
            best = scored[0]
            status = scorer.classify(best["score"])
            chosen = best
        elif not th["song_title"]:
            # 曲名都没有 → 是数据源未收录的占位行
            status = "missing_source"
            chosen = None
        else:
            # 有曲目但搜不到候选 → 网易云未上架
            status = "missing_netease"
            chosen = None

        status_label = {
            "auto": "🟢 自动", "pending_review": "🟡 待审核", "low": "🔴 低分",
            "missing_netease": "⬜ 未上架", "missing_source": "⬜ 未收录",
        }[status]

        match_rows.append(
            {
                "anime": cn, "kind": th["kind"], "seq": th["seq"],
                "theme_title": th["song_title"], "theme_artists": "、".join(th["artists"]),
                "chosen_name": chosen["name"] if chosen else "—",
                "chosen_artists": "、".join(chosen["artists"]) if chosen else "—",
                "chosen_id": chosen["netease_song_id"] if chosen else "",
                "chosen_duration": chosen["duration_ms"] if chosen else None,
                "score": chosen["score"] if chosen else 0.0,
                "status": status, "status_label": status_label,
                "candidates": scored[:8],
                "note": th.get("note", ""),
                "manual": False,
            }
        )

        if chosen and status in ("auto", "pending_review"):
            push_rows.append(
                {
                    "included": True,
                    "name": chosen["name"],
                    "artists": "、".join(chosen["artists"]),
                    "netease_song_id": chosen["netease_song_id"],
                    "playlist": "26年10月",
                    "status": "待推送",
                    "anime": cn, "kind": th["kind"],
                }
            )

    match_rows.sort(key=lambda r: (r["anime"], r["kind"], r["seq"]))
    push_rows.sort(key=lambda r: (r["anime"], r["kind"]))

    return {
        "anime": anime_rows,
        "theme": theme_rows,
        "match": match_rows,
        "push": push_rows,
        "missing_source": missing_source,
    }


def stats(data: dict[str, list[dict]]) -> dict[str, int]:
    m = data["match"]
    return {
        "anime": len([r for r in data["anime"] if r["included"]]),
        "theme": len([r for r in data["theme"] if r["included"]]),
        "match_total": len(m),
        "auto": len([r for r in m if r["status"] == "auto"]),
        "review": len([r for r in m if r["status"] == "pending_review"]),
        "missing": len([r for r in m if r["status"].startswith("missing")]),
        "push": len(data["push"]),
    }
