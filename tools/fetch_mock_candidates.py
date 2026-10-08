"""抓取真实网易云候选，生成 UI 演示 fixture（直连 API，不需要客户端）。

背景（实测结论）：
    `https://music.163.com/api/search/get` 从 Python 直连即可用，
    且**返回 alias 字段**（"TV动画《鬼灭之刃》片头曲"）—— 那正是打分器最依赖的信号。

    它比驱动客户端 CEF 快得多，也不需要窗口可见。
    唯一缺的是 VIP/MV/原唱 这些 tag（可由 fee / mvid / 艺人匹配推断）。

    完整对比见 DESIGN.md 的 Collector 一节。

用法：
    python tools/fetch_mock_candidates.py
"""

from __future__ import annotations

import io
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(WS, "data", "mock", "netease_candidates.json")
CTX = ssl.create_default_context()
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Accept": "application/json, text/plain, */*",
}

# (番剧, 类型, 搜索词, 期望命中的网易云 song_id)—— 期望值仅用于演示打分，不作断言
QUERIES = [
    ("冰之城墙 第二季", "OP", "Escort THE BEAT GARDEN", None),
    ("冰之城墙 第二季", "ED", "Hanakage Superfly", None),
    ("FX战士久留美", "OP", "FX Senshi Kurumi-chan Aina Suzuki", None),
    ("FX战士久留美", "ED", "Aina Suzuki 久留美", None),
    ("名侦探光之美少女！", "OP", "名探偵プリキュア", None),
    ("鬼灭之刃", "OP", "紅蓮華 LiSA", 1375305989),  # 多版本对照演示
]


def search(keyword: str, limit: int = 10) -> list[dict]:
    url = (
        "https://music.163.com/api/search/get"
        f"?s={urllib.parse.quote(keyword)}&type=1&offset=0&total=true&limit={limit}"
    )
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
        data = json.loads(r.read())
    if data.get("code") != 200:
        raise RuntimeError(f"code={data.get('code')}")
    songs = (data.get("result") or {}).get("songs") or []
    out = []
    for s in songs:
        album = s.get("album") or {}
        out.append(
            {
                "netease_song_id": str(s.get("id")),
                "name": s.get("name"),
                "artists": [a.get("name") for a in (s.get("artists") or [])],
                "album": album.get("name") or "",
                "cover": album.get("picUrl") or "",
                "duration_ms": s.get("duration"),
                "fee": s.get("fee"),
                "mvid": s.get("mvid") or 0,
                "alias": s.get("alias") or [],
                # 直连 API 不返回 VIP/MV/原唱 tag，这里按 fee/mvid 推断（演示用）
                "tags": [],
                "transNames": s.get("transNames") or [],
            }
        )
    return out


def infer_tags(c: dict) -> list[str]:
    tags = []
    if c.get("fee") in (1, 4, 8):
        tags.append("VIP")
    if c.get("mvid"):
        tags.append("MV")
    return tags


def main() -> int:
    groups = []
    for anime, kind, query, expect in QUERIES:
        print(f"搜索「{query}」 ... ", end="")
        try:
            cands = search(query)
        except Exception as e:
            print(f"失败: {type(e).__name__}: {e}")
            continue
        for c in cands:
            c["tags"] = infer_tags(c)
        print(f"{len(cands)} 条")
        groups.append(
            {
                "anime": anime,
                "kind": kind,
                "query": query,
                "expected_song_id": str(expect) if expect else None,
                "candidates": cands,
            }
        )

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)
    print(f"\n已保存 {len(groups)} 组到 {OUT}\n")

    for g in groups:
        print(f"【{g['anime']} {g['kind']}】  query={g['query']!r}")
        for c in g["candidates"][:5]:
            alias = (c["alias"] or [""])[0]
            print(
                f"   {c['netease_song_id']:<12} {str(c['name'])[:32]:<34}"
                f"{'、'.join(c['artists'])[:18]:<20} {(c['duration_ms'] or 0) // 1000}s  "
                f"[{','.join(c['tags'])}]  {alias}"
            )
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
