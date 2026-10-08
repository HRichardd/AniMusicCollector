"""推送到歌单的编排测试。

这是整个工具里**唯一会改动用户账号**的路径，所以行为必须钉死：

    ① 试运行绝不写入
    ② 幂等 —— 重复跑不会重复加歌，且要区分「本次新推」与「早就在了」
    ③ 失败必须逐条报出原因，且 **不能** 记进台账（否则下次会被当成功跳过）
    ④ 单首失败不能中断整批
    ⑤ 客户端连不上就整体中止，不要逐首试

全部离线：collector 是假的。
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

# ⚠️ 必须在 import animusic 之前设置（别名自学习会回写配置目录）
os.environ.setdefault(
    "ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, "data", "_test_tmp", "config")
)

from animusic.models import (  # noqa: E402
    Anime,
    Candidate,
    DecisionStatus,
    HealthStatus,
    Origin,
    PlaylistRef,
    Season,
    SeasonBucket,
    Theme,
    ThemeKind,
)
from animusic.normalize import AliasTable  # noqa: E402
from animusic.orchestrator import Orchestrator, PushReport  # noqa: E402
from animusic.store import Store  # noqa: E402

_TMP = os.path.join(_ROOT, "data", "_test_tmp")


class FakePlaylistCollector:
    """内存歌单 —— 可指定哪些歌写入失败、哪些歌单已存在。"""

    def __init__(self, *, fail_ids=(), existing_playlists=(), create_ok=True,
                 health_ok=True, add_exception=None):
        self.songs: set[str] = set()
        self.playlists: dict[str, PlaylistRef] = {
            n: PlaylistRef(name=n, netease_id=str(1000 + i))
            for i, n in enumerate(existing_playlists)
        }
        self.fail_ids = set(fail_ids)
        self.create_ok = create_ok
        self.health_ok = health_ok
        self.add_exception = add_exception
        self.calls: list[str] = []
        self.created: list[str] = []
        self.last_write_note = ""
        self.last_write_error = ""
        #: 真实 collector 会报告「本次实际做了什么」的结构化标记。
        #: 编排层必须读它，而不是读自己的台账（见 test_pushed_vs_existed_*）。
        self.last_write_action = None
        self._next_id = 2000

    def health(self) -> HealthStatus:
        return HealthStatus(self.health_ok, "fake" if self.health_ok else "端口未监听（测试桩）")

    def list_playlists(self):
        return list(self.playlists.values())

    def find_playlist(self, name: str):
        return self.playlists.get(name)

    def ensure_playlist(self, name: str) -> PlaylistRef:
        self.calls.append(f"ensure:{name}")
        if name in self.playlists:
            self.last_write_note = "复用已有歌单"
            self.last_write_action = "reused"
            return self.playlists[name]
        if not self.create_ok:
            raise RuntimeError("模拟创建歌单失败")
        self._next_id += 1
        ref = PlaylistRef(name=name, netease_id=str(self._next_id))
        self.playlists[name] = ref
        self.created.append(name)
        self.last_write_note = "已创建歌单"
        self.last_write_action = "created"
        return ref

    def add_to_playlist(self, song_id: str, playlist: PlaylistRef) -> bool:
        self.calls.append(f"add:{song_id}")
        self.last_write_note = ""
        self.last_write_error = ""
        self.last_write_action = None
        if self.add_exception is not None:
            raise self.add_exception
        if song_id in self.songs:
            self.last_write_note = "已经在歌单里 —— 跳过"
            self.last_write_action = "skipped"
            return True
        if song_id in self.fail_ids:
            self.last_write_error = f"回查失败：写入 {song_id} 后歌单里没有它"
            self.last_write_action = "failed"
            return False
        self.songs.add(song_id)
        self.last_write_note = "已加入（回查确认）"
        self.last_write_action = "added"
        return True


class PushTestBase(unittest.TestCase):
    def setUp(self):
        os.makedirs(_TMP, exist_ok=True)
        self.db = os.path.join(_TMP, f"push_{id(self)}.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)
        self.store = Store(self.db)
        self.season = Season.parse("2026Q4")
        self.sid = self.store.get_season_id(2026, 4)
        self.table = AliasTable()

    def tearDown(self):
        self.store.close()
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)

    def add_song(self, title="Song", artist="Artist", song_id="1", *,
                 status=DecisionStatus.AUTO, with_candidate=True, anime="番A"):
        aid = self.store.upsert_anime(self.sid, Anime(
            season_key="2026Q4", title_cn=anime, animethemes_slug=f"s{title}",
            season_bucket=SeasonBucket.TARGET, origin=Origin.BANGUMI,
        ))
        tid = self.store.upsert_theme(aid, Theme(
            anime_id=aid, kind=ThemeKind.OP, song_title=title, artists=[artist],
            sequence=1, source="animethemes", origin=Origin.ANIMETHEMES,
        ))
        cid = None
        if with_candidate:
            cid = self.store.upsert_candidate(tid, Candidate(
                theme_id=tid, netease_song_id=song_id, name=title, artists=[artist],
                duration_ms=240_000, score=0.9, origin=Origin.SEARCH,
            ))
        self.store.set_decision(tid, status, cid)
        return tid

    def orch(self):
        return Orchestrator(store=self.store, aliases=self.table,
                            progress=lambda m: None)


# ==================================================================== 可推送集合

class TestPushable(PushTestBase):
    def test_only_auto_and_confirmed(self):
        self.add_song("A", song_id="1", status=DecisionStatus.AUTO)
        self.add_song("B", song_id="2", status=DecisionStatus.CONFIRMED)
        self.add_song("C", song_id="3", status=DecisionStatus.PENDING_REVIEW)
        self.add_song("D", song_id="4", status=DecisionStatus.MISSING_MATCH)
        got = {x["song_id"] for x in self.orch().pushable(self.season)}
        self.assertEqual(got, {"1", "2"}, "只该推自动通过和人工确认的")

    def test_skips_without_candidate(self):
        self.add_song("A", song_id="1", status=DecisionStatus.AUTO, with_candidate=False)
        self.assertEqual(self.orch().pushable(self.season), [])

    def test_skips_empty_song_id(self):
        self.add_song("A", song_id="", status=DecisionStatus.AUTO)
        self.assertEqual(self.orch().pushable(self.season), [],
                         "空 song_id 不该进推送队列")

    def test_row_has_needed_fields(self):
        self.add_song("A", artist="Artist", song_id="42")
        row = self.orch().pushable(self.season)[0]
        for k in ("theme_id", "anime", "kind", "target_title", "song_id", "name"):
            self.assertIn(k, row)


# ==================================================================== 试运行

class TestDryRun(PushTestBase):
    def test_dry_run_writes_nothing(self):
        self.add_song("A", song_id="1")
        col = FakePlaylistCollector()
        rep = self.orch().push_season(self.season, col, dry_run=True)

        self.assertTrue(rep.dry_run)
        self.assertEqual(col.calls, [], "试运行不该有任何写入或建歌单调用")
        self.assertEqual(col.songs, set())
        self.assertEqual(len(rep.items), 1)

    def test_dry_run_needs_no_client(self):
        """试运行连客户端都不该连 —— 这样没装/没开也能先看清单。"""
        self.add_song("A", song_id="1")
        col = FakePlaylistCollector(health_ok=False)
        rep = self.orch().push_season(self.season, col, dry_run=True)
        self.assertEqual(rep.fatal, "", "试运行不该因为客户端没开而失败")
        self.assertEqual(rep.failed, [])
        self.assertEqual(col.calls, [])

    def test_dry_run_summary_says_so(self):
        self.add_song("A", song_id="1")
        rep = self.orch().push_season(self.season, FakePlaylistCollector(), dry_run=True)
        self.assertIn("试运行", rep.summary())


# ==================================================================== 正常推送

class TestPush(PushTestBase):
    def test_pushes_and_records(self):
        self.add_song("A", song_id="1")
        col = FakePlaylistCollector()
        rep = self.orch().push_season(self.season, col)

        self.assertTrue(rep.ok, rep.summary())
        self.assertEqual(rep.pushed, 1)
        self.assertEqual(col.songs, {"1"})
        self.assertTrue(self.store.is_pushed("26年10月", "1"))

    def test_reuses_existing_playlist(self):
        self.add_song("A", song_id="1")
        col = FakePlaylistCollector(existing_playlists=["26年10月"])
        rep = self.orch().push_season(self.season, col)
        self.assertFalse(rep.playlist_created)
        self.assertEqual(col.created, [], "已有歌单不该被重新创建")

    def test_creates_playlist_when_missing(self):
        self.add_song("A", song_id="1")
        col = FakePlaylistCollector()
        rep = self.orch().push_season(self.season, col)
        self.assertTrue(rep.playlist_created)
        self.assertEqual(col.created, ["26年10月"])
        self.assertEqual(rep.playlist, "26年10月")

    def test_second_run_reports_existed(self):
        """**幂等**：第二遍不该说「新推送」，也不该重复加。"""
        self.add_song("A", song_id="1")
        col = FakePlaylistCollector()
        orch = self.orch()

        r1 = orch.push_season(self.season, col)
        self.assertEqual(r1.pushed, 1)

        r2 = orch.push_season(self.season, col)
        self.assertEqual(r2.pushed, 0, "第二遍不该报新推送")
        self.assertEqual(r2.existed, 1, "应报「已存在」")
        self.assertEqual(len(col.songs), 1, "不该重复加歌")

    def test_pushed_vs_existed_uses_collector_action_not_ledger(self):
        """**关键**：「本次新推」的判据是 collector 实际做了什么，不是我们的台账。

        实测踩过的坑（集成测试才发现）：歌单里本来就有这首歌时，
        collector 会跳过（返回 True，零 dispatch），而我们的台账里没有
        → 之前报成了「新推送」。**报告说推了一首，实际一个写入请求都没发。**

        这条用「台账为空但 collector 说 skipped」精确复现那个场景。
        """
        self.add_song("A", song_id="1")
        col = FakePlaylistCollector()
        col.songs.add("1")          # 客户端歌单里其实已经有了

        rep = self.orch().push_season(self.season, col)
        self.assertEqual(rep.pushed, 0,
                         "collector 说 skip 了，却报成了「新推送」")
        self.assertEqual(rep.existed, 1)

    def test_real_add_is_reported_as_pushed(self):
        """反面：collector 确实写了 → 必须报「新推送」。"""
        self.add_song("A", song_id="1")
        col = FakePlaylistCollector()
        rep = self.orch().push_season(self.season, col)
        self.assertEqual(rep.pushed, 1)
        self.assertEqual(rep.existed, 0)

    def test_ledger_is_not_the_authority(self):
        """台账说推过、实际歌单里没有（用户手动删了）→ 必须补推。

        真正的判据是客户端的歌单内容，不是我们的备忘录。

        注意状态是 **`pushed` 而不是 `existed`** —— 因为这次确实发生了
        真实写入。状态的语义是「本次实际做了什么」，而不是
        「我们的台账里有没有」。（早期实现用台账判断，会把这种情况
        误报成「已存在」，让用户以为没动过手。）
        """
        self.add_song("A", song_id="1")
        self.store.mark_pushed("26年10月", "1", None)   # 台账里有
        col = FakePlaylistCollector()                    # 但歌单其实是空的

        rep = self.orch().push_season(self.season, col)
        self.assertEqual(col.songs, {"1"}, "台账有但歌单没有 → 必须真的补推")
        self.assertEqual(rep.pushed, 1, "确实写了 → 应报「新推送」")

    def test_limit(self):
        for i in range(5):
            self.add_song(f"S{i}", song_id=str(i))
        col = FakePlaylistCollector()
        rep = self.orch().push_season(self.season, col, limit=2)
        self.assertEqual(len(rep.items), 2)
        self.assertEqual(len(col.songs), 2)


# ==================================================================== 失败路径

class TestPushFailures(PushTestBase):
    def test_failed_not_recorded_in_ledger(self):
        """**关键**：失败绝不能记进台账。

        记了的话下次会被当成功跳过 —— 那首歌就永远进不去了。
        """
        self.add_song("A", song_id="1")
        col = FakePlaylistCollector(fail_ids={"1"})
        rep = self.orch().push_season(self.season, col)

        self.assertEqual(len(rep.failed), 1)
        self.assertFalse(self.store.is_pushed("26年10月", "1"),
                         "失败被记进台账了 —— 下次会被跳过")

    def test_failure_has_reason(self):
        self.add_song("A", song_id="1")
        col = FakePlaylistCollector(fail_ids={"1"})
        rep = self.orch().push_season(self.season, col)
        self.assertTrue(rep.failed[0].error, "失败项必须带原因")
        self.assertIn("回查", rep.failed[0].error)

    def test_one_failure_does_not_stop_batch(self):
        for i in range(4):
            self.add_song(f"S{i}", song_id=str(i))
        col = FakePlaylistCollector(fail_ids={"1"})
        rep = self.orch().push_season(self.season, col)

        self.assertEqual(rep.pushed, 3)
        self.assertEqual(len(rep.failed), 1)
        self.assertEqual(col.songs, {"0", "2", "3"})

    def test_exception_per_song_is_contained(self):
        self.add_song("A", song_id="1")
        col = FakePlaylistCollector(add_exception=RuntimeError("炸了"))
        rep = self.orch().push_season(self.season, col)
        self.assertEqual(len(rep.failed), 1)
        self.assertIn("炸了", rep.failed[0].error)

    def test_client_down_aborts_entirely(self):
        """连不上客户端就整体中止 —— 不要逐首去试。"""
        self.add_song("A", song_id="1")
        col = FakePlaylistCollector(health_ok=False)
        rep = self.orch().push_season(self.season, col)

        self.assertTrue(rep.fatal)
        self.assertIn("未监听", rep.fatal)
        self.assertEqual(col.calls, [], "客户端不可用时不发任何调用")

    def test_playlist_creation_failure_is_fatal(self):
        self.add_song("A", song_id="1")
        col = FakePlaylistCollector(create_ok=False)
        rep = self.orch().push_season(self.season, col)
        self.assertTrue(rep.fatal)
        self.assertIn("歌单", rep.fatal)
        self.assertEqual(col.songs, set())

    def test_no_pushable_items(self):
        rep = self.orch().push_season(self.season, FakePlaylistCollector())
        self.assertFalse(rep.fatal)
        self.assertEqual(rep.items, [])
        self.assertIn("没有", rep.summary())


# ==================================================================== 报告

class TestDescribePush(PushTestBase):
    def test_lists_failures_with_reason(self):
        self.add_song("A", song_id="1")
        col = FakePlaylistCollector(fail_ids={"1"})
        orch = self.orch()
        rep = orch.push_season(self.season, col)
        text = "\n".join(orch.describe_push(rep))

        self.assertIn("失败", text)
        self.assertIn("回查", text, "失败原因要展示出来，不能糊成「部分失败」")

    def test_dry_run_does_not_claim_playlist_state(self):
        """试运行时没检查过歌单，就不能说「复用已有」。

        声称自己知道实际不知道的事，比不说更糟 —— 用户会据此以为
        歌单已经存在，而实际上待会儿可能要新建一个。
        """
        self.add_song("A", song_id="1")
        orch = self.orch()
        rep = orch.push_season(self.season, FakePlaylistCollector(), dry_run=True)
        text = "\n".join(orch.describe_push(rep))
        self.assertNotIn("复用已有", text)
        self.assertNotIn("本次新建", text)
        self.assertIn("未检查", text)

    def test_dry_run_lists_all(self):
        for i in range(3):
            self.add_song(f"S{i}", song_id=str(i))
        orch = self.orch()
        rep = orch.push_season(self.season, FakePlaylistCollector(), dry_run=True)
        text = "\n".join(orch.describe_push(rep))
        self.assertIn("试运行", text)
        self.assertEqual(text.count("→"), 3)

    def test_fatal_is_reported(self):
        self.add_song("A", song_id="1")     # 有货才会走到健康检查
        col = FakePlaylistCollector(health_ok=False)
        orch = self.orch()
        rep = orch.push_season(self.season, col)
        self.assertTrue(rep.fatal)
        self.assertIn("中止", "\n".join(orch.describe_push(rep)))

    def test_empty_queue_reports_no_items_not_fatal(self):
        """没有可推的曲目 ≠ 失败。别把它报成错误，用户会以为出问题了。"""
        orch = self.orch()
        rep = orch.push_season(self.season, FakePlaylistCollector(health_ok=False))
        self.assertEqual(rep.fatal, "")
        self.assertIn("没有", rep.summary())

    def test_success_summary_has_playlist(self):
        self.add_song("A", song_id="1")
        orch = self.orch()
        rep = orch.push_season(self.season, FakePlaylistCollector())
        text = "\n".join(orch.describe_push(rep))
        self.assertIn("26年10月", text)
        self.assertIn("新建", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
