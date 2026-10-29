"""离线交接收尾：增量指标与一致副本迁移，不访问真实客户端。"""

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT / "src"), str(_ROOT / "vendor"), str(_ROOT / "tools")]
os.environ.setdefault("ANIMUSIC_CONFIG_DIR", str(_ROOT / ".tmp" / "followup-test-config"))

from animusic.config import Config
from animusic.models import Anime, Candidate, DecisionStatus, Season, Theme, ThemeKind
from animusic.normalize import AliasTable
from animusic.orchestrator import MatchReport, Orchestrator
from animusic.store import SCHEMA, Store
from verify_store_migration import verify_migration


class TestIncrementalMatchMetrics(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.season = Season(2026, 4)
        self.sid = self.store.get_season_id(2026, 4)
        self.aid = self.store.upsert_anime(self.sid, Anime(season_key="2026Q4", title_cn="番"))
        self.search = Mock()
        self.search.search.return_value = []
        self.orch = Orchestrator(store=self.store, config=Config(), search=self.search, aliases=AliasTable())
        for n in range(3):
            tid = self.theme(f"Old{n}")
            self.store.set_decision(tid, DecisionStatus.AUTO, self.candidate(tid))

    def theme(self, title):
        return self.store.upsert_theme(self.aid, Theme(
            anime_id=self.aid, kind=ThemeKind.OP, song_title=title, artists=["Artist"], source="manual"))

    def candidate(self, tid):
        return self.store.upsert_candidate(tid, Candidate(
            theme_id=tid, netease_song_id=str(tid), name="Song", artists=["Artist"]))

    def test_noop_does_not_report_old_results_as_new(self):
        rep = self.orch.match_season(self.season)
        self.assertEqual((rep.searched, rep.decided, rep.auto_rate, rep.usable_rate), (0, 0, 0, 0))
        self.search.search.assert_not_called()

    def test_new_missing_result_does_not_inherit_historical_auto_count(self):
        self.theme("New")
        rep = self.orch.match_season(self.season)
        self.assertEqual((rep.themes_total, rep.searched, rep.auto, rep.missing_netease), (4, 1, 0, 1))
        self.assertEqual((rep.auto_rate, rep.usable_rate), (0, 0))

    def test_new_auto_and_missing_have_a_measured_half_rate(self):
        self.theme("NewAuto")
        self.theme("NewMissing")
        self.search.search.side_effect = lambda query, **kw: [Candidate(
            theme_id=0, netease_song_id="99", name="NewAuto", artists=["Artist"])] if "NewAuto" in query else []
        with patch("animusic.orchestrator.scorer.score", return_value=SimpleNamespace(
                score=.99, detail={"artist": 1., "title": 1.})):
            rep = self.orch.match_season(self.season)
        self.assertEqual((rep.searched, rep.decided, rep.auto, rep.missing_netease), (2, 2, 1, 1))
        self.assertEqual((rep.auto_rate, rep.usable_rate), (.5, .5))

    def test_failed_retry_does_not_count_the_preserved_old_missing_result(self):
        tid = self.theme("Retry")
        self.store.set_decision(tid, DecisionStatus.MISSING_NETEASE, None)
        self.search.search.side_effect = RuntimeError("离线模拟网络失败")
        rep = self.orch.match_season(self.season, retry_missing=True)
        self.assertEqual((rep.searched, rep.decided, rep.auto), (0, 0, 0))
        self.assertEqual(self.store.decision_map(self.sid)[tid]["status"], "missing_netease")

    def test_excluded_historical_results_are_not_counted(self):
        self.store.conn.execute("UPDATE anime SET included=0 WHERE id=?", (self.aid,))
        rep = self.orch.match_season(self.season)
        self.assertEqual((rep.themes_total, rep.searched, rep.auto), (0, 0, 0))

    def test_low_is_not_counted_twice_in_decided(self):
        rep = MatchReport(self.season, searched=2, auto=1, low=1, missing_match=1)
        self.assertEqual(rep.decided, 2)


class TestMigrationVerification(unittest.TestCase):
    def setUp(self):
        root = _ROOT / ".tmp"
        root.mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)

    def test_wal_backup_preserves_manual_records_choices_and_original_schema(self):
        source = self.path / "source.db"
        legacy = SCHEMA.replace("    source_kind TEXT,\n", "").replace(
            "    source_sequence INTEGER,\n", "").replace("    source_song_title TEXT,\n", "").replace(
            "UNIQUE (season_id, theme_id, netease_song_id)", "UNIQUE (season_id, netease_song_id)")
        with closing(sqlite3.connect(source)) as conn:
            conn.executescript(legacy)
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("INSERT INTO season(id,year,quarter,playlist_name) VALUES(1,2026,4,'目标')")
            conn.execute("INSERT INTO anime(id,season_id,title_cn,origin) VALUES(1,1,'手工番','manual')")
            conn.execute("INSERT INTO theme(id,anime_id,kind,song_title,artists,source,origin) "
                         "VALUES(1,1,'OP','人工曲名','[]','wikipedia','manual')")
            conn.execute("INSERT INTO pushed(playlist_name,netease_song_id,theme_id) VALUES('目标','1',1)")
            conn.execute("INSERT INTO push_selection(season_id,netease_song_id,theme_id,included,target_playlist) "
                         "VALUES(1,'1',1,0,'自选')")
            conn.commit()
            old_schema = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
            self.assertGreater(Path(str(source) + "-wal").stat().st_size, 0)
            report = verify_migration(source, self.path / "verification")
            self.assertTrue(report["passed"], report["checks"])
            self.assertEqual(report["before"]["theme"]["rows"], 1)
            self.assertEqual(report["before"], report["after"])
            self.assertEqual(report["unresolved_manual_identities"], 1)
            self.assertEqual(old_schema, conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall())
            self.assertNotIn("source_kind", [r[1] for r in conn.execute("PRAGMA table_info(theme)")])

    def test_existing_output_is_never_overwritten(self):
        source = self.path / "source.db"
        with Store(str(source)):
            pass
        output = self.path / "evidence"
        output.mkdir()
        marker = output / "keep.txt"
        marker.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            verify_migration(source, output)
        self.assertEqual(marker.read_bytes(), b"keep")


class TestMeasurementSeedCount(unittest.TestCase):
    def test_duplicate_source_entries_are_counted_from_database(self):
        from measure_accuracy import seed
        from animusic.sources.animethemes import AtAnime, AtTheme
        with Store(":memory:") as store:
            sid = store.get_season_id(2026, 4)
            theme = AtTheme(ThemeKind.OP, "重复曲", ["艺人"], 1)
            anime = AtAnime("番", slug="test", themes=[theme, theme])
            with patch("measure_accuracy.AnimeThemesSource.fetch_season", return_value=[anime]), patch(
                    "animusic.sources.anilist.AniListSource.fetch_season", return_value=[]), patch(
                    "animusic.http.HttpClient.request", side_effect=AssertionError("不许访问真实网络")):
                self.assertEqual(seed(store, sid, Season(2026, 4), None, False), 1)
                self.assertEqual(seed(store, sid, Season(2026, 4), None, False), 1)
                self.assertEqual(len(store.theme_list(sid)), 1)


if __name__ == "__main__":
    unittest.main()
