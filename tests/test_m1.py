"""M1 单元测试 —— 覆盖设计文档里那几条**硬约束**与实测回归案例。

全部离线（不访问网络），用标准库 unittest，不需要 pytest。

    $env:PYTHONPATH = "<工作区>\src;<工作区>\vendor"
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

# 注意：刻意**不用 tempfile** —— 受限环境里 tempfile 清理会调 chmod 而被拒绝
# （见设计文档附录 G.2），所以测试产物一律落在工作区内自行清理。
_TMP = os.path.join(_ROOT, "data", "_test_tmp")
os.makedirs(_TMP, exist_ok=True)

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
from animusic.normalize import (  # noqa: E402
    AliasTable,
    artist_romaji_similarity,
    fold_romaji,
    is_cross_script,
    normalize_text,
    romanize,
    script_of,
    title_ratio,
)
from animusic.query_builder import build_queries, primary_artist  # noqa: E402
from animusic.scorer import Target  # noqa: E402
from animusic.sources.bangumi import BangumiSource, BangumiReadError  # noqa: E402
from animusic.store import Store  # noqa: E402


# ==================================================================== 归一化

class TestNormalize(unittest.TestCase):
    def test_fullwidth_and_brackets(self):
        self.assertEqual(normalize_text("【測試】ＬｉＳＡ"), "測試 lisa")
        # 括号去掉；"ver." 里的句点保留（Mr.Children 这类名字需要它）
        self.assertEqual(normalize_text("紅蓮華（TV ver.）"), "紅蓮華 tv ver.")

    def test_feat_stripped(self):
        self.assertEqual(normalize_text("Song feat. Someone"), "song")

    def test_cjk_punctuation_stripped(self):
        # 实测动机：花冷え。 与 HANABIE. 只差一个句号，
        # 归一后应能把标点去掉而不动拉丁内容
        self.assertEqual(normalize_text("花冷え。"), "花冷え")
        self.assertEqual(normalize_text("ＡＩＵＥＯ、"), "aiueo")
        # NFKC 会把全角 ！ 折成 ASCII !，所以两种形式都要能被剥掉
        self.assertEqual(normalize_text("ボールパークでShake！"), "ボールパークでshake")
        self.assertEqual(normalize_text("Hurray!!"), "hurray")

    def test_script_of(self):
        self.assertEqual(script_of("紅蓮華"), "cjk")
        self.assertEqual(script_of("FACSTORY"), "latin")
        self.assertEqual(script_of("ボールパークでShake!"), "mixed")
        self.assertEqual(script_of(""), "empty")

    def test_is_cross_script_includes_mixed(self):
        self.assertTrue(is_cross_script("花蔭", "Hanakage"))
        self.assertTrue(is_cross_script("ボールパークでShake!", "Ballpark de Shake!"))
        self.assertFalse(is_cross_script("紅蓮華", "紅蓮華"))

    def test_romanize(self):
        self.assertEqual(romanize("花蔭").replace(" ", ""), "hanakage")
        self.assertTrue(romanize("紅蓮華"))          # 读音不一定准，但要有输出
        self.assertEqual(romanize(""), "")
        self.assertEqual(romanize(None), "")

    def test_fold_romaji(self):
        self.assertEqual(fold_romaji("ziyou"), fold_romaji("jiyо".replace("о", "o")))


class TestTitleRatio(unittest.TestCase):
    """实测回归：这几组数字是校准过的，改动算法必须重跑。"""

    def test_identical(self):
        self.assertEqual(title_ratio("FACSTORY", "FACSTORY"), 1.0)

    def test_subset_is_not_a_perfect_match(self):
        """token_set_ratio 会把「子集」判成 1.0 —— 这是必须被长度惩罚挡住的。

        实测："Cocoon" 与 "Zutto Zutto Zutto ~cocoon Ver.~" 是两首不同的歌。
        """
        v = title_ratio("Cocoon", "Zutto Zutto Zutto ~cocoon Ver.~")
        self.assertLess(v, 0.6, f"子集不应被判高分，实际 {v}")

    def test_tv_version_not_high(self):
        """同歌不同版本可以高，但不能是满分（否则 TV size 会挤掉 full size）。"""
        v = title_ratio("紅蓮華", "紅蓮華 -TV ver.-")
        self.assertLess(v, 0.9)

    def test_cross_script_via_pykakasi(self):
        if not romanize("真昼の月"):
            self.skipTest("pykakasi 不可用")
        v = title_ratio("真昼の月", "Mahiru no Tsuki")
        self.assertGreater(v, 0.8, f"跨书写系统应靠罗马音救回，实际 {v}")

    def test_unrelated_is_low(self):
        """真正的不变量是「低于审核门槛」，而不是某个绝对小数。

        跨书写系统时罗马音比较会给出一点偶然重叠（实测 0.378），
        但只要稳稳低于 REVIEW_THRESHOLD，就会被判为匹配不上。
        """
        self.assertLess(title_ratio("拝啓、いつか愛した人へ", "Haru ni Kiete"),
                        constants.REVIEW_THRESHOLD)
        self.assertLess(title_ratio("神様", "Watashi dake no Polaris"),
                        constants.REVIEW_THRESHOLD)


class TestArtistRomaji(unittest.TestCase):
    def test_same_name(self):
        self.assertGreaterEqual(artist_romaji_similarity("WANIMA", "WANIMA"), 0.99)

    def test_reversed_name_order(self):
        """实测：日文姓名是「姓 名」，罗马音常写成「名 姓」。"""
        if not romanize("奥井雅美"):
            self.skipTest("pykakasi 不可用")
        self.assertGreaterEqual(artist_romaji_similarity("奥井雅美", "Masami Okui"), 0.9)

    def test_katakana_vs_latin(self):
        if not romanize("花冷え。"):
            self.skipTest("pykakasi 不可用")
        self.assertGreaterEqual(artist_romaji_similarity("花冷え。", "HANABIE."), 0.9)

    def test_unrelated(self):
        self.assertLess(artist_romaji_similarity("MIYAVI", "Leon Yoon"), 0.6)


# ==================================================================== 打分器

class TestScorer(unittest.TestCase):
    def _t(self, **kw):
        base = dict(song_title="TEST", artists=["Artist"], anime_names=["Anime"], kind="OP")
        base.update(kw)
        return Target(**base)

    def test_exact_match_without_alias_reaches_auto(self):
        """关键校准：新歌没有 alias 时，正确匹配必须能进自动区。

        实测 FACSTORY 曾卡在 0.847（阈值 0.85）掉出自动区。
        """
        t = self._t(song_title="FACSTORY", artists=["MeseMoa."])
        s = scorer.score(
            {"name": "FACSTORY", "artists": ["MeseMoa."], "alias": [],
             "duration_ms": 240_000, "tags": [], "search_rank": 0},
            t,
        )
        self.assertGreaterEqual(s.score, constants.AUTO_THRESHOLD,
                                f"正确匹配应自动通过，实际 {s.score}")
        self.assertEqual(scorer.classify(s.score), "auto")

    def test_anime_alias_is_the_strongest_signal(self):
        t = self._t(song_title="紅蓮華", artists=["LiSA"], anime_names=["鬼灭之刃"], kind="OP")
        with_alias = scorer.score(
            {"name": "紅蓮華", "artists": ["LiSA"],
             "alias": ["TV动画《鬼灭之刃》片头曲"],
             "duration_ms": 236_000, "tags": ["原唱"], "search_rank": 0}, t)
        without = scorer.score(
            {"name": "紅蓮華", "artists": ["LiSA"], "alias": [],
             "duration_ms": 236_000, "tags": [], "search_rank": 0}, t)
        self.assertGreater(with_alias.score, without.score)
        self.assertEqual(with_alias.detail["anime_alias"], 1.0)

    def test_alias_kind_mismatch_penalised(self):
        """标注的是 ED，但目标要 OP —— 应只给 0.4 而不是 1.0。"""
        t = self._t(song_title="X", artists=["A"], anime_names=["鬼灭之刃"], kind="OP")
        s = scorer.score(
            {"name": "X", "artists": ["A"], "alias": ["TV动画《鬼灭之刃》片尾曲"],
             "duration_ms": 240_000, "tags": []}, t)
        self.assertEqual(s.detail["anime_alias"], 0.4)

    def test_tv_size_penalised(self):
        t = self._t(song_title="Song", artists=["A"])
        full = scorer.score({"name": "Song", "artists": ["A"], "alias": [],
                             "duration_ms": 240_000, "tags": []}, t)
        tv = scorer.score({"name": "Song (TV size)", "artists": ["A"], "alias": [],
                           "duration_ms": 90_000, "tags": []}, t)
        self.assertGreater(full.score, tv.score)
        self.assertIn("tv_size", tv.detail["penalties"])

    def test_cover_penalised_but_not_when_artist_matches(self):
        t = self._t(song_title="Song", artists=["A"])
        cover = scorer.score({"name": "Song (Cover)", "artists": ["B"], "alias": [],
                              "duration_ms": 240_000, "tags": []}, t)
        self.assertIn("cover", cover.detail["penalties"])
        same = scorer.score({"name": "Song (Cover)", "artists": ["A"], "alias": [],
                             "duration_ms": 240_000, "tags": []}, t)
        self.assertNotIn("cover", same.detail["penalties"])

    def test_off_vocal_heavily_penalised(self):
        t = self._t(song_title="Song", artists=["A"])
        s = scorer.score({"name": "Song (off vocal)", "artists": ["A"], "alias": [],
                          "duration_ms": 240_000, "tags": []}, t)
        self.assertEqual(s.detail["penalties"].get("off_vocal"), 0.20)

    def test_wrong_song_stays_below_review(self):
        """「艺人对但曲名错」必须被挡在审核区外 —— 这是提高审核门槛的目的。"""
        t = self._t(song_title="Watashi dake no Polaris", artists=["mekakushe"])
        s = scorer.score({"name": "神様", "artists": ["mekakushe"], "alias": [],
                          "duration_ms": 240_000, "tags": []}, t)
        self.assertLess(s.score, constants.REVIEW_THRESHOLD,
                        f"同艺人的别的歌不该进审核区，实际 {s.score}")

    def test_pick_best_prefers_full_size_on_tie(self):
        a = {"score": 0.8, "duration_ms": 90_000, "netease_song_id": "1"}
        b = {"score": 0.8, "duration_ms": 240_000, "netease_song_id": "2"}
        self.assertEqual(scorer.pick_best([a, b])["netease_song_id"], "2")

    def test_dedupe_key_ignores_id(self):
        x = {"name": "紅蓮華", "artists": ["LiSA"], "netease_song_id": "1"}
        y = {"name": "紅蓮華", "artists": ["LiSA"], "netease_song_id": "999"}
        self.assertEqual(scorer.dedupe_key(x), scorer.dedupe_key(y))


# ==================================================================== 搜索词

class TestQueryBuilder(unittest.TestCase):
    def test_latin_title_puts_artist_first(self):
        """实测：罗马音曲名对网易云是噪声，必须先用「仅艺人」找。"""
        p = build_queries("Kienai Riyuu", ["WANIMA"], ["炎炎ノ消防隊"], "OP", limit=4)
        self.assertEqual(p.queries[0], "WANIMA 炎炎ノ消防隊")
        self.assertIn("WANIMA", p.queries)

    def test_cjk_title_uses_title_first(self):
        p = build_queries("消えない理由", ["WANIMA"], ["炎炎ノ消防隊"], "OP", limit=4)
        self.assertEqual(p.queries[0], "消えない理由 WANIMA")

    def test_dedup_and_limit(self):
        p = build_queries("Song", ["A"], ["Anime"], "OP", limit=4)
        self.assertEqual(len(p.queries), len(set(p.queries)))
        self.assertLessEqual(len(p.queries), 4)

    def test_primary_artist_strips_feat(self):
        self.assertEqual(primary_artist(["LiSA feat. X"]), "LiSA")
        self.assertEqual(primary_artist(["A / B"]), "A")
        self.assertEqual(primary_artist([]), "")


# ==================================================================== 季度

class TestSeason(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(Season.parse("2026Q4"), Season(2026, 4))
        self.assertEqual(Season.parse("2026q1"), Season(2026, 1))
        with self.assertRaises(ValueError):
            Season.parse("2026")

    def test_current_maps_every_month_to_its_quarter(self):
        """`Season.current()` 是**所有 `--season` 默认值的唯一来源**。

        以前季度硬编码 `2026Q4` 散在 9 处 —— 后果不是「可能会错」，
        而是**下个季度一定会静默做错事**。所以分季边界必须钉死。
        """
        from datetime import date

        cases = {
            1: 1, 2: 1, 3: 1,        # 冬
            4: 2, 5: 2, 6: 2,        # 春
            7: 3, 8: 3, 9: 3,        # 夏
            10: 4, 11: 4, 12: 4,     # 秋
        }
        for m, q in cases.items():
            with self.subTest(month=m):
                self.assertEqual(Season.current(date(2026, m, 15)), Season(2026, q))

    def test_current_handles_the_year_boundary(self):
        """跨年是最容易写错的地方：12 月是 Q4，1 月要**进到下一年**。"""
        from datetime import date

        self.assertEqual(Season.current(date(2026, 12, 31)), Season(2026, 4))
        self.assertEqual(Season.current(date(2027, 1, 1)), Season(2027, 1))

    def test_current_default_is_today(self):
        from datetime import date

        self.assertEqual(Season.current(), Season.current(date.today()))
        self.assertIn(Season.current().quarter, (1, 2, 3, 4))

    def test_no_hardcoded_season_left_in_the_entry_points(self):
        """源码里不许再出现写死的季度默认值。

        这条是**回归哨兵**：季度写死过一次（9 处），就是这个 bug 让
        「换个季度跑」变成「静默处理错的季度」。允许出现的地方只有
        注释、help 文本、以及测试自己。
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[1]
        bad = []
        for rel in ("src/animusic/cli.py", "run_gui.py", "src/animusic/gui/session.py"):
            for i, line in enumerate((root / rel).read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if re.search(r'default\s*=\s*"20\d\dQ[1-4]"|Season\.parse\("20\d\dQ[1-4]"\)', code):
                    bad.append(f"{rel}:{i}: {line.strip()}")
        self.assertEqual(bad, [], "又出现写死的季度默认值了：\n" + "\n".join(bad))

    def test_playlist_name_matches_user_convention(self):
        # 用户现有命名习惯：26年4月 / 25年10月
        self.assertEqual(constants.playlist_name(2026, 4), "26年10月")
        self.assertEqual(constants.playlist_name(2026, 2), "26年4月")
        self.assertEqual(constants.playlist_name(2025, 1), "25年1月")


class TestBangumiClassify(unittest.TestCase):
    def _s(self, air):
        from datetime import date

        from animusic.models import BangumiSubject

        return BangumiSubject(bgm_id=1, title_cn="X", air_date=air)

    def test_target(self):
        from datetime import date

        b = BangumiSource.classify(self._s(date(2026, 10, 1)), Season(2026, 4))
        self.assertEqual(b, SeasonBucket.TARGET)

    def test_cross_season(self):
        from datetime import date

        # 年番：首播在 Q1，但 10 月仍在播
        b = BangumiSource.classify(self._s(date(2026, 2, 1)), Season(2026, 4))
        self.assertEqual(b, SeasonBucket.CROSS_SEASON)

    def test_future_is_other(self):
        from datetime import date

        b = BangumiSource.classify(self._s(date(2027, 1, 1)), Season(2026, 4))
        self.assertEqual(b, SeasonBucket.OTHER)

    def test_unknown_when_no_date(self):
        self.assertEqual(
            BangumiSource.classify(self._s(None), Season(2026, 4)),
            SeasonBucket.UNKNOWN,
        )

    def test_filter_includes_cross_season(self):
        from datetime import date

        subs = [self._s(date(2026, 10, 1)), self._s(date(2026, 2, 1)), self._s(date(2027, 1, 1))]
        got = BangumiSource.filter_by_season(subs, Season(2026, 4), include_cross_season=True)
        self.assertEqual(len(got), 2)
        got2 = BangumiSource.filter_by_season(subs, Season(2026, 4), include_cross_season=False)
        self.assertEqual(len(got2), 1)

    def test_parse_api_body_rejects_html(self):
        with self.assertRaises(BangumiReadError):
            BangumiSource._parse_api_body("<!DOCTYPE html><html>...")

    def test_parse_api_body_ok(self):
        d = BangumiSource._parse_api_body('{"data":[{"subject":{"id":1}}],"total":1}')
        self.assertEqual(d["total"], 1)


# ==================================================================== 存储

class TestStore(unittest.TestCase):
    """重点验证 §8.3 那条硬约束：手工数据在重跑时绝不被覆盖。"""

    def setUp(self):
        self.path = os.path.join(_TMP, f"store_{id(self)}.db")
        for p in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(p):
                os.remove(p)
        self.store = Store(self.path)
        self.sid = self.store.get_season_id(2026, 4)

    def tearDown(self):
        self.store.close()
        for p in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(p):
                os.remove(p)

    def _anime(self, **kw):
        d = dict(season_key="2026Q4", title_cn="番A", animethemes_slug="slug-a")
        d.update(kw)
        return Anime(**d)

    def test_upsert_anime_idempotent(self):
        a = self.store.upsert_anime(self.sid, self._anime())
        b = self.store.upsert_anime(self.sid, self._anime())
        self.assertEqual(a, b)
        self.assertEqual(self.store.counts(self.sid)["anime"], 1)

    def test_manual_data_is_protected(self):
        """手工改过的番剧名不能被下一次拉取覆盖。"""
        aid = self.store.upsert_anime(self.sid, self._anime(title_cn="我的名字"))
        self.store.conn.execute("UPDATE anime SET origin='manual' WHERE id=?", (aid,))
        self.store.conn.commit()

        self.store.upsert_anime(self.sid, self._anime(title_cn="数据源名字"))
        row = self.store.anime_list(self.sid)[0]
        self.assertEqual(row["title_cn"], "我的名字", "手工值被覆盖了 —— 违反 §8.3 硬约束")

    def test_manual_data_still_fills_empty_fields(self):
        """只补空，不覆盖。"""
        aid = self.store.upsert_anime(self.sid, self._anime(title_cn="手工名", title_jp=None))
        self.store.conn.execute("UPDATE anime SET origin='manual' WHERE id=?", (aid,))
        self.store.conn.commit()

        self.store.upsert_anime(self.sid, self._anime(title_cn="源名", title_jp="源日文名"))
        row = self.store.anime_list(self.sid)[0]
        self.assertEqual(row["title_cn"], "手工名")
        self.assertEqual(row["title_jp"], "源日文名", "空字段应该被补上")

    def test_theme_upsert_dedup(self):
        aid = self.store.upsert_anime(self.sid, self._anime())
        t = Theme(anime_id=aid, kind=ThemeKind.OP, song_title="Song", artists=["A"], sequence=1)
        x = self.store.upsert_theme(aid, t)
        y = self.store.upsert_theme(aid, t)
        self.assertEqual(x, y)
        self.assertEqual(len(self.store.theme_list(self.sid)), 1)

    def test_candidate_upsert_keeps_one_row_per_song(self):
        aid = self.store.upsert_anime(self.sid, self._anime())
        tid = self.store.upsert_theme(aid, Theme(anime_id=aid, kind=ThemeKind.OP,
                                                 song_title="Song", artists=["A"]))
        c = Candidate(theme_id=tid, netease_song_id="123", name="Song", artists=["A"])
        i1 = self.store.upsert_candidate(tid, c)
        c.score = 0.9
        i2 = self.store.upsert_candidate(tid, c)
        self.assertEqual(i1, i2)
        self.assertEqual(len(self.store.candidate_list(tid)), 1)
        self.assertAlmostEqual(self.store.candidate_list(tid)[0]["score"], 0.9, places=3)

    def test_decision_upsert(self):
        aid = self.store.upsert_anime(self.sid, self._anime())
        tid = self.store.upsert_theme(aid, Theme(anime_id=aid, kind=ThemeKind.OP,
                                                 song_title="S", artists=["A"]))
        self.store.set_decision(tid, DecisionStatus.PENDING_REVIEW)
        self.store.set_decision(tid, DecisionStatus.CONFIRMED, decided_by="human")
        m = self.store.decision_map(self.sid)
        self.assertEqual(m[tid]["status"], "confirmed")
        self.assertEqual(m[tid]["decided_by"], "human")

    def test_retry_only_targets_missing_states(self):
        """§5.12：--retry-missing 只重试 missing_source / missing_netease，
        不能碰 missing_match（那个需要人工，重试没用）。"""
        aid = self.store.upsert_anime(self.sid, self._anime())
        ids = {}
        for i, st in enumerate([DecisionStatus.MISSING_SOURCE,
                                DecisionStatus.MISSING_NETEASE,
                                DecisionStatus.MISSING_MATCH,
                                DecisionStatus.AUTO]):
            tid = self.store.upsert_theme(
                aid, Theme(anime_id=aid, kind=ThemeKind.OP, song_title=f"S{i}", artists=["A"]))
            self.store.set_decision(tid, st)
            ids[st] = tid
        retryable = {r["id"] for r in self.store.retryable_themes(self.sid)}
        self.assertIn(ids[DecisionStatus.MISSING_SOURCE], retryable)
        self.assertIn(ids[DecisionStatus.MISSING_NETEASE], retryable)
        self.assertNotIn(ids[DecisionStatus.MISSING_MATCH], retryable)
        self.assertNotIn(ids[DecisionStatus.AUTO], retryable)

    def test_pushed_is_idempotent(self):
        self.assertFalse(self.store.is_pushed("26年10月", "123"))
        self.store.mark_pushed("26年10月", "123", None)
        self.store.mark_pushed("26年10月", "123", None)
        self.assertTrue(self.store.is_pushed("26年10月", "123"))
        self.assertEqual(len(self.store.pushed_songs("26年10月")), 1)

    def test_downstream_dirty_keeps_data(self):
        """§8.4：上游改动 → 下游标 dirty，但数据保留不清空。"""
        aid = self.store.upsert_anime(self.sid, self._anime())
        self.store.upsert_theme(aid, Theme(anime_id=aid, kind=ThemeKind.OP,
                                           song_title="S", artists=["A"]))
        self.store.set_stage(self.sid, "theme", "confirmed", confirmed=True)
        self.store.set_stage(self.sid, "match", "confirmed", confirmed=True)

        self.store.mark_downstream_dirty(self.sid, "anime")
        self.assertEqual(str(self.store.get_stage(self.sid, "theme")), "dirty")
        self.assertEqual(str(self.store.get_stage(self.sid, "match")), "dirty")
        self.assertEqual(len(self.store.theme_list(self.sid)), 1, "数据不应被清空")


# ==================================================================== 别名表

class TestAliasTable(unittest.TestCase):
    def test_expand_and_learn(self):
        t = AliasTable()
        t.anime = {"炎炎ノ消防隊": ["Fire Force", "Enen no Shouboutai"]}
        names = t.expand_names("anime", ["Enen no Shouboutai"])
        self.assertIn("炎炎ノ消防隊", names)
        self.assertIn("Fire Force", names)

    def test_learn_new_variant(self):
        t = AliasTable()
        self.assertTrue(t.learn("artist", "女王蜂", "Ziyoou-vachi"))
        self.assertFalse(t.learn("artist", "女王蜂", "Ziyoou-vachi"))  # 重复不写
        self.assertIn("Ziyoou-vachi", t.variants_of("artist", "女王蜂"))

    def test_roundtrip_yaml(self):
        t = AliasTable()
        t.anime = {"A": ["a1", "a2"]}
        t.artist = {"B": ["b1"]}
        d = os.path.join(_TMP, "alias_roundtrip")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "aliases.yaml")
        t.save(p)
        t2 = AliasTable.load(p)
        self.assertEqual(t2.anime, t.anime)
        self.assertEqual(t2.artist, t.artist)
        os.remove(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
