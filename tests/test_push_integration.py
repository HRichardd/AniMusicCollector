"""推送编排 × 真实 DesktopCDPCollector 的集成测试。

**为什么单独要这个文件**：之前两边的测试各自都很好，但没人测它们之间的接缝 ——

    · `test_push.py`          用**假 collector** 测编排（26 个）
    · `test_write_path.py`    用**真 collector** 测写入（81 个）
    · **没有**任何测试把「真编排 + 真 collector」放在一起跑

而 bug 恰恰藏在接缝处：方法名对不上、返回值类型不符、
`last_write_error` 的属性名写错、`PlaylistRef` 字段名不一致……
这些在两边各自的测试里都发现不了。

这里用**假 CDP 服务器 + 真 DesktopCDPCollector + 真 Orchestrator + 真 Store**
把整条链路跑通。
"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor"), _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault(
    "ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, "data", "_test_tmp", "config")
)

from test_cdp import HAS_WS, FakeCDP  # noqa: E402
from test_desktop_cdp import _fake_state  # noqa: E402
from test_write_path import (  # noqa: E402
    _WriteResponses,
    _add_song_to_state,
    _dispatch_ok,
    _health,
)

from animusic.cdp import CDPBrowser  # noqa: E402
from animusic.collector import write_path as wp  # noqa: E402
from animusic.collector import desktop_cdp as dc  # noqa: E402
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

_TMP = os.path.join(_ROOT, "data", "_test_tmp")
PLAYLIST = "26年10月"


@unittest.skipUnless(HAS_WS, "需要 websockets")
class TestPushThroughRealCollector(unittest.TestCase):
    """整条链路：Orchestrator.push_season → DesktopCDPCollector → 假 CDP 服务器。"""

    @classmethod
    def setUpClass(cls):
        cls.fake = FakeCDP().start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        os.makedirs(_TMP, exist_ok=True)
        self.db = os.path.join(_TMP, f"pushint_{id(self)}.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)
        self.store = Store(self.db)
        self.season = Season.parse("2026Q4")
        self.sid = self.store.get_season_id(2026, 4)

        # ---- 假 CDP：state 里已有「26年10月」歌单（含 501/502 两首）
        self.state = _fake_state()
        self.fake.tabs.clear()
        self.fake.eval_log.clear()
        self.fake.expect_exception = False
        resp = _WriteResponses({
            "document.readyState": "complete",
            "document.hidden": False,
            dc.JS_MARKERS["health"]: _health(),
            dc.JS_MARKERS["state"]: "{}",
            # 默认 dispatch 不改 state —— 即「写了但没生效」
            dc.JS_MARKERS["dispatch"]: _dispatch_ok(),
        })
        resp.state = self.state
        self.responses = resp
        self.fake.responses = resp
        CDPBrowser(self.fake.http_port).open_tab("orpheus://orpheus/pub/app.html")

    def tearDown(self):
        self.store.close()
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)

    # ------------------------------------------------------------ 工具

    def _collector(self, **kw):
        # 关掉限速与回查等待，让测试快 —— 但代码路径完全不变
        kw.setdefault("use_web_playlist", False)   # 单测不联网
        return dc.DesktopCDPCollector(
            self.fake.http_port, timeout=5.0,
            write_policy=wp.WritePolicy(rate_rps=0.0, confirm_timeout=0.0,
                                        confirm_poll=0.01),
            **kw,
        )

    def _orch(self):
        return Orchestrator(store=self.store, aliases=AliasTable(),
                            progress=lambda m: None)

    def _dispatches(self):
        return [e for e in self.fake.eval_log
                if e.startswith(dc.JS_MARKERS["dispatch"])]

    def _add_song(self, title="Song", song_id="999", status=DecisionStatus.AUTO):
        aid = self.store.upsert_anime(self.sid, Anime(
            season_key="2026Q4", title_cn=f"番{title}", animethemes_slug=f"s{title}",
            season_bucket=SeasonBucket.TARGET, origin=Origin.BANGUMI,
        ))
        tid = self.store.upsert_theme(aid, Theme(
            anime_id=aid, kind=ThemeKind.OP, song_title=title, artists=["A"],
            sequence=1, source="animethemes", origin=Origin.ANIMETHEMES,
        ))
        cid = self.store.upsert_candidate(tid, Candidate(
            theme_id=tid, netease_song_id=song_id, name=title, artists=["A"],
            duration_ms=240_000, score=0.9, origin=Origin.SEARCH,
        ))
        self.store.set_decision(tid, status, cid)
        return tid

    def _make_dispatch_work(self, song_id: str, playlist_index: int = 0):
        """让 dispatch 真的把歌写进 state（模拟客户端成功）。"""
        def on_dispatch(_expr):
            _add_song_to_state(self.state, song_id, playlist_index)
            return _dispatch_ok()
        self.responses[dc.JS_MARKERS["dispatch"]] = on_dispatch

    # ------------------------------------------------------------ 接缝本身

    def test_collector_satisfies_protocol(self):
        """接口对不对得上 —— 这是最容易出问题的地方。"""
        from animusic.collector.base import PlaylistCollector

        c = self._collector()
        self.assertIsInstance(c, PlaylistCollector)
        for name in ("health", "list_playlists", "ensure_playlist", "add_to_playlist"):
            self.assertTrue(callable(getattr(c, name, None)), f"缺方法 {name}")

    def test_health_returns_healthstatus(self):
        from animusic.models import HealthStatus

        st = self._collector().health()
        self.assertIsInstance(st, HealthStatus)
        self.assertTrue(st.ok, st.detail)

    def test_find_and_ensure_return_playlistref(self):
        c = self._collector()
        found = c.find_playlist(PLAYLIST)
        self.assertIsInstance(found, PlaylistRef)
        self.assertEqual(found.netease_id, "1001")

        ref = c.ensure_playlist(PLAYLIST)
        self.assertIsInstance(ref, PlaylistRef)
        self.assertEqual(ref.netease_id, "1001")
        self.assertEqual(self._dispatches(), [], "已有歌单不该触发创建")

    # ------------------------------------------------------------ 端到端推送

    def test_push_reuses_playlist_and_adds_song(self):
        self._add_song("新歌", song_id="999")
        self._make_dispatch_work("999")

        rep = self._orch().push_season(self.season, self._collector())

        self.assertTrue(rep.ok, f"{rep.summary()} / fatal={rep.fatal}")
        self.assertFalse(rep.playlist_created, "应复用客户端里已有的歌单")
        self.assertEqual(rep.playlist_id, "1001")
        self.assertEqual(rep.pushed, 1)
        self.assertTrue(self.store.is_pushed(PLAYLIST, "999"),
                        "成功推送后应记进台账")

    def _load_track_objects(self, *song_ids: str):
        """把 `trackIds` 指向的歌曲对象补进 state。

        真实客户端只有在**打开过那个歌单**之后，store 里才会有这些歌曲对象。
        没有它们时 collector 判定「证据不足」，拒绝写入（见下一条测试）。
        """
        songs = self.state.setdefault("song", {}).setdefault("songs", {})
        for sid in song_ids:
            songs[sid] = {
                "id": int(sid), "name": f"Song {sid}", "ar": [{"id": 1, "name": "A"}],
                "al": {"id": 1, "name": "Al"}, "dt": 200000, "fee": 0,
            }

    def test_push_reports_song_already_in_playlist(self):
        """歌单里本来就有（且客户端已加载曲目）→ 报「已存在」，零 dispatch。"""
        self._add_song("老歌", song_id="501")
        self._load_track_objects("501", "502")   # 模拟用户打开过该歌单

        rep = self._orch().push_season(self.season, self._collector())

        self.assertTrue(rep.ok, rep.summary())
        self.assertEqual(rep.existed, 1, f"应报已存在，实际 {rep.summary()}")
        self.assertEqual(rep.pushed, 0)
        self.assertEqual(self._dispatches(), [],
                         "歌已在歌单里却发了写入请求 —— 幂等预检没生效")

    def test_refuses_to_write_when_track_data_incomplete(self):
        """**重要的安全行为**：store 里的曲目数据不完整时，拒绝写入。

        怎么造出「不完整」：歌单自报 `trackCount=5`，但 `trackIds` 是空的
        —— 客户端知道它有 5 首，但歌曲对象还没拉下来。
        此时「解析出 0 首」**不等于「歌单是空的」**：

            · 当成 False → 会重复加歌
            · 当成 True  → 就是假装成功

        所以正确做法是**拒绝动手**并把原因说清楚，而不是猜一个。
        """
        pl = next(p for p in self.state["playlist"]["myPlaylist"]
                  if p.get("name") == PLAYLIST)
        pl["trackCount"] = 5
        pl["trackIds"] = []
        pl.pop("tracks", None)
        self._add_song("也许已在歌单里", song_id="501")

        rep = self._orch().push_season(self.season, self._collector())

        self.assertEqual(self._dispatches(), [], "证据不足时不该盲目发写入请求")
        self.assertTrue(rep.failed, "应显式失败（而不是静默算成功）")
        self.assertFalse(self.store.is_pushed(PLAYLIST, "501"),
                         "没确认成功却记了台账")
        why = rep.failed[0].error
        self.assertTrue(why, "失败必须带原因")
        self.assertTrue(
            any(k in why for k in ("打开", "曲目", "完整", "确认", "证据")),
            f"原因里应说明判不出来的原因：{why[:200]}",
        )

    def test_membership_from_trackids_alone_is_enough(self):
        """反面：只有 `trackIds`（没有歌曲对象）也**足以**判定「在不在歌单里」。

        这是子代理实现里一个值得肯定的判断 —— 判断「在不在」只需要 id，
        歌曲对象是拿来**展示**的。所以这种情况不该拒绝写入，
        而应该正常识别为「已存在、跳过」。
        """
        self._add_song("老歌", song_id="501")
        # 刻意不补歌曲对象：state 里只有 trackIds
        rep = self._orch().push_season(self.season, self._collector())

        self.assertTrue(rep.ok, f"trackIds 足以判定存在性：{rep.summary()}")
        self.assertEqual(rep.existed, 1)
        self.assertEqual(self._dispatches(), [])

    def test_second_push_is_fully_idempotent(self):
        """**集成层面的幂等**：跑两遍，第二遍零 dispatch。"""
        self._add_song("新歌", song_id="999")
        self._make_dispatch_work("999")
        orch = self._orch()

        r1 = orch.push_season(self.season, self._collector())
        self.assertEqual(r1.pushed, 1)
        n_after_first = len(self._dispatches())

        r2 = orch.push_season(self.season, self._collector())
        self.assertEqual(r2.pushed, 0, "第二遍不该报新推送")
        self.assertEqual(r2.existed, 1)
        self.assertEqual(len(self._dispatches()), n_after_first,
                         "第二遍发了额外的写入请求")

    def test_failed_push_not_recorded(self):
        """dispatch 说成功、但 state 没变（假客户端真没加进去）→ 必须判失败。"""
        self._add_song("加不进去的歌", song_id="999")
        # dispatch 不改 state（setUp 的默认行为）
        rep = self._orch().push_season(self.season, self._collector())

        self.assertEqual(len(rep.failed), 1, f"应判失败，实际 {rep.summary()}")
        self.assertFalse(self.store.is_pushed(PLAYLIST, "999"),
                         "失败被记进台账 —— 下次会被跳过，那首歌永远进不去")
        self.assertTrue(rep.failed[0].error, "失败必须带原因")

    def test_partial_failure_continues(self):
        """一首失败不影响其它。"""
        self._add_song("好歌", song_id="998")
        self._add_song("坏歌", song_id="999")

        def on_dispatch(expr):
            if "998" in expr:
                _add_song_to_state(self.state, "998")
                return _dispatch_ok()
            return _dispatch_ok()      # 999：说成功但不改 state

        self.responses[dc.JS_MARKERS["dispatch"]] = on_dispatch
        rep = self._orch().push_season(self.season, self._collector())

        self.assertEqual(rep.pushed, 1)
        self.assertEqual(len(rep.failed), 1)
        self.assertTrue(self.store.is_pushed(PLAYLIST, "998"))
        self.assertFalse(self.store.is_pushed(PLAYLIST, "999"))

    def test_client_down_is_fatal_and_writes_nothing(self):
        self._add_song("歌", song_id="999")
        # 把客户端「关掉」：清掉标签页
        self.fake.tabs.clear()

        rep = self._orch().push_season(self.season, self._collector())
        self.assertTrue(rep.fatal, "客户端不可用必须整体中止")
        self.assertEqual(self._dispatches(), [])
        self.assertFalse(self.store.is_pushed(PLAYLIST, "999"))

    def test_dry_run_through_real_collector_writes_nothing(self):
        self._add_song("歌", song_id="999")
        before = copy.deepcopy(self.state)
        rep = self._orch().push_season(self.season, self._collector(), dry_run=True)

        self.assertTrue(rep.dry_run)
        self.assertEqual(self._dispatches(), [])
        self.assertEqual(self.state, before, "试运行改了客户端状态")
        self.assertFalse(self.store.is_pushed(PLAYLIST, "999"))

    def test_creates_playlist_when_absent(self):
        """目标歌单不在客户端里 → 走创建流程。

        创建候选全是推测，假服务器默认让它「创建后回查不到」，
        所以这里断言的是**失败要显式报出来**，而不是假装成功。
        """
        self._add_song("歌", song_id="999")
        for pl in self.state["playlist"]["myPlaylist"]:
            if pl.get("name") == PLAYLIST:
                pl["name"] = "改个名，让它找不到"

        rep = self._orch().push_season(self.season, self._collector())
        self.assertTrue(rep.fatal, "创建后回查不到时必须显式失败，不能假装成功")
        self.assertIn("歌单", rep.fatal)
        self.assertFalse(self.store.is_pushed(PLAYLIST, "999"))

    def test_nonexistent_song_is_reported_not_silently_ok(self):
        """歌单里没有、也加不进去的歌，绝不能悄悄算成功。"""
        self._add_song("幽灵歌", song_id="777777")
        rep = self._orch().push_season(self.season, self._collector())
        self.assertEqual(rep.pushed, 0)
        self.assertFalse(self.store.is_pushed(PLAYLIST, "777777"))

    def test_describe_push_after_real_run(self):
        """报告能正常渲染（接缝处字段名对不上会在这里炸）。"""
        self._add_song("新歌", song_id="999")
        self._make_dispatch_work("999")
        orch = self._orch()
        rep = orch.push_season(self.season, self._collector())
        lines = orch.describe_push(rep)
        text = "\n".join(lines)
        self.assertIn(PLAYLIST, text)
        self.assertIn("复用已有", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
