"""第④步（推送）的接线测试 —— **全部离线，绝不构造 GUI、绝不碰模态框**。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么单独写、而且刻意不建窗口
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
第④步此前**一条测试都没有**，而它是整条链路里唯一会往用户账号写数据的地方。

第一版（已删）在 `setUpClass` 里建了 `QApplication` 并驱动真实的 `_push_songs`
代码路径。那条路径会弹**模态** `QMessageBox`：

    · 有人的桌面上  → 真的弹窗，等人点；点「是」会**真的往账号推歌**
    · offscreen 下  → 没人能点 → **测试永久挂住**（全量测试从 110 秒变成跑不完）

所以这里换一条路：**用非绑定方法复用真实实现**（`MainWindow._push_groups` /
`_push_group` 原样拿来），只把 `session` 与 collector 换成假的。
不建窗口 ⇒ 没有模态框 ⇒ 不可能挂死、不可能误触真机。

覆盖的是**「推什么、推去哪、失败怎么办」**这几条判据；真机写入本身仍然只有
G.15 那次手工验证（离线测试永远替代不了它）。
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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, "data", "_test_tmp", "config"))

from animusic.gui.main_window import MainWindow  # noqa: E402
from animusic.models import (  # noqa: E402
    Anime,
    Origin,
    Season,
    SeasonBucket,
    Theme,
    ThemeKind,
)
from animusic.store import Store  # noqa: E402
from animusic.config import Config
from animusic.models import Candidate
from animusic.orchestrator import Orchestrator

_TMP = os.path.join(_ROOT, "data", "_test_tmp")

#: 真实存在的 theme_id —— `pushed` 表对 `theme_id` 有**外键**，塞一个不存在的 id
#: 会 `FOREIGN KEY constraint failed`。`setUp` 里会把它换成刚种下的那条。
_THEME_ID: int | None = None
_SONG_THEMES: dict[str, int] = {}


# ---------------------------------------------------------------- 假 collector

class FakeCollector:
    """最小假 collector —— 只实现 `_push_group` 真正用到的那几个方法。

    `alive=False` 模拟「客户端中途没了」：`health()` 返回不 ok，
    这正是 `_push_groups` 必须把 `fatal` 往上传的那种情形。
    """

    def __init__(self, *, alive: bool = True, already: set[str] | None = None,
                 fail_for: set[str] | None = None, actions=None):
        self.alive = alive
        self.already = set(already or ())
        self.fail_for = set(fail_for or ())
        self.actions = actions or {}       # song_id -> last_write_action
        self.calls: list[str] = []
        self.last_write_error = ""
        self.last_write_note = ""
        self.last_write_action = None

    def health(self):
        from animusic.models import HealthStatus

        return HealthStatus(self.alive, "假 collector" if self.alive else "客户端没了")

    def find_playlist(self, name):
        from animusic.models import PlaylistRef

        return PlaylistRef(name=name, netease_id="999")

    def ensure_playlist(self, name):
        from animusic.models import PlaylistRef

        return PlaylistRef(name=name, netease_id="999")

    def add_to_playlist(self, song_id, playlist):
        sid = str(song_id)
        self.calls.append(sid)
        # 空 ID 绝对不能发出去 —— 界面会一切正常而歌一首没进。
        assert sid, "把空 song_id 发给 collector 了"
        if not self.alive:
            self.last_write_action = "failed"
            self.last_write_error = "客户端没了"
            return False
        if sid in self.fail_for:
            self.last_write_action = "failed"
            self.last_write_error = "这首歌失败"
            return False
        if sid in self.already:
            self.last_write_action = "skipped"
            self.last_write_note = f"{sid} 已经在里面了 —— 跳过（幂等）"
            return True
        self.last_write_action = "added"
        self.last_write_note = f"成功：{sid}"
        return True


class _FakeSession:
    """只提供 `_push_group` / `_push_groups` 用到的那些属性。"""

    def __init__(self, store: Store):
        self.store = store
        self.season = Season(2026, 4)
        self.season_id = store.get_season_id(2026, 4)

    def orchestrator(self):
        # 共用执行器必须在真实决策上复检，不能用任意song_id冒充可推歌曲。
        orch = Orchestrator.__new__(Orchestrator)
        orch.store = self.store
        orch.config = Config()
        orch.progress = lambda msg: None
        return orch


def _subject(store: Store, collector: FakeCollector):
    """把真实实现挂到一个最小 stub 上 —— 复用代码，但不建窗口。"""

    class _Stub:
        _push_group = MainWindow._push_group
        _push_groups = MainWindow._push_groups

        def __init__(self):
            self.session = _FakeSession(store)
            self.lines: list[str] = []
            self._collector = collector

        def _make_push_collector(self):
            return self._collector

        def log(self, msg: str) -> None:
            self.lines.append(str(msg))

    return _Stub()


def _row(song_id: str, name: str = "歌", playlist: str = "", **kw) -> dict:
    return {"song_id": song_id, "netease_song_id": song_id, "name": name,
            "playlist": playlist, "theme_id": kw.pop("theme_id", _SONG_THEMES.get(song_id, _THEME_ID)),
            "anime": kw.pop("anime", "番"), "kind": kw.pop("kind", "OP"), **kw}


class TestGroupByPlaylist(unittest.TestCase):
    """纯函数：勾选的行怎么按目标歌单分组。"""

    def test_groups_by_row_playlist_and_keeps_first_seen_order(self):
        rows = [_row("1", playlist="A"), _row("2", playlist="B"), _row("3", playlist="A")]
        g = MainWindow._group_by_playlist(rows, "默认")
        self.assertEqual(list(g), ["A", "B"])
        self.assertEqual([r["song_id"] for r in g["A"]], ["1", "3"])
        self.assertEqual([r["song_id"] for r in g["B"]], ["2"])

    def test_empty_row_playlist_falls_back_to_the_default(self):
        """手动新增的行那一格本来就是空的 —— 不能因此塞进一个叫 "" 的歌单。"""
        g = MainWindow._group_by_playlist([_row("1", playlist=""), _row("2", playlist="  ")],
                                          "26年10月")
        self.assertEqual(list(g), ["26年10月"])
        self.assertEqual(len(g["26年10月"]), 2)

    def test_row_playlist_wins_over_default(self):
        g = MainWindow._group_by_playlist([_row("1", playlist="我的歌单")], "26年10月")
        self.assertEqual(list(g), ["我的歌单"])

    def test_empty_input(self):
        self.assertEqual(MainWindow._group_by_playlist([], "x"), {})


class TestPushGroups(unittest.TestCase):
    """`_push_groups` / `_push_group` —— 含两条**回归**（都是我审出来的真 bug）。"""

    def setUp(self):
        global _THEME_ID
        os.makedirs(_TMP, exist_ok=True)
        self.db = os.path.join(_TMP, f"pushwiring_{id(self)}.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)
        self.store = Store(self.db)
        # 种一条真实的 theme —— `pushed` 表对 theme_id 有外键，编一个 id 会炸
        sid = self.store.get_season_id(2026, 4)
        aid = self.store.upsert_anime(sid, Anime(
            season_key="2026Q4", title_cn="接线测试番", animethemes_slug="wiring-test",
            season_bucket=SeasonBucket.TARGET, origin=Origin.BANGUMI))
        _THEME_ID = self.store.upsert_theme(aid, Theme(
            anime_id=aid, kind=ThemeKind.OP, song_title="接线测试曲", artists=["A"],
            sequence=1, source="wikipedia", origin=Origin.BANGUMI))
        _SONG_THEMES.clear()
        for song in ("1", "2", "3", "501", "502", "503", "601", "602"):
            tid = self.store.upsert_theme(aid, Theme(anime_id=aid, kind=ThemeKind.OP, song_title=f"歌{song}"))
            cid = self.store.upsert_candidate(tid, Candidate(theme_id=tid, netease_song_id=song, name=f"歌{song}"))
            self.store.set_decision(tid, "auto", cid)
            _SONG_THEMES[song] = tid

    def tearDown(self):
        self.store.close()
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                try:
                    os.remove(self.db + ext)
                except OSError:
                    pass

    # ------------------------------------------------------------ 回归 ①

    def test_client_gone_mid_batch_is_not_silently_dropped(self):
        """**回归**：`_push_groups` 曾经只 `extend(子报告.items)`，把 `fatal` 丢了。

        而客户端不可用时 `_push_group` 返回的是「有 `fatal`、零条目」的报告 ——
        合并后 `fatal` 变回空串，于是界面**显示一切正常、实际一首都没推**。
        这正是本项目最不能接受的那类失败：静默。
        """
        sub = _subject(self.store, FakeCollector(alive=False))
        groups = {"A": [_row("1")], "B": [_row("2")]}
        rep = sub._push_groups(groups)

        self.assertTrue(rep.fatal, "客户端没了必须体现在 fatal 上，不能被吞掉")
        self.assertIn("客户端", rep.fatal)
        self.assertEqual(rep.items, [], "客户端不可用时不发任何写入")

    def test_first_group_fatal_stops_the_rest(self):
        """客户端没了就是整批没意义 —— 不该继续对后面几个歌单空试。"""
        col = FakeCollector(alive=False)
        sub = _subject(self.store, col)
        sub._push_groups({"A": [_row("1")], "B": [_row("2")], "C": [_row("3")]})
        self.assertEqual(col.calls, [], "连第一首都不该发")

    # ------------------------------------------------------------ 正常路径

    def test_success_is_recorded_and_reported_as_pushed(self):
        col = FakeCollector()
        sub = _subject(self.store, col)
        rep = sub._push_groups({"26年10月": [_row("501", "花蔭")]})
        self.assertEqual(rep.pushed, 1)
        self.assertFalse(rep.fatal)
        self.assertEqual(col.calls, ["501"])
        self.assertTrue(self.store.is_pushed("26年10月", "501"),
                        "确认成功之后才记台账")

    def test_already_present_is_existed_not_pushed(self):
        """歌单里本来就有 → collector 零 dispatch 返回 True。

        判据必须是 **collector 实际做了什么**（`last_write_action`），
        不是我们自己的台账 —— 拿台账判会把「跳过」报成「新推送」。
        """
        col = FakeCollector(already={"501"})
        sub = _subject(self.store, col)
        rep = sub._push_groups({"26年10月": [_row("501")]})
        self.assertEqual(rep.existed, 1)
        self.assertEqual(rep.pushed, 0)

    def test_one_failure_does_not_stop_the_batch_and_is_not_recorded(self):
        """单首失败要继续、要带原因、**绝不记台账**（记了那首歌永远进不去）。"""
        col = FakeCollector(fail_for={"502"})
        sub = _subject(self.store, col)
        rep = sub._push_groups({"26年10月": [_row("501"), _row("502"), _row("503")]})
        self.assertEqual(len(rep.failed), 1)
        self.assertEqual(rep.failed[0].song_id, "502")
        self.assertTrue(rep.failed[0].error, "失败必须带原因")
        self.assertEqual(rep.pushed, 2, "另外两首不该受影响")
        self.assertFalse(self.store.is_pushed("26年10月", "502"))
        self.assertTrue(self.store.is_pushed("26年10月", "503"))

    def test_song_id_is_read_from_either_key(self):
        """行的键有两处来源：`pushable()` 用 `song_id`，表格行用 `netease_song_id`。

        ⚠️ 只在认一个时会取到空串 → 界面一切正常，**歌一首没进**。
        假 collector 里对空 ID 直接 assert，所以这条测试会当场炸。
        """
        col = FakeCollector()
        sub = _subject(self.store, col)
        sub._push_groups({"26年10月": [
            {"song_id": "601", "name": "从 pushable 来", "theme_id": _SONG_THEMES["601"]},
            {"netease_song_id": "602", "name": "从表格行来", "theme_id": _SONG_THEMES["602"]},
        ]})
        self.assertEqual(sorted(col.calls), ["601", "602"])

    def test_multiple_playlists_each_get_their_own_songs(self):
        col = FakeCollector()
        sub = _subject(self.store, col)
        groups = {"A": [_row("1")], "B": [_row("2"), _row("3")]}
        rep = sub._push_groups(groups)
        self.assertEqual(rep.pushed, 3)
        self.assertTrue(self.store.is_pushed("A", "1"))
        self.assertFalse(self.store.is_pushed("A", "2"), "不能串到别的歌单去")
        self.assertTrue(self.store.is_pushed("B", "2"))
        self.assertTrue(self.store.is_pushed("B", "3"))


class TestUncheckingIsNotAPermanentDecision(unittest.TestCase):
    """**回归哨兵**：取消勾选只意味着「这次先不推」，不许落成 `SKIPPED` 决策。

    曾经有过一版在推送后把「可推但没勾」的行全部写成 `DecisionStatus.SKIPPED`。
    后果（用户实测提问才发现）：

        · 第④步不再显示它们（`pushable()` 只收 auto/confirmed）；
        · 第③步当时**根本没有「跳过」这个动作** —— 也就是说 `SKIPPED`
          唯一的产生方式，是用户猜不到的副作用；
        · 40 首里先推 5 首 → 其余 35 首全变「已跳过」，**分批推送被粉碎**
          （而限速 1 首/秒、对话框自己还写着「请勿关闭窗口」，分批本来就合理）。

    这条用源码扫描来钉，而不是调方法 —— 因为要防的正是「有人把它加回来」。
    """

    def test_gui_never_writes_skipped_decisions(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[1]
        bad = []
        for p in sorted((root / "src" / "animusic" / "gui").glob("*.py")):
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if re.search(r"set_decision\([^)]*SKIPPED", code):
                    bad.append(f"{p.name}:{i}: {line.strip()}")
        self.assertEqual(
            bad, [],
            "GUI 又在推送时偷偷写 SKIPPED 决策了 —— 取消勾选只该是「这次不推」：\n"
            + "\n".join(bad))

    def test_the_removed_method_stays_removed(self):
        self.assertFalse(
            hasattr(MainWindow, "_mark_unselected_skipped"),
            "「没勾选就记为跳过」这个方法被加回来了；"
            "要做永久跳过请在第③步做成**用户显式**的动作")


if __name__ == "__main__":
    unittest.main(verbosity=2)
