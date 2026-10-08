"""直连网易云搜索 API —— 搜索侧的主力实现。

对应 DESIGN.md §5.8 / 附录 G.3。

    GET https://music.163.com/api/search/get?s=<kw>&type=1&offset=0&total=true&limit=10
        Referer: https://music.163.com/

实测返回字段：
    album, alias, artists, copyrightId, duration, fee, ftype, id, mark, mvid, name, rtype, status

**`alias` 就是「TV动画《鬼灭之刃》片头曲」** —— 打分器权重 0.30 的核心信号。

与客户端路径的字段差异：
    ✅ 直连有：id / name / artists / album / duration / alias / fee / mvid
    ❌ 直连缺：VIP / MV / 原唱 这些 tag（可由 fee / mvid / 艺人匹配推断）

注意：这是**非官方接口**，只用于只读搜索；写操作一律走客户端。
限速取 `SEARCH_RATE_RPS`，比写操作宽松但仍保守。
"""

from __future__ import annotations

import urllib.parse

from .. import constants
from ..http import HttpClient
from ..models import Candidate, HealthStatus, Origin

_MAX_LIMIT = 30


class ApiSearchCollector:
    def __init__(self, client: HttpClient | None = None):
        self.http = client or HttpClient(
            user_agent=constants.NETEASE_HEADERS["User-Agent"],
            rps=constants.SEARCH_RATE_RPS,
        )

    # ---------------------------------------------------------------- 健康

    def health(self) -> HealthStatus:
        try:
            cands = self.search("紅蓮華 LiSA", limit=1)
            if cands:
                return HealthStatus(True, f"搜索可用（示例命中 {cands[0].name}）")
            return HealthStatus(False, "搜索返回空结果")
        except Exception as e:  # noqa: BLE001
            return HealthStatus(False, f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 搜索

    def search(self, keyword: str, limit: int = 10) -> list[Candidate]:
        kw = (keyword or "").strip()
        if not kw:
            return []
        limit = max(1, min(limit, _MAX_LIMIT))
        url = (
            f"{constants.NETEASE_SEARCH_API}"
            f"?s={urllib.parse.quote(kw)}"
            f"&type=1&offset=0&total=true&limit={limit}"
        )
        data = self.http.get_json(
            url,
            headers={
                "Referer": constants.NETEASE_HEADERS["Referer"],
                "Accept": constants.NETEASE_HEADERS["Accept"],
            },
        )
        if data.get("code") != 200:
            raise RuntimeError(f"网易云搜索返回 code={data.get('code')}")

        songs = ((data.get("result") or {}).get("songs")) or []
        out: list[Candidate] = []
        for rank, s in enumerate(songs):
            out.append(self._to_candidate(s, keyword=kw, rank=rank))
        return out

    # ---------------------------------------------------------------- 解析

    @staticmethod
    def _to_candidate(s: dict, keyword: str, rank: int) -> Candidate:
        album = s.get("album") or {}
        alias = s.get("alias") or []
        if isinstance(alias, str):
            alias = [alias]

        tags: list[str] = []
        fee = s.get("fee")
        mvid = s.get("mvid") or 0
        if fee in (1, 4, 8):
            tags.append("VIP")
        if mvid:
            tags.append("MV")

        return Candidate(
            theme_id=0,
            netease_song_id=str(s.get("id") or ""),
            name=(s.get("name") or "").strip(),
            artists=[a.get("name") for a in (s.get("artists") or []) if a.get("name")],
            album=(album.get("name") or "").strip() or None,
            cover=album.get("picUrl") or None,
            duration_ms=s.get("duration"),
            tags=tags,
            anime_alias=alias[0] if alias else None,
            trans_names=[str(x) for x in (s.get("transNames") or [])],
            fee=fee,
            mvid=mvid or None,
            query_used=keyword,
            search_rank=rank,
            origin=Origin.SEARCH,
        )

    # ---------------------------------------------------------------- 详情

    def song_detail(self, song_ids: list[str]) -> list[dict]:
        """按 ID 取详情（手动粘贴链接兜底时用）。

        与搜索不同，这个走的是公开的 detail 端点；
        失败时返回空列表而不是抛错，让上层能优雅降级。
        """
        if not song_ids:
            return []
        ids = ",".join(str(x) for x in song_ids if str(x).isdigit())
        if not ids:
            return []
        url = f"https://music.163.com/api/song/detail?ids=%5B{ids}%5D"
        try:
            data = self.http.get_json(
                url, headers={"Referer": constants.NETEASE_HEADERS["Referer"]}
            )
        except Exception:  # noqa: BLE001
            return []
        return data.get("songs") or []
