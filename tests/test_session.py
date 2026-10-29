"""GUI 会话层测试 —— 校验「页面 ↔ 后端」的数据契约。

为什么单独测这个：
    页面对行字典的字段名是硬依赖（`chosen_id` / `candidates` / `status_label`…），
    改后端时如果漏了一个键，界面不会报错，只会**静默显示空白**，
    很难在人工点测里发现。

    另外这里覆盖两条最容易被破坏的约束：
      · **手工修改在重跑后不被覆盖**（§8.3）
      · **回退显示名不能被当成真名写回库**（否则编辑一次就污染数据）

全部离线，用 workspace 内的临时库（不用 tempfile —— 见附录 G.2）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

# ⚠️ 必须在 import animusic **之前**设置。
# 别名自学习会回写配置目录，不隔离的话跑一遍测试就会把
# 用户真实的 config/aliases.yaml 写成测试夹具数据（实测踩过）。
os.environ.setdefault(
    "ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, "data", "_test_tmp", "config")
)

from animusic import constants  # noqa: E402
from animusic.gui.session import STATUS_LABEL, WizardSession  # noqa: E402
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

# 页面真正读取的键（对照 pages.py / widgets.py 里的 Column 与 CandidateItem）
ANIME_KEYS = {
    "included", "title_cn", "title_jp", "air_date", "bucket_label", "origin_label", "note",
}
THEME_KEYS = {
    "included", "anime", "kind", "seq", "song_title", "artists", "origin_label", "note",
    # 排除的两个原因要分开报（「②里取消勾选」与「所属番在①被排除」去处不同），
    # 所以行上必须同时有「曲目自己」和「番」两个标志。
    "theme_included", "anime_included",
}
MATCH_KEYS = {
    "anime", "kind", "seq", "theme_title", "theme_artists", "chosen_name", "chosen_artists",
    "score", "status_label", "note", "candidates", "theme_id", "status",
}
CAND_KEYS = {
    "netease_song_id", "name", "artists", "album", "cover", "duration_ms",
    "tags", "alias", "score", "detail", "status",
}
PUSH_KEYS = {"included", "name", "artists", "netease_song_id", "playlist", "status"}


class SessionTestBase(unittest.TestCase):
    def setUp(self):
        os.makedirs(_TMP, exist_ok=True)
        self.db = os.path.join(_TMP, f"session_{id(self)}.db")
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

    # ---- 造数据
    def add_anime(self, cn="番A", jp="ばんA", slug=None, origin=Origin.BANGUMI, included=True):
        return self.store.upsert_anime(self.sid, Anime(
            season_key="2026Q4", title_cn=cn, title_jp=jp, animethemes_slug=slug,
            season_bucket=SeasonBucket.TARGET, origin=origin, included=included,
            air_date=None,
        ))

    def add_theme(self, aid, title="Song", kind=ThemeKind.OP, artists=("A",), seq=1,
                  origin=Origin.ANIMETHEMES, included=True):
        return self.store.upsert_theme(aid, Theme(
            anime_id=aid, kind=kind, song_title=title, artists=list(artists),
            sequence=seq, source="animethemes", origin=origin, included=included,
        ))

    def add_candidate(self, tid, song_id="123", name="Song", artists=("A",), score=0.9,
                      alias=None, dur=240_000):
        return self.store.upsert_candidate(tid, Candidate(
            theme_id=tid, netease_song_id=song_id, name=name, artists=list(artists),
            album="Album", cover="http://x/y.jpg", duration_ms=dur,
            tags=["VIP"], anime_alias=alias,
            score=score, score_detail={"title": 1.0, "artist": 1.0},
            search_rank=0, origin=Origin.SEARCH,
        ))


# ==================================================================== 契约

class TestBuildContract(SessionTestBase):
    def test_empty_build_has_all_sections(self):
        d = self.sess.build()
        self.assertEqual(set(d), {"anime", "theme", "match", "push", "missing_source"})
        for k in ("anime", "theme", "match", "push", "missing_source"):
            self.assertIsInstance(d[k], list)

    def test_anime_row_keys(self):
        self.add_anime()
        row = self.sess.build()["anime"][0]
        self.assertFalse(ANIME_KEYS - set(row), f"缺键 {ANIME_KEYS - set(row)}")

    def test_theme_row_keys(self):
        aid = self.add_anime()
        self.add_theme(aid)
        row = self.sess.build()["theme"][0]
        self.assertFalse(THEME_KEYS - set(row), f"缺键 {THEME_KEYS - set(row)}")

    def test_stats_reports_both_theme_counts(self):
        """曲目数必须有**两个口径**：收录的 N，与总行数。

        为什么要两个：界面只露 `theme`（只数收录），而拉取报告说的是
        「新增曲目 N 条」（写入的行数）。用户在步骤②取消勾选一条之后，
        界面从 15 变 14、报告仍说 15 —— 两个数都对，但只要显示一个，
        看起来就像丢了数据（实测被问过一次，见 DESIGN.md G.18）。
        """
        aid = self.add_anime()
        self.add_theme(aid, title="Keep", included=True)
        self.add_theme(aid, title="Dropped", kind=ThemeKind.ED, included=False)
        st = self.sess.stats()
        self.assertEqual(st["theme"], 1, "收录数只算勾上的")
        self.assertEqual(st["theme_total"], 2, "总行数要把未收录的也算上")
        # ⚠️ 差额的**原因**也要报出来，而且不许把它说成「未收录」（那是
        # 「数据源里没有」= MISSING_SOURCE）：这一条是用户在②自己取消勾选的。
        self.assertEqual(st["theme_unchecked"], 1)
        self.assertEqual(st["theme_anime_excluded"], 0)

    def test_match_row_and_candidate_keys(self):
        aid = self.add_anime()
        tid = self.add_theme(aid)
        self.add_candidate(tid)
        self.store.set_decision(tid, DecisionStatus.AUTO, 1)
        d = self.sess.build()
        row = d["match"][0]
        self.assertFalse(MATCH_KEYS - set(row), f"匹配行缺键 {MATCH_KEYS - set(row)}")
        self.assertTrue(row["candidates"], "候选列表不该为空")
        cand = row["candidates"][0]
        self.assertFalse(CAND_KEYS - set(cand), f"候选缺键 {CAND_KEYS - set(cand)}")

    def test_push_row_keys(self):
        aid = self.add_anime()
        tid = self.add_theme(aid)
        cid = self.add_candidate(tid)
        self.store.set_decision(tid, DecisionStatus.AUTO, cid)
        d = self.sess.build()
        self.assertEqual(len(d["push"]), 1)
        self.assertFalse(PUSH_KEYS - set(d["push"][0]), f"缺键 {PUSH_KEYS - set(d['push'][0])}")

    def test_unmatched_themes_are_pending_not_review(self):
        """还没跑匹配时必须显示「待匹配」，不能显示成「待审核」。

        否则用户刚拉完番单就看到满屏「待审核」，会误以为匹配已经跑过了。
        """
        aid = self.add_anime()
        self.add_theme(aid)
        d = self.sess.build()
        self.assertEqual(d["match"][0]["status"], "pending")
        self.assertEqual(d["match"][0]["status_label"], STATUS_LABEL["pending"])
        st = self.sess.stats(d)
        self.assertEqual(st["review"], 0, "未匹配的不应计入待审核")
        self.assertEqual(st["auto"], 0)

    def test_push_only_for_usable_statuses(self):
        aid = self.add_anime()
        ok = self.add_theme(aid, title="Good")
        bad = self.add_theme(aid, title="Bad", kind=ThemeKind.ED)
        c1 = self.add_candidate(ok, song_id="1")
        self.add_candidate(bad, song_id="2")
        self.store.set_decision(ok, DecisionStatus.AUTO, c1)
        self.store.set_decision(bad, DecisionStatus.MISSING_MATCH, None)
        d = self.sess.build()
        self.assertEqual(len(d["push"]), 1)
        self.assertEqual(d["push"][0]["netease_song_id"], "1")

    def test_confirmed_also_pushes(self):
        aid = self.add_anime()
        tid = self.add_theme(aid)
        cid = self.add_candidate(tid)
        self.store.set_decision(tid, DecisionStatus.CONFIRMED, cid, decided_by="human")
        d = self.sess.build()
        self.assertEqual(len(d["push"]), 1)
        self.assertEqual(d["match"][0]["status"], "confirmed")
        self.assertTrue(d["match"][0]["manual"])

    def test_missing_source_listed(self):
        """有番但一条 theme 都没有 → 应进「数据源未收录」列表。"""
        self.add_anime(cn="没收录的番")
        d = self.sess.build()
        self.assertIn("没收录的番", d["missing_source"])

    def test_candidate_alias_becomes_list(self):
        aid = self.add_anime()
        tid = self.add_theme(aid)
        self.add_candidate(tid, alias="TV动画《番A》片头曲")
        cand = self.sess.build()["match"][0]["candidates"][0]
        self.assertEqual(cand["alias"], ["TV动画《番A》片头曲"])
        self.assertIsInstance(cand["tags"], list)
        self.assertIsInstance(cand["detail"], dict)

    def test_candidate_status_uses_calibrated_thresholds(self):
        """候选卡片的配色阈值必须跟 constants 一致，不能写死 0.85/0.55。"""
        aid = self.add_anime()
        tid = self.add_theme(aid)
        self.add_candidate(tid, song_id="hi", score=constants.AUTO_THRESHOLD)
        self.add_candidate(tid, song_id="mid", score=constants.REVIEW_THRESHOLD)
        self.add_candidate(tid, song_id="lo", score=constants.REVIEW_THRESHOLD - 0.01)
        by_id = {c["netease_song_id"]: c for c in self.sess.build()["match"][0]["candidates"]}
        self.assertEqual(by_id["hi"]["status"], "auto")
        self.assertEqual(by_id["mid"]["status"], "pending_review")
        self.assertEqual(by_id["lo"]["status"], "low")


# ==================================================================== 显示回退

class TestTitleFallback(SessionTestBase):
    def test_fallback_used_for_display(self):
        # AnimeThemes 来源只有罗马音 + 日文名，没有中文名
        self.store.upsert_anime(self.sid, Anime(
            season_key="2026Q4", title_jp="薬屋のひとりごと", title_romaji="Kusuriya",
            animethemes_slug="k", origin=Origin.ANIMETHEMES,
        ))
        row = self.sess.build()["anime"][0]
        self.assertEqual(row["title_cn"], "薬屋のひとりごと", "应有可读的显示名")
        self.assertTrue(row["_cn_auto"], "要标记这是回退值")

    def test_fallback_is_not_written_back_as_real_name(self):
        """最关键的一条：回退显示名不能被当成真的中文名写回库。

        否则用户只是点了「下一步」，库里就会凭空多出一堆
        「中文名 = 日文名」的脏数据。
        """
        self.store.upsert_anime(self.sid, Anime(
            season_key="2026Q4", title_jp="薬屋のひとりごと", title_romaji="Kusuriya",
            animethemes_slug="k", origin=Origin.ANIMETHEMES,
        ))
        rows = self.sess.build()["anime"]
        self.sess.save_anime(rows)          # 模拟用户点了「下一步」
        after = self.store.anime_list(self.sid)[0]
        self.assertIn(after["title_cn"], (None, ""),
                      f"中文名被回退值污染了：{after['title_cn']!r}")

    def test_user_typed_name_is_saved(self):
        self.store.upsert_anime(self.sid, Anime(
            season_key="2026Q4", title_jp="薬屋のひとりごと", title_romaji="Kusuriya",
            animethemes_slug="k", origin=Origin.ANIMETHEMES,
        ))
        rows = self.sess.build()["anime"]
        rows[0]["title_cn"] = "药屋少女的呢喃"     # 用户真的填了
        self.sess.save_anime(rows)
        after = self.store.anime_list(self.sid)[0]
        self.assertEqual(after["title_cn"], "药屋少女的呢喃")

    def test_existing_cn_name_is_not_marked_auto(self):
        self.add_anime(cn="真中文名", jp="ほんとう")
        row = self.sess.build()["anime"][0]
        self.assertFalse(row["_cn_auto"])


# ==================================================================== 落库

class TestPersistence(SessionTestBase):
    def test_save_decisions_marks_human(self):
        aid = self.add_anime()
        tid = self.add_theme(aid)
        self.add_candidate(tid, song_id="999", name="Selected")
        rows = self.sess.build()["match"]
        rows[0]["chosen_id"] = "999"
        rows[0]["chosen_name"] = "Selected"
        rows[0]["status"] = "confirmed"
        self.sess.save_decisions(rows)

        d = self.store.decision_map(self.sid)[tid]
        self.assertEqual(d["status"], "confirmed")
        self.assertEqual(d["decided_by"], "human",
                         "人工确认必须记为 human，否则下次自动重跑会覆盖掉")

    def test_human_decision_picks_the_right_candidate(self):
        aid = self.add_anime()
        tid = self.add_theme(aid)
        self.add_candidate(tid, song_id="111", name="A", score=0.9)
        target = self.add_candidate(tid, song_id="222", name="B", score=0.1)
        rows = self.sess.build()["match"]
        rows[0]["chosen_id"] = "222"
        rows[0]["chosen_name"] = "B"
        rows[0]["status"] = "confirmed"
        self.sess.save_decisions(rows)
        d = self.store.decision_map(self.sid)[tid]
        self.assertEqual(d["chosen_candidate_id"], target)

    def test_manual_song_becomes_manual_candidate(self):
        """用户手工粘了一个链接/ID → 要建 manual 候选，之后重跑不会被清掉。"""
        aid = self.add_anime()
        tid = self.add_theme(aid)
        rows = self.sess.build()["match"]
        rows[0]["chosen_id"] = "888888"
        rows[0]["chosen_name"] = "手工找到的歌"
        rows[0]["chosen_artists"] = "某人、另一人"
        rows[0]["status"] = "confirmed"
        self.sess.save_decisions(rows)

        cands = self.store.candidate_list(tid)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["netease_song_id"], "888888")
        self.assertEqual(cands[0]["origin"], "manual")

    def test_save_themes_updates_and_adds(self):
        aid = self.add_anime()
        tid = self.add_theme(aid, title="Original", artists=("A",))
        rows = self.sess.build()["theme"]
        rows[0]["song_title"] = "Edited"
        rows[0]["artists"] = ["B", "C"]
        rows.append({
            "anime": "番A", "anime_id": aid, "kind": "ED", "seq": 1,
            "song_title": "Brand New", "artists": ["New"], "included": True, "note": "",
        })
        self.sess.save_themes(rows)

        titles = {r["song_title"]: r for r in self.store.theme_list(self.sid)}
        self.assertIn("Edited", titles)
        self.assertNotIn("Original", titles)
        self.assertEqual(titles["Edited"]["id"], tid)
        self.assertEqual(len(titles), 2)
        self.assertEqual(json.loads(titles["Edited"]["artists"]), ["B", "C"])
        self.assertIn("Brand New", titles)
        self.assertEqual(titles["Brand New"]["origin"], "manual")

    def test_save_themes_accepts_string_artists(self):
        """表格里编辑过的艺人可能是「A、B」这种字符串，得能吃掉。"""
        aid = self.add_anime()
        self.add_theme(aid, title="S")
        rows = self.sess.build()["theme"]
        rows[0]["artists"] = "A、B,C"
        self.sess.save_themes(rows)
        row = self.store.theme_list(self.sid)[0]
        self.assertEqual(json.loads(row["artists"]), ["A", "B", "C"])

    def test_manual_anime_survives_refetch(self):
        """§8.3 硬约束在会话层也要成立：手工改过的番重跑拉取不被覆盖。"""
        self.add_anime(cn="我改的名字", jp="かえた", slug="s")
        self.store.conn.execute(
            "UPDATE anime SET origin='manual', title_cn='我改的名字' WHERE season_id=?", (self.sid,))
        self.store.conn.commit()

        # 模拟一次重新拉取（同名 slug，但标题不同）
        self.store.upsert_anime(self.sid, Anime(
            season_key="2026Q4", title_cn="数据源名字", title_jp="かえた",
            animethemes_slug="s", origin=Origin.ANIMETHEMES,
        ))
        row = self.store.anime_list(self.sid)[0]
        self.assertEqual(row["title_cn"], "我改的名字", "手工值被覆盖了")


# ==================================================================== 状态落库

class TestDecisionStatusMapping(SessionTestBase):
    """界面上的状态字符串**永远不能**原样进 SQL。

    事故：第④步点「完成」时把推送页的行（`status` = "待推送"）交给了
    `save_decisions()`，撞上 `decision` 表的 CHECK 约束 →
    `IntegrityError: CHECK constraint failed: status IN (...)`，
    整步保存失败、弹窗（离线已复现，见 `.tmp/repro_bugs.py`）。

    根因不是「第④步传错了行」，而是 `save_decisions()` 把「界面上显示什么」
    直接当成「库里该存什么」。所以这里钉住两层：
      · 展示用的键（`STATUS_LABEL`）**一个都不能**把 SQL 弄崩；
      · 语义上不该落库的状态（⏳ 待匹配）必须**跳过并报出来**，不能瞎写一个值。
    """

    def _row(self, tid: int, status: str) -> dict:
        return {"theme_id": tid, "status": status, "chosen_id": "", "note": ""}

    def test_no_status_label_can_break_the_sql(self):
        """把 `STATUS_LABEL` 的**每一个**键都喂一遍 —— 全都不许抛异常。

        为什么用 `STATUS_LABEL` 当输入集合：它是「界面可能显示的状态」的
        唯一清单。新增一个展示状态却忘了给落库映射，这条会立刻红。
        """
        aid = self.add_anime()
        for i, label in enumerate(STATUS_LABEL):
            tid = self.add_theme(aid, title=f"T{i}")
            try:
                self.sess.save_decisions([self._row(tid, label)])
            except Exception as e:  # noqa: BLE001
                self.fail(f"状态 {label!r} 把保存弄崩了：{type(e).__name__}: {e}")

    def test_pending_is_not_written_at_all(self):
        """⏳ 待匹配 = 「还没做过决定」→ 库里就该没有这一行。

        写任何值都是把「没决定」记成「已决定」：界面下次会显示成
        「待审核」，用户再也看不出哪些曲目其实还没跑过匹配。
        """
        aid = self.add_anime()
        tid = self.add_theme(aid)
        stat = self.sess.save_decisions([self._row(tid, "pending")])
        self.assertNotIn(tid, self.store.decision_map(self.sid))
        self.assertEqual(stat["written"], 0)
        self.assertEqual([t for t, _ in stat["skipped"]], [tid])
        # 界面依然显示「待匹配」
        self.assertEqual(self.sess.build()["match"][0]["status"], "pending")

    def test_low_score_becomes_pending_review(self):
        """🔴 低分：库里没有这个状态，它的**含义**是「要人看」→ 待审核。"""
        aid = self.add_anime()
        tid = self.add_theme(aid)
        stat = self.sess.save_decisions([self._row(tid, "low")])
        self.assertEqual(stat["written"], 1)
        self.assertEqual(self.store.decision_map(self.sid)[tid]["status"], "pending_review")

    def test_push_page_label_is_reported_not_silently_swallowed(self):
        """第④步那种中文状态：不落库，但**必须报出来**。

        静默跳过比报错更坏 —— 用户点了「完成」，什么都没发生，也没有任何提示。
        """
        aid = self.add_anime()
        tid = self.add_theme(aid)
        stat = self.sess.save_decisions([self._row(tid, "待推送")])
        self.assertEqual(stat["written"], 0)
        self.assertEqual(stat["skipped"], [(tid, "待推送")])
        self.assertNotIn(tid, self.store.decision_map(self.sid))

    def test_unknown_status_is_not_a_crash(self):
        aid = self.add_anime()
        tid = self.add_theme(aid)
        stat = self.sess.save_decisions([self._row(tid, "完全没见过的状态")])
        self.assertEqual(stat["written"], 0)
        self.assertEqual(len(stat["skipped"]), 1)

    def test_valid_statuses_still_land(self):
        """映射不能把**正常**的状态弄丢。"""
        aid = self.add_anime()
        cases = {
            "auto": DecisionStatus.AUTO,
            "confirmed": DecisionStatus.CONFIRMED,
            "pending_review": DecisionStatus.PENDING_REVIEW,
            "skipped": DecisionStatus.SKIPPED,
            "missing_source": DecisionStatus.MISSING_SOURCE,
            "missing_netease": DecisionStatus.MISSING_NETEASE,
            "missing_match": DecisionStatus.MISSING_MATCH,
        }
        for i, (label, want) in enumerate(cases.items()):
            tid = self.add_theme(aid, title=f"C{i}")
            self.sess.save_decisions([self._row(tid, label)])
            got = self.store.decision_map(self.sid)[tid]["status"]
            self.assertEqual(got, str(want), f"{label} 落库成了 {got}")


# ==================================================================== 季度

class TestSeasonSwitch(SessionTestBase):
    def test_playlist_name(self):
        self.assertEqual(self.sess.playlist_name, "26年10月")

    def test_set_season_changes_id_and_name(self):
        self.sess.set_season(Season.parse("2026Q1"))
        self.assertEqual(self.sess.playlist_name, "26年1月")
        self.assertNotEqual(self.sess.season_id, self.sid)

    def test_seasons_are_isolated(self):
        self.add_anime(cn="秋季番")
        self.sess.set_season(Season.parse("2026Q1"))
        self.assertEqual(self.sess.build()["anime"], [],
                         "换季度后不应残留上一季的数据")


if __name__ == "__main__":
    unittest.main(verbosity=2)
