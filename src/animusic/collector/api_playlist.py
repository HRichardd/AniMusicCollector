"""网易云歌单曲目读取 —— 走 web API（只读）。

对应 DESIGN.md §5.10（二次比对）/ §7.2。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么需要这个模块（**这是实测逼出来的，不是设计洁癖**）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
客户端 redux store 里的歌单对象**没有曲目数据**：

    · `tracks` / `trackIds` 键根本不存在（用户没在客户端打开过那个歌单时）
    · 只有 `trackCount` 一个数字

实测（2026-10，客户端 3.1.7.203649）：`createPlaylist` 129 个、`favPlaylist` 179 个
歌单对象，**没有一个**带曲目数组。于是：

    · `playlist_songs()` 恒返回 `[]`
    · `check_membership()` 恒返回 `None`（证据不足）
    · `add_to_playlist()` 的幂等预检在 `require_complete_membership=True` 下
      **恒定拒绝写入** —— 整个 M3 写入链路一封都发不出去

原来的「解法」是让人先去客户端手动点开那个歌单把曲目拉下来。40 首要用户
点 40 次，这不是解法。

而 web 接口是**服务端真相**，一次 GET 就拿到完整且带顺序的曲目 id：

    GET https://music.163.com/api/v6/playlist/detail?id=<id>

实测（用户真实歌单）：

    id            名称      trackCount   len(trackIds)   len(tracks)   privacy
    17977808146   26年4月   43           43              10            0
    17665880465   26年1月   65           65              10            0
    17457984710   25.10     17           17              10            0

    ⚠️ 关键：**`len(trackIds) == trackCount`** —— `trackIds` 是**完整**成员表；
       而 `tracks` 只有前 10 个完整对象（网易云故意截断）。
       所以「在不在」用 `trackIds` 判，「叫什么名字」再用 song/detail 补齐。

    · `trackIds` 元素形状：`{"id":3395172821,"v":5,"t":0,"at":...,"uid":...,"alg":null,...}`
      —— 只有 `id` 是我们要的；`at` 是加入时间，实测**按 `at` 降序**（= 歌单显示顺序）
    · 不存在的歌单返回 `{"code":404,"message":"歌单不存在"}`（HTTP 200）
    · id 非数字返回 `{"code":400,"message":"请求参数错误"}`

补齐名字用：

    GET https://music.163.com/api/song/detail?ids=%5B<id>,<id>,...%5D

实测 65 个 id 一次请求可全部返回，且**保持请求顺序**。字段与搜索接口同族
（`name` / `artists` / `album` / `duration` / `alias` / `fee` / `mvid` / `transNames`），
只有 `album` 的写法不同：这里是 dict，`desktop_cdp.normalize_song` 只认 `al`
或字符串 `album` —— 所以本模块自己归一化，不复用它。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
边界与安全
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
· **只读**。本模块不登录、不带 cookie、不发任何写请求。
· **不猜**。`trackIds` 缺失或长度 < `trackCount` 时，快照标记为「id 不全」，
  调用方必须当「证据不足」处理，不能当「歌单里没有这首歌」。
  （私有歌单在未登录视角下可能被截断，这条规则同时兜住那种情况。）
· **限速** `constants.PLAYLIST_READ_RATE_RPS`（默认 1.5 rps）。
  `_membership` 的回查循环会反复读，不限速会真的打出风控。
· 失败一律抛 `WebPlaylistError` 子类，**绝不返回空列表冒充「歌单是空的」**。
  「读失败」和「歌单空的」是两件事，混起来就是重复加歌或假装成功。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .. import constants
from ..http import HttpClient, HttpError

logger = logging.getLogger(__name__)

__all__ = [
    "PLAYLIST_DETAIL_API",
    "SONG_DETAIL_API",
    "WebPlaylistError",
    "PlaylistNotFound",
    "PlaylistSnapshot",
    "WebPlaylistReader",
    "normalize_web_song",
    "song_ids_of",
]

PLAYLIST_DETAIL_API = "https://music.163.com/api/v6/playlist/detail"
SONG_DETAIL_API = "https://music.163.com/api/song/detail"

#: song/detail 一次请求塞多少个 id。实测 65 个一次没问题；
#: 取 100 是「一个歌单基本一次拿完」与「URL 不会过长」之间的折中。
_SONG_ID_CHUNK = 100

#: 网易云的「这首歌要钱」标记（与 api_search 的判法一致）
_VIP_FEES = (1, 4, 8)


class WebPlaylistError(RuntimeError):
    """读歌单失败（网络 / 解析 / 服务端返回非 200 业务码）。"""


class PlaylistNotFound(WebPlaylistError):
    """歌单不存在（服务端 code=404）。

    单独一个类型，是因为它**是一种确定的答案**：「这个 id 指向的歌单没了」，
    而不是「读不到」。上层可以把「不存在」当成「该建新歌单」，
    而把其它错误当成「别乱动」。
    """


@dataclass(frozen=True)
class PlaylistSnapshot:
    """一次歌单读取的结果。

    `track_ids` 是**顺序的、完整的**（当 `ids_complete` 为真时）成员表；
    `songs` 可能比它短（只在没调 `songs()` 或 song/detail 失败时）。
    """

    playlist_id: str
    name: str | None = None
    track_ids: list[str] = field(default_factory=list)
    songs: list[dict] = field(default_factory=list)
    declared_count: int | None = None
    privacy: int | None = None
    subscribed: bool | None = None
    creator_id: str | None = None
    #: 响应里**到底有没有** `trackIds` 这个键。
    #:
    #: 为什么要单独记：「给了空数组」和「压根没给」是两件事。
    #: `trackIds: []` + `trackCount: 0` = 服务端明确告诉我们「这歌单是空的」；
    #: 键不存在 = 服务端不想说（私有/受限歌单），此时 trackCount 也可能是 0，
    #: 把它当「空歌单」就会得出「这首歌不在里面」→ **重复加歌**。
    has_track_ids: bool = False

    @property
    def ids_complete(self) -> bool:
        """`track_ids` 是否可信地覆盖了整张歌单。

        两个条件**都要**满足：

          1. 服务端确实给了 `trackIds` 数组（见 `has_track_ids`）；
          2. `len(trackIds) >= trackCount`（服务端自报的总数）。

        缺 `trackCount` 一律当不全 —— 宁可说「不知道」，也不能说「不在里面」。
        """
        if not self.has_track_ids:
            return False
        if self.declared_count is None:
            return False
        return len(self.track_ids) >= self.declared_count

    @property
    def incomplete_reason(self) -> str:
        if not self.has_track_ids:
            return "web API 的响应里没有 trackIds 数组 —— 无法证明曲目表是完整的"
        if self.declared_count is None:
            return "web API 没给 trackCount —— 无法判断 trackIds 是否完整"
        return (f"web API 自报 trackCount={self.declared_count}，"
                f"但只给出 {len(self.track_ids)} 个 trackId —— id 表不完整")


# ---------------------------------------------------------------- 归一化

def _norm_id(v: Any) -> str | None:
    """id → str；空/容器/布尔一律 None（不猜）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if float(v).is_integer() else None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return None


def song_ids_of(track_ids: Any) -> list[str]:
    """把 `trackIds` 数组（元素是 dict 或裸 id）变成 id 字符串列表，保序、去重。"""
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(track_ids, (list, tuple)):
        return out
    for it in track_ids:
        if isinstance(it, dict):
            sid = _norm_id(it.get("id"))
        else:
            sid = _norm_id(it)
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _artists_of(artists: Any) -> list[str]:
    if isinstance(artists, list):
        out: list[str] = []
        for a in artists:
            if isinstance(a, dict):
                nm = a.get("name")
                if nm:
                    out.append(str(nm))
            elif isinstance(a, str) and a.strip():
                out.append(a.strip())
        return out
    if isinstance(artists, str):
        return [p.strip() for p in artists.split("/") if p.strip()]
    return []


def normalize_web_song(s: Any) -> dict:
    """把 web 接口的歌曲对象拉平成与 `desktop_cdp.normalize_song` 同形的字典。

    同形的意义：`write_path.contains_song` / `iter_song_ids` 只认
    `netease_song_id` 这类键，两个来源的产物必须能混着用。
    """
    if not isinstance(s, dict):
        s = {}
    album = s.get("album")
    if not isinstance(album, dict):
        album = s.get("al") if isinstance(s.get("al"), dict) else {}
    name = s.get("name")
    if name is None:
        name = s.get("songName")
    duration = s.get("duration", s.get("dt", s.get("duration_ms")))
    fee = s.get("fee")
    alias = s.get("alias") or s.get("aliases") or []
    if isinstance(alias, str):
        alias = [alias]
    if not isinstance(alias, list):
        alias = []
    tags: list[str] = []
    if fee in _VIP_FEES:
        tags.append("VIP")
    if s.get("mvid"):
        tags.append("MV")
    return {
        "netease_song_id": _norm_id(s.get("id")) or "",
        "name": ("" if name is None else str(name)).strip(),
        "artists": _artists_of(s.get("artists") or s.get("ar")),
        "album": (album.get("name") or None),
        "duration_ms": int(duration) if isinstance(duration, (int, float))
        and not isinstance(duration, bool) else None,
        "fee": int(fee) if isinstance(fee, (int, float)) and not isinstance(fee, bool) else None,
        "alias": [str(a) for a in alias],
        "anime_alias": (str(alias[0]) if alias else None),
        "tags": tags,
        "raw": s,
    }


# ---------------------------------------------------------------- 读取器

class WebPlaylistReader:
    """歌单曲目的 web API 读取器。

    两档粒度，因为两者的代价差一倍，而**幂等预检只需要便宜的那一档**：

        `snapshot(pid)`  → 1 次请求，拿到完整 id 表（够判「在不在」）
        `songs(pid)`     → 2 次请求，额外补齐 name/artists（给界面与二次比对看）

    两个方法都不缓存：写后回查必须看到**服务端最新**的状态，
    在这里加缓存等于给「刚写进去的歌」造一个假的「不在里面」。
    限速由 `HttpClient` 的令牌桶负责。
    """

    def __init__(self, client: HttpClient | None = None):
        self.http = client or HttpClient(
            user_agent=constants.NETEASE_HEADERS["User-Agent"],
            rps=constants.PLAYLIST_READ_RATE_RPS,
        )
        #: 诊断：读了几次歌单、最后一条错误
        self.reads = 0
        self.last_error: str | None = None

    # ---------------------------------------------------------------- 内部

    def _get_json(self, url: str, what: str) -> dict:
        try:
            data = self.http.get_json(url, headers={
                "Referer": constants.NETEASE_HEADERS["Referer"],
                "Accept": constants.NETEASE_HEADERS["Accept"],
            })
        except HttpError as e:
            raise WebPlaylistError(f"{what}：网络请求失败 —— {e}") from None
        if not isinstance(data, dict):
            raise WebPlaylistError(f"{what}：返回的不是 JSON 对象（{type(data).__name__}）")
        return data

    @staticmethod
    def _check_code(data: dict, what: str, playlist_id: str = "") -> None:
        code = data.get("code")
        if code == 200:
            return
        msg = data.get("message") or data.get("msg") or ""
        if code == 404:
            raise PlaylistNotFound(
                f"{what}：歌单不存在（code=404{('，' + msg) if msg else ''}）"
                + (f" —— id={playlist_id}" if playlist_id else ""))
        raise WebPlaylistError(
            f"{what}：服务端返回 code={code}"
            + (f"（{msg}）" if msg else "")
            + (f" —— id={playlist_id}" if playlist_id else ""))

    # ---------------------------------------------------------------- 公开

    def snapshot(self, playlist_id: str | int) -> PlaylistSnapshot:
        """读一次歌单：完整 id 表 + 服务端自报的元信息。**不含歌曲名。**

        抛 `PlaylistNotFound` / `WebPlaylistError`（**不返回空快照**）。
        """
        pid = _norm_id(playlist_id)
        if pid is None:
            raise WebPlaylistError(f"playlist_id 为空或非法：{playlist_id!r}")
        what = "读歌单 trackIds"
        try:
            data = self._get_json(f"{PLAYLIST_DETAIL_API}?id={pid}", what)
        except WebPlaylistError as e:
            self.last_error = str(e)
            raise
        try:
            self._check_code(data, what, pid)
        except WebPlaylistError as e:
            self.last_error = str(e)
            raise
        self.reads += 1
        pl = data.get("playlist")
        if not isinstance(pl, dict):
            msg = f"{what}：返回里没有 playlist 对象 —— 无法判断曲目"
            self.last_error = msg
            raise WebPlaylistError(msg)
        declared = pl.get("trackCount")
        priv = pl.get("privacy")
        subs = pl.get("subscribed")
        raw_ids = pl.get("trackIds")
        snap = PlaylistSnapshot(
            playlist_id=pid,
            name=(str(pl["name"]) if pl.get("name") is not None else None),
            track_ids=song_ids_of(raw_ids),
            songs=[],
            declared_count=(int(declared) if isinstance(declared, (int, float))
                            and not isinstance(declared, bool) else None),
            privacy=(int(priv) if isinstance(priv, (int, float))
                     and not isinstance(priv, bool) else None),
            subscribed=(bool(subs) if isinstance(subs, bool) else None),
            creator_id=_norm_id((pl.get("creator") or {}).get("userId")
                                if isinstance(pl.get("creator"), dict) else None),
            has_track_ids=isinstance(raw_ids, (list, tuple)),
        )
        self.last_error = None
        return snap

    def songs(self, playlist_id: str | int) -> PlaylistSnapshot:
        """`snapshot()` + 用 song/detail 把 name/artists 补齐。

        补齐失败**不抛错**，只把 `songs` 留空（id 表仍然有效，幂等预检照常工作）
        —— 「读不到歌名」不该让「歌在不在」这件事一起失败。
        """
        snap = self.snapshot(playlist_id)
        if not snap.track_ids:
            return snap
        songs = self.song_details(snap.track_ids)
        return PlaylistSnapshot(
            playlist_id=snap.playlist_id, name=snap.name, track_ids=snap.track_ids,
            songs=songs, declared_count=snap.declared_count, privacy=snap.privacy,
            subscribed=snap.subscribed, creator_id=snap.creator_id,
            has_track_ids=snap.has_track_ids)

    def song_details(self, song_ids: list[str]) -> list[dict]:
        """按 id 批量取歌曲对象，**顺序与入参一致**；失败返回 `[]`（不抛）。"""
        want = [_norm_id(x) for x in (song_ids or [])]
        want = [x for x in want if x]
        if not want:
            return []
        by_id: dict[str, dict] = {}
        for i in range(0, len(want), _SONG_ID_CHUNK):
            chunk = want[i:i + _SONG_ID_CHUNK]
            url = f"{SONG_DETAIL_API}?ids=%5B{','.join(chunk)}%5D"
            try:
                data = self._get_json(url, "读歌曲详情")
                if data.get("code") not in (200, None):
                    raise WebPlaylistError(f"读歌曲详情：code={data.get('code')}")
            except WebPlaylistError as e:
                self.last_error = str(e)
                return []
            for s in (data.get("songs") or []):
                sid = _norm_id(s.get("id")) if isinstance(s, dict) else None
                if sid and sid not in by_id:
                    by_id[sid] = normalize_web_song(s)
        # 按入参顺序还原（服务端虽然实测保序，但不值得把顺序押在它身上）
        return [by_id[x] for x in want if x in by_id]
