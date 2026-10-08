"""抓取真实数据，生成 UI 演示用的 fixture。

UI mock 不编造数据，而是用真实拉取结果：
  - 番单  : 实测的 Bangumi「在看」列表（UID 已匿名，2026-10 时点）
  - 曲目  : AnimeThemes 真实的 2026 Fall 曲目数据
  - 匹配  : 实测抓到的网易云候选（含紅蓮華的多版本对照）

用法：
    python tools/fetch_mock_data.py
"""

from __future__ import annotations

import io
import json
import os
import ssl
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

UA = {"User-Agent": "AniMusicCollector/0.1"}
CTX = ssl.create_default_context()
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(WS, "data", "mock")


def get(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        return json.loads(r.read())


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)

    print("抓取 AnimeThemes 2026 Fall ...")
    url = (
        "https://api.animethemes.moe/anime"
        "?filter%5Byear%5D=2026&filter%5Bseason%5D=Fall"
        "&include=animethemes.song.artists"
        "&page%5Bsize%5D=100"
    )
    data = get(url)
    anime_raw = data["anime"]
    print(f"    共 {len(anime_raw)} 部")

    out = []
    for a in anime_raw:
        themes = []
        for t in a.get("animethemes") or []:
            song = t.get("song") or {}
            themes.append(
                {
                    "kind": t["type"],  # OP / ED
                    "sequence": t.get("sequence"),
                    "song_title": song.get("title"),
                    "artists": [x["name"] for x in (song.get("artists") or [])],
                }
            )
        if themes:
            out.append(
                {
                    "name": a["name"],
                    "slug": a.get("slug"),
                    "year": a.get("year"),
                    "season": a.get("season"),
                    "media_format": a.get("media_format"),
                    "themes": themes,
                }
            )

    path = os.path.join(OUT_DIR, "animethemes_2026fall.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"    已保存 {len(out)} 部到 {path}")

    print("\n前 8 部（用于挑选演示数据）：")
    for a in out[:8]:
        print(f"  · {a['name']}")
        for t in a["themes"][:3]:
            seq = t["sequence"] or ""
            print(f"      {t['kind']}{seq}  {t['song_title']}  /  {', '.join(t['artists'])}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
