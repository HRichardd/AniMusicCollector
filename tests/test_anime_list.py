"""第一步只读番单：假来源与内存库验证，不连接浏览器或网易云。"""

from datetime import date
import unittest
from unittest.mock import Mock

from animusic.config import Config
from animusic.models import Anime, BangumiSubject, Origin, Season, Stage, StageStatus
from animusic.orchestrator import OperationCancelled, Orchestrator
from animusic.sources.bangumi import ReadResult
from animusic.store import Store


class TestAnimeList(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.season = Season.parse("2026Q4")
        self.orch = Orchestrator.__new__(Orchestrator)
        self.orch.store = self.store
        self.orch.config = Config()
        self.orch.progress = Mock()
        self.orch.should_stop = None
        self.orch.bangumi = Mock()
        self.orch.bangumi.read_watching.return_value = ReadResult([
            BangumiSubject(1, "测试番", "テスト", date(2026, 10, 1)),
        ], "fake")
        for name in ("_at_pool", "_collect_themes", "_match_anime", "match_season"):
            setattr(self.orch, name, Mock(side_effect=AssertionError("第一步禁止曲目查询或匹配")))

    def test_fetch_only_saves_anime_and_waits_for_confirmation(self):
        rep = self.orch.fetch_anime_list(self.season)
        sid = self.store.get_season_id(2026, 4)
        rows = self.store.anime_list(sid)
        self.assertEqual([(r["bgm_id"], r["title_jp"]) for r in rows], [(1, "テスト")])
        self.assertEqual(rep.subjects_in_season, 1)
        self.assertEqual(self.store.get_stage(sid, Stage.ANIME), StageStatus.FETCHED)
        for table in ("theme", "candidate", "decision", "pushed"):
            self.assertEqual(self.store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        for name in ("_at_pool", "_collect_themes", "_match_anime", "match_season"):
            getattr(self.orch, name).assert_not_called()

    def test_repeat_fetch_preserves_manual_edits_exclusion_and_note(self):
        sid = self.store.get_season_id(2026, 4)
        aid = self.store.upsert_anime(sid, Anime(
            season_key=self.season.key, bgm_id=1, title_cn="人工名字", title_jp="手工原名",
            origin=Origin.MANUAL, included=False, note="旧备注保留",
        ))
        for _ in range(2):
            self.orch.fetch_anime_list(self.season)
        rows = self.store.anime_list(sid, only_included=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], aid)
        self.assertEqual(rows[0]["title_cn"], "人工名字")
        self.assertEqual(rows[0]["title_jp"], "手工原名")
        self.assertEqual(rows[0]["included"], 0)
        self.assertEqual(rows[0]["note"], "旧备注保留")

    def test_failed_save_rolls_back_anime_and_stage(self):
        sid = self.store.get_season_id(2026, 4)
        self.store.set_stage(sid, Stage.ANIME, StageStatus.CONFIRMED, confirmed=True)
        real_upsert = self.store.upsert_anime

        def fail_after_insert(*args):
            real_upsert(*args)
            raise ValueError("模拟保存失败")

        self.store.upsert_anime = fail_after_insert
        with self.assertRaises(ValueError):
            self.orch.fetch_anime_list(self.season)
        self.assertEqual(len(self.store.anime_list(sid)), 0)
        self.assertEqual(self.store.get_stage(sid, Stage.ANIME), StageStatus.CONFIRMED)

    def test_cancel_during_save_rolls_back_partial_list(self):
        sid = self.store.get_season_id(2026, 4)
        self.orch.should_stop = lambda: bool(self.store.anime_list(sid))
        with self.assertRaises(OperationCancelled):
            self.orch.fetch_anime_list(self.season)
        self.assertEqual(self.store.anime_list(sid), [])
        self.assertEqual(self.store.get_stage(sid, Stage.ANIME), StageStatus.PENDING)

    def test_quarter_filter_and_downstream_state(self):
        sid = self.store.get_season_id(2026, 4)
        self.orch.config.bangumi.include_cross_season = False
        self.orch.bangumi.read_watching.return_value.subjects.extend([
            BangumiSubject(2, "旧番", "旧作", date(2026, 7, 1)),
            BangumiSubject(3, "未来番", "新作", date(2027, 1, 1)),
        ])
        self.store.set_stage(sid, Stage.THEME, StageStatus.CONFIRMED, confirmed=True)
        self.orch.fetch_anime_list(self.season)
        self.assertEqual([r["bgm_id"] for r in self.store.anime_list(sid)], [1])
        self.assertEqual(self.store.get_stage(sid, Stage.THEME), StageStatus.DIRTY)
