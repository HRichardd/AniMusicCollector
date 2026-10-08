"""季度内增量重跑测试。

赛季持续十几周，这期间新番陆续加入、歌曲陆续上架。
增量重跑要保证三件事：

    ① **只做该做的** —— 已有决策的曲目不再重搜（省额度、快）
    ② **该重试的重试** —— missing_source / missing_netease 每次都要再试
    ③ **说清变了什么** —— 没变化也要明确说「无变化」，
       否则用户没法判断该不该继续操作

全部离线：搜索引擎是假的（预设返回），
番单拉取用 `do_fetch=False` 或故意失败的桩。
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

# ⚠️ 必须在 import animusic **之前**设置（别名自学习会回写配置目录）
os.environ.setdefault(
    "ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, "data", "_test_tmp", "config")
)

from animusic.models import (  # noqa: E402
    Anime,
    Candidate,
    DecisionStatus,
    HealthStatus,
    Origin,
    Season,
    SeasonBucket,
    Theme,
    ThemeKind,
)
from animusic.normalize import AliasTable  # noqa: E402
from animusic.orchestrator import (  # noqa: E402
    Orchestrator,
    SeasonSnapshot,
    UpdateReport,
)
from animusic.store import Store  # noqa: E402

_TMP = os.path.join(_ROOT, "data", "_test_tmp")


class FakeSearch:
    """按关键词返回预设候选；记录被搜过的词，用于断言「有没有重复搜」。"""

    def __init__(self, hits: dict[str, list[Candidate]] | None = None, fail: bool = False):
        self.hits = hits or {}
        self.fail = fail
        self.queries: list[str] = []

    def health(self) -> HealthStatus:
        return HealthStatus(True, "fake")

    def search(self, keyword: str, limit: int = 10) -> list[Candidate]:
        self.queries.append(keyword)
        if self.fail:
            raise RuntimeError("模拟搜索故障")
        return [self._copy(c) for c in self.hits.get(keyword, [])][:limit]

    @staticmethod
    def _copy(c: Candidate) -> Candidate:
        # 编排层会就地改 score/score_detail，所以每次要给新对象
        from dataclasses import replace

        return replace(c)


class DeadBridge:
    """模拟浏览器没接管 —— 拉番单必然失败。"""

    def health(self) -> HealthStatus:
        return HealthStatus(False, "端口未监听（测试桩）")


def _cand(song_id: str, name: str, artist: str, rank: int = 0) -> Candidate:
    return Candidate(
        theme_id=0, netease_song_id=song_id, name=name, artists=[artist],
        duration_ms=240_000, search_rank=rank, origin=Origin.SEARCH,
    )


class UpdateTestBase(unittest.TestCase):
    def setUp(self):
        os.makedirs(_TMP, exist_ok=True)
        self.db = os.path.join(_TMP, f"upd_{id(self)}.db")
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

    def add_anime(self, cn="番A", slug="a"):
        return self.store.upsert_anime(self.sid, Anime(
            season_key="2026Q4", title_cn=cn, animethemes_slug=slug,
            season_bucket=SeasonBucket.TARGET, origin=Origin.BANGUMI,
        ))

    def add_theme(self, aid, title="Song", artist="Artist", kind=ThemeKind.OP):
        return self.store.upsert_theme(aid, Theme(
            anime_id=aid, kind=kind, song_title=title, artists=[artist],
            sequence=1, source="animethemes", origin=Origin.ANIMETHEMES,
        ))

    def orch(self, search, bridge=None):
        return Orchestrator(
            store=self.store, search=search, aliases=self.table,
            bridge=bridge, progress=lambda m: None,
        )


# ==================================================================== 快照

class TestSnapshot(UpdateTestBase):
    def test_empty(self):
        s = SeasonSnapshot.take(self.store, self.sid)
        self.assertEqual(s.anime_ids, set())
        self.assertEqual(s.theme_ids, set())
        self.assertEqual(s.decisions, {})

    def test_captures_ids_and_status(self):
        aid = self.add_anime()
        tid = self.add_theme(aid)
        self.store.set_decision(tid, DecisionStatus.AUTO, None)
        s = SeasonSnapshot.take(self.store, self.sid)
        self.assertEqual(s.anime_ids, {aid})
        self.assertEqual(s.theme_ids, {tid})
        self.assertEqual(s.decisions[tid], "auto")

    def test_is_isolated_per_season(self):
        self.add_anime()
        other = self.store.get_season_id(2026, 1)
        s = SeasonSnapshot.take(self.store, other)
        self.assertEqual(s.anime_ids, set(), "不该看到别的季度")


# ==================================================================== 报告

class TestUpdateReport(unittest.TestCase):
    def test_noop(self):
        r = UpdateReport(season=Season.parse("2026Q4"))
        self.assertTrue(r.is_noop)
        self.assertEqual(r.summary(), "无变化")

    def test_summary_bits(self):
        r = UpdateReport(season=Season.parse("2026Q4"), new_anime=2, new_themes=5)
        self.assertFalse(r.is_noop)
        self.assertIn("新增番剧 2", r.summary())
        self.assertIn("新增曲目 5", r.summary())


# ==================================================================== 增量行为

class TestIncremental(UpdateTestBase):
    def test_second_run_is_noop(self):
        """**最核心的断言**：跑第二遍不该有任何变化。

        周更时大多数情况就是无变化，如果第二遍还能「变」出东西，
        说明幂等性破了 —— 那会不断重复搜索、重复写库。
        """
        aid = self.add_anime()
        tid = self.add_theme(aid, title="Song", artist="Artist")
        search = FakeSearch({"Artist 番A": [_cand("1", "Song", "Artist", 0)]})
        orch = self.orch(search)

        r1 = orch.update_season(self.season, do_fetch=False)
        self.assertFalse(r1.is_noop, "第一次应该搜到东西")
        n_queries_1 = len(search.queries)

        r2 = orch.update_season(self.season, do_fetch=False)
        self.assertTrue(r2.is_noop, f"第二遍不该有变化，实际 {r2.summary()}")
        self.assertEqual(len(search.queries), n_queries_1,
                         "已有决策的曲目不该被重新搜索")

    def test_already_decided_not_researched(self):
        aid = self.add_anime()
        tid = self.add_theme(aid, title="Song", artist="Artist")
        # 先手工给它一个决策
        self.store.set_decision(tid, DecisionStatus.MISSING_MATCH, None)
        search = FakeSearch()
        self.orch(search).update_season(self.season, do_fetch=False)
        # missing_match 不在重试集合里（那个重试没用，要人工）
        self.assertEqual(search.queries, [],
                         f"missing_match 不该被重搜，实际搜了 {search.queries}")

    def test_missing_netease_is_retried(self):
        aid = self.add_anime()
        tid = self.add_theme(aid, title="Song", artist="Artist")
        self.store.set_decision(tid, DecisionStatus.MISSING_NETEASE, None)
        search = FakeSearch({"Artist 番A": [_cand("1", "Song", "Artist", 0)]})
        self.orch(search).update_season(self.season, do_fetch=False)
        self.assertTrue(search.queries, "missing_netease 必须被重试")

    def test_newly_available_detected(self):
        """之前搜不到、这次搜到了 → 必须出现在 newly_available 里。

        这正是周更的主要价值：新番歌曲通常开播后 2~8 周才上架。
        """
        aid = self.add_anime()
        tid = self.add_theme(aid, title="Song", artist="Artist")
        self.store.set_decision(tid, DecisionStatus.MISSING_NETEASE, None)

        search = FakeSearch({"Artist 番A": [_cand("1", "Song", "Artist", 0)]})
        rep = self.orch(search).update_season(self.season, do_fetch=False)

        self.assertEqual(len(rep.newly_available), 1, f"应报告 1 条新上架，实际 {rep.summary()}")
        got_tid, was, now = rep.newly_available[0]
        self.assertEqual(got_tid, tid)
        self.assertEqual(was, "missing_netease")
        self.assertNotIn(now, ("missing_netease", "missing_source"))

    def test_still_unavailable_stays_missing(self):
        aid = self.add_anime()
        tid = self.add_theme(aid, title="Song", artist="Artist")
        self.store.set_decision(tid, DecisionStatus.MISSING_NETEASE, None)
        search = FakeSearch()          # 什么都搜不到
        rep = self.orch(search).update_season(self.season, do_fetch=False)
        self.assertEqual(rep.newly_available, [])
        self.assertEqual(
            self.store.decision_map(self.sid)[tid]["status"], "missing_netease"
        )

    def test_new_theme_gets_matched(self):
        aid = self.add_anime()
        old = self.add_theme(aid, title="Old", artist="Artist")
        self.store.set_decision(old, DecisionStatus.MISSING_MATCH, None)

        search = FakeSearch({"Artist 番A": [_cand("9", "New", "Artist", 0)]})
        orch = self.orch(search)
        orch.update_season(self.season, do_fetch=False)
        n_after_first = len(search.queries)

        # 模拟数据源后来补了一条新曲目
        new_tid = self.add_theme(aid, title="New", artist="Artist", kind=ThemeKind.ED)
        rep = orch.update_season(self.season, do_fetch=False)

        self.assertGreater(len(search.queries), n_after_first, "新曲目应该被搜索")
        # `new_themes` 统计的是「本次拉取发现的」，而这条是测试直接插库的，
        # 所以该看「新匹配」而不是「新增曲目」
        self.assertIn(new_tid, [t for t, _st in rep.newly_decided])

    def test_newly_decided_reported_on_first_match(self):
        """首次匹配出结果必须被算作变化。

        否则周更报告会说「无变化」，而实际上刚匹配了一整批 ——
        用户会以为工具没干活。
        """
        aid = self.add_anime()
        tid = self.add_theme(aid, title="Song", artist="Artist")
        search = FakeSearch({"Artist 番A": [_cand("1", "Song", "Artist", 0)]})
        rep = self.orch(search).update_season(self.season, do_fetch=False)
        self.assertFalse(rep.is_noop)
        self.assertEqual([t for t, _s in rep.newly_decided], [tid])
        self.assertIn("新匹配 1", rep.summary())

    def test_search_failure_does_not_crash(self):
        aid = self.add_anime()
        self.add_theme(aid, title="Song", artist="Artist")
        search = FakeSearch(fail=True)
        rep = self.orch(search).update_season(self.season, do_fetch=False)
        self.assertIsInstance(rep, UpdateReport)

    def test_total_search_failure_is_not_recorded_as_missing(self):
        """**关键的安全性测试。**

        搜索全挂 ≠ 网易云没上架。之前没有这个判断，网络一断就会把每条
        都记成 `missing_netease` —— 那首歌会被当成「等上架就行」，
        而除了 `--retry-missing` 没人会再碰它，**等于永久跳过**。

        正确行为：什么都不记，保留未决策状态，下次重跑自然再试。
        """
        aid = self.add_anime()
        tid = self.add_theme(aid, title="Song", artist="Artist")
        search = FakeSearch(fail=True)
        self.orch(search).update_season(self.season, do_fetch=False)

        self.assertNotIn(tid, self.store.decision_map(self.sid),
                         "搜索全失败时不该写任何决策")

    def test_partial_failure_still_records(self):
        """部分查询成功且**确实没结果** → 那是有效的「没有这首歌」。"""
        aid = self.add_anime()
        tid = self.add_theme(aid, title="Song", artist="Artist")

        class PartialSearch(FakeSearch):
            def search(self, keyword, limit=10):
                self.queries.append(keyword)
                if len(self.queries) == 1:
                    raise RuntimeError("第一条查询挂了")
                return []

        self.orch(PartialSearch()).update_season(self.season, do_fetch=False)
        self.assertIn(tid, self.store.decision_map(self.sid),
                      "有查询成功过就该正常记录")

    def test_search_failed_not_counted_as_searched(self):
        """`searched` 必须和各状态之和对得上，否则比率会被悄悄稀释。"""
        aid = self.add_anime()
        self.add_theme(aid, title="Song", artist="Artist")
        orch = self.orch(FakeSearch(fail=True))
        rep = orch.match_season(self.season)
        self.assertEqual(rep.searched, 0)
        self.assertEqual(rep.auto + rep.pending_review + rep.missing_source
                         + rep.missing_netease + rep.missing_match, 0)

    def test_fetch_failure_still_retries_missing(self):
        """**重要的降级行为**：浏览器没开也不该妨碍「重试缺歌」。

        后者才是周更的主要价值 —— 不该因为番单拉不到就整个跳过。
        """
        aid = self.add_anime()
        tid = self.add_theme(aid, title="Song", artist="Artist")
        self.store.set_decision(tid, DecisionStatus.MISSING_NETEASE, None)

        search = FakeSearch({"Artist 番A": [_cand("1", "Song", "Artist", 0)]})
        rep = self.orch(search, bridge=DeadBridge()).update_season(
            self.season, do_fetch=True
        )
        self.assertTrue(search.queries, "拉番单失败时仍应重试缺歌")
        self.assertEqual(len(rep.newly_available), 1)


# ==================================================================== 描述

class TestDescribe(UpdateTestBase):
    def test_noop_message_explains_why(self):
        """无变化时必须给用户一个解释，否则他会以为工具坏了。"""
        search = FakeSearch()
        orch = self.orch(search)
        rep = orch.update_season(self.season, do_fetch=False)
        text = "\n".join(orch.describe_update(rep))
        self.assertIn("无变化", text)
        self.assertIn("上架", text, "应提示新番歌曲上架有延迟")

    def test_describe_lists_changes(self):
        aid = self.add_anime()
        tid = self.add_theme(aid, title="Song", artist="Artist")
        self.store.set_decision(tid, DecisionStatus.MISSING_NETEASE, None)
        search = FakeSearch({"Artist 番A": [_cand("1", "Song", "Artist", 0)]})
        orch = self.orch(search)
        rep = orch.update_season(self.season, do_fetch=False)
        text = "\n".join(orch.describe_update(rep))
        self.assertIn("新上架", text)
        self.assertIn("重试", text)

    def test_truncates_long_change_lists(self):
        aid = self.add_anime()
        search = FakeSearch()
        orch = self.orch(search)
        rep = UpdateReport(season=self.season)
        rep.newly_available = [(i, "missing_netease", "auto") for i in range(60)]
        lines = orch.describe_update(rep)
        self.assertLessEqual(len(lines), 30, "变化很多时不该刷屏")
        self.assertTrue(any("未列出" in x for x in lines))


if __name__ == "__main__":
    unittest.main(verbosity=2)
