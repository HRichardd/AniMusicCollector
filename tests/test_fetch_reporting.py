"""`fetch_season` 的**结果数字**必须数的是「真结果」，不是「流程计数器」。

起因（用户实测反馈，见 DESIGN.md G.18）：日志打的是

    当季 49 部
    匹配 2/5，新增曲目 15

而用户在看列表里只有 5 部番、5 部**全部**从维基拿到了曲目。于是：

  · `匹配 2/5` 是 **AnimeThemes 的名称匹配数**（回退路径的命中率），
    在维基成为曲目主源之后它已经**不等于成功率** —— 用户以为 3 部失败了，
    其实那 3 部正是「不需要 AnimeThemes 也能拿到曲目」的那些；
  · `当季 49 部` 是**匹配池**的大小，不是要收进来的番 —— 日志没说，看着像
    工具背着他把一整季都收了（库里其实只有 5 部）；
  · `新增曲目 15` 数的是 upsert **次数**：同曲名会被合并成一行、重跑时
    全是更新已有行，这两种情况下它都会虚报。

这个文件把三条都钉住：**数字从库里数差值、主语说清是哪个口径**。
全部离线：假的 Bangumi/AnimeThemes/维基 + 真 Store（临时库），
不联网、不碰 `data/state.db`、不碰账号。
"""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault(
    "ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, "data", "_test_tmp", "config")
)

from animusic.models import BangumiSubject, Season, ThemeKind  # noqa: E402
from animusic.normalize import AliasTable  # noqa: E402
from animusic.orchestrator import Orchestrator  # noqa: E402
from animusic.sources.animethemes import AtAnime, AtTheme  # noqa: E402
from animusic.sources.bangumi import ReadResult  # noqa: E402
from animusic.store import Store  # noqa: E402

_TMP = os.path.join(_ROOT, "data", "_test_tmp")


# ==================================================================== 假的三个源

@dataclass
class FakeWikiTheme:
    kind: str
    song_title: str
    sequence: int = 1
    artists: tuple = ("某人",)
    note: str = ""


class FakeBangumi:
    """`read_watching()` 返回固定的在看列表。"""

    def __init__(self, subjects):
        self._subjects = subjects

    def read_watching(self) -> ReadResult:
        return ReadResult(list(self._subjects), "api_tab", f"{len(self._subjects)} 条")


class FakeAnimeThemes:
    """当季**匹配池**（不是番单）。`search_anime` 恒空 → 池子外匹配不上。"""

    last_search_error = None

    def __init__(self, pool):
        self._pool = pool

    def fetch_season(self, season):
        return list(self._pool)

    def search_anime(self, title, limit=5):
        return []


class FakeWikipedia:
    """按番名给曲目：`{名字: [主题曲…]}`。没给的名字 → 空（让 AnimeThemes 回退）。"""

    def __init__(self, by_name):
        self._by_name = by_name

    def fetch_themes(self, names):
        for n in names:
            if n in self._by_name:
                return list(self._by_name[n]), f"词条:{n}"
        return [], ""


# ==================================================================== 组装

def build_orch(store, subjects, pool, wiki) -> tuple[Orchestrator, list[str]]:
    """不经 `__init__` 组装最小编排器（避开 BrowserBridge / 配置 / 网络）。

    与 `tests/test_failure_classification.py::_bare_orch` 同一条路子。
    """
    logs: list[str] = []
    orch = Orchestrator.__new__(Orchestrator)
    orch.store = store
    orch.aliases = AliasTable()
    orch.progress = logs.append
    orch.animethemes = FakeAnimeThemes(pool)
    orch.bangumi = FakeBangumi(subjects)
    orch.wikipedia = FakeWikipedia(wiki)
    orch._at_pool_by_season = {}
    # AniList 桥接恒空 → 不进网络（`_match_via_anilist` 见空池会立刻退出）
    orch._anilist_pool = lambda season: []
    orch.config = type("C", (), {
        "bangumi": type("B", (), {"include_cross_season": True})(),
    })()
    return orch, logs


def subject(cn: str, jp: str, bgm: int) -> BangumiSubject:
    return BangumiSubject(bgm_id=bgm, title_cn=cn, title_jp=jp,
                          air_date=date(2026, 10, 1), url=f"https://bgm.tv/subject/{bgm}")


class FetchReportingBase(unittest.TestCase):
    def setUp(self):
        os.makedirs(_TMP, exist_ok=True)
        self.db = os.path.join(_TMP, f"fetchrep_{id(self)}.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)
        self.store = Store(self.db)
        self.season = Season.parse("2026Q4")

    def tearDown(self):
        self.store.close()
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)

    # --- 场景：2 部在看，其中只有 1 部在 AnimeThemes 池里有对应名字
    def scenario(self, wiki=None):
        subjects = [subject("番A", "ばんA", 1), subject("番B", "ばんB", 2)]
        pool = [
            AtAnime(name="ばんA", slug="ban_a"),        # 只有它能在池里对上
            AtAnime(name="全然違う", slug="other_1"),   # 池子里的其它番（与在看无关）
            AtAnime(name="関係ない", slug="other_2"),
        ]
        wiki = wiki if wiki is not None else {
            "番A": [FakeWikiTheme("OP", "AのOP")],
            "番B": [FakeWikiTheme("OP", "BのOP"), FakeWikiTheme("ED", "BのED")],
        }
        return build_orch(self.store, subjects, pool, wiki)


# ==================================================================== 主结果行

class TestCompletionLine(FetchReportingBase):
    def test_completion_counts_subjects_that_got_themes(self):
        """主结果行必须是「拿到曲目的番数」，不是 AnimeThemes 的匹配数。

        这次 2 部都拿到了曲目（1 部靠池子、1 部只靠维基），
        旧口径会打「匹配 1/2」—— 看起来像有一部失败了。
        """
        orch, logs = self.scenario()
        rep = orch.fetch_season(self.season)
        joined = "\n".join(logs)

        self.assertEqual(rep.subjects_with_themes, 2)
        self.assertEqual(len(rep.matched), 1, "前提：只有 1 部能在 AnimeThemes 池里对上")
        self.assertIn("2/2 部拿到曲目", joined)
        self.assertNotIn("完成：匹配", joined,
                         "主结果行又变回打 AnimeThemes 的匹配数了")
        self.assertIn("维基 2 部", joined)

    def test_animethemes_match_is_labelled_as_a_fallback(self):
        """AnimeThemes 的匹配数可以显示，但必须说清它只是回退源。"""
        orch, logs = self.scenario()
        orch.fetch_season(self.season)
        joined = "\n".join(logs)
        self.assertIn("AnimeThemes", joined)
        self.assertIn("回退源", joined)
        self.assertIn("没匹配上不代表这部番没有曲目", joined)


# ==================================================================== 曲目计数

class TestThemesAdded(FetchReportingBase):
    def test_first_run_counts_new_rows(self):
        orch, logs = self.scenario()
        rep = orch.fetch_season(self.season)
        self.assertEqual(rep.themes_added, 3, "首次拉取应当新增 3 行")
        self.assertEqual(rep.themes_written, 3)
        self.assertIn("新增曲目 3 条", "\n".join(logs))

    def test_second_run_reports_updates_not_new_rows(self):
        """重跑一遍：**一行都没新增**，日志不能说「新增 3 条」。

        这是旧口径最会骗人的地方 —— 它数的是 upsert 次数，
        所以每次重跑都报「新增 N 条」，而库里一行都没多。
        """
        orch, logs = self.scenario()
        orch.fetch_season(self.season)
        logs.clear()

        orch2, logs2 = self.scenario()
        rep = orch2.fetch_season(self.season)
        joined = "\n".join(logs2)
        self.assertEqual(rep.themes_added, 0, "重跑不该报出新增行")
        self.assertEqual(rep.themes_written, 3, "但确实更新了 3 行")
        self.assertIn("新增曲目 0 条", joined)
        self.assertIn("另有 3 条是更新已有行", joined)

    def test_merged_duplicate_does_not_inflate_the_count(self):
        """同番同类型同曲名出现两次 → `upsert_theme` 合并成一行。

        写库次数是 2，真实行数是 1 —— 日志必须报 1，并说明另一条是更新。
        """
        wiki = {"番A": [FakeWikiTheme("OP", "同じ曲"), FakeWikiTheme("OP", "同じ曲")],
                "番B": []}
        orch, logs = self.scenario(wiki=wiki)
        rep = orch.fetch_season(self.season)
        self.assertEqual(rep.themes_added, 1)
        self.assertEqual(rep.themes_written, 2)
        joined = "\n".join(logs)
        self.assertIn("新增曲目 1 条", joined)
        self.assertIn("另有 1 条是更新已有行", joined)


# ==================================================================== 匹配池

class TestPoolIsNotTheAnimeList(FetchReportingBase):
    def test_pool_line_says_it_is_only_for_matching(self):
        """池子那行必须写明「不会加入番单」—— 否则用户以为整季都被收进来了。"""
        orch, logs = self.scenario()
        rep = orch.fetch_season(self.season)
        joined = "\n".join(logs)
        self.assertIn("不会加入番单", joined)
        self.assertIn("匹配池", joined)
        self.assertIn("当季 3 部", joined)

    def test_pool_size_never_becomes_anime_rows(self):
        """库里只该有在看列表那 2 部，池子里的另外 2 部一部都不能进。"""
        orch, _logs = self.scenario()
        rep = orch.fetch_season(self.season)
        sid = self.store.get_season_id(self.season.year, self.season.quarter)
        rows = self.store.anime_list(sid, only_included=False)
        self.assertEqual(len(rows), 2, f"池子漏进番单了：{[r['title_cn'] for r in rows]}")
        self.assertEqual({r["title_cn"] for r in rows}, {"番A", "番B"})
        self.assertEqual(rep.subjects_in_season, 2)

    def test_pool_match_shows_how_many_were_used(self):
        """池子那几行要能看出「49 部里用到了几部」。"""
        orch, logs = self.scenario()
        orch.fetch_season(self.season)
        joined = "\n".join(logs)
        self.assertIn("名称匹配上 1/2 部", joined)
        self.assertIn("与你在看的番无关", joined)


# ==================================================================== 另一条入口

class TestAnimethemesOnlyPath(FetchReportingBase):
    """「仅从 AnimeThemes 拉当季」是**同一个毛病**的另一个入口。

    它原来也数 upsert 次数，所以每次重跑都报「新增 N 条」。
    """

    def fake_at(self):
        return [
            AtAnime(name="A", slug="a", themes=[
                AtTheme(kind=ThemeKind.OP, song_title="AのOP", sequence=1)]),
            AtAnime(name="B", slug="b", themes=[
                AtTheme(kind=ThemeKind.OP, song_title="BのOP", sequence=1),
                AtTheme(kind=ThemeKind.ED, song_title="BのED", sequence=1)]),
        ]

    def build(self):
        logs: list[str] = []
        orch = Orchestrator.__new__(Orchestrator)
        orch.store = self.store
        orch.aliases = AliasTable()
        orch.progress = logs.append
        orch.animethemes = FakeAnimeThemes(self.fake_at())
        orch._at_pool_by_season = {}
        orch._anilist_pool = lambda season: []
        orch.config = type("C", (), {
            "bangumi": type("B", (), {"include_cross_season": True})(),
        })()
        return orch, logs

    def test_first_run_counts_new_rows(self):
        orch, logs = self.build()
        rep = orch.fetch_season_from_animethemes(self.season, bridge_names=False)
        self.assertEqual(rep.themes_added, 3)
        self.assertEqual(rep.subjects_with_themes, 2)
        joined = "\n".join(logs)
        self.assertIn("新增曲目 3 条", joined)
        self.assertIn("2 部拿到曲目", joined)

    def test_second_run_does_not_claim_new_rows(self):
        self.build()[0].fetch_season_from_animethemes(self.season, bridge_names=False)
        orch, logs = self.build()
        rep = orch.fetch_season_from_animethemes(self.season, bridge_names=False)
        self.assertEqual(rep.themes_added, 0, "重跑不该报出新增行")
        joined = "\n".join(logs)
        self.assertIn("新增曲目 0 条", joined)
        self.assertIn("另有 3 条是更新已有行", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
