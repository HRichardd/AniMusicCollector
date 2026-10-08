"""「失败」与「确实没有」的区分 —— 一类静默数据损坏的回归测试。

背景（附录 G.11）：代码里 `except: continue` 之后如果还有一个兜底分支，
那个分支会把**所有**失败原因吞掉并归入同一个结论。典型后果：

    for q in queries:
        try:
            cands = search(q)
        except Exception:
            continue          # ← 失败就跳过
    if best is None:
        → 「网易云未上架」      # ← 网断了也这么说

这个结论**会持久化进数据库**，用户看到的是一份看起来完全正常的
「这季的歌都还没上架」报告，而真相是网络问题。
比崩溃危险得多 —— 崩溃至少会让人知道出事了。

这个文件把已知的两处都钉住：
    ① 曲目搜索（`_match_one`）—— 已修
    ② 番剧搜索（`_match_anime`）—— 已修
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

from animusic.models import (  # noqa: E402
    Anime,
    BangumiSubject,
    Candidate,
    Season,
    SeasonBucket,
    Theme,
    ThemeKind,
)
from animusic.normalize import AliasTable  # noqa: E402
from animusic.orchestrator import Orchestrator  # noqa: E402
from animusic.store import Store  # noqa: E402

_TMP = os.path.join(_ROOT, "data", "_test_tmp")


# ==================================================================== 曲目搜索

class FailingSearch:
    """每次搜索都抛异常 —— 模拟网络断/接口挂/被风控。"""

    def __init__(self):
        self.calls = 0

    def health(self):
        from animusic.models import HealthStatus

        return HealthStatus(True, "fail-stub")

    def search(self, keyword, limit=10):
        self.calls += 1
        raise TimeoutError("模拟网络超时")


class FlakySearch:
    """第一次挂、之后返回空 —— 模拟「部分失败但确实没结果」。"""

    def __init__(self):
        self.calls = 0

    def health(self):
        from animusic.models import HealthStatus

        return HealthStatus(True, "flaky-stub")

    def search(self, keyword, limit=10):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("第一条查询挂了")
        return []


class TestThemeSearchFailure(unittest.TestCase):
    def setUp(self):
        os.makedirs(_TMP, exist_ok=True)
        self.db = os.path.join(_TMP, f"failcls_{id(self)}.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)
        self.store = Store(self.db)
        self.season = Season.parse("2026Q4")
        self.sid = self.store.get_season_id(2026, 4)

        aid = self.store.upsert_anime(self.sid, Anime(
            season_key="2026Q4", title_cn="番A", animethemes_slug="a",
            season_bucket=SeasonBucket.TARGET,
        ))
        self.tid = self.store.upsert_theme(aid, Theme(
            anime_id=aid, kind=ThemeKind.OP, song_title="Song", artists=["Artist"],
            sequence=1, source="animethemes",
        ))

    def tearDown(self):
        self.store.close()
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)

    def _orch(self, search):
        return Orchestrator(store=self.store, search=search, aliases=AliasTable(),
                            progress=lambda m: None)

    def test_all_searches_failing_records_nothing(self):
        """网络全挂 → 不能写任何决策，保留待重试。"""
        self._orch(FailingSearch()).match_season(self.season)
        self.assertNotIn(self.tid, self.store.decision_map(self.sid),
                         "搜索全失败却写了决策 —— 那首歌会被永久跳过")

    def test_all_searches_failing_not_counted_as_searched(self):
        """`searched` 要和各状态之和对得上，否则比率被悄悄稀释。"""
        rep = self._orch(FailingSearch()).match_season(self.season)
        self.assertEqual(rep.searched, 0)
        self.assertEqual(
            rep.auto + rep.pending_review + rep.missing_source
            + rep.missing_netease + rep.missing_match, 0,
        )

    def test_failure_is_visible_in_details(self):
        """不能悄悄吞掉 —— 要在结果里留下痕迹。"""
        rep = self._orch(FailingSearch()).match_season(self.season)
        self.assertTrue(any(d.get("status") == "search_failed" for d in rep.details),
                        "搜索失败应在 details 里可见")

    def test_partial_failure_records_normally(self):
        """有查询成功过且返回空 → 那是有效的「没有这首歌」。"""
        self._orch(FlakySearch()).match_season(self.season)
        self.assertIn(self.tid, self.store.decision_map(self.sid))

    def test_retry_after_failure_succeeds(self):
        """失败后不写决策 → 下次重跑自然会再试，这是修复的核心价值。"""
        self._orch(FailingSearch()).match_season(self.season)
        self.assertNotIn(self.tid, self.store.decision_map(self.sid))

        class OkSearch(FailingSearch):
            def search(self, keyword, limit=10):
                self.calls += 1
                return [Candidate(theme_id=0, netease_song_id="1",
                                  name="Song", artists=["Artist"],
                                  duration_ms=240_000, search_rank=0)]

        self._orch(OkSearch()).match_season(self.season)
        self.assertIn(self.tid, self.store.decision_map(self.sid),
                      "上次失败、这次成功 → 必须能补上")


# ==================================================================== 番剧搜索

class FailingAnimeThemes:
    """search_anime 失败（返回空 + 记录原因）。"""

    def __init__(self, error: str = "TimeoutError: boom"):
        self.last_search_error = error

    def search_anime(self, title, limit=5):
        return []

    def fetch_season(self, season):
        return []


class OkAnimeThemes:
    last_search_error = None

    def search_anime(self, title, limit=5):
        return []

    def fetch_season(self, season):
        return []


def _bare_orch(animethemes) -> Orchestrator:
    """不经 __init__ 组装一个最小编排器（避开 Store/Bridge 依赖）。"""
    orch = Orchestrator.__new__(Orchestrator)
    orch.animethemes = animethemes
    orch.aliases = AliasTable()
    orch.progress = lambda m: None
    # `_match_anime` 现在会走 AniList 桥接，需要这两个属性存在
    orch._at_pool_by_season = {}
    orch._anilist_source = None
    # ⚠️ 必须显式给空池：`_anilist_pool` 默认会**真的去请求 AniList**，
    # 于是这个「避开网络」的夹具其实一直在联网（网络被拦时才恰好变成空池，
    # 拦不住的时候就是真的在跑）。单测不该有网络依赖。
    orch._anilist_pool = lambda season: []
    orch.config = type("C", (), {"bangumi": type("B", (), {"include_cross_season": True})()})()
    return orch


class TestAnimeSearchFailure(unittest.TestCase):
    def _subject(self):
        return BangumiSubject(bgm_id=1, title_cn="某番", title_jp="あるアニメ")

    def test_search_failure_marked_differently_from_not_found(self):
        """搜索失败与「确实没收录」必须是两种不同的结论。"""
        m = _bare_orch(FailingAnimeThemes())._match_anime(
            self._subject(), SeasonBucket.TARGET, [], Season.parse("2026Q4")
        )
        self.assertEqual(m.how, "search_failed",
                         "搜索失败被报成了 'none'（= 数据源没有）")
        self.assertTrue(m.search_error)

    def test_successful_search_with_no_hits_is_none(self):
        m = _bare_orch(OkAnimeThemes())._match_anime(
            self._subject(), SeasonBucket.TARGET, [], Season.parse("2026Q4")
        )
        self.assertEqual(m.how, "none", "搜到了但没结果 → 这才是真的没收录")
        self.assertFalse(m.search_error)

    def test_fetch_report_separates_the_two(self):
        from animusic.orchestrator import AnimeMatch, FetchReport

        rep = FetchReport(season=Season.parse("2026Q4"))
        rep.unmatched = [
            AnimeMatch(["A"], "A", SeasonBucket.TARGET, None, 0.0, "none"),
            AnimeMatch(["B"], "B", SeasonBucket.TARGET, None, 0.0, "search_failed",
                       "TimeoutError"),
            AnimeMatch(["C"], "C", SeasonBucket.TARGET, None, 0.0, "search_failed",
                       "TimeoutError"),
        ]
        self.assertEqual(len(rep.search_failed_unmatched), 2)
        self.assertEqual(len([m for m in rep.unmatched if m.how != "search_failed"]), 1)


# ==================================================================== 数据源层

class TestAnimeThemesSourceRecordsError(unittest.TestCase):
    def test_last_search_error_is_set_on_failure(self):
        """数据源本身也要能说清「我没问到」，而不是静默返回空。"""
        from animusic.sources.animethemes import AnimeThemesSource

        class BoomClient:
            def get_json(self, *a, **kw):
                raise TimeoutError("boom")

        src = AnimeThemesSource(client=BoomClient())
        self.assertEqual(src.search_anime("anything"), [])
        self.assertIsNotNone(src.last_search_error, "失败原因没被记录下来")
        self.assertIn("boom", src.last_search_error or "")

    def test_error_cleared_on_success(self):
        from animusic.sources.animethemes import AnimeThemesSource

        class OkClient:
            def get_json(self, *a, **kw):
                return {"anime": []}

        src = AnimeThemesSource(client=OkClient())
        src.last_search_error = "旧错误"
        src.search_anime("anything")
        self.assertIsNone(src.last_search_error, "成功后应清掉旧错误")


if __name__ == "__main__":
    unittest.main(verbosity=2)
