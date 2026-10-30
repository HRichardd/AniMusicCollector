"""GUI 端到端流程测试 —— 不需要网络，也不需要浏览器。

覆盖的是**接线**而不是界面外观：
    · MainWindow 能拿 session 构造出来
    · 四个页面的工具栏按钮确实接到了真实动作上（不再是「（原型）」占位）
    · 点「下一步」会把本页的人工修改落库
    · reload() 能把库里的变化反映到页面上
    · 换季度、重跑拉取都不会破坏手工数据

为什么值得写：这些连接是**静默失败**的 ——
按钮接错了、save 没被调用，界面看起来完全正常，只有用户点到才会发现。
"""

from __future__ import annotations

import gc
import os
import sys
import threading
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

# ⚠️ 必须在 import animusic **之前**设置（别名自学习会回写配置目录）
os.environ.setdefault(
    "ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, "data", "_test_tmp", "config")
)

try:
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication, QMessageBox, QSplitter

    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

if HAS_QT:
    from animusic.gui import style
    from animusic.gui.main_window import MainWindow
    from animusic.gui.session import WizardSession
    from animusic.models import (
        Anime,
        Candidate,
        DecisionStatus,
        Origin,
        Season,
        SeasonBucket,
        Theme,
        ThemeKind,
    )
    from animusic.store import Store

_TMP = os.path.join(_ROOT, "data", "_test_tmp")


@unittest.skipUnless(HAS_QT, "需要 PySide6")
class TestGuiFlow(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        os.makedirs(_TMP, exist_ok=True)
        cls.app = QApplication.instance() or QApplication(sys.argv)
        cls.app.setStyleSheet(style.QSS)

    def setUp(self):
        self.db = os.path.join(_TMP, f"flow_{id(self)}.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)
        self.store = Store(self.db)
        self.sess = WizardSession(store=self.store, season=Season.parse("2026Q4"))
        self.win = MainWindow(self.sess, probe_enabled=False)
        self._pump()

    def tearDown(self):
        self.win.close()
        self._pump()
        # ⚠️ 窗口必须**在主线程里当场析构**，不能丢给 GC。
        #
        # 踩过：MainWindow 带着一个活动的 QTimer；测试结束只 close() 的话，
        # 那个窗口会一直挂在引用环里，直到**某个后台线程**（例如假 HTTP 服务器、
        # 或本文件里的工作线程）触发一次分代 GC 时才被析构 ——
        # Qt 于是报 "Timers cannot be stopped from another thread"，
        # 并实测把整个进程带走（退出码 0xC0000409，全量测试连汇总都不打）。
        # 症状是**间歇性**的：同一份代码重跑可能全绿，特别容易被当成偶发。
        self.win.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.win = None
        gc.collect()                # 在主线程里把 Qt 对象环收干净
        self.store.close()
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)

    def _pump(self, n: int = 6):
        for _ in range(n):
            self.app.processEvents()

    def _seed(self):
        sid = self.sess.season_id
        aid = self.store.upsert_anime(sid, Anime(
            season_key="2026Q4", title_cn="测试番", title_jp="テスト",
            animethemes_slug="t", season_bucket=SeasonBucket.TARGET,
            origin=Origin.BANGUMI,
        ))
        tid = self.store.upsert_theme(aid, Theme(
            anime_id=aid, kind=ThemeKind.OP, song_title="Song", artists=["A"],
            sequence=1, source="animethemes", origin=Origin.ANIMETHEMES,
        ))
        cid = self.store.upsert_candidate(tid, Candidate(
            theme_id=tid, netease_song_id="123", name="Song", artists=["A"],
            duration_ms=240_000, score=0.9, score_detail={"title": 1.0},
            origin=Origin.SEARCH,
        ))
        return aid, tid, cid

    # ---------------------------------------------------------------- 构造与接线

    def test_window_constructs_with_empty_db(self):
        self.assertEqual(len(self.win.pages), 4)
        for p in self.win.pages:
            self.assertEqual(p.model.rowCount(), 0)

    def test_toolbar_buttons_are_real(self):
        """不能有残留的「（原型）」占位按钮。"""
        for i, p in enumerate(self.win.pages):
            texts = [p.actions_layout.itemAt(j).widget().text()
                     for j in range(p.actions_layout.count())]
            self.assertTrue(texts, f"步骤{i+1} 没有工具栏按钮")
            for t in texts:
                self.assertNotIn("原型", t, f"步骤{i+1} 还有占位按钮：{t}")

    def test_primary_action_is_first(self):
        self.assertIn("拉取", self.win.pages[0].actions_layout.itemAt(0).widget().text())
        self.assertIn("匹配", self.win.pages[2].actions_layout.itemAt(0).widget().text())

    # ---------------------------------------------------------------- 数据反映

    def test_reload_populates_pages(self):
        self._seed()
        self.win.reload()
        self._pump()
        self.assertEqual(self.win.pages[0].model.rowCount(), 1)
        self.assertEqual(self.win.pages[1].model.rowCount(), 1)
        self.assertEqual(self.win.pages[2].model.rowCount(), 1)

    def test_status_bar_reflects_data(self):
        self._seed()
        self.win.reload()
        self._pump()
        msg = self.win.status.currentMessage()
        self.assertIn("26年10月", msg)
        self.assertIn("番剧 1", msg)

    def test_match_page_pending_before_matching(self):
        self._seed()
        self.win.reload()
        self._pump()
        mp = self.win.pages[2]
        self.assertIn("待匹配 1", mp.stat.text())
        self.assertIn("1 条需要处理", mp.badge.text())

    def test_match_page_stats_after_decision(self):
        _, tid, cid = self._seed()
        self.store.set_decision(tid, DecisionStatus.AUTO, cid)
        self.win.reload()
        self._pump()
        mp = self.win.pages[2]
        self.assertIn("自动 1", mp.stat.text())

    # ---------------------------------------------------------------- 曲目数的两个口径

    def test_status_shows_both_theme_counts_when_they_differ(self):
        """界面上「曲目」与报告里「新增曲目」口径不同 —— 差的时候要说清。

        用户实测踩过：取消勾选一条曲目后界面显示 14，而拉取报告写「新增曲目 15 条」，
        看起来像丢了一条。现在两个口径都摆出来。
        """
        aid, tid, _cid = self._seed()
        self.store.upsert_theme(aid, Theme(
            anime_id=aid, kind=ThemeKind.ED, song_title="被排除的曲", artists=["B"],
            sequence=1, source="wikipedia", origin=Origin.SEARCH, included=False,
        ))
        self.win.reload()
        self._pump()
        msg = self.win.status.currentMessage()
        # ⚠️ 措辞不许是「未收录」：那一行的数据**有**，只是用户在②没勾。
        self.assertIn("曲目 1（共 2：1 条在②取消勾选）", msg)
        self.assertNotIn("未收录", msg)

    def test_theme_text_says_nothing_extra_when_counts_agree(self):
        """没有没勾上的曲目时不要添乱 —— 只打「曲目 N」。"""
        from animusic.gui.main_window import MainWindow

        self.assertEqual(MainWindow._theme_text({"theme": 15, "theme_total": 15}), "曲目 15")
        self.assertEqual(MainWindow._theme_text({"theme": 3}), "曲目 3")

    def test_theme_text_names_both_reasons_separately(self):
        """两个排除原因**去处不同**，所以要分开说：

            · ②里取消勾选 → 回②把曲目勾上
            · 所属番在①被排除 → 回①把番勾上

        合成一句「2 条未勾选」用户还是不知道该去哪一步。
        """
        from animusic.gui.main_window import MainWindow

        self.assertEqual(
            MainWindow._theme_text({"theme": 8, "theme_total": 10,
                                    "theme_unchecked": 1, "theme_anime_excluded": 1}),
            "曲目 8（共 10：1 条在②取消勾选 · 1 条的番在①被排除）")
        self.assertEqual(
            MainWindow._theme_text({"theme": 9, "theme_total": 10,
                                    "theme_unchecked": 0, "theme_anime_excluded": 1}),
            "曲目 9（共 10：1 条的番在①被排除）")
        # 算不出原因（手工拼的 stats）时只说实话，不编来源
        self.assertEqual(MainWindow._theme_text({"theme": 9, "theme_total": 10}),
                         "曲目 9（共 10：1 条未勾选）")

    # ---------------------------------------------------------------- 落库

    def test_confirm_anime_saves_all_rows_and_only_advances(self):
        self._seed()
        self.win.reload()
        page = self.win.pages[0]
        self.assertEqual(page.next_btn.text(), "确认番单")
        self.assertNotIn("note", [c.key for c in page.columns])
        page.model.rows[0].update(title_cn="确认后的名字", included=False)
        page.filter_combo.setCurrentText("仅当季")
        before_themes = [dict(r) for r in self.store.conn.execute("SELECT * FROM theme")]
        with patch.object(self.sess, "run_operation") as operation, patch.object(self.win, "_run_task") as task, \
                patch.object(self.win, "log") as log:
            page.next_btn.click()
            self.assertEqual(self.win.stack.currentIndex(), 1)
            self.win._goto(0)
            page.next_btn.click()
            operation.assert_not_called()
            task.assert_not_called()
            self.assertTrue(any("番单保存完成" in call.args[0] for call in log.call_args_list))
        saved = self.store.anime_list(self.sess.season_id, only_included=False)[0]
        self.assertEqual(saved["title_cn"], "确认后的名字")
        self.assertEqual(saved["included"], 0)
        self.assertEqual(before_themes, [dict(r) for r in self.store.conn.execute("SELECT * FROM theme")])

    def test_confirm_anime_save_failure_stays_on_first_page(self):
        with patch.object(self.sess, "save_anime", side_effect=ValueError("保存失败")), \
                patch.object(QMessageBox, "warning") as warning, \
                patch.object(self.win, "_run_task") as task, patch.object(self.win, "log") as log:
            self.win.pages[0].next_btn.click()
            self.assertEqual(self.win.stack.currentIndex(), 0)
            warning.assert_called_once()
            task.assert_not_called()
            self.assertFalse(any("番单保存完成" in call.args[0] for call in log.call_args_list))

    def test_anime_toolbar_only_offers_anime_fetch(self):
        page = self.win.pages[0]
        buttons = [page.actions_layout.itemAt(j).widget() for j in range(page.actions_layout.count())]
        texts = [b.text() for b in buttons]
        self.assertNotIn("仅从 AnimeThemes 拉当季", texts)
        self.assertNotIn("增量重跑（周更）", texts)
        self.assertNotIn("排除补充的番", texts)
        pending = next(b for b in buttons if b.text() == "当季全部（来源待定）")
        self.assertFalse(pending.isEnabled())
        with patch.object(self.win, "_run_task") as task, patch.object(self.sess, "run_operation") as operation:
            buttons[0].click()
            task.assert_called_once()
            task.call_args.args[0]()
            self.assertEqual(operation.call_args.args[0], "fetch_anime_list")

    def test_next_saves_anime_edits(self):
        self._seed()
        self.win.reload()
        self._pump()
        page = self.win.pages[0]
        page.model.rows[0]["title_cn"] = "我改的名字"
        self.win._goto(0)
        self.win._next()
        self._pump()
        after = self.store.anime_list(self.sess.season_id)[0]
        self.assertEqual(after["title_cn"], "我改的名字")

    def test_next_saves_edits_to_rows_hidden_by_the_filter(self):
        """筛选只影响**显示** —— 点「下一步」时被筛掉的行也必须落库。

        否则用户筛着「已排除」改完东西再点下一步，那些当时没显示出来的编辑
        会被**静默丢掉**（界面看起来一切正常，没有一个地方会报错）。
        这条同时是「落库读全量」这个约定的回归哨兵：`_next()` 读的是
        `page.all_rows()` 而不是「当前看得见的行」。
        """
        self._seed()
        self.win.reload()
        self._pump()
        page = self.win.pages[0]
        page.model.rows[0]["title_cn"] = "改过的名字"
        # 筛到「已排除」：这一行是 included=True，于是它被藏起来
        page.filter_combo.setCurrentText("已排除")
        self._pump()
        self.assertEqual(len(page.visible_rows()), 0, "夹具前提：这一行应当被筛掉")

        self.win._goto(0)
        self.win._next()
        self._pump()
        after = self.store.anime_list(self.sess.season_id)[0]
        self.assertEqual(after["title_cn"], "改过的名字",
                         "被筛选隐藏的行的编辑没有落库 —— 用户的修改被静默丢了")

    def test_next_advances_step(self):
        self.win._goto(0)
        self.win._next()
        self._pump()
        self.assertEqual(self.win.stack.currentIndex(), 1)

    def test_next_saves_match_decision(self):
        _, tid, cid = self._seed()
        self.win.reload()
        self._pump()
        mp = self.win.pages[2]
        mp.model.rows[0]["chosen_id"] = "123"
        mp.model.rows[0]["status"] = "confirmed"
        self.win._goto(2)
        self.win._next()
        self._pump()
        d = self.store.decision_map(self.sess.season_id)[tid]
        self.assertEqual(d["status"], "confirmed")
        self.assertEqual(d["decided_by"], "human")

    def test_back_marks_dirty_but_keeps_data(self):
        self._seed()
        self.win.reload()
        self.win._goto(2)
        self.win._back()
        self._pump()
        self.assertEqual(self.win.stack.currentIndex(), 1)
        # 数据不能被清掉（§8.4）
        self.assertEqual(self.win.pages[2].model.rowCount(), 1)

    # ---------------------------------------------------------------- 第④步「完成」

    def test_step4_finish_saves_selection_without_touching_decisions(self):
        """第④步点「完成」→ 保存**选择**，绝不动 decision（这就是崩溃点）。

        修之前：「完成」调的是 `save_decisions()`（第③步的 API），
        它把推送页那列给人看的 `status`（"待推送"）直接写进
        `decision.status`，撞 CHECK 约束 → `IntegrityError` + 弹「保存失败」。
        也就是说**第④步的保存从第一次点击起就必失败**。

        ⚠️ 这条路径会弹模态 `QMessageBox.information`（完成提示）。
        按 AGENTS.md §2：测试里必须把它 stub 掉，并断言「确实问过」——
        绝不能让它真弹出来等人点（那样 offscreen 下会永久挂住）。
        """
        _, tid, cid = self._seed()
        self.store.set_decision(tid, DecisionStatus.AUTO, cid)
        self.win.reload()
        self._pump()
        page = self.win.pages[3]
        self.assertEqual(page.model.rowCount(), 1, "第④步应有 1 行可推送")
        row = page.model.rows[0]
        row["playlist"] = "26年1月"        # 用户逐行改了目标歌单
        row["included"] = False            # 并且取消了勾选
        before = self.store.decision_map(self.sess.season_id)[tid]["status"]

        with patch.object(QMessageBox, "information") as info:
            self.win._goto(3)
            self.win._next()
        self._pump()

        self.assertTrue(info.called, "走完四步应当给一次完成提示")
        sel = self.store.push_selection_map(self.sess.season_id)
        sid = str(row["netease_song_id"])
        self.assertIn(sid, sel, "第④步的选择没有落库")
        self.assertEqual(sel[sid]["playlist"], "26年1月")
        self.assertFalse(sel[sid]["included"])
        self.assertEqual(self.store.decision_map(self.sess.season_id)[tid]["status"], before,
                         "第④步动了第③步的决策状态")

    def test_step4_rows_carry_chinese_status_and_that_is_fine(self):
        """推送页的 `status` 就该是给人看的中文 —— 别为了迁就 SQL 改它。"""
        _, tid, cid = self._seed()
        self.store.set_decision(tid, DecisionStatus.AUTO, cid)
        self.win.reload()
        self._pump()
        self.assertEqual(self.win.pages[3].model.rows[0]["status"], "待推送")

    # ---------------------------------------------------------------- 跨线程

    def test_session_build_works_from_a_worker_thread(self):
        """`reload()` 读的 `session.build()` 会从后台线程被调用。

        修之前：`Store` 只有一条主线程建的 sqlite 连接，任何后台任务
        （拉 Bangumi / 匹配 / 推送）都会抛
        `ProgrammingError: SQLite objects created in a thread can only be used...`。
        """
        self._seed()
        box: dict = {}

        def job():
            try:
                box["data"] = self.sess.build()
            except BaseException as e:      # noqa: BLE001
                box["err"] = e
            finally:
                self.store.close_thread()   # TaskWorker 的 cleanup 就是这么做的

        t = threading.Thread(target=job, name="fake-TaskWorker")
        t.start()
        t.join(timeout=30)
        self.assertFalse(t.is_alive(), "后台线程没结束")
        self.assertNotIn("err", box, f"后台线程里读数据库报错：{box.get('err')}")
        self.assertEqual(len(box["data"]["anime"]), 1)

    # ---------------------------------------------------------------- 日志面板

    def _ancestors(self, w):
        out, p = [], w.parentWidget()
        while p is not None:
            out.append(p)
            p = p.parentWidget()
        return out

    def test_log_pane_is_resizable(self):
        """日志面板必须能长高、能拖（修之前是 `setFixedHeight(72)`）。

        `setFixedHeight` 同时钉死 minimum 和 maximum，等于告诉所有布局
        「这块永远 72px 高」—— 用户拖不动、程序也不给它长，报错时看不到上下文。
        """
        lp = self.win.logpane
        self.assertGreater(lp.maximumHeight(), 200, "日志面板的高度上限还是被钉死的")
        self.assertLess(lp.minimumHeight(), 200, "最小高度不该等于一个固定值")
        self.assertTrue(
            any(isinstance(p, QSplitter) for p in self._ancestors(lp)),
            "日志面板不在可拖动的分界（QSplitter）里，用户没法调大小",
        )

    def test_log_pane_toggle(self):
        """日志被收起后要能弄回来 —— 不能只靠「重开程序」。"""
        self.assertTrue(self.win.logpane.isVisibleTo(self.win))
        self.win.btn_log.setChecked(False)
        self._pump()
        self.assertFalse(self.win.logpane.isVisibleTo(self.win))
        self.assertIn("▸", self.win.btn_log.text())
        self.win.btn_log.setChecked(True)
        self._pump()
        self.assertTrue(self.win.logpane.isVisibleTo(self.win))

    # ---------------------------------------------------------------- 季度切换

    def test_season_switch_clears_view(self):
        self._seed()
        self.win.reload()
        self._pump()
        self.sess.set_season(Season.parse("2026Q1"))
        self.win.reload()
        self._pump()
        self.assertEqual(self.win.pages[0].model.rowCount(), 0)
        self.assertIn("26年1月", self.win.status.currentMessage())

    def test_season_combo_is_wired_to_session(self):
        """下拉框必须是**能用的控件**，不是装饰品。

        ⚠️ 这条测试就是之前缺的那条：下拉框曾经只列着硬编码的 2026 四季，
        全项目没有一处读它 —— 用户在界面上换季度，session 完全没变，
        而所有业务逻辑读的都是 session。界面看起来一切正常。
        """
        self._seed()
        self.win.reload()
        self._pump()
        idx = self.win.env.season.findText("2026 冬（1月）")
        self.assertGreaterEqual(idx, 0, "下拉框里没有 2026 冬")
        self.win.env.season.setCurrentIndex(idx)
        self._pump()
        self.assertEqual(self.sess.season, Season.parse("2026Q1"))
        # 下游步骤的视图要跟着清空（不能把上一季的行留在界面上）
        self.assertEqual(self.win.pages[0].model.rowCount(), 0)
        self.assertIn("26年1月", self.win.status.currentMessage())


if __name__ == "__main__":
    unittest.main(verbosity=2)
