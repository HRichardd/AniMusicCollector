"""R01—R04/R06：真实保存链路与无窗口回调；不启动 GUI、不访问账号。"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")]
os.environ.setdefault("ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, ".tmp", "edit-test-config"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from animusic.config import Config, MatchConfig
from animusic.gui.session import WizardSession
from animusic.models import Anime, Candidate, Season, Stage, StageStatus, Theme, ThemeKind
from animusic.normalize import AliasTable
from animusic.orchestrator import MatchReport, Orchestrator, PushItem, PushReport
from animusic.scorer import classify
from animusic.store import SCHEMA, Store


class TestEditConsistency(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.session = WizardSession(store=self.store, config=Config(), season=Season(2026, 4),
                                     aliases=AliasTable())
        self.sid = self.session.season_id
        self.aid = self.store.upsert_anime(self.sid, Anime(
            season_key="2026Q4", title_cn="番", title_jp="番JP", bgm_id=123))
        self.tid = self.store.upsert_theme(self.aid, self.source_theme())

    def source_theme(self):
        return Theme(anime_id=self.aid, kind=ThemeKind.OP, song_title="Old",
                     artists=["Original"], sequence=1, source="wikipedia")

    def theme(self):
        return self.store.conn.execute("SELECT * FROM theme WHERE id=?", (self.tid,)).fetchone()

    def test_rename_keeps_identity_and_refetch_does_not_resurrect_original(self):
        rows = self.session.build()["theme"]
        rows[0]["song_title"] = "Edited"
        rows[0]["kind"] = "ED"
        self.session.save_themes(rows)
        self.assertEqual(self.store.upsert_theme(self.aid, self.source_theme()), self.tid)
        self.assertEqual(len(self.store.theme_list(self.sid)), 1)
        self.assertEqual((self.theme()["song_title"], self.theme()["kind"]), ("Edited", "ED"))
        self.assertEqual(self.theme()["source_song_title"], "Old")

    def test_artist_edit_is_protected_through_real_save(self):
        rows = self.session.build()["theme"]
        rows[0]["artists"] = ["Edited"]
        self.session.save_themes(rows)
        self.store.upsert_theme(self.aid, self.source_theme())
        self.assertEqual(self.theme()["artists"], '["Edited"]')
        self.assertEqual(self.theme()["origin"], "manual")

    def test_anime_edit_is_protected_through_real_save(self):
        rows = self.session.build()["anime"]
        rows[0]["title_cn"] = "人工番名"
        self.session.save_anime(rows)
        self.store.upsert_anime(self.sid, Anime(season_key="2026Q4", title_cn="来源名字", bgm_id=123))
        self.assertEqual(self.store.anime_list(self.sid)[0]["title_cn"], "人工番名")

    def test_no_edit_or_only_note_and_selection_does_not_mark_manual(self):
        rows = self.session.build()["theme"]
        rows[0].update(included=False, note="备注")
        self.session.save_themes(rows)
        self.assertNotEqual(self.theme()["origin"], "manual")
        rows = self.session.build()["anime"]
        rows[0].update(included=False, note="备注")
        self.session.save_anime(rows)
        self.assertNotEqual(self.store.anime_list(self.sid, False)[0]["origin"], "manual")

    def test_parent_exclusion_does_not_change_child_selection(self):
        self.store.set_anime_included(self.aid, False)
        self.session.save_themes(self.session.build()["theme"])
        self.assertEqual(self.theme()["included"], 1)

    def test_exclusion_survives_refetch_and_keeps_history(self):
        cid = self.store.upsert_candidate(self.tid, Candidate(theme_id=self.tid, netease_song_id="1", name="Song"))
        self.store.set_decision(self.tid, "confirmed", cid, decided_by="human")
        self.store.mark_pushed("歌单", "1", self.tid)
        rows = self.session.build()["theme"]
        rows[0]["included"] = False
        self.session.save_themes(rows)
        self.store.upsert_theme(self.aid, self.source_theme())
        self.assertEqual(self.theme()["included"], 0)
        self.assertTrue(self.store.is_pushed("歌单", "1"))
        self.assertEqual(self.store.decision_map(self.sid)[self.tid]["chosen_candidate_id"], cid)
        self.session.save_themes([])
        self.assertIsNotNone(self.theme())

    def test_content_change_invalidates_choice_without_deleting_history(self):
        cid = self.store.upsert_candidate(self.tid, Candidate(theme_id=self.tid, netease_song_id="1", name="Song"))
        self.store.set_decision(self.tid, "confirmed", cid, decided_by="human")
        rows = self.session.build()["theme"]
        rows[0]["song_title"] = "New"
        self.session.save_themes(rows)
        d = self.store.decision_map(self.sid)[self.tid]
        self.assertEqual(d["status"], "pending_review")
        self.assertEqual(d["decided_by"], "auto")
        self.assertIsNone(d["chosen_candidate_id"])
        self.assertEqual(len(self.store.candidate_list(self.tid)), 1)
        self.assertEqual(self.session.orchestrator().pushable(self.session.season), [])
        self.assertEqual(self.session.orchestrator().learn_from_season(self.session.season)["checked"], 0)

    def test_sequence_change_preserves_confirmed_choice(self):
        cid = self.store.upsert_candidate(self.tid, Candidate(theme_id=self.tid, netease_song_id="1", name="Song"))
        self.store.set_decision(self.tid, "confirmed", cid, decided_by="human")
        rows = self.session.build()["theme"]
        rows[0]["seq"] = 2
        self.session.save_themes(rows)
        self.assertEqual(self.store.decision_map(self.sid)[self.tid]["chosen_candidate_id"], cid)

    def test_new_rows_receive_ids_and_repeated_save_does_not_duplicate(self):
        rows = [{"anime_id": self.aid, "song_title": "New", "kind": "ED", "artists": []}]
        self.session.save_themes(rows)
        tid = rows[0]["theme_id"]
        self.session.save_themes(rows)
        self.assertEqual(rows[0]["theme_id"], tid)
        self.assertEqual(len(self.store.theme_list(self.sid)), 2)

    def test_unchanged_display_sequence_does_not_mark_source_row_manual(self):
        self.store.conn.execute("UPDATE theme SET sequence=NULL WHERE id=?", (self.tid,))
        self.store.conn.commit()
        self.session.save_themes(self.session.build()["theme"])
        self.assertIsNone(self.theme()["sequence"])
        self.assertNotEqual(self.theme()["origin"], "manual")

    def test_ambiguous_source_identity_is_reported_and_skipped(self):
        self.store.conn.execute(
            "INSERT INTO theme(anime_id,kind,song_title,artists,source,source_kind,source_sequence,source_song_title) "
            "VALUES(?,'OP','重复行','[]','wikipedia','OP',1,'Old')", (self.aid,))
        self.store.conn.commit()
        orch = self.session.orchestrator()
        orch.progress = Mock()
        self.assertFalse(orch._save_fetched_theme(self.aid, self.source_theme()))
        self.assertEqual(len(self.store.theme_list(self.sid)), 2)
        self.assertEqual(self.theme()["artists"], '["Original"]')
        self.assertIn("跳过本条", orch.progress.call_args[0][0])

    def test_anime_save_rolls_back_inserts_and_updates_when_stage_fails(self):
        rows = self.session.build()["anime"]
        rows[0]["title_cn"] = "改名"
        rows.append({"title_cn": "新番"})
        with patch.object(self.store, "set_stage", side_effect=RuntimeError("阶段失败")):
            with self.assertRaises(RuntimeError):
                self.session.save_anime(rows)
        self.assertEqual(len(self.store.anime_list(self.sid)), 1)
        self.assertEqual(self.store.anime_list(self.sid)[0]["title_cn"], "番")
        self.assertNotIn("anime_id", rows[1])

    def test_anime_date_updates_clears_and_invalid_input_rolls_back(self):
        rows = self.session.build()["anime"]
        rows[0]["air_date"] = "2026-11-01"
        self.session.save_anime(rows)
        self.assertEqual(self.store.anime_list(self.sid)[0]["air_date"], "2026-11-01")
        rows[0]["air_date"] = ""
        self.session.save_anime(rows)
        self.assertIsNone(self.store.anime_list(self.sid)[0]["air_date"])
        rows[0]["title_cn"] = "不应保存"
        rows.append({"title_cn": "坏日期", "air_date": "2026-02-30"})
        with self.assertRaises(ValueError):
            self.session.save_anime(rows)
        self.assertEqual(self.store.anime_list(self.sid)[0]["title_cn"], "番")
        self.assertNotIn("anime_id", rows[1])

    def test_unknown_and_cross_season_ids_are_rejected(self):
        other_sid = self.store.get_season_id(2027, 1)
        other = self.store.upsert_anime(other_sid, Anime(season_key="2027Q1", title_cn="别季"))
        for aid in (other, 99999):
            with self.subTest(aid=aid), self.assertRaises(ValueError):
                self.session.save_anime([{"anime_id": aid, "title_cn": "错误"}])
            with self.subTest(theme_parent=aid), self.assertRaises(ValueError):
                self.session.save_themes([{"anime_id": aid, "song_title": "错误"}])
        with self.assertRaises(ValueError):
            self.session.save_themes([{"theme_id": 99999, "anime_id": self.aid, "song_title": "错误"}])

    def test_failure_after_insert_rolls_back_rows_ids_and_stage(self):
        rows = [{"anime_id": self.aid, "song_title": "New", "kind": "ED"}]
        with patch.object(self.store, "mark_downstream_dirty", side_effect=RuntimeError("保存失败")):
            with self.assertRaises(RuntimeError):
                self.session.save_themes(rows)
        self.assertNotIn("theme_id", rows[0])
        self.assertEqual(len(self.store.theme_list(self.sid)), 1)
        self.assertEqual(self.store.get_stage(self.sid, Stage.THEME), StageStatus.PENDING)

    def test_nested_upsert_does_not_commit_outer_transaction(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.upsert_theme(self.aid, Theme(anime_id=self.aid, kind=ThemeKind.ED, song_title="New"))
                raise RuntimeError("回滚")
        self.assertEqual(len(self.store.theme_list(self.sid)), 1)

    def test_custom_threshold_reaches_match_decision_and_card(self):
        self.session.config.match = MatchConfig(auto_threshold=.99, review_threshold=.7)
        search = SimpleNamespace(search=lambda *a, **kw: [Candidate(
            theme_id=self.tid, netease_song_id="1", name="Old", artists=["Original"])])
        orch = Orchestrator(config=self.session.config, store=self.store, search=search, aliases=AliasTable())
        score = SimpleNamespace(score=.85, detail={"artist": 1., "title": 1.})
        with patch("animusic.orchestrator.scorer.score", return_value=score):
            orch._match_one(self.store.theme_list(self.sid)[0], self.sid, MatchReport(self.session.season))
        self.assertEqual(self.store.decision_map(self.sid)[self.tid]["status"], "pending_review")
        self.assertEqual(self.session.build()["match"][0]["candidates"][0]["status"], "pending_review")


class TestLegacyMigration(unittest.TestCase):
    def test_old_file_database_migrates_without_inventing_manual_identity(self):
        tmp_root = os.path.join(_ROOT, ".tmp")
        os.makedirs(tmp_root, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as folder:
            path = os.path.join(folder, "legacy.db")
            legacy_schema = SCHEMA.replace("    source_kind TEXT,\n", "").replace(
                "    source_sequence INTEGER,\n", "").replace("    source_song_title TEXT,\n", "")
            with sqlite3.connect(path) as conn:
                conn.executescript(legacy_schema)
                conn.execute("INSERT INTO season(id,year,quarter,playlist_name) VALUES(1,2026,4,'目标')")
                conn.execute("INSERT INTO anime(id,season_id,title_cn) VALUES(1,1,'番')")
                for title, origin in (("原名", "search"), ("已改名", "manual")):
                    conn.execute("INSERT INTO theme(anime_id,kind,song_title,artists,source,origin) "
                                 "VALUES(1,'OP',?,'[]','wikipedia',?)", (title, origin))
            conn.close()
            for _ in range(2):
                with Store(path) as store:
                    rows = store.theme_list(1)
                    self.assertEqual(len(rows), 2)
                    by_title = {r["song_title"]: r for r in rows}
                    self.assertEqual(by_title["原名"]["source_song_title"], "原名")
                    self.assertIsNone(by_title["已改名"]["source_song_title"])


class TestThresholdValidation(unittest.TestCase):
    def test_boundaries_and_invalid_thresholds(self):
        self.assertEqual(classify(.99, auto_threshold=.99, review_threshold=.7), "auto")
        self.assertEqual(classify(.7, auto_threshold=.99, review_threshold=.7), "pending_review")
        for auto, review in ((.5, .6), (float("nan"), .6), (1.1, .6), (.8, -1), (".9", .6), (True, .6)):
            with self.subTest(auto=auto, review=review), self.assertRaises(ValueError):
                classify(.85, auto_threshold=auto, review_threshold=review)

    def test_config_load_validates_yaml_thresholds(self):
        with patch("animusic.config.os.path.exists", return_value=True), patch(
                "builtins.open", unittest.mock.mock_open(read_data="match:\n  auto_threshold: 0.5\n  review_threshold: 0.9\n")):
            with self.assertRaises(ValueError):
                Config.load("isolated.yaml")


class TestWithoutWindows(unittest.TestCase):
    def test_remove_existing_model_rows_excludes_and_unsaved_rows_are_removed(self):
        from animusic.gui.widgets import Column, DictTableModel
        model = DictTableModel([Column("included", "收录", kind="check")], [
            {"theme_id": 1, "included": True, "theme_included": True}, {"included": True}])
        model.remove_rows([0, 1])
        self.assertEqual(model.rowCount(), 1)
        self.assertFalse(model.rows[0]["included"])
        self.assertFalse(model.rows[0]["theme_included"])

    def test_task_done_preserves_push_results_and_keeps_default_refresh(self):
        from animusic.gui.main_window import MainWindow
        stub = SimpleNamespace(_busy=Mock(), log=Mock(), reload=Mock(), _update_status=Mock())
        done = Mock()
        MainWindow._task_done(stub, "push", "结果", done, refresh_on_success=False)
        done.assert_called_once_with("结果")
        stub.reload.assert_not_called()
        stub._update_status.assert_called_once()
        MainWindow._task_done(stub, "fetch", "结果", None)
        stub.reload.assert_called_once_with("fetch")

    def test_push_results_do_not_cross_playlists(self):
        from animusic.gui.pages import PushPage
        rows = [{"playlist": "A", "netease_song_id": "1", "status": "待推送"},
                {"playlist": "B", "netease_song_id": "1", "status": "待推送"}]
        model = SimpleNamespace(rowCount=lambda: 2, columnCount=lambda: 1,
                                index=lambda *a: None, dataChanged=SimpleNamespace(emit=Mock()))
        stub = SimpleNamespace(all_rows=lambda: rows, model=model, _refresh=Mock())
        PushPage.mark_results(stub, {("A", "1"): "失败", ("B", "1"): "已推送"})
        self.assertEqual([r["status"] for r in rows], ["失败", "已推送"])

    def test_push_done_warns_with_stub_and_does_not_reload(self):
        from animusic.gui.main_window import MainWindow, QMessageBox
        report = PushReport(season=Season(2026, 4), playlist="A", items=[PushItem(
            theme_id=1, anime="番", kind="OP", target_title="歌", song_id="1", name="歌",
            status="failed", playlist="A")])
        page = SimpleNamespace(all_rows=lambda: [], mark_results=Mock())
        stub = SimpleNamespace(session=SimpleNamespace(
            orchestrator=lambda: SimpleNamespace(describe_push=lambda r: []), save_push_selection=Mock()),
            pages=[None, None, None, page], log=Mock(), reload=Mock(), _PUSH_STATUS=MainWindow._PUSH_STATUS)
        with patch.object(QMessageBox, "warning") as warning:
            MainWindow._push_done(stub, report)
            warning.assert_called_once()
        page.mark_results.assert_called_once_with({("A", "1"): "失败"})
        stub.reload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
