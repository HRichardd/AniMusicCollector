"""第①步的取舍 vs 第②步的取舍 —— 以及用户实测报的三个问题。

用户原话（2026-10-07 真机）：
  ①「第一步里我取消勾选不需要的番，但第二步里还是会自动全选所有歌曲」
  ②「在第二步里拉取/更新 op/ed 选项，会拉取当季全部歌曲，而不是我第一步里选的番」
  ③「在 2 之后，我点上一步，重新拉取 bangumi 在看时，会把当季所有番拉下来」

三者的根因都**先用离线复现确认过**（`.tmp/repro_three_bugs.py`），再在这里钉成回归测试：

  ① `session.py` 造第②步行时用 `anime_list(sid)`（默认 `only_included=True`）——
     被排除的番不在结果里，`next(..., 1)` 兜底成 True ⇒ 曲目一律显示"已收录"
  ② 第②步的按钮接的是 `fetch_season_from_animethemes`（=「仅从 AnimeThemes 拉当季」），
     按设计会把**整季**番写进番单
  ③ 那些番是②留下的，而拉取只增不删，所以重新拉 Bangumi 之后它们还在

外加一条**设计层**的修正：第②步"看不出在管什么"（用户："显得第二步有点意义不明"），
所以统计条要拆出「已排除（其中 N 条的番在第①步被排除）」+ 两个筛选 + 进入步骤时的口径行。

全部离线：不联网、不碰账号、不写用户库（临时库 + 临时配置目录）。
"""

from __future__ import annotations

import gc
import os
import shutil
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
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from animusic.models import (  # noqa: E402
    Anime, BangumiSubject, Origin, Season, Theme, ThemeKind,
)
from animusic.normalize import AliasTable  # noqa: E402
from animusic.orchestrator import Orchestrator  # noqa: E402
from animusic.sources.animethemes import AtAnime, AtTheme  # noqa: E402
from animusic.sources.bangumi import ReadResult  # noqa: E402
from animusic.store import Store  # noqa: E402

try:
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

if HAS_QT:
    from animusic.gui.main_window import MainWindow  # noqa: E402
    from animusic.gui.pages import ThemePage  # noqa: E402
    from animusic.gui.session import WizardSession  # noqa: E402

_TMP = os.path.join(_ROOT, "data", "_test_tmp")


# ==================================================================== 假数据源

@dataclass
class FakeWikiTheme:
    kind: str
    song_title: str
    sequence: int = 1
    artists: tuple = ("某人",)
    note: str = ""


class FakeBangumi:
    def __init__(self, subjects):
        self._s = subjects

    def read_watching(self):
        return ReadResult(list(self._s), "api_tab", f"{len(self._s)} 条")


class FakeAnimeThemes:
    last_search_error = None

    def __init__(self, pool):
        self._pool = pool

    def fetch_season(self, season):
        return list(self._pool)

    def search_anime(self, title, limit=5):
        return []


class FakeWikipedia:
    def __init__(self, by_name):
        self._by_name = by_name

    def fetch_themes(self, names):
        for n in names:
            if n in self._by_name:
                return list(self._by_name[n]), f"词条:{n}"
        return [], ""


def subject(cn, jp, bgm):
    return BangumiSubject(bgm_id=bgm, title_cn=cn, title_jp=jp,
                          air_date=date(2026, 10, 1), url=f"https://bgm.tv/subject/{bgm}")


def bare_orch(store, subjects=(), pool=(), wiki=None):
    orch = Orchestrator.__new__(Orchestrator)
    orch.store = store
    orch.aliases = AliasTable()
    orch.progress = lambda m: None
    orch.animethemes = FakeAnimeThemes(pool)
    orch.bangumi = FakeBangumi(subjects)
    orch.wikipedia = FakeWikipedia(wiki or {})
    orch._at_pool_by_season = {}
    orch._anilist_pool = lambda season: []
    orch.config = type("C", (), {
        "bangumi": type("B", (), {"include_cross_season": True})(),
    })()
    return orch


class StoreBase(unittest.TestCase):
    def setUp(self):
        os.makedirs(_TMP, exist_ok=True)
        self.db = os.path.join(_TMP, f"scope_{id(self)}.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)
        self.store = Store(self.db)
        self.season = Season.parse("2026Q4")
        self.sid = self.store.get_season_id(self.season.year, self.season.quarter)

    def tearDown(self):
        self.store.close()
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)

    # ---- 造数据的小工具
    def add_anime(self, cn, included=True, origin=Origin.BANGUMI, bgm_id=None):
        return self.store.upsert_anime(self.sid, Anime(
            season_key=self.season.key, title_cn=cn, title_jp=f"ばん{cn}",
            bgm_id=bgm_id, origin=origin, included=included,
        ))

    def add_theme(self, anime_id, title, kind="OP", included=True,
                  origin=Origin.SEARCH, artists=("A",), sequence=1, note=""):
        return self.store.upsert_theme(anime_id, Theme(
            anime_id=anime_id, kind=ThemeKind(kind), song_title=title,
            artists=list(artists), sequence=sequence, source="wikipedia",
            origin=origin, included=included, note=note,
        ))


# ==================================================================== ① 的取舍要传到 ②

class TestStep1PropagatesToStep2(StoreBase):
    """bug①：①里取消勾选的番，②里它的曲目必须显示为**未收录**（不是显示成已收录）。"""

    def _theme_rows(self):
        sess = WizardSession(store=self.store, season=self.season,
                             aliases=AliasTable())
        return sess.build()["theme"]

    def test_excluded_anime_makes_its_themes_excluded(self):
        a = self.add_anime("番A")
        b = self.add_anime("番B")
        self.add_theme(a, "A的OP")
        self.add_theme(b, "B的OP")
        self.store.set_anime_included(b, False)

        rows = {r["anime"]: r for r in self._theme_rows()}
        self.assertTrue(rows["番A"]["included"], "纳入的番，曲目该是勾选的")
        self.assertFalse(rows["番B"]["included"],
                         "番B 在①里被排除，②里它的曲目**不该**显示成已收录")
        self.assertFalse(rows["番B"]["anime_included"],
                         "还要能区分出'是它的番被排除了'")

    def test_excluded_theme_alone_is_also_unchecked(self):
        """曲目自己取消收录（番仍在）—— 两种原因都要显示成没勾，但能区分。"""
        a = self.add_anime("番A")
        self.add_theme(a, "A的OP", included=False)
        row = self._theme_rows()[0]
        self.assertFalse(row["included"])
        self.assertTrue(row["anime_included"], "它的番没被排除")

    def test_included_everything_is_checked(self):
        a = self.add_anime("番A")
        self.add_theme(a, "A的OP")
        row = self._theme_rows()[0]
        self.assertTrue(row["included"])
        self.assertTrue(row["anime_included"])

    def test_missing_anime_row_falls_back_to_included(self):
        """番行缺失（理论上的异常）兜底为"纳入"，不是"排除"。

        ⚠️ 这个异常**走不到真实数据**：`theme_list` 是 INNER JOIN anime，
        孤立 theme 行压根取不出来（我第一版想用删行来构造，结果查询返回 0 行）。
        所以这里直接替掉 `theme_list` 来测那行兜底代码 —— 它存在是为了
        "万一以后 JOIN 改了，也不要凭空把用户的曲目全排除掉"。
        """
        a = self.add_anime("番A")
        self.add_theme(a, "A的OP")
        real = self.store.theme_list

        def fake(season_id, only_included=True):
            rows = [dict(r) for r in real(season_id, only_included=False)]
            rows[0]["anime_id"] = 99999          # 指向一个不存在的番
            return rows

        self.store.theme_list = fake            # type: ignore[assignment]
        rows = self._theme_rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["anime_included"])


# ==================================================================== ② 只更新番单曲目

class TestRefreshThemes(StoreBase):
    """bug②：②的按钮只许碰番单里已纳入的番 —— 不许引入任何新番。"""

    def setUp(self):
        super().setUp()
        self.subjects = [subject("番A", "ばんA", 1), subject("番B", "ばんB", 2)]
        self.pool = [AtAnime(name=f"当季番{i}", slug=f"season_{i}",
                             themes=[AtTheme(kind="OP", song_title=f"曲{i}",
                                             sequence=1, artists=["某艺人"])])
                     for i in range(1, 7)]
        self.wiki = {
            "番A": [FakeWikiTheme("OP", "AのOP"), FakeWikiTheme("ED", "AのED")],
            "ばんA": [FakeWikiTheme("OP", "AのOP"), FakeWikiTheme("ED", "AのED")],
            "番B": [FakeWikiTheme("OP", "BのOP")],
            "ばんB": [FakeWikiTheme("OP", "BのOP")],
        }
        self.orch = bare_orch(self.store, self.subjects, self.pool, self.wiki)
        self.orch.fetch_season(self.season)          # 先按正常流程拉一次番单

    def test_does_not_add_any_anime(self):
        before = {a["id"] for a in self.store.anime_list(self.sid, only_included=False)}
        self.orch.refresh_themes(self.season)
        after = {a["id"] for a in self.store.anime_list(self.sid, only_included=False)}
        self.assertEqual(before, after, "refresh_themes 绝不能新增番（那是①的按钮干的事）")
        self.assertEqual(len(after), 2)

    def test_adds_themes_for_included_anime_only(self):
        b = next(a for a in self.store.anime_list(self.sid) if a["title_cn"] == "番B")
        self.store.set_anime_included(b["id"], False)
        # 排除之后给「番B」一条新的曲目：它不该被写进来
        self.wiki["番B"] = [FakeWikiTheme("ED", "Bの新ED")]
        self.wiki["ばんB"] = [FakeWikiTheme("ED", "Bの新ED")]

        self.orch.refresh_themes(self.season)
        titles = {t["song_title"] for t in self.store.theme_list(self.sid,
                                                                only_included=False)}
        self.assertNotIn("Bの新ED", titles, "被排除的番不该被更新")

    def test_report_numbers_come_from_the_db(self):
        """G.18 口径：新增数必须是库里数出来的差值，重跑一遍应为 0。"""
        rep1 = self.orch.refresh_themes(self.season)
        rep2 = self.orch.refresh_themes(self.season)
        self.assertGreaterEqual(rep1.themes_written, 0)
        self.assertEqual(rep2.themes_added, 0, "第二次跑不该有'新增'")
        self.assertGreater(rep2.themes_written, 0, "但它确实又 upsert 了一遍")
        self.assertEqual(rep2.subjects_with_themes, 2)

    def test_manual_theme_is_not_overwritten(self):
        """手工行照旧受保护：重跑不许覆盖用户填的 artist，也不许改回收录状态。

        这里用**全新库**（不先跑 fetch_season）：否则 setUp 里那次拉取已经写过
        同名曲目，`upsert_theme` 会按 `(番, 类型, 序号)` 合并到那一行上 ——
        那个行为本身是对的（去重），但会让"手工行被保护"这件事测不到。
        """
        store = Store(":memory:")
        sid = store.get_season_id(self.season.year, self.season.quarter)
        aid = store.upsert_anime(sid, Anime(
            season_key=self.season.key, title_cn="番A", title_jp="ばんA",
            origin=Origin.BANGUMI, included=True))
        tid = store.upsert_theme(aid, Theme(
            anime_id=aid, kind=ThemeKind.OP, song_title="AのOP",
            artists=["我自己填的"], sequence=1, source="manual",
            origin=Origin.MANUAL, included=False, note="我的备注"))
        orch = bare_orch(store, self.subjects, self.pool, self.wiki)
        orch.refresh_themes(self.season)
        row = store.conn.execute("SELECT * FROM theme WHERE id=?", (tid,)).fetchone()
        self.assertIn("我自己填的", row["artists"], "手工填的艺人被覆盖了")
        self.assertEqual(row["included"], 0, "用户取消的收录不许被重跑改回来")
        self.assertEqual(row["note"], "我的备注")
        store.close()

    def test_empty_season_is_not_an_error(self):
        store = Store(":memory:")
        orch = bare_orch(store, self.subjects, self.pool, self.wiki)
        rep = orch.refresh_themes(self.season)      # 还没拉过番单
        self.assertEqual(rep.subjects_total, 0)
        store.close()


# ==================================================================== ③ 收尾：排除补充的番

class TestExcludeSupplemental(StoreBase):
    """bug③ 的收尾：把「仅从 AnimeThemes 拉当季」带进来的番一键取消纳入。"""

    def test_only_animethemes_without_bgm(self):
        bg = self.add_anime("在看番", origin=Origin.BANGUMI, bgm_id=111)
        sup = self.add_anime("当季补充", origin=Origin.ANIMETHEMES)
        man = self.add_anime("手工加的", origin=Origin.MANUAL)
        names = self.store.exclude_supplemental_anime(self.sid)
        self.assertEqual(names, ["当季补充"])
        inc = {a["title_cn"]: bool(a["included"])
               for a in self.store.anime_list(self.sid, only_included=False)}
        self.assertTrue(inc["在看番"])
        self.assertFalse(inc["当季补充"])
        self.assertTrue(inc["手工加的"], "手工新增的番不是'补充的番'")
        self.assertIsNotNone(bg) and self.assertIsNotNone(sup) and self.assertIsNotNone(man)

    def test_nothing_is_deleted(self):
        sup = self.add_anime("当季补充", origin=Origin.ANIMETHEMES)
        self.add_theme(sup, "补充曲", origin=Origin.ANIMETHEMES)
        self.store.exclude_supplemental_anime(self.sid)
        self.assertEqual(len(self.store.anime_list(self.sid, only_included=False)), 1)
        self.assertEqual(len(self.store.theme_list(self.sid, only_included=False)), 1)
        self.assertTrue(any(a["title_cn"] == "当季补充"
                            for a in self.store.anime_list(self.sid, only_included=False)))

    def test_second_call_is_a_noop(self):
        self.add_anime("当季补充", origin=Origin.ANIMETHEMES)
        self.assertEqual(len(self.store.exclude_supplemental_anime(self.sid)), 1)
        self.assertEqual(self.store.exclude_supplemental_anime(self.sid), [])


# ==================================================================== ② 的可见性（GUI）

def theme_row(anime, kind="OP", included=True, origin="animethemes",
              anime_included=True, origin_label="数据源"):
    return {
        "included": included, "anime": anime, "anime_id": 1, "kind": kind, "seq": 1,
        "song_title": f"{anime}的{kind}", "artists": ["A"], "origin": origin,
        "origin_label": origin_label, "note": "", "anime_included": anime_included,
    }


@unittest.skipUnless(HAS_QT, "需要 PySide6")
class TestThemePageVisibility(unittest.TestCase):
    """第②步要能回答「我到底取舍了什么」—— 用户说它"显得意义不明"。"""

    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.logs = []
        rows = [
            theme_row("番A", "OP"),
            theme_row("番B", "ED", included=False, anime_included=False),   # 番被排除
            theme_row("番A", "ED", included=False),                          # 曲目被排除
            theme_row("番C", "OP", origin="manual", origin_label="手工"),
        ]
        self.page = ThemePage(rows, missing=["番D"])
        self.page.log.connect(self.logs.append)

    def tearDown(self):
        self.page.setParent(None)
        self.page.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        gc.collect()

    def test_has_filters_for_the_two_ways_of_excluding(self):
        names = [self.page.filter_combo.itemText(i)
                 for i in range(self.page.filter_combo.count())]
        for want in ("全部", "仅 OP", "仅 ED", "已排除", "手工补充"):
            self.assertIn(want, names)

    def test_excluded_filter_hides_exactly_the_unchecked(self):
        self.page._apply_filter("已排除")
        shown = [r["anime"] for r in self.page.visible_rows()]
        self.assertEqual(shown, ["番B", "番A"], "番B（番被排除）与番A的ED（曲目被排除）")

    def test_manual_filter(self):
        self.page._apply_filter("手工补充")
        self.assertEqual([r["anime"] for r in self.page.visible_rows()], ["番C"])

    def test_stat_line_distinguishes_the_two_causes(self):
        text = self.page.stat.text()
        self.assertIn("合计 2 条", text)      # 4 行里 2 行是收录的
        self.assertIn("2 条已排除", text)
        self.assertIn("其中 1 条的番在第①步被排除", text)

    def test_filtering_never_changes_the_data(self):
        self.page._apply_filter("仅 OP")
        self.assertEqual(len(self.page.all_rows()), 4, "筛选只影响显示")


# ==================================================================== 进入步骤时的口径行

@unittest.skipUnless(HAS_QT, "需要 PySide6")
class TestStepScopeLog(unittest.TestCase):
    """用户实测说"第二步显得意义不明" ⇒ 进每一步都要把口径摊开。"""

    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.home = os.path.join(_TMP, f"scope_home_{id(self)}")
        shutil.rmtree(self.home, ignore_errors=True)
        os.makedirs(self.home, exist_ok=True)
        self._old = os.environ.get("ANIMUSIC_CONFIG_DIR")
        os.environ["ANIMUSIC_CONFIG_DIR"] = os.path.join(self.home, "config")
        self.db = os.path.join(self.home, "state.db")
        self.store = Store(self.db)
        self.sess = WizardSession(store=self.store, season=Season.parse("2026Q4"))
        self.win = MainWindow(self.sess)
        #: ⚠️ MainWindow 的 `log` 是**普通方法**（不是 EnvPanel 那种 Signal），
        #: 所以直接把它在这个实例上换成一个收集器即可。
        self.logs: list[str] = []
        self.win.log = self.logs.append        # type: ignore[method-assign]

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        gc.collect()
        self.store.close()
        if self._old is None:
            os.environ.pop("ANIMUSIC_CONFIG_DIR", None)
        else:
            os.environ["ANIMUSIC_CONFIG_DIR"] = self._old
        shutil.rmtree(self.home, ignore_errors=True)

    def _logged(self, needle: str) -> bool:
        return any(needle in m for m in self.logs)

    def test_step1_line_says_it_decides_the_anime(self):
        self.logs.clear()
        self.win._goto(0)
        self.assertTrue(self._logged("只管「哪些番」"), self.logs)

    def test_step2_line_counts_excluded_and_manual(self):
        a = self.add_anime("番A")
        b = self.add_anime("番B")
        self.add_theme(a, "A的OP")
        self.add_theme(b, "B的OP")
        self.store.set_anime_included(b, False)
        self.add_theme(a, "手工曲", origin=Origin.MANUAL)
        self.win.reload()
        self.logs.clear()
        self.win._goto(1)
        joined = "\n".join(self.logs)
        self.assertIn("只管「哪些曲」", joined)
        self.assertIn("共 3 条", joined)
        self.assertIn("收录 2 条", joined)
        self.assertIn("已排除 1 条", joined)
        self.assertIn("因为它的番在第①步被排除了", joined)
        self.assertIn("手工补充 1 条", joined)

    def add_anime(self, cn):
        return self.store.upsert_anime(
            self.sess.season_id,
            Anime(season_key=self.sess.season.key, title_cn=cn, title_jp=f"ばん{cn}",
                  origin=Origin.BANGUMI, included=True),
        )

    def add_theme(self, anime_id, title, origin=Origin.SEARCH):
        return self.store.upsert_theme(anime_id, Theme(
            anime_id=anime_id, kind=ThemeKind.OP, song_title=title, artists=["A"],
            sequence=1, source="wikipedia", origin=origin, included=True,
        ))


if __name__ == "__main__":
    unittest.main()
