"""别名表自学习测试。

核心不变量：**确认一次 → 以后自动通过**。

这是附录 G.9 定下的出路：算法做到约 57% 可用率后卡住，
剩余误差主要是 pykakasi 读不准的片假名外来语、团体简称、人名 ——
算法永远猜不准，但用户确认一次就能永久记住。

所以这里全部用**实测失败过的真实案例**做回归，
而不是编几个好看的名字：
    ずっと真夜中でいいのに。 ↔ Zutomayo      （团体简称）
    HANABIE.               ↔ 花冷え。        （片假名 + 标点）
    梅田サイファー          ↔ Umeda Cypher    （サイファー = cypher）
    Kienai Riyuu           ↔ 消えない理由     （罗马音 ↔ 日文原名）
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

from animusic import constants, scorer  # noqa: E402
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
from animusic.normalize import AliasTable, normalize_text  # noqa: E402
from animusic.orchestrator import Orchestrator  # noqa: E402
from animusic.query_builder import build_queries  # noqa: E402
from animusic.store import Store  # noqa: E402

_TMP = os.path.join(_ROOT, "data", "_test_tmp")

# 实测失败案例：(目标曲名, 目标艺人, 网易云曲名, 网易云艺人)
REAL_CASES = [
    ("Zutto Zutto Zutto ~cocoon Ver.~", "ずっと真夜中でいいのに。",
     "Zutto Zutto Zutto Zutto", "ZUTOMAYO"),
    ("Baribari Buddy", "HANABIE.", "バリバリBuddy", "花冷え。"),
    ("Urusiren", "梅田サイファー", "ウルサイレン", "Umeda Cypher"),
    ("Kienai Riyuu", "WANIMA", "消えない理由", "WANIMA"),
]


def _cand(name, artist):
    return {"name": name, "artists": [artist], "alias": [], "duration_ms": 240_000,
            "tags": [], "search_rank": 0}


def _target(title, artist, aliases=None):
    return scorer.Target(song_title=title, artists=[artist], anime_names=["某番"],
                         kind="OP", aliases=aliases)


# ==================================================================== 表结构

class TestAliasTable(unittest.TestCase):
    def test_new_group(self):
        t = AliasTable()
        self.assertTrue(t.learn_pair("artist", "ずっと真夜中でいいのに。", "Zutomayo"))
        self.assertEqual(t.stats()["artist"], 2)

    def test_canonical_prefers_native_script(self):
        """含 CJK 的当主名称 —— 它比罗马音稳定，两边数据源也更可能认得。"""
        t = AliasTable()
        t.learn_pair("artist", "Zutomayo", "ずっと真夜中でいいのに。")
        self.assertIn("ずっと真夜中でいいのに。", t.artist)

    def test_attach_to_existing_group(self):
        t = AliasTable()
        t.learn_pair("artist", "漢字名", "Romaji")
        t.learn_pair("artist", "漢字名", "Another")
        self.assertEqual(len(t.artist), 1, "不该新建第二组")
        self.assertEqual(len(t.artist["漢字名"]), 2)

    def test_merge_two_groups(self):
        """两个已有的组被一条新关系连起来时必须合并，否则表会越学越乱。"""
        t = AliasTable()
        t.learn_pair("artist", "A", "B")
        t.learn_pair("artist", "C", "D")
        t.learn_pair("artist", "B", "C")
        self.assertEqual(len(t.artist), 1, f"应合并成一组，实际 {t.artist}")
        self.assertEqual({normalize_text(x) for x in t.variants_of("artist", "A")},
                         {"a", "b", "c", "d"})

    def test_idempotent(self):
        t = AliasTable()
        for _ in range(3):
            t.learn_pair("artist", "ずっと真夜中でいいのに。", "Zutomayo")
        self.assertEqual(t.stats()["artist"], 2)
        self.assertFalse(t.learn_pair("artist", "ずっと真夜中でいいのに。", "Zutomayo"))

    def test_same_group(self):
        t = AliasTable()
        t.learn_pair("artist", "HANABIE.", "花冷え。")
        self.assertTrue(t.same_group("artist", "花冷え。", "HANABIE."))
        self.assertTrue(t.same_group("artist", "HANABIE.", "花冷え。"))
        self.assertFalse(t.same_group("artist", "HANABIE.", "别的艺人"))
        self.assertFalse(t.same_group("artist", "没人", "别的人"))

    def test_expand_names_dedupes_by_normalized(self):
        t = AliasTable()
        t.learn_pair("artist", "HANABIE.", "花冷え。")
        got = t.expand_names("artist", ["HANABIE."])
        self.assertIn("花冷え。", got)
        # 大小写不同的写法不该重复出现
        got2 = t.expand_names("artist", ["hanabie.", "HANABIE."])
        self.assertEqual(len([g for g in got2 if normalize_text(g) == normalize_text("HANABIE.")]), 1)

    def test_empty_and_identical_rejected(self):
        t = AliasTable()
        self.assertFalse(t.learn_pair("artist", "", "X"))
        self.assertFalse(t.learn_pair("artist", "X", ""))
        self.assertFalse(t.learn_pair("artist", "Same", "same"))
        self.assertEqual(t.stats()["artist"], 0)

    def test_roundtrip_yaml_includes_title(self):
        os.makedirs(_TMP, exist_ok=True)
        p = os.path.join(_TMP, "alias_rt.yaml")
        t = AliasTable()
        t.learn_title("Kienai Riyuu", "消えない理由")
        t.learn_artists(["HANABIE."], ["花冷え。"])
        t.learn_anime(["鬼灭之刃"], "鬼滅の刃")
        t.save(p)

        t2 = AliasTable.load(p)
        self.assertEqual(t2.title, t.title)
        self.assertEqual(t2.artist, t.artist)
        self.assertEqual(t2.anime, t.anime)
        os.remove(p)

    def test_load_missing_file_is_empty_not_error(self):
        t = AliasTable.load(os.path.join(_TMP, "definitely_not_here.yaml"))
        self.assertEqual(t.stats(), {"anime": 0, "artist": 0, "title": 0})


# ==================================================================== 番剧别名抽取

class TestLearnArtists(unittest.TestCase):
    """艺人学习的安全边界 —— 这一组是**防污染**的核心。

    别名是隐式生效的：学错了不会报错，只会让分数变得无法解释。
    所以这里用实测踩过的坑做反例。
    """

    def test_single_to_single_always_learns(self):
        """1:1 无歧义 —— 哪怕相似度很低也要学。

        实测案例：シャイトープ ↔ Shytaupe 的罗马音相似度只有 0.50，
        但这**正是最需要学的那类**（片假名外来语拼写完全不同）。
        如果也套相似度阈值，最该学的反而学不到。
        """
        t = AliasTable()
        self.assertEqual(t.learn_artists(["シャイトープ"], ["Shytaupe"]), 1)
        self.assertTrue(t.same_group("artist", "シャイトープ", "Shytaupe"))

    def test_no_cartesian_product_on_multi_artist(self):
        """**最关键的安全测试。**

        实测踩过的坑：目标 [J'Nique Nicole] 对上候选
        [Kevin Penkin, J'Nique Nicole] 时，朴素的笛卡尔积会把
        Kevin Penkin 和 J'Nique Nicole 学成同一个人。

        这类污染不会报错（别名是隐式生效的），只会让分数变得无法解释。
        """
        t = AliasTable()
        t.learn_artists(["J'Nique Nicole"], ["Kevin Penkin", "J'Nique Nicole"])
        self.assertFalse(t.same_group("artist", "J'Nique Nicole", "Kevin Penkin"),
                         "不同的人被学成同一个了")

    def test_voice_actor_list_not_merged(self):
        """候选挂一堆不同声优时，只能学到真正对应的那一个。

        否则多个不同的人会被合并成一组，之后所有相关匹配全部乱套。
        """
        t = AliasTable()
        cast = ["内田真礼", "長谷川育美", "相良茉優", "瀬戸桃子",
                "Ikumi Hasegawa", "Fairouz Ai", "Mayu Sagara", "Momoko Seto"]
        t.learn_artists(["ファイルーズあい"], cast)
        self.assertTrue(t.same_group("artist", "ファイルーズあい", "Fairouz Ai"),
                        f"应该学到 Fairouz Ai，实际 {t.artist}")
        for other in ("内田真礼", "長谷川育美", "相良茉優"):
            self.assertFalse(t.same_group("artist", "ファイルーズあい", other),
                             f"把 {other} 错认成 ファイルーズあい 了")

    def test_multi_to_multi_aligns_one_to_one(self):
        t = AliasTable()
        t.learn_artists(["HaKoniwalily"], ["ハコニワリリィ", "MARUMOCHI from HoneyWorks"])
        self.assertTrue(t.same_group("artist", "HaKoniwalily", "ハコニワリリィ"))
        self.assertFalse(t.same_group("artist", "HaKoniwalily", "MARUMOCHI from HoneyWorks"))

    def test_duplicate_names_deduped(self):
        t = AliasTable()
        n = t.learn_artists(["A", "A", "a"], ["B"])
        self.assertEqual(n, 1)
        self.assertEqual(t.stats()["artist"], 2)


class TestLearnAnime(unittest.TestCase):
    def test_extract_from_alias_text(self):
        """标注里的中文名与目标日文名不同 → 学成一对他名。"""
        t = AliasTable()
        self.assertTrue(t.learn_anime(["鬼滅の刃"], "TV动画《鬼灭之刃》片头曲"))
        self.assertTrue(t.same_group("anime", "鬼滅の刃", "鬼灭之刃"))

    def test_nothing_to_learn_when_alias_equals_target(self):
        """标注里的名字和目标**一模一样**时不算新知识，不该写表。"""
        t = AliasTable()
        self.assertFalse(t.learn_anime(["鬼灭之刃"], "TV动画《鬼灭之刃》片头曲"))
        self.assertEqual(t.stats()["anime"], 0)

    def test_rejects_unrelated_alias(self):
        """标注里的番剧名跟目标差太远 → 宁可不学，也不能把错关系写进表。

        别名是**隐式生效**的，写错了之后很难排查（分数会莫名其妙变高）。
        """
        t = AliasTable()
        self.assertFalse(t.learn_anime(["鬼灭之刃"], "TV动画《进击的巨人》片头曲"))
        self.assertEqual(t.stats()["anime"], 0)

    def test_no_brackets_is_noop(self):
        t = AliasTable()
        self.assertFalse(t.learn_anime(["X"], "某个没有书名号的标注"))
        self.assertFalse(t.learn_anime(["X"], None))
        self.assertFalse(t.learn_anime(["X"], ""))


# ==================================================================== 打分改善

class TestScoringImprovement(unittest.TestCase):
    """这是整套自学习的**验收依据**：学到之后必须真的从「匹配不上」变成「自动通过」。"""

    def test_before_learning_these_fail(self):
        for title, artist, cname, cartist in REAL_CASES:
            s = scorer.score(_cand(cname, cartist), _target(title, artist))
            if title == "Kienai Riyuu":
                continue  # 这个本来就靠罗马音救回来了
            self.assertLess(
                s.score, constants.AUTO_THRESHOLD,
                f"{title} 学习前不该自动通过（实际 {s.score}）—— 案例选错了？",
            )

    def test_after_learning_all_auto(self):
        table = AliasTable()
        for title, artist, cname, cartist in REAL_CASES:
            table.learn_title(title, cname)
            table.learn_artists([artist], [cartist])

        for title, artist, cname, cartist in REAL_CASES:
            s = scorer.score(_cand(cname, cartist), _target(title, artist, table))
            self.assertGreaterEqual(
                s.score, constants.AUTO_THRESHOLD,
                f"{title} 学过之后应当自动通过（实际 {s.score}）",
            )
            self.assertEqual(scorer.classify(s.score), "auto")

    def test_artist_match_is_full_marks_after_learning(self):
        """别名是人工确认过的事实 → 艺人项给满分 1.0，而不是模糊匹配的 0.8。"""
        table = AliasTable()
        table.learn_artists(["HANABIE."], ["花冷え。"])
        s = scorer.score(_cand("Baribari Buddy", "花冷え。"), _target("Baribari Buddy", "HANABIE.", table))
        self.assertEqual(s.detail["artist"], 1.0)

    def test_aliases_do_not_help_unrelated_candidates(self):
        """安全性检查：学了 A 的别名，不能把 B 的分数也抬上去。"""
        table = AliasTable()
        for title, artist, cname, cartist in REAL_CASES:
            table.learn_title(title, cname)
            table.learn_artists([artist], [cartist])

        s = scorer.score(_cand("完全无关的歌", "完全无关的人"),
                         _target("Baribari Buddy", "HANABIE.", table))
        self.assertLess(s.score, constants.REVIEW_THRESHOLD,
                        f"别名不该让无关候选得分（实际 {s.score}）")


# ==================================================================== 搜索词

class TestQueryUsesAliases(unittest.TestCase):
    def test_prefers_cjk_title_after_learning(self):
        """网易云索引的是日文原名，罗马音是噪声。

        学到 CJK 写法后，首查询词应该换成它 ——
        这一步往往比打分更关键：**搜不到就什么都谈不上**。
        """
        table = AliasTable()
        table.learn_title("Kienai Riyuu", "消えない理由")
        plan = build_queries("Kienai Riyuu", ["WANIMA"], ["某番"], "OP", limit=3, aliases=table)
        self.assertIn("消えない理由", plan.queries[0],
                      f"首查询词应含日文原名，实际 {plan.queries}")

    def test_latin_title_without_alias_keeps_artist_first(self):
        plan = build_queries("Some Latin Title", ["SomeArtist"], ["某番"], "OP", limit=3)
        self.assertEqual(plan.queries[0], "SomeArtist 某番")

    def test_prefers_cjk_artist(self):
        table = AliasTable()
        table.learn_artists(["HANABIE."], ["花冷え。"])
        plan = build_queries("Baribari Buddy", ["HANABIE."], ["某番"], "OP", limit=3, aliases=table)
        self.assertTrue(any("花冷え。" in q for q in plan.queries),
                        f"应从别名里用上日文艺人名，实际 {plan.queries}")


# ==================================================================== 端到端学习

class TestLearnFromSeason(unittest.TestCase):
    """`learn_from_season` 的**安全边界**：默认只学人工确认的。"""

    def setUp(self):
        os.makedirs(_TMP, exist_ok=True)
        self.db = os.path.join(_TMP, f"learn_{id(self)}.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)
        self.store = Store(self.db)
        self.sess_season = Season.parse("2026Q4")
        self.sid = self.store.get_season_id(2026, 4)
        self.table = AliasTable()          # 不落盘，避免碰真实 config

    def tearDown(self):
        self.store.close()
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)

    def _mk(self, theme_title, theme_artist, cand_name, cand_artist,
            status, decided_by, anime_cn="某番", with_candidate=True):
        aid = self.store.upsert_anime(self.sid, Anime(
            season_key="2026Q4", title_cn=anime_cn, animethemes_slug=f"s{theme_title}",
            season_bucket=SeasonBucket.TARGET, origin=Origin.BANGUMI,
        ))
        tid = self.store.upsert_theme(aid, Theme(
            anime_id=aid, kind=ThemeKind.OP, song_title=theme_title,
            artists=[theme_artist], sequence=1, source="animethemes",
            origin=Origin.ANIMETHEMES,
        ))
        cid = self.store.upsert_candidate(tid, Candidate(
            theme_id=tid, netease_song_id=f"id{theme_title}", name=cand_name,
            artists=[cand_artist], duration_ms=240_000, score=0.9,
            origin=Origin.SEARCH,
        ))
        # 真实代码里 missing_* 状态是不带候选的（set_decision(..., None)），
        # 所以这里要能模拟出来 —— 否则测不到「没选中候选就跳过」这条
        self.store.set_decision(tid, status, cid if with_candidate else None,
                                decided_by=decided_by)
        return aid, tid, cid

    def _orch(self):
        return Orchestrator(store=self.store, aliases=self.table, progress=lambda m: None)

    def test_learns_from_human_confirmed(self):
        self._mk("Urusiren", "梅田サイファー", "ウルサイレン", "Umeda Cypher",
                 DecisionStatus.CONFIRMED, "human")
        stat = self._orch().learn_from_season(self.sess_season)
        self.assertEqual(stat["checked"], 1)
        self.assertTrue(self.table.same_group("artist", "梅田サイファー", "Umeda Cypher"))
        self.assertTrue(self.table.same_group("title", "Urusiren", "ウルサイレン"))

    def test_ignores_auto_decisions_by_default(self):
        """**这是最重要的安全约束。**

        从自动结果里学，算法判错时会把错的对应关系固化进别名表；
        而别名是隐式生效的，之后分数会莫名其妙变高，极难排查。
        """
        self._mk("Urusiren", "梅田サイファー", "錯誤な曲", "間違った人",
                 DecisionStatus.AUTO, "auto")
        stat = self._orch().learn_from_season(self.sess_season)
        self.assertEqual(stat["checked"], 0, "不该学自动结果")
        self.assertEqual(self.table.stats(), {"anime": 0, "artist": 0, "title": 0})

    def test_can_opt_in_to_auto(self):
        self._mk("Urusiren", "梅田サイファー", "ウルサイレン", "Umeda Cypher",
                 DecisionStatus.AUTO, "auto")
        stat = self._orch().learn_from_season(self.sess_season, confirm_only=False)
        self.assertEqual(stat["checked"], 1)
        self.assertTrue(self.table.same_group("artist", "梅田サイファー", "Umeda Cypher"))

    def test_skips_undecided_and_unchosen(self):
        # missing_* 状态在真实流程里是不带候选的
        self._mk("A", "艺人A", "曲A", "艺人A", DecisionStatus.MISSING_MATCH, "auto",
                 with_candidate=False)
        stat = self._orch().learn_from_season(self.sess_season, confirm_only=False)
        self.assertEqual(stat["checked"], 0, "没有选中候选的不该学")
        self.assertEqual(self.table.stats(), {"anime": 0, "artist": 0, "title": 0})

    def test_no_write_when_nothing_learned(self):
        """没学到东西时不该落盘 —— 避免每次点下一步都改文件时间戳。"""
        self._mk("Same", "SameArtist", "Same", "SameArtist",
                 DecisionStatus.CONFIRMED, "human")
        self.table.path = os.path.join(_TMP, "should_not_exist.yaml")
        if os.path.exists(self.table.path):
            os.remove(self.table.path)
        self._orch().learn_from_season(self.sess_season)
        self.assertFalse(os.path.exists(self.table.path),
                         "没有新别名却写了文件")

    def test_second_run_is_noop(self):
        self._mk("Urusiren", "梅田サイファー", "ウルサイレン", "Umeda Cypher",
                 DecisionStatus.CONFIRMED, "human")
        orch = self._orch()
        orch.learn_from_season(self.sess_season)
        before = self.table.stats()
        self.table.dirty = False
        orch.learn_from_season(self.sess_season)
        self.assertEqual(self.table.stats(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
