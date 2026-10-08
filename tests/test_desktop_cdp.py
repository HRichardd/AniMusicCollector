"""`collector/desktop_cdp.py` 的离线测试。

两类：

1. 纯函数测试 —— 歌单名模糊匹配（这是最容易出错、也最该被钉住的部分），
   加上「从假造的 redux state 里抽歌单 / 抽歌曲」的解析逻辑。
2. CDP 测试 —— 复用 `tests/test_cdp.py` 的假 CDP 服务器 `FakeCDP`，
   让它对我们的 JS 表达式回**假造的 store state JSON**，验证
   `health` / `list_playlists` / `find_playlist` / `playlist_songs` 的解析结果。

不碰网络、不需要客户端在跑；不用 `tempfile`（该环境里清理会被权限拒）。

`FakeCDP` 的响应表是 `expr in responses` / `responses[expr]` 两步查表，
所以这里塞一个「前缀匹配」的 dict 子类进去 → 不用改动 `test_cdp.py`，
也不必在测试里复制一份响应逻辑。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from animusic.cdp import CDPBrowser  # noqa: E402
from animusic.collector import desktop_cdp as dc  # noqa: E402
from animusic.collector.desktop_cdp import (  # noqa: E402
    DesktopCDPCollector,
    DesktopCDPError,
    extract_playlists,
    match_playlist_name,
    rank_playlist_names,
    score_playlist_name,
    season_key,
)
from animusic.constants import playlist_name  # noqa: E402
from animusic.models import PlaylistRef  # noqa: E402
from test_cdp import HAS_WS, FakeCDP  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _PrefixResponses(dict):
    """按**前缀**匹配的响应表（`FakeCDP` 只做 `in` / `[]` 两步查表，够用）。"""

    def _match(self, key: str) -> str | None:
        if dict.__contains__(self, key):
            return key
        best: str | None = None
        for k in self:
            if key.startswith(k) and (best is None or len(k) > len(best)):
                best = k
        return best

    def __contains__(self, key: object) -> bool:  # type: ignore[override]
        return isinstance(key, str) and self._match(key) is not None

    def __getitem__(self, key: str):
        m = self._match(key)
        if m is None:
            raise KeyError(key)
        return dict.__getitem__(self, m)


def _fake_state() -> dict:
    """假造的客户端 store：歌单列表在 `playlist.myPlaylist`（猜测路径之一）。"""
    return {
        "user": {"profile": {"userId": 1000001, "nickname": "HRichardY", "vipType": 11}},
        "playlist": {
            "myPlaylist": [
                {
                    "id": 1001, "name": "26年10月", "trackCount": 2,
                    "creator": {"userId": 1000001, "nickname": "HRichardY"},
                    "trackIds": [{"id": 501}, {"id": 502}],
                },
                {
                    "id": 1002, "name": "25.04", "trackCount": 1,
                    "creator": {"userId": 1000001},
                    "tracks": [{
                        "id": 601, "name": "Some OP", "ar": [{"id": 1, "name": "Aki"}],
                        "al": {"id": 9, "name": "Album X"}, "dt": 214000, "fee": 1,
                        "alias": ["某番 OP"],
                    }],
                },
                {
                    "id": 1003, "name": "26年4月", "trackCount": 1,
                    "subscribed": True, "creator": {"userId": 999},
                },
                {
                    "id": 1004, "name": "别人建的歌单", "trackCount": 0,
                    "creator": {"userId": 12345},
                },
            ]
        },
        "song": {
            "songs": {
                "501": {"id": 501, "name": "Escort", "ar": [{"id": 7, "name": "THE BEAT GARDEN"}],
                        "al": {"id": 5, "name": "Escort"}, "dt": 231000, "fee": 0},
                "502": {"id": 502, "name": "Hanakage", "ar": [{"id": 8, "name": "Superfly"}],
                        "al": {"id": 6, "name": "Hanakage"}, "dt": 245000, "fee": 1},
            }
        },
        "ui": {"nowPlaying": {"id": 999, "name": "无关的东西", "trackCount": 3}},
    }


def _fake_dom_songs() -> dict:
    return {
        "rows": [
            {"netease_song_id": "701", "name": "DOM Song", "artists": ["Someone"],
             "duration_ms": 200000, "fee": 0, "alias": None},
            {"netease_song_id": "701", "name": "DOM Song", "artists": ["Someone"],
             "duration_ms": 200000, "fee": 0, "alias": None},   # 重复行，应被去重
        ],
        "context": {"title": "网易云音乐", "url": "orpheus://orpheus/pub/app.html", "count": 2},
    }


# ═══════════════════════════════════════════════════════════════════ 纯函数


class TestSeasonKey(unittest.TestCase):
    """歌单名 → (年, 月份集合)"""

    def test_target_format_from_constants(self):
        """目标名由 constants.playlist_name 生成，必须能解析回来。"""
        self.assertEqual(playlist_name(2026, 4), "26年10月")
        k = season_key(playlist_name(2026, 4))
        self.assertEqual((k.year, k.months), (2026, frozenset({10})))
        self.assertEqual(season_key(playlist_name(2025, 2)).months, frozenset({4}))

    def test_user_writings(self):
        for name, year, months in [
            ("26年10月", 2026, {10}),
            ("26年10月份", 2026, {10}),
            ("2026年10月", 2026, {10}),
            ("26.10", 2026, {10}),
            ("2026.10", 2026, {10}),
            ("26-10", 2026, {10}),
            ("26/10", 2026, {10}),
            ("【26年10月】", 2026, {10}),
            ("26年10月 新番", 2026, {10}),
            ("2026年10", 2026, {10}),
            ("26.04", 2026, {4}),
            ("25.1", 2025, {1}),
            ("25年4月 OP", 2025, {4}),
            ("26Q4", 2026, {10, 11, 12}),
            ("2026Q4", 2026, {10, 11, 12}),
            ("26q4", 2026, {10, 11, 12}),
            ("26年Q4", 2026, {10, 11, 12}),
            ("26年第四季度", 2026, {10, 11, 12}),
            ("26年4季度", 2026, {10, 11, 12}),
            ("26年秋", 2026, {10, 11, 12}),
            ("26年春", 2026, {4, 5, 6}),
            ("二〇二六年十月", 2026, {10}),
            ("二六年十月", 2026, {10}),
            ("２６年１０月", 2026, {10}),      # 全角数字
            ("26 年 10 月", 2026, {10}),        # 带空格
        ]:
            with self.subTest(name=name):
                k = season_key(name)
                self.assertIsNotNone(k, f"{name!r} 应该能解析出季节信息")
                self.assertEqual(k.year, year)
                self.assertEqual(k.months, frozenset(months))

    def test_year_only(self):
        k = season_key("26年")
        self.assertEqual(k.year, 2026)
        self.assertTrue(k.year_only)
        self.assertEqual(len(k.months), 12)

    def test_yearless(self):
        for name, months in [("Q4", {10, 11, 12}), ("q1", {1, 2, 3}),
                             ("10月", {10}), ("十月份", {10}), ("秋", {10, 11, 12}),
                             ("第四季度", {10, 11, 12})]:
            with self.subTest(name=name):
                k = season_key(name)
                self.assertIsNotNone(k, name)
                self.assertIsNone(k.year)
                self.assertEqual(k.months, frozenset(months))

    def test_not_season_names(self):
        """不是季度歌单的名字不能被硬凑出季节信息。"""
        for name in ["", "我喜欢的音乐", "收藏的歌单", "周杰伦", "BEST 25", "OP25",
                     "TVA", "128", "3.1.7", "25.13", "128.5", "春日影", "K-ON!",
                     None]:
            with self.subTest(name=name):
                self.assertIsNone(season_key(name), f"{name!r} 不该解析出季节")

    def test_two_digit_and_four_digit_year_are_the_same_year(self):
        self.assertEqual(season_key("26年10月").year, season_key("2026年10月").year)
        self.assertEqual(season_key("26年10月").year, 2026)
        self.assertEqual(season_key("99年10月").year, 2099)


class TestPlaylistNameMatching(unittest.TestCase):
    def test_aligns_all_documented_writings(self):
        for cand in ["26年10月", "26.10", "26年10月份", "2026年10月", "2026.10",
                     "26年10月新番", "26Q4", "2026年第四季度", "二〇二六年十月"]:
            with self.subTest(cand=cand):
                self.assertEqual(match_playlist_name("26年10月", [cand]), cand,
                                 f"{cand!r} 应该对齐到 26年10月")

    def test_quarter_writings_match_the_quarter_months(self):
        """10/11/12 月都算 Q4（任务里明确要求）。"""
        self.assertIsNotNone(match_playlist_name("26年10月", ["26Q4"]))
        self.assertIsNotNone(match_playlist_name("26年10月", ["26.10"]))
        self.assertIsNotNone(match_playlist_name("26年10月", ["26年10月"]))

    def test_must_distinguish_years(self):
        """核心反例：25 年的歌单绝不能被 26 年的目标匹配上。"""
        self.assertIsNone(match_playlist_name("26年10月", ["25年10月"]))
        self.assertIsNone(match_playlist_name("25年10月", ["26年10月"]))
        self.assertIsNone(match_playlist_name("26年10月", ["25.10", "25年10月", "25Q4"]))
        self.assertIsNone(match_playlist_name("26年1月", ["25年1月", "25.1"]))
        self.assertEqual(match_playlist_name("25年10月", ["25年10月", "26年10月"]), "25年10月")
        self.assertEqual(match_playlist_name("26年10月", ["25年10月", "26年10月"]), "26年10月")

    def test_must_distinguish_quarters_in_same_year(self):
        self.assertIsNone(match_playlist_name("26年10月", ["26年4月", "26.04", "26Q2"]))
        self.assertIsNone(match_playlist_name("26年1月", ["26Q4"]))
        self.assertIsNone(match_playlist_name("26年7月", ["26年10月"]))
        self.assertEqual(match_playlist_name("26年4月", ["26年10月", "26.04"]), "26.04")

    def test_exact_name_wins_and_beats_ties(self):
        cands = ["26年10月份", "26.10", "26Q4", "26年4月"]
        ranked = rank_playlist_names("26年10月", cands)
        self.assertEqual(ranked[0][1], "26年10月份")      # 同分 → 保持候选顺序
        self.assertEqual([c for _, c, _ in ranked], ["26年10月份", "26.10", "26Q4"])
        self.assertEqual(match_playlist_name("26年10月", ["26年10月", "26.10"]), "26年10月")

    def test_noise_candidates_are_rejected(self):
        self.assertIsNone(match_playlist_name("26年10月", ["我喜欢的音乐", "周杰伦", "TVA", "OP25"]))

    def test_yearless_candidates_need_opt_in(self):
        self.assertIsNone(match_playlist_name("26年10月", ["10月"]))
        self.assertIsNone(match_playlist_name("26年10月", ["Q4"]))
        self.assertEqual(
            match_playlist_name("26年10月", ["10月"], allow_yearless=True, min_score=0.6),
            "10月")
        self.assertEqual(
            match_playlist_name("26年10月", ["Q4"], allow_yearless=True, min_score=0.6),
            "Q4")

    def test_year_only_candidate_is_too_weak(self):
        """只写年份的候选（「26年」）默认不认 —— 它能对上该年任何一个季度。"""
        self.assertIsNone(match_playlist_name("26年10月", ["26年"]))
        self.assertIsNone(match_playlist_name("26年10月", ["26年"], min_score=0.4))
        self.assertEqual(
            match_playlist_name("26年10月", ["26年"], allow_year_only=True, min_score=0.4),
            "26年")

    def test_year_bearing_candidate_beats_yearless(self):
        cands = ["10月", "26年10月"]
        self.assertEqual(match_playlist_name("26年10月", cands, allow_yearless=True,
                                            min_score=0.6), "26年10月")

    def test_scores_are_sane(self):
        self.assertEqual(score_playlist_name("26年10月", "26年10月"), 1.0)
        self.assertGreater(score_playlist_name("26年10月", "26.10"),
                           score_playlist_name("26年10月", "26Q4"))
        self.assertGreater(score_playlist_name("26年10月", "26Q4"), 0.0)
        self.assertEqual(score_playlist_name("26年10月", "25年10月"), 0.0)
        self.assertEqual(score_playlist_name("26年10月", "我喜欢的音乐"), 0.0)
        # 未知目标 → 不做匹配，而不是瞎猜
        self.assertEqual(score_playlist_name("我喜欢的音乐", "26年10月"), 0.0)
        self.assertIsNone(match_playlist_name("我喜欢的音乐", ["26年10月"]))

    def test_rank_is_stable_and_filtered(self):
        ranked = rank_playlist_names("26年10月", ["26年4月", "26年10月", "25年10月", "26Q4"])
        self.assertEqual([c for _, c, _ in ranked], ["26年10月", "26Q4"])
        idx = [i for i, _, _ in ranked]
        self.assertEqual(idx, [1, 3])


class TestStateParsing(unittest.TestCase):
    """从假造的 store state 里抽歌单 / 歌曲（不碰网络）。"""

    def test_describe_state(self):
        d = dc.describe_state({"a": {"b": 1}, "c": [1, 2, 3], "d": "x", "e": None})
        self.assertEqual(d["type"], "dict")
        self.assertEqual(d["keys"]["a"]["type"], "dict")
        self.assertEqual(d["keys"]["a"]["keys"]["b"]["value"], 1)
        self.assertEqual(d["keys"]["c"]["len"], 3)
        self.assertEqual(d["keys"]["d"]["type"], "str")
        self.assertEqual(d["keys"]["e"]["type"], "null")

    def test_describe_state_depth_limit(self):
        d = dc.describe_state({"a": {"b": {"c": {"d": 1}}}}, max_depth=2)
        self.assertEqual(d["keys"]["a"]["keys"]["b"]["type"], "dict")
        self.assertEqual(d["keys"]["a"]["keys"]["b"]["keys"], {})

    def test_extract_from_known_path(self):
        items, source = extract_playlists(_fake_state(), self_id="1000001")
        self.assertEqual(source, "playlist.myPlaylist")
        self.assertEqual([i["name"] for i in items],
                         ["26年10月", "25.04", "26年4月", "别人建的歌单"])
        self.assertEqual(items[0]["id"], "1001")
        self.assertIs(items[0]["created"], True)      # creator.userId == 登录用户
        self.assertIs(items[2]["created"], False)     # subscribed: True
        self.assertIs(items[3]["created"], False)     # creator 是别人

    def test_created_flag_unknown_without_login_user(self):
        """拿不到登录用户 id 时，判断不了就返回 None（上层宽松保留，不漏歌单）。"""
        items, _ = extract_playlists(_fake_state())
        self.assertIsNone(items[0]["created"])

    def test_extract_from_unknown_layout_by_scan(self):
        """布局未知（没有任何猜测路径命中）→ 全树扫描兜住。"""
        state = {
            "zzz": {"whatever": {"deep": [
                {"playlistId": "77", "playlistName": "26年10月", "trackCount": 4,
                 "tracks": []},
                {"playlistId": "78", "playlistName": "25.10", "songCount": 1},
            ]}},
            "songMap": {"1": {"id": 1, "name": "s", "ar": [], "dt": 1}},
        }
        items, source = extract_playlists(state)
        self.assertTrue(source.startswith("scan:"), source)
        self.assertEqual(sorted(i["name"] for i in items), ["25.10", "26年10月"])
        self.assertIsNone(items[0]["created"])        # 判断不了 → None（上层宽松保留）

    def test_extract_ignores_songs_and_noise(self):
        state = {"blob": [
            {"id": 5, "name": "Escort", "ar": [{"name": "x"}], "dt": 200000},
            {"id": 6, "name": "没有歌单特征的字典"},
            {"name": "没有 id 的歌单", "trackCount": 3},
        ]}
        items, source = extract_playlists(state)
        self.assertEqual(items, [])
        self.assertEqual(source, "none")

    def test_song_index_and_resolution(self):
        state = _fake_state()
        idx = dc.song_index(state)
        self.assertIn("501", idx)
        self.assertIn("502", idx)
        self.assertNotIn("1001", idx)     # 歌单对象不能被当成歌
        items, _ = extract_playlists(state)
        entry = next(i for i in items if i["id"] == "1001")
        songs = dc.songs_from_playlist_entry(entry, state)
        self.assertEqual([s["name"] for s in songs], ["Escort", "Hanakage"])
        self.assertEqual(songs[0]["artists"], ["THE BEAT GARDEN"])
        self.assertEqual(songs[0]["album"], "Escort")
        self.assertEqual(songs[0]["duration_ms"], 231000)
        self.assertEqual(songs[1]["netease_song_id"], "502")

    def test_embedded_tracks_win_and_keep_order(self):
        state = _fake_state()
        items, _ = extract_playlists(state)
        entry = next(i for i in items if i["id"] == "1002")
        songs = dc.songs_from_playlist_entry(entry, state)
        self.assertEqual([s["name"] for s in songs], ["Some OP"])
        self.assertEqual(songs[0]["anime_alias"], "某番 OP")
        self.assertEqual(songs[0]["fee"], 1)

    def test_unresolvable_track_ids_are_skipped_not_invented(self):
        state = {"playlist": {"myPlaylist": [
            {"id": 1, "name": "26年10月", "trackCount": 2, "trackIds": [{"id": 111}, {"id": 222}]},
        ]}}
        items, _ = extract_playlists(state)
        self.assertEqual(dc.songs_from_playlist_entry(items[0], state), [])

    def test_find_self_user(self):
        self.assertEqual(dc.user_id_of(dc.find_self_user(_fake_state())), "1000001")
        self.assertIsNone(dc.find_self_user({"a": 1}))
        self.assertTrue(dc.looks_like_user({"userId": 1, "nickname": "n"}))
        self.assertFalse(dc.looks_like_user({"userId": 1}))
        self.assertFalse(dc.looks_like_user(None))


# ═══════════════════════════════════════════════════════════════════ 假 CDP


@unittest.skipUnless(HAS_WS, "需要 websockets")
class TestDesktopCollectorAgainstFakeCDP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeCDP().start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.state = _fake_state()
        self.fake.tabs.clear()
        self.fake.eval_log.clear()
        self.fake.expect_exception = False
        self.fake.responses = _PrefixResponses({
            "document.readyState": "complete",
            "document.hidden": False,
            dc.JS_MARKERS["state"]: json.dumps(self.state, ensure_ascii=False),
            dc.JS_MARKERS["health"]: json.dumps({
                "hasStore": True, "hidden": False, "title": "网易云音乐",
                "url": "orpheus://orpheus/pub/app.html",
                "topKeys": ["user", "playlist", "song"],
                "user": {"userId": 1000001, "nickname": "HRichardY", "vipType": 11},
                "error": None, "diag": {"via": "ancestor", "depth": 4},
            }, ensure_ascii=False),
            dc.JS_MARKERS["dom"]: json.dumps(_fake_dom_songs(), ensure_ascii=False),
        })
        self._open_client_tab()

    def _open_client_tab(self):
        br = CDPBrowser(self.fake.http_port)
        br.open_tab("orpheus://orpheus/pub/app.html")

    def _collector(self, **kw) -> DesktopCDPCollector:
        # 单测一律不联网：曲目的 web API 路径关掉，只测 store 这条。
        # web 路径本身在 test_api_playlist.py 与 test_playlist_web_path.py 里测。
        kw.setdefault("use_web_playlist", False)
        return DesktopCDPCollector(self.fake.http_port, timeout=5.0, **kw)

    # ------------------------------------------------------------ health

    def test_health_when_port_closed(self):
        st = DesktopCDPCollector(_free_port()).health()
        self.assertFalse(st.ok)
        self.assertIn("未监听", st.detail)

    def test_health_ok_and_reports_user(self):
        st = self._collector().health()
        self.assertTrue(st.ok, st.detail)
        self.assertIn("HRichardY", st.detail)
        self.assertEqual(st.extra["user_id"], "1000001")
        self.assertEqual(st.extra["nickname"], "HRichardY")
        self.assertTrue(st.extra["has_store"])
        self.assertEqual(st.extra["store_diag"], {"via": "ancestor", "depth": 4})

    def test_health_without_client_tab(self):
        self.fake.tabs.clear()
        st = self._collector().health()
        self.assertFalse(st.ok)
        self.assertIn("orpheus", st.detail)

    def test_health_without_store(self):
        self.fake.responses[dc.JS_MARKERS["health"]] = json.dumps(
            {"hasStore": False, "error": "no-store", "topKeys": []})
        st = self._collector().health()
        self.assertFalse(st.ok)
        self.assertIn("redux store", st.detail)

    def test_health_when_not_logged_in(self):
        self.fake.responses[dc.JS_MARKERS["health"]] = json.dumps(
            {"hasStore": True, "user": None, "topKeys": ["a"]})
        st = self._collector().health()
        self.assertFalse(st.ok)
        self.assertIn("未登录", st.detail)

    def test_health_window_hidden_is_still_ok(self):
        self.fake.responses[dc.JS_MARKERS["health"]] = json.dumps({
            "hasStore": True, "hidden": True, "title": "网易云音乐",
            "user": {"userId": 1, "nickname": "n"},
        })
        st = self._collector().health()
        self.assertTrue(st.ok, st.detail)
        self.assertIn("隐藏", st.detail)
        self.assertTrue(st.extra["hidden"])

    # ------------------------------------------------------------ list / find

    def test_list_playlists_parses_state(self):
        c = self._collector()
        refs = c.list_playlists()
        # 26年4月 是收藏的（subscribed），别人建的 creator 不是我 → 都不算「我创建的」
        self.assertEqual([r.name for r in refs], ["26年10月", "25.04"])
        self.assertEqual(refs[0].netease_id, "1001")
        self.assertIsInstance(refs[0], PlaylistRef)
        self.assertEqual(c.last_playlist_source, "playlist.myPlaylist")
        # 确实发的是带标记的 STATE 表达式（标记 → 假服务器按前缀分派靠它）
        self.assertTrue(any(e.startswith(dc.JS_MARKERS["state"]) for e in self.fake.eval_log))

    def test_list_playlists_includes_collected_on_demand(self):
        refs = self._collector().list_playlists(only_created=False)
        self.assertEqual([r.name for r in refs],
                         ["26年10月", "25.04", "26年4月", "别人建的歌单"])

    def test_list_playlists_includes_collected_on_demand(self):
        refs = self._collector().list_playlists(only_created=False)
        self.assertIn("26年4月", [r.name for r in refs])

    def test_find_playlist_fuzzy(self):
        c = self._collector()
        self.assertEqual(c.find_playlist("26年10月").netease_id, "1001")
        self.assertEqual(c.find_playlist("26.10").netease_id, "1001")
        self.assertEqual(c.find_playlist("2026年10月").netease_id, "1001")
        self.assertEqual(c.find_playlist("25年4月").netease_id, "1002")

    def test_find_playlist_does_not_cross_years(self):
        c = self._collector()
        self.assertIsNone(c.find_playlist("25年10月"))
        self.assertIsNone(c.find_playlist("26年1月"))
        self.assertIsNone(c.find_playlist("27年10月"))

    def test_find_playlist_ignores_collected_only_playlist(self):
        """26年4月 是收藏的（subscribed）→ 不在创建列表里，就不该被找到。"""
        self.assertIsNone(self._collector().find_playlist("26年4月"))

    # ------------------------------------------------------------ songs

    def test_playlist_songs_from_song_index(self):
        c = self._collector()
        ref = c.find_playlist("26年10月")
        songs = c.playlist_songs(ref)
        self.assertEqual([s["name"] for s in songs], ["Escort", "Hanakage"])
        self.assertEqual(songs[0]["netease_song_id"], "501")
        self.assertEqual(songs[1]["artists"], ["Superfly"])

    def test_playlist_songs_accepts_name_and_id(self):
        c = self._collector()
        self.assertEqual(len(c.playlist_songs("25年4月")), 1)
        self.assertEqual(len(c.playlist_songs(1001)), 2)
        self.assertEqual(c.playlist_songs("不存在的歌单"), [])

    def test_playlist_songs_does_not_use_dom_by_default(self):
        """默认不读 DOM —— 当前渲染的可能是搜索结果，误读会污染去重比对。"""
        c = self._collector()
        self.assertEqual(c.playlist_songs(1004), [])
        self.assertFalse(any(e.startswith(dc.JS_MARKERS["dom"]) for e in self.fake.eval_log))

    def test_playlist_songs_dom_fallback_when_enabled(self):
        c = self._collector(allow_dom_fallback=True)
        songs = c.playlist_songs(1004)
        self.assertEqual([s["name"] for s in songs], ["DOM Song"])   # 重复行已去重
        self.assertEqual(songs[0]["artists"], ["Someone"])
        self.assertTrue(any(e.startswith(dc.JS_MARKERS["dom"]) for e in self.fake.eval_log))
        self.assertEqual(c.last_dom_context["count"], 2)

    # ------------------------------------------------------------ 错误路径

    def test_list_playlists_when_port_closed(self):
        with self.assertRaises(DesktopCDPError) as ctx:
            DesktopCDPCollector(_free_port()).list_playlists()
        self.assertIn("未监听", str(ctx.exception))

    def test_list_playlists_when_no_client_tab(self):
        self.fake.tabs.clear()
        with self.assertRaises(DesktopCDPError) as ctx:
            self._collector().list_playlists()
        self.assertIn("orpheus", str(ctx.exception))

    def test_bad_json_becomes_collector_error(self):
        self.fake.responses[dc.JS_MARKERS["state"]] = "这不是 JSON"
        with self.assertRaises(DesktopCDPError) as ctx:
            self._collector().list_playlists()
        self.assertIn("JSON", str(ctx.exception))

    def test_missing_store_becomes_collector_error(self):
        self.fake.responses[dc.JS_MARKERS["state"]] = json.dumps(
            {"__dcdp_error__": "no-store"})
        with self.assertRaises(DesktopCDPError) as ctx:
            self._collector().list_playlists()
        self.assertIn("redux store", str(ctx.exception))

    def test_js_exception_is_reported_not_raised(self):
        """health() 永远返回 HealthStatus（GUI 要按它显示状态），JS 抛错也一样。"""
        self.fake.expect_exception = True
        st = self._collector().health()
        self.assertFalse(st.ok)
        self.assertIn("前端求值失败", st.detail)

    def test_write_methods_fail_loudly_instead_of_pretending(self):
        """跨里程碑哨兵：写操作**绝不能静默成功**。

        这条替代了上一个里程碑的 `test_write_methods_are_stubs`（那时写入还是桩、
        调用即抛 NotImplementedError）。写入实现之后，它保护的语义没变 ——
        「配置不全 / 无法确认时必须显式失败」，只是失败形式换成了抛错与 False：

          · 已存在的歌单要**复用**（一个创建请求都不发）
          · 创建后回查不到 → 抛错，而不是乐观返回一个 ref
          · 写入后回查不到 → 返回 False，而不是「dispatch 没报错就算成功」
        """
        from animusic.collector import write_path as wp

        def dispatches() -> list[str]:
            return [e for e in self.fake.eval_log if e.startswith(dc.JS_MARKERS["dispatch"])]

        # 限速/回查超时都调到 0，免得这条测试为了等限速睡掉几十秒
        c = DesktopCDPCollector(
            self.fake.http_port, timeout=5.0,
            write_policy=wp.WritePolicy(rate_rps=0.0, confirm_timeout=0.0, confirm_poll=0.01))

        # ① 已存在 → 复用，且没发任何写入请求
        self.assertEqual(c.ensure_playlist("26年10月").netease_id, "1001")
        self.assertEqual(dispatches(), [], "命中已有歌单不该发任何创建请求")

        # ② 假服务器对未知表达式回 "echo:..."（不是 JSON）→ 创建那步必然失败，
        #    回查也看不到歌单出现 → 必须抛错，不能返回一个「大概建好了」的 ref
        with self.assertRaises(DesktopCDPError) as ctx:
            c.ensure_playlist("26年7月")
        self.assertIn("回查", str(ctx.exception))
        self.assertIsNotNone(c.last_write_error)

        # ③ 写入后回查不到 → False（dispatch 没报错也不行）
        self.assertFalse(c.add_to_playlist(
            "999", PlaylistRef(name="26年10月", netease_id="1001")))
        self.assertIn("回查", c.last_write_error or "")

    def test_state_is_cached_within_ttl(self):
        c = self._collector()
        c.list_playlists()
        n = len([e for e in self.fake.eval_log if e.startswith(dc.JS_MARKERS["state"])])
        c.list_playlists()
        c.dump_state()
        n2 = len([e for e in self.fake.eval_log if e.startswith(dc.JS_MARKERS["state"])])
        self.assertEqual(n, 1)
        self.assertEqual(n2, 1, "TTL 内的第二次调用不该再打一次 state")


if __name__ == "__main__":
    unittest.main(verbosity=2)
