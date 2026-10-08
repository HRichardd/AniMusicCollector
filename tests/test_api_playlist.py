"""`collector/api_playlist.py` —— 歌单曲目的 web API 读取器（纯离线单测）。

这里测的是**解析与判定**，不是「网易云接口没变」。真机形状是用
`tools/probe_netease_playlist.py` 探出来的，探的结果写在模块 docstring 里；
本文件把那些实测形状固化成断言，接口一变就会响。

最要紧的一组断言是 `ids_complete` 与「读失败 ≠ 歌单是空的」：
写错这两条不会报错，只会**静默重复加歌**或**静默假装成功**。
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from animusic.collector import api_playlist as ap  # noqa: E402
from animusic.http import HttpError  # noqa: E402


# ---------------------------------------------------------------- 假 HTTP

class _FakeHttp:
    """假的 `HttpClient`：按 URL 子串分派，记下每一次请求。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def get_json(self, url: str, **kw):
        self.calls.append(url)
        for key, val in self.routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val(url) if callable(val) else val
        raise HttpError(f"no route for {url}")

    @property
    def n_playlist_reads(self) -> int:
        return sum(1 for c in self.calls if "playlist/detail" in c)

    @property
    def n_song_reads(self) -> int:
        return sum(1 for c in self.calls if "song/detail" in c)


def _playlist_body(*, track_ids, track_count=None, name="26年4月", privacy=0,
                   subscribed=False, creator="365170321") -> dict:
    return {
        "code": 200,
        "playlist": {
            "id": 17977808146, "name": name,
            "trackCount": (len(track_ids) if track_count is None else track_count),
            "trackIds": [({"id": i} if not isinstance(i, dict) else i) for i in track_ids],
            "tracks": [],
            "privacy": privacy, "subscribed": subscribed,
            "creator": {"userId": creator},
        },
    }


def _song_body(ids) -> dict:
    return {"code": 200, "songs": [
        {"id": int(i), "name": f"Song {i}", "artists": [{"id": 1, "name": "A"}],
         "album": {"id": 2, "name": "Alb"}, "duration": 200000, "fee": 0,
         "alias": [], "mvid": 0}
        for i in ids
    ]}


# ═══════════════════════════════════════════════════════════════ id 表解析

class TestSongIdsOf(unittest.TestCase):
    def test_dicts_and_bare_ids_and_dedup(self):
        got = ap.song_ids_of([
            {"id": 501, "v": 5, "at": 1, "alg": None},   # 真机形状
            502,
            "503",
            {"id": 501},                                  # 重复
            {"nope": 1},                                  # 没 id → 丢
            None, "", {"id": ""},
        ])
        self.assertEqual(got, ["501", "502", "503"])

    def test_not_a_list_gives_empty(self):
        for bad in (None, {}, "501", 501):
            with self.subTest(bad=bad):
                self.assertEqual(ap.song_ids_of(bad), [])

    def test_bool_is_not_an_id(self):
        """`True` 是 `int` 的子类，漏掉这条会变成 id="1" 这种幽灵成员。"""
        self.assertEqual(ap.song_ids_of([{"id": True}, {"id": 501}]), ["501"])


# ═══════════════════════════════════════════════════════════════ 快照

class TestSnapshot(unittest.TestCase):
    def _reader(self, routes):
        http = _FakeHttp(routes)
        return ap.WebPlaylistReader(client=http), http

    def test_parses_the_real_shape(self):
        r, http = self._reader({"playlist/detail": _playlist_body(
            track_ids=[501, 502, 503], name="26年4月", privacy=0, subscribed=False)})
        snap = r.snapshot(17977808146)
        self.assertEqual(snap.playlist_id, "17977808146")
        self.assertEqual(snap.name, "26年4月")
        self.assertEqual(snap.track_ids, ["501", "502", "503"])
        self.assertEqual(snap.declared_count, 3)
        self.assertEqual(snap.privacy, 0)
        self.assertIs(snap.subscribed, False)
        self.assertEqual(snap.creator_id, "365170321")
        self.assertTrue(snap.has_track_ids)
        self.assertTrue(snap.ids_complete)
        self.assertEqual(http.n_playlist_reads, 1)
        self.assertEqual(r.reads, 1)

    def test_ids_complete_is_about_covering_track_count(self):
        """`len(trackIds) >= trackCount` 才算「完整」—— 这是幂等预检的地基。"""
        cases = [
            (dict(track_ids=[1, 2, 3], track_count=3), True, "等长"),
            (dict(track_ids=[1, 2, 3, 4], track_count=3), True, "多出来（有歌被删过）"),
            (dict(track_ids=[1, 2], track_count=3), False, "少了（截断/私有）"),
            (dict(track_ids=[], track_count=0), True, "真空歌单"),
            (dict(track_ids=[], track_count=5), False, "说是 5 首却一个都不给"),
        ]
        for kw, want, label in cases:
            with self.subTest(label=label):
                r, _ = self._reader({"playlist/detail": _playlist_body(**kw)})
                self.assertIs(r.snapshot(1).ids_complete, want)

    def test_missing_track_count_is_never_complete(self):
        """没有 trackCount 就无法证明完整 —— **不许猜**。

        猜「应该全了吧」的直接后果是把「读不到」当成「不在里面」，然后重复加歌。
        """
        body = _playlist_body(track_ids=[1, 2, 3])
        del body["playlist"]["trackCount"]
        r, _ = self._reader({"playlist/detail": body})
        snap = r.snapshot(1)
        self.assertIsNone(snap.declared_count)
        self.assertFalse(snap.ids_complete)
        self.assertIn("没给 trackCount", snap.incomplete_reason)

    def test_empty_array_is_a_verified_empty_playlist(self):
        """`trackIds: []` + `trackCount: 0` = 服务端**明确**说「这歌单是空的」。"""
        r, _ = self._reader({"playlist/detail": _playlist_body(track_ids=[])})
        snap = r.snapshot(1)
        self.assertEqual(snap.track_ids, [])
        self.assertTrue(snap.has_track_ids)
        self.assertTrue(snap.ids_complete)

    def test_missing_track_ids_key_is_never_complete(self):
        """**键不存在 ≠ 空数组** —— 这条不写，私有歌单会被当成空歌单。

        服务端不想说的时候（受限歌单）可能连 `trackCount` 都给 0。
        把「键不存在」当「空歌单」，就会得出「这首歌不在里面」→ 重复加歌。
        """
        body = _playlist_body(track_ids=[1, 2, 3])
        del body["playlist"]["trackIds"]
        body["playlist"]["trackCount"] = 0        # 服务端把总数也藏了
        r, _ = self._reader({"playlist/detail": body})
        snap = r.snapshot(1)
        self.assertEqual(snap.track_ids, [])
        self.assertFalse(snap.has_track_ids)
        self.assertFalse(snap.ids_complete)
        self.assertIn("没有 trackIds", snap.incomplete_reason)

    # ------------------------------------------------------------ 错误码

    def test_404_is_its_own_exception_type(self):
        """「歌单不存在」是一个**确定的答案**，上层要能和「读不到」区分开。"""
        r, _ = self._reader({"playlist/detail": {
            "code": 404, "message": "歌单不存在", "msg": "歌单不存在"}})
        with self.assertRaises(ap.PlaylistNotFound) as ctx:
            r.snapshot(999999999999999999)
        self.assertIn("歌单不存在", str(ctx.exception))
        self.assertIsInstance(ctx.exception, ap.WebPlaylistError)

    def test_400_is_a_plain_error(self):
        r, _ = self._reader({"playlist/detail": {"code": 400, "message": "请求参数错误"}})
        with self.assertRaises(ap.WebPlaylistError) as ctx:
            r.snapshot(0)
        self.assertNotIsInstance(ctx.exception, ap.PlaylistNotFound)
        self.assertIn("400", str(ctx.exception))

    def test_missing_playlist_object_raises_not_empty_snapshot(self):
        """有曲目数据才算读到了；返回空快照会被上层误当「空歌单」。"""
        r, _ = self._reader({"playlist/detail": {"code": 200}})
        with self.assertRaises(ap.WebPlaylistError) as ctx:
            r.snapshot(1)
        self.assertIn("没有 playlist", str(ctx.exception))

    def test_network_error_becomes_web_playlist_error(self):
        r, _ = self._reader({"playlist/detail": HttpError("boom", url="x")})
        with self.assertRaises(ap.WebPlaylistError) as ctx:
            r.snapshot(1)
        self.assertIn("网络请求失败", str(ctx.exception))

    def test_non_dict_json_is_an_error(self):
        r, _ = self._reader({"playlist/detail": ["not", "a", "dict"]})
        with self.assertRaises(ap.WebPlaylistError):
            r.snapshot(1)

    def test_blank_id_is_rejected_before_any_request(self):
        r, http = self._reader({"playlist/detail": _playlist_body(track_ids=[1])})
        for bad in ("", "   ", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ap.WebPlaylistError):
                    r.snapshot(bad)
        self.assertEqual(http.calls, [], "非法 id 不该发出任何请求")

    def test_last_error_is_recorded_on_failure(self):
        r, _ = self._reader({"playlist/detail": {"code": 404, "message": "歌单不存在"}})
        with self.assertRaises(ap.PlaylistNotFound):
            r.snapshot(7)
        self.assertIn("歌单不存在", r.last_error or "")


# ═══════════════════════════════════════════════════════════════ 歌曲详情

class TestSongDetails(unittest.TestCase):
    def test_order_follows_the_request_not_the_server(self):
        """服务端实测保序，但顺序押在它身上不值得 —— 按入参重排。"""
        http = _FakeHttp({"song/detail": {"code": 200, "songs": [
            {"id": 3, "name": "c", "artists": []},
            {"id": 1, "name": "a", "artists": []},
            {"id": 2, "name": "b", "artists": []},
        ]}})
        r = ap.WebPlaylistReader(client=http)
        got = r.song_details(["1", "2", "3"])
        self.assertEqual([s["netease_song_id"] for s in got], ["1", "2", "3"])
        self.assertEqual([s["name"] for s in got], ["a", "b", "c"])

    def test_chunks_large_playlists(self):
        http = _FakeHttp({"song/detail": lambda url: _song_body(
            [x for x in range(1, 251)])})
        r = ap.WebPlaylistReader(client=http)
        got = r.song_details([str(i) for i in range(1, 251)])
        self.assertEqual(len(got), 250)
        self.assertEqual(http.n_song_reads, 3, "250 个 id 按 100 一批 → 3 次请求")

    def test_failure_returns_empty_and_records_the_error(self):
        http = _FakeHttp({"song/detail": HttpError("down", url="x")})
        r = ap.WebPlaylistReader(client=http)
        self.assertEqual(r.song_details(["1"]), [])
        self.assertIn("网络请求失败", r.last_error or "")

    def test_empty_input_makes_no_request(self):
        http = _FakeHttp({})
        r = ap.WebPlaylistReader(client=http)
        self.assertEqual(r.song_details([]), [])
        self.assertEqual(http.calls, [])

    def test_songs_keeps_ids_when_details_fail(self):
        """歌名读不到**不该**让「在不在歌单里」一起失败 —— id 表才是判据。"""
        http = _FakeHttp({
            "playlist/detail": _playlist_body(track_ids=[501, 502]),
            "song/detail": HttpError("down", url="x"),
        })
        r = ap.WebPlaylistReader(client=http)
        snap = r.songs(17977808146)
        self.assertEqual(snap.track_ids, ["501", "502"])
        self.assertEqual(snap.songs, [])
        self.assertTrue(snap.ids_complete)

    def test_songs_on_an_empty_playlist_makes_one_request_only(self):
        http = _FakeHttp({"playlist/detail": _playlist_body(track_ids=[])})
        r = ap.WebPlaylistReader(client=http)
        snap = r.songs(17977808146)
        self.assertEqual(snap.songs, [])
        self.assertEqual(http.n_song_reads, 0, "空歌单不该再去问歌曲详情")


# ═══════════════════════════════════════════════════════════════ 归一化

class TestNormalizeWebSong(unittest.TestCase):
    def test_real_song_detail_shape(self):
        """`song/detail` 的 `album` 是 dict（搜索接口同族，但 store 用 `al`）。"""
        s = ap.normalize_web_song({
            "id": 3336809696, "name": "ハク", "artists": [{"id": 1, "name": "あたらよ"}],
            "album": {"id": 2, "name": "Album"}, "duration": 231000, "fee": 8,
            "alias": [], "mvid": 0, "transNames": [],
        })
        self.assertEqual(s["netease_song_id"], "3336809696")
        self.assertEqual(s["name"], "ハク")
        self.assertEqual(s["artists"], ["あたらよ"])
        self.assertEqual(s["album"], "Album")
        self.assertEqual(s["duration_ms"], 231000)
        self.assertEqual(s["fee"], 8)
        self.assertEqual(s["tags"], ["VIP"])

    def test_alias_str_and_al_dict_and_dt(self):
        """同一族接口的三种写法都要吃：`alias` 字符串、`al`、`dt`。"""
        s = ap.normalize_web_song({
            "id": "1", "name": "OP", "artists": ["X"], "al": {"name": "A"},
            "dt": 1000, "alias": "某番 OP", "mvid": 5,
        })
        self.assertEqual(s["album"], "A")
        self.assertEqual(s["duration_ms"], 1000)
        self.assertEqual(s["alias"], ["某番 OP"])
        self.assertEqual(s["anime_alias"], "某番 OP")
        self.assertIn("MV", s["tags"])

    def test_artists_string_fallback_and_junk(self):
        s = ap.normalize_web_song({"id": 1, "name": "x", "artists": "A / B"})
        self.assertEqual(s["artists"], ["A", "B"])
        self.assertEqual(ap.normalize_web_song(None)["netease_song_id"], "")
        self.assertEqual(ap.normalize_web_song(5)["name"], "")

    def test_id_zero_is_kept(self):
        """id=0 是合法的「没有 id」哨兵吗？不是 —— 这里只保证不炸，不猜语义。"""
        self.assertEqual(ap.normalize_web_song({"id": 0, "name": "x"})["netease_song_id"], "0")

    def test_output_is_compatible_with_write_path_id_scan(self):
        """两个来源的产物必须能被同一套 id 扫描吃掉（混着用）。"""
        from animusic.collector import write_path as wp

        s = ap.normalize_web_song({"id": 501, "name": "x"})
        self.assertTrue(wp.contains_song([s], "501"))
        self.assertTrue(wp.contains_song([s], 501))


if __name__ == "__main__":
    unittest.main(verbosity=2)
