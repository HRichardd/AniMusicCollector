"""`playlist_songs` / 幂等预检的 **web API 路径**（假 CDP + 假 web 读取器）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
这个文件存在的理由，是一个**实测事实**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
客户端 store 里的歌单对象**没有曲目数组**（实测 129 + 179 个全都没有，只有
一个 `trackCount` 数字）。所以在接上 web API 之前：

    · `playlist_songs()` 恒返回 `[]`
    · `check_membership()` 恒「证据不足」
    · `add_to_playlist()` 的幂等预检在 `require_complete_membership=True` 下
      **恒定拒绝写入** —— 一个写入请求都发不出去

`test_web_api_unblocks_the_idempotency_precheck` 和
`test_web_api_makes_the_precheck_idempotent` 就是这个断言的正面/反面：
前者证明「判得出不在」时**允许写**，后者证明「判得出在」时**不许重复写**。
两条都必须过 —— 只过一条说明幂等坏了。

⚠️ 这里用假 web 读取器，**不联网**；真实解析在 test_api_playlist.py。
⚠️ 这里测的是「读取来源的选择与判定」，**不是**「payload 形状对了」。
"""

from __future__ import annotations

import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from animusic.cdp import CDPBrowser  # noqa: E402
from animusic.collector import api_playlist as ap  # noqa: E402
from animusic.collector import desktop_cdp as dc  # noqa: E402
from animusic.collector import write_path as wp  # noqa: E402
from animusic.models import (  # noqa: E402
    Anime,
    Candidate,
    DecisionStatus,
    Origin,
    PlaylistRef,
    Season,
    SeasonBucket,
    Theme,
    ThemeKind,
)
from animusic.normalize import AliasTable  # noqa: E402
from animusic.orchestrator import Orchestrator  # noqa: E402
from animusic.store import Store  # noqa: E402
from test_cdp import HAS_WS, FakeCDP  # noqa: E402
from test_desktop_cdp import _fake_state  # noqa: E402
from test_write_path import (  # noqa: E402
    _WriteResponses, _add_song_to_state, _dispatch_ok, _health,
)

_TMP = os.path.join(_ROOT, "data", "_test_tmp")


# ---------------------------------------------------------------- 假 web 读取器

def _snap(pid, *, ids, count=None, songs=None) -> ap.PlaylistSnapshot:
    """造一个快照。`count=None` → 自报 trackCount = len(ids)（完整）。"""
    return ap.PlaylistSnapshot(
        playlist_id=str(pid),
        track_ids=[str(i) for i in ids],
        songs=list(songs or []),
        declared_count=(len(ids) if count is None else count),
        has_track_ids=True,
    )


def _web_song(sid: str, name: str, artist: str = "A") -> dict:
    return ap.normalize_web_song(
        {"id": int(sid), "name": name, "artists": [{"id": 1, "name": artist}],
         "album": {"id": 2, "name": "Alb"}, "duration": 200000, "fee": 0, "alias": []})


class FakeWebPlaylist:
    """假的歌单 web 读取器。与真读取器同形，抛同样的异常类型。

    `data` 是**活的 dict**（`pid` → `PlaylistSnapshot` 或 `Exception`），
    所以测试可以在 dispatch 的副作用里改它，模拟「服务端真的把歌写进去了」
    —— 而 store 仍然一无所知（那正是真实客户端常见的样子）。
    """

    def __init__(self, data: dict | None = None):
        self.data: dict = {} if data is None else data
        self.calls: list[tuple[str, str]] = []
        self.last_error: str | None = None

    def _lookup(self, pid):
        pid = str(pid)
        v = self.data.get(pid)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise ap.PlaylistNotFound(f"fake web：id={pid} 不存在")
        return v

    def snapshot(self, pid) -> ap.PlaylistSnapshot:
        self.calls.append(("snapshot", str(pid)))
        return self._lookup(pid)

    def songs(self, pid) -> ap.PlaylistSnapshot:
        self.calls.append(("songs", str(pid)))
        return self._lookup(pid)

    def song_details(self, song_ids) -> list[dict]:
        """喂给 `_song_resource()` 用（实测的 splice payload 要完整歌曲对象）。"""
        self.calls.append(("song_details", ",".join(str(x) for x in song_ids)))
        out = []
        for sid in song_ids:
            out.append(_web_song(str(sid), f"Song {sid}"))
        return out

    @property
    def n_calls(self) -> int:
        return len(self.calls)


# ══════════════════════════════════════════════════════════════ 读取来源

@unittest.skipUnless(HAS_WS, "需要 websockets")
class TestPlaylistSongsSource(unittest.TestCase):
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
        resp = _WriteResponses({
            "document.readyState": "complete",
            "document.hidden": False,
            dc.JS_MARKERS["health"]: _health(),
            dc.JS_MARKERS["state"]: "{}",
        })
        resp.state = self.state
        self.responses = resp
        self.fake.responses = resp
        CDPBrowser(self.fake.http_port).open_tab("orpheus://orpheus/pub/app.html")
        self.web = FakeWebPlaylist()
        # 1001「26年10月」在假 store 里已有 501/502（Escort / Hanakage）
        self.web.data["1001"] = _snap("1001", ids=[501, 502],
                                      songs=[_web_song("501", "WEB Escort", "WEB A"),
                                             _web_song("502", "WEB Hanakage", "WEB B")])

    def _collector(self, **kw) -> dc.DesktopCDPCollector:
        kw.setdefault("write_policy", wp.WritePolicy(
            rate_rps=0.0, confirm_timeout=0.0, confirm_poll=0.01))
        kw.setdefault("web_playlist", self.web)
        return dc.DesktopCDPCollector(self.fake.http_port, timeout=5.0, **kw)

    def _ref(self, name="26年10月", pid="1001") -> PlaylistRef:
        return PlaylistRef(name=name, netease_id=pid)

    # ------------------------------------------------------------ 选择

    def test_web_api_wins_over_store_for_song_names(self):
        """web 有名字就用 web —— store 那份是「恰好还留着」的缓存，不是真相。"""
        c = self._collector()
        songs = c.playlist_songs(self._ref())
        self.assertEqual([s["name"] for s in songs], ["WEB Escort", "WEB Hanakage"])
        self.assertEqual(c.last_songs_source, "web:1001")
        self.assertIsNone(c.last_web_error)
        self.assertIn(("songs", "1001"), self.web.calls)

    def test_falls_back_to_store_when_web_fails(self):
        self.web.data.clear()
        c = self._collector()
        songs = c.playlist_songs(self._ref())
        self.assertEqual([s["name"] for s in songs], ["Escort", "Hanakage"])
        self.assertEqual(c.last_songs_source, "store")
        self.assertIn("不存在", c.last_web_error or "")

    def test_source_web_never_falls_back_to_store(self):
        """钉死来源时不偷偷降级 —— 否则「这条路通不通」永远测不出来。"""
        self.web.data.clear()
        c = self._collector()
        self.assertEqual(c.playlist_songs(self._ref(), source="web"), [])
        self.assertIn("不存在", c.last_web_error or "")

    def test_source_store_never_touches_the_network(self):
        c = self._collector()
        songs = c.playlist_songs(self._ref(), source="store")
        self.assertEqual([s["name"] for s in songs], ["Escort", "Hanakage"])
        self.assertEqual(c.last_songs_source, "store")
        self.assertEqual(self.web.calls, [], "source='store' 不该碰 web")

    def test_name_is_resolved_to_an_id_for_the_web_api(self):
        """web API 只认 id：给名字时必须先在 store 里把 id 换出来。"""
        c = self._collector()
        songs = c.playlist_songs("26年10月")
        self.assertEqual(songs[0]["name"], "WEB Escort")
        self.assertIn(("songs", "1001"), self.web.calls)

    def test_digits_string_is_an_id_not_a_name(self):
        """**真机踩到的坑**：id 从命令行/日志抄进来必然是字符串。

        真机 id 长这样：`17977808146`。当成名字去找 → 找不到 → 静默返回 `[]`，
        看起来像「歌单是空的」而不是「你参数传错了」。
        """
        c = self._collector()
        songs = c.playlist_songs("1001")
        self.assertEqual([s["name"] for s in songs], ["WEB Escort", "WEB Hanakage"])
        self.assertIn(("songs", "1001"), self.web.calls)
        self.assertEqual(c.last_songs_source, "web:1001")

    def test_digits_string_does_not_go_through_name_matching(self):
        """纯数字串不该去 store 里做模糊匹配（那会给出一堆 0 分）。"""
        c = self._collector()
        self.assertEqual(c._lookup_playlist_id(" 1001 "), "1001")
        self.assertEqual(c._lookup_playlist_id("17977808146"), "17977808146")
        self.assertEqual(c._lookup_playlist_id("26年10月"), "1001")
        self.assertIsNone(c._lookup_playlist_id("不存在的歌单"))
        self.assertIsNone(c._lookup_playlist_id("   "))

    def test_bool_is_not_an_id(self):
        """`True` 是 int 的子类，别让它变成 id="1"。"""
        c = self._collector()
        self.assertEqual(c.playlist_songs(True), [])
        self.assertIn("拿不到歌单", c.last_web_error or "")

    def test_unresolvable_name_records_why_and_falls_back(self):
        c = self._collector()
        songs = c.playlist_songs("不存在的歌单")
        self.assertEqual(songs, [])
        self.assertIn("拿不到歌单", c.last_web_error or "")
        self.assertEqual(self.web.calls, [], "名字换不出 id 就不该去问 web")

    def test_web_unavailable_entirely_is_clean(self):
        """`use_web_playlist=False` → 完全走老路，一次网络调用都没有。"""
        c = self._collector(use_web_playlist=False)
        self.assertIsNone(c.web_playlist)
        songs = c.playlist_songs(self._ref())
        self.assertEqual([s["name"] for s in songs], ["Escort", "Hanakage"])
        self.assertEqual(self.web.calls, [])


# ══════════════════════════════════════════════════════════ 幂等预检（核心）

@unittest.skipUnless(HAS_WS, "需要 websockets")
class TestIdempotencyPrecheckViaWeb(unittest.TestCase):
    """**item 3 要解的问题就在这个类里。**

    歌单 1003「26年4月」在假 store 里是 `trackCount=1` 且**没有任何曲目条目**
    —— 正好等于真实客户端的样子。所以：

        store 这条路：`check_membership` → None（证据不足）→ 加 web 之前必拒写
        web 这条路：`trackIds` 完整 → 能给出确定的「在」或「不在」
    """

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
        resp = _WriteResponses({
            "document.readyState": "complete",
            "document.hidden": False,
            dc.JS_MARKERS["health"]: _health(),
            dc.JS_MARKERS["state"]: "{}",
        })
        resp.state = self.state
        self.responses = resp
        self.fake.responses = resp
        CDPBrowser(self.fake.http_port).open_tab("orpheus://orpheus/pub/app.html")
        self.web = FakeWebPlaylist()
        self.web.data["1003"] = _snap("1003", ids=["111"])

    def _collector(self, **kw) -> dc.DesktopCDPCollector:
        kw.setdefault("write_policy", wp.WritePolicy(
            rate_rps=0.0, confirm_timeout=0.0, confirm_poll=0.01))
        kw.setdefault("web_playlist", self.web)
        return dc.DesktopCDPCollector(self.fake.http_port, timeout=5.0, **kw)

    def _ref(self) -> PlaylistRef:
        return PlaylistRef(name="26年4月", netease_id="1003")

    def _dispatches(self) -> list[str]:
        return [e for e in self.fake.eval_log if e.startswith(dc.JS_MARKERS["dispatch"])]

    # ------------------------------------------------------------ 基线

    def test_without_web_the_precheck_refuses_to_write(self):
        """**加 web API 之前的样子**：唯一的曲目来源是 store，而 store 没有曲目。

        这条断言把「旧行为」钉住，这样下面的对比才有意义 ——
        如果哪天它变成 True，说明有别的东西动了 store 那条路。
        """
        c = self._collector(use_web_playlist=False)
        self.assertFalse(c.add_to_playlist("999", self._ref()))
        self.assertEqual(self._dispatches(), [], "证据不足时一个写入请求都不该发")
        self.assertEqual(c.last_write_action, dc.WRITE_ACTION_FAILED)
        self.assertIn("无法确认", c.last_write_error or "")

    # ------------------------------------------------------------ 正面：允许写

    def test_web_api_unblocks_the_idempotency_precheck(self):
        """只存在于 web 的「判得出不在」→ 预检放行 → 真的 dispatch → 回查确认。

        dispatch 后**只改 web 那份数据**，store 一直一无所知：
        这样测的是「web 能兜住 store 的空白」，而不是「store 又碰巧知道了」。
        """
        def on_dispatch(expr):
            # 模拟服务端接受了这次写入（客户端 store 不更新）
            self.web.data["1003"] = _snap("1003", ids=["111", "999"])
            return _dispatch_ok()

        self.responses[dc.JS_MARKERS["dispatch"]] = on_dispatch
        c = self._collector()
        self.assertTrue(c.add_to_playlist("999", self._ref()),
                        f"web 说得出「不在」就该允许写：{c.last_write_error}")
        self.assertEqual(c.last_write_action, dc.WRITE_ACTION_ADDED)
        self.assertEqual(len(self._dispatches()), 1, "回查确认后就该停，不要盲试 7 条候选")
        self.assertIn("回查", c.last_write_note or "")

    # ------------------------------------------------------------ 反面：不许重复写

    def test_web_api_makes_the_precheck_idempotent(self):
        """web 说「已经在里面」→ 返回 True 但**零 dispatch**。

        这是重复推送的防线：判成「不在」会重复加歌，判成「在」才对。
        """
        self.web.data["1003"] = _snap("1003", ids=["999"])
        c = self._collector()
        self.assertTrue(c.add_to_playlist("999", self._ref()))
        self.assertEqual(c.last_write_action, dc.WRITE_ACTION_SKIPPED)
        self.assertEqual(self._dispatches(), [], "已经在歌单里时一个请求都不该发")
        self.assertIn("幂等", c.last_write_note or "")

    def test_ids_alone_are_enough_for_membership(self):
        """只要 id 表就够判「在不在」—— 歌名是拿来**展示**的。"""
        self.web.data["1003"] = _snap("1003", ids=["999"], songs=[])
        c = self._collector()
        self.assertTrue(c.add_to_playlist("999", self._ref()))
        self.assertEqual(c.last_write_action, dc.WRITE_ACTION_SKIPPED)
        self.assertEqual(self._dispatches(), [])

    # ------------------------------------------------------------ 安全边界

    def test_both_sources_inconclusive_still_refuses(self):
        """web 的表**不完整**（只给 1 个 id，自报 5 首）→ 仍然证据不足 → 拒写。

        这条是「宁可漏推，不可重复推」的执行者。
        """
        self.web.data["1003"] = _snap("1003", ids=["111"], count=5)
        c = self._collector()
        self.assertFalse(c.add_to_playlist("999", self._ref()))
        self.assertEqual(self._dispatches(), [])
        err = c.last_write_error or ""
        self.assertIn("store 与 web API 都判不出来", err)
        self.assertIn("store", err)
        self.assertIn("web API", err)

    def test_playlist_missing_on_the_server_is_not_treated_as_absent(self):
        """`code=404` 是「歌单没了」，**不是**「歌不在里面」。当成后者就会重复加歌。"""
        self.web.data["1003"] = ap.PlaylistNotFound("歌单不存在")
        c = self._collector()
        self.assertFalse(c.add_to_playlist("999", self._ref()))
        self.assertEqual(self._dispatches(), [])
        err = c.last_write_error or ""
        self.assertIn("不存在", err)
        self.assertNotIn("已经有", err)

    def test_web_readable_empty_playlist_allows_the_first_write(self):
        """真空歌单（trackIds=[] + trackCount=0）→ 确定不在 → 允许写第一首。"""
        def on_dispatch(expr):
            self.web.data["1003"] = _snap("1003", ids=["999"])
            return _dispatch_ok()

        self.web.data["1003"] = _snap("1003", ids=[])
        self.responses[dc.JS_MARKERS["dispatch"]] = on_dispatch
        c = self._collector()
        self.assertTrue(c.add_to_playlist("999", self._ref()))
        self.assertEqual(c.last_write_action, dc.WRITE_ACTION_ADDED)

    def test_web_error_message_is_surfaced_when_it_is_the_only_source(self):
        self.web.data["1003"] = ap.WebPlaylistError("网络请求失败 —— 连接超时")
        c = self._collector()
        self.assertFalse(c.add_to_playlist("999", self._ref()))
        self.assertIn("连接超时", c.last_write_error or "")


# ══════════════════════════════════════════════════════════ 来源优先级

@unittest.skipUnless(HAS_WS, "需要 websockets")
class TestMembershipSourcePriority(unittest.TestCase):
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
        resp = _WriteResponses({
            "document.readyState": "complete",
            "document.hidden": False,
            dc.JS_MARKERS["health"]: _health(),
            dc.JS_MARKERS["state"]: "{}",
        })
        resp.state = self.state
        self.responses = resp
        self.fake.responses = resp
        CDPBrowser(self.fake.http_port).open_tab("orpheus://orpheus/pub/app.html")
        self.web = FakeWebPlaylist()

    def _collector(self, **kw) -> dc.DesktopCDPCollector:
        kw.setdefault("write_policy", wp.WritePolicy(
            rate_rps=0.0, confirm_timeout=0.0, confirm_poll=0.01))
        kw.setdefault("web_playlist", self.web)
        return dc.DesktopCDPCollector(self.fake.http_port, timeout=5.0, **kw)

    def test_store_answer_short_circuits_the_web_api(self):
        """store 能给出确定答案时**不联网** —— 省掉一整轮请求与风控面。

        歌单 1002「25.04」在假 store 里带完整 `tracks`（歌曲 601）。
        """
        c = self._collector()
        m = c._membership(PlaylistRef(name="25.04", netease_id="1002"), "601", refresh=False)
        self.assertIs(m.present, True)
        self.assertEqual(self.web.calls, [], "store 已经答了，不该再去问 web")
        self.assertEqual(c.last_web_error, None)

    def test_definite_web_answer_beats_store_unknown(self):
        c = self._collector()   # 1001: store 有 trackIds → store 其实能答
        self.web.data["9999"] = _snap("9999", ids=["1", "2"])
        m = c._membership(PlaylistRef(name="别的", netease_id="9999"), "2", refresh=False)
        self.assertIs(m.present, True)
        self.assertIn(("snapshot", "9999"), self.web.calls)

    def test_playlist_absent_from_store_still_consults_the_web(self):
        """store 里压根没有这个歌单（用户刚在别处建的）→ 仍该去 web 问一次。"""
        self.web.data["7777"] = _snap("7777", ids=["999"])
        c = self._collector()
        m = c._membership(PlaylistRef(name="26年7月", netease_id="7777"), "999", refresh=False)
        self.assertIs(m.present, True)
        self.assertIn(("snapshot", "7777"), self.web.calls)

    def test_diagnostics_are_populated(self):
        self.web.data["7777"] = _snap("7777", ids=["1"])
        c = self._collector()
        c._membership(PlaylistRef(name="26年7月", netease_id="7777"), "999", refresh=False)
        self.assertIsNone(c.last_web_error)
        self.assertEqual(c.web_playlist.data["7777"].declared_count, 1)


# ══════════════════════════════════════════════════════════ 端到端（编排层）

@unittest.skipUnless(HAS_WS, "需要 websockets")
class TestPushSeasonWithWebMembership(unittest.TestCase):
    """跑一遍真正的 `push_season`，只把「曲目从哪读」换成 web。

    前面的类测的是 collector 的一个方法；这里测的是**编排层看到的最终结果**
    （`pushed` / `existed` / `failed` 的计数与台账）—— 「报告说推了一首、
    实际一封都没发」那类 bug 只在编排层才看得见。

    场景把假 store 改成**真机的样子**：歌单「26年10月」只有 `trackCount`，
    `trackIds` 被摘掉。于是 store 恒「证据不足」，唯一的信息来源是 web。
    """

    PLAYLIST = "26年10月"
    PID = "1001"

    @classmethod
    def setUpClass(cls):
        cls.fake = FakeCDP().start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        os.makedirs(_TMP, exist_ok=True)
        self.db = os.path.join(_TMP, f"webpush_{id(self)}.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)
        self.store = Store(self.db)
        self.season = Season.parse("2026Q4")
        self.sid = self.store.get_season_id(2026, 4)

        self.state = _fake_state()
        # ★ 关键：把 trackIds 摘掉，让 store 这条路恒「证据不足」——
        #   这正是真实客户端 store 的样子（实测 129+179 个歌单全都没有曲目数组）。
        pl = self.state["playlist"]["myPlaylist"][0]
        self.assertEqual(str(pl["id"]), self.PID)
        pl.pop("trackIds", None)
        pl["trackCount"] = 2

        self.fake.tabs.clear()
        self.fake.eval_log.clear()
        self.fake.expect_exception = False
        resp = _WriteResponses({
            "document.readyState": "complete",
            "document.hidden": False,
            dc.JS_MARKERS["health"]: _health(),
            dc.JS_MARKERS["state"]: "{}",
            dc.JS_MARKERS["dispatch"]: _dispatch_ok(),
        })
        resp.state = self.state
        self.responses = resp
        self.fake.responses = resp
        CDPBrowser(self.fake.http_port).open_tab("orpheus://orpheus/pub/app.html")

        self.web = FakeWebPlaylist({self.PID: _snap(self.PID, ids=["501", "502"])})

    def tearDown(self):
        self.store.close()
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                try:
                    os.remove(self.db + ext)
                except OSError:
                    pass

    # ------------------------------------------------------------ 工具

    def _collector(self) -> dc.DesktopCDPCollector:
        return dc.DesktopCDPCollector(
            self.fake.http_port, timeout=5.0, web_playlist=self.web,
            write_policy=wp.WritePolicy(rate_rps=0.0, confirm_timeout=0.0,
                                        confirm_poll=0.01))

    def _orch(self) -> Orchestrator:
        return Orchestrator(store=self.store, aliases=AliasTable(),
                            progress=lambda m: None)

    def _dispatches(self) -> list[str]:
        return [e for e in self.fake.eval_log
                if e.startswith(dc.JS_MARKERS["dispatch"])]

    def _add_song(self, title="Song", song_id="999",
                  status=DecisionStatus.AUTO) -> int:
        aid = self.store.upsert_anime(self.sid, Anime(
            season_key="2026Q4", title_cn=f"番{title}", animethemes_slug=f"s{title}",
            season_bucket=SeasonBucket.TARGET, origin=Origin.BANGUMI,
        ))
        tid = self.store.upsert_theme(aid, Theme(
            anime_id=aid, kind=ThemeKind.OP, song_title=title, artists=["A"],
            sequence=1, source="wikipedia", origin=Origin.BANGUMI,
        ))
        cid = self.store.upsert_candidate(tid, Candidate(
            theme_id=tid, netease_song_id=song_id, name=title, artists=["A"],
            duration_ms=240_000, score=0.9, origin=Origin.SEARCH,
        ))
        self.store.set_decision(tid, status, cid)
        return tid

    # ------------------------------------------------------------ 断言

    def test_store_alone_cannot_decide_here(self):
        """先把「这个场景里 store 确实判不出来」钉住，否则下面两条没有意义。"""
        from animusic.collector.desktop_cdp import DesktopCDPCollector

        c = DesktopCDPCollector(self.fake.http_port, timeout=5.0,
                                use_web_playlist=False,
                                write_policy=wp.WritePolicy(
                                    rate_rps=0.0, confirm_timeout=0.0,
                                    confirm_poll=0.01))
        m = c._membership(PlaylistRef(name=self.PLAYLIST, netease_id=self.PID),
                          "501", refresh=False)
        self.assertIsNone(m.present, f"这个场景 store 本该判不出来：{m.reason}")

    def test_push_season_skips_songs_the_web_says_are_already_there(self):
        """web 说歌已经在歌单里 → 算「已存在」，**零 dispatch**。

        这正是加 web API 之前做不到的事：预检恒拒绝，整条推送全是 failed。
        """
        self._add_song("老歌", song_id="501")
        rep = self._orch().push_season(self.season, self._collector())
        self.assertTrue(rep.ok, rep.summary())
        self.assertEqual(rep.existed, 1, f"该算 existed：{rep.summary()}")
        self.assertEqual(rep.pushed, 0)
        self.assertEqual(rep.failed, [])
        self.assertEqual(self._dispatches(), [], "已存在时不该发写入请求")
        self.assertTrue(self.web.calls, "这个结论只能来自 web API")

    def test_push_season_writes_a_song_the_web_says_is_absent(self):
        """web 的表完整且里面没有这首歌 → 预检放行 → 真的 dispatch → 记台账。"""
        def on_dispatch(_expr):
            # 服务端接受了；store 依旧一无所知（真实客户端常见）
            self.web.data[self.PID] = _snap(self.PID, ids=["501", "502", "999"])
            return _dispatch_ok()

        self.responses[dc.JS_MARKERS["dispatch"]] = on_dispatch
        self._add_song("新歌", song_id="999")

        rep = self._orch().push_season(self.season, self._collector())
        self.assertTrue(rep.ok, rep.summary())
        self.assertEqual(rep.pushed, 1, f"web 说不在就该真推：{rep.summary()}")
        self.assertEqual(rep.existed, 0)
        self.assertEqual(len(self._dispatches()), 1, "回查确认后应立刻停")
        self.assertTrue(self.store.is_pushed(self.PLAYLIST, "999"),
                        "确认成功之后才记台账")

    def test_second_push_is_still_idempotent_through_the_web(self):
        """连推两遍：第二遍必须零 dispatch（web 现在说它在了）。"""
        def on_dispatch(_expr):
            self.web.data[self.PID] = _snap(self.PID, ids=["501", "502", "999"])
            return _dispatch_ok()

        self.responses[dc.JS_MARKERS["dispatch"]] = on_dispatch
        self._add_song("新歌", song_id="999")
        orch = self._orch()

        r1 = orch.push_season(self.season, self._collector())
        self.assertEqual(r1.pushed, 1)
        n_after_first = len(self._dispatches())

        r2 = orch.push_season(self.season, self._collector())
        self.assertEqual(r2.existed, 1, f"第二遍该跳过：{r2.summary()}")
        self.assertEqual(r2.pushed, 0)
        self.assertEqual(len(self._dispatches()), n_after_first,
                         "第二遍一个写入请求都不该有")


if __name__ == "__main__":
    unittest.main(verbosity=2)
