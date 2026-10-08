"""步骤④ 的**用户选择**要能存住、能回读 —— 顺带钉死那个「完成」必崩的 bug。

两条历史问题：

1. 第④步点「完成」把推送页的行喂给 `save_decisions()`（第③步的 API），
   界面文案「待推送」原样进 SQL → `IntegrityError`（CHECK 约束）。
   现在第④步走 `save_push_selection()`，**完全不碰 decision**。
2. 第④步「目标歌单」列是用户逐行改出来的，但 `session` 每次 reload 都按
   季度名重建它 —— 改过就没了。现在存进 `push_selection` 表并回读。

全部离线，库开在 workspace 内的临时目录（不用 tempfile —— 见附录 G.2）。
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

os.environ.setdefault(
    "ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, "data", "_test_tmp", "config")
)

from animusic.gui.session import WizardSession  # noqa: E402
from animusic.models import (  # noqa: E402
    Anime,
    Candidate,
    DecisionStatus,
    Origin,
    Season,
    SeasonBucket,
    Theme,
    ThemeKind,
)
from animusic.store import Store  # noqa: E402

_TMP = os.path.join(_ROOT, "data", "_test_tmp")


class PushSelectionBase(unittest.TestCase):
    def setUp(self):
        os.makedirs(_TMP, exist_ok=True)
        self.db = os.path.join(_TMP, f"pushsel_{id(self)}.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)
        self.store = Store(self.db)
        self.sess = WizardSession(store=self.store, season=Season.parse("2026Q4"))
        self.sid = self.sess.season_id

    def tearDown(self):
        self.store.close()
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)

    # ---- 造一条「可以推送」的曲目
    def add_pushable(self, song_id="1001", name="Song"):
        # 用 `sess.season_id`（而不是 setUp 里那份）—— 换季度后要造到新季度里去
        sid = self.sess.season_id
        aid = self.store.upsert_anime(sid, Anime(
            season_key="2026Q4", title_cn=f"番{name}", title_jp=f"ばん{name}",
            animethemes_slug=f"slug-{song_id}", season_bucket=SeasonBucket.TARGET,
            origin=Origin.BANGUMI,
        ))
        tid = self.store.upsert_theme(aid, Theme(
            anime_id=aid, kind=ThemeKind.OP, song_title=name, artists=["A"],
            sequence=1, source="animethemes", origin=Origin.ANIMETHEMES,
        ))
        cid = self.store.upsert_candidate(tid, Candidate(
            theme_id=tid, netease_song_id=song_id, name=name, artists=["A"],
            duration_ms=240_000, score=0.9, score_detail={"title": 1.0},
            origin=Origin.SEARCH,
        ))
        self.store.set_decision(tid, DecisionStatus.AUTO, cid)
        return tid

    def push_row(self, tid, song_id="1001", **over):
        row = {
            "included": True, "name": "Song", "artists": "A",
            "netease_song_id": song_id, "playlist": "26年10月",
            "status": "待推送", "anime": "番", "kind": "OP", "theme_id": tid,
        }
        row.update(over)
        return row


class TestStep4DoesNotTouchDecisions(PushSelectionBase):
    def test_step4_rows_with_chinese_status_do_not_crash(self):
        """第④步那串中文状态**绝不**能进 decision 表（这就是原来的崩溃点）。"""
        tid = self.add_pushable()
        before = {t: dict(r) for t, r in self.store.decision_map(self.sid).items()}
        stat = self.sess.save_push_selection([self.push_row(tid)])
        self.assertEqual(stat["saved"], 1)
        after = self.store.decision_map(self.sid)
        self.assertEqual(set(after), set(before), "第④步不该新增/删除 decision 行")
        self.assertEqual(after[tid]["status"], before[tid]["status"],
                         "第④步不该改动第③步定下的决策状态")

    def test_saving_selection_again_is_idempotent(self):
        tid = self.add_pushable()
        row = self.push_row(tid)
        self.sess.save_push_selection([row])
        self.sess.save_push_selection([row])
        self.assertEqual(len(self.store.push_selection_map(self.sid)), 1)

    def test_row_without_song_id_is_reported_not_stored(self):
        """「+ 手动加歌」刚加的空行没有 ID —— 存不了要如实报出来，不能装作存了。"""
        stat = self.sess.save_push_selection([
            {"included": True, "netease_song_id": "", "playlist": "26年10月"},
        ])
        self.assertEqual(stat["saved"], 0)
        self.assertEqual(stat["unsaved"], 1)
        self.assertEqual(self.store.push_selection_map(self.sid), {})


class TestPushSelectionRoundTrip(PushSelectionBase):
    def test_unchecked_row_stays_unchecked(self):
        """取消勾选必须活过 reload —— 推送只认勾选，丢一次就等于多推一首。"""
        tid = self.add_pushable()
        self.sess.save_push_selection([self.push_row(tid, included=False)])
        # build() 是 reload 的入口，等价于「关掉再打开」
        rows = self.sess.build()["push"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["included"])

    def test_custom_target_playlist_survives_reload(self):
        """逐行改的「目标歌单」不能被下一次 reload 打回季度默认值。"""
        tid = self.add_pushable()
        self.sess.save_push_selection([self.push_row(tid, playlist="26年1月")])
        self.assertEqual(self.sess.build()["push"][0]["playlist"], "26年1月")

    def test_never_saved_row_defaults_to_checked_and_season_playlist(self):
        """没存过的（新歌、新季度）默认：勾选 + 季度歌单名。"""
        self.add_pushable()
        row = self.sess.build()["push"][0]
        self.assertTrue(row["included"])
        self.assertEqual(row["playlist"], "26年10月")

    def test_selection_is_scoped_to_its_season(self):
        """两个季度的选择不能串味。"""
        tid = self.add_pushable()
        self.sess.save_push_selection([self.push_row(tid, included=False, playlist="26年1月")])

        self.sess.set_season(Season.parse("2026Q1"))
        tid_q1 = self.add_pushable(song_id="2002", name="Q1歌")
        self.sess.save_push_selection([self.push_row(tid_q1, playlist="26年1月")])
        self.assertEqual(self.sess.build()["push"][0]["playlist"], "26年1月")
        self.assertTrue(self.sess.build()["push"][0]["included"])

        self.sess.set_season(Season.parse("2026Q4"))
        row = self.sess.build()["push"][0]
        self.assertFalse(row["included"], "秋季的取消勾选串到别的季度去了")
        self.assertEqual(row["playlist"], "26年1月")

    def test_row_dropped_from_the_page_is_forgotten(self):
        """页面上已经没有的行要清掉，否则回读会把用户删掉的歌复活。"""
        a = self.add_pushable(song_id="1001", name="A")
        b = self.add_pushable(song_id="1002", name="B")
        self.sess.save_push_selection([
            self.push_row(a, song_id="1001"),
            self.push_row(b, song_id="1002"),
        ])
        self.assertEqual(len(self.store.push_selection_map(self.sid)), 2)
        self.sess.save_push_selection([self.push_row(a, song_id="1001")])
        self.assertEqual(list(self.store.push_selection_map(self.sid)), ["1001"])

    def test_pushed_status_uses_the_rows_own_playlist(self):
        """台账是按歌单名记的 —— 推到别的歌单时不能拿季度歌单的台账下结论。

        否则用户把某行改推「26年1月」，界面仍显示「已推送」（其实只推到过
        季度歌单里），他会跳过这一首。
        """
        tid = self.add_pushable(song_id="1001")
        self.store.mark_pushed("26年1月", "1001", tid)      # 只在 26年1月 推过
        self.sess.save_push_selection([self.push_row(tid, playlist="26年1月")])
        self.assertEqual(self.sess.build()["push"][0]["status"], "已推送")

        self.sess.save_push_selection([self.push_row(tid, playlist="26年4月")])
        self.assertEqual(self.sess.build()["push"][0]["status"], "待推送",
                         "没在 26年4月 推过，却报了「已推送」")


if __name__ == "__main__":
    unittest.main(verbosity=2)
