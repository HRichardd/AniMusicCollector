"""四个步骤的「筛选」—— 以及一条比筛选本身更重要的约束。

用户报的：第③步点「只看待审核」，再点「显示全部」不生效；右侧筛选框也不同步。
一复现发现比报告的更糟 —— 那是一条**死路**：

    点「只看待审核」 → 表格筛了，但筛选框还写着「全部」
    点「显示全部」   → 不生效（它用的是**已经被筛过**的 model.rows）
    再去点筛选框的「全部」 → 文本没变 → currentTextChanged 不发 → 什么都发生不了

    ⇒ 用户卡在筛选结果里，只能靠切到别的筛选项再切回来，或者换季度重来。

根因是「按钮」和「筛选框」各管一半的状态，两边都不把视图同步给对方。
同一个毛病在 ①②④ 上更彻底：那三页的筛选框**只往日志里打一行字**，
表格一行都不动（四个页面里只有一个真会筛）。

**最关键的一条是最后那组测试**：筛选只许改「看得见什么」，
绝不许改「会写进账号什么」。第④步的推送、四步的落库都必须读全量，
否则筛一下就会静默漏推 / 漏存。

全部离线：offscreen、不 show、不进事件循环、不联网、不碰账号
（构造窗口与断言 —— 见 AGENTS.md §2）。
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

try:
    from PySide6.QtWidgets import QApplication

    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

if HAS_QT:
    from animusic.gui.pages import (  # noqa: E402
        AnimePage,
        MatchPage,
        PushPage,
        ThemePage,
    )


# ==================================================================== 夹具

#: 第③步：7 条把**每一种**状态都覆盖到 —— 包括 skipped（它是用户的决定，
#: 不该被算进「需要处理」）。
MATCH_STATUSES = ["auto", "confirmed", "pending_review", "low", "pending",
                  "missing_netease", "skipped"]


def match_row(i: int, status: str) -> dict:
    return {
        "theme_id": i, "anime": f"番{i}", "kind": "OP", "seq": 1,
        "theme_title": f"曲{i}", "theme_artists": "A",
        "chosen_name": "—", "chosen_artists": "—", "chosen_id": "",
        "score": 0.0, "status": status, "status_label": status,
        "candidates": [], "note": "", "manual": False,
    }


def anime_row(cn: str, bucket: str, included: bool = True) -> dict:
    return {
        "included": included, "title_cn": cn, "title_jp": f"ばん{cn}", "air_date": "",
        "bucket": bucket, "bucket_label": bucket, "origin": "bangumi",
        "origin_label": "Bangumi", "note": "", "bgm_id": "", "animethemes_slug": "",
    }


def theme_row(anime: str, kind: str) -> dict:
    return {
        "included": True, "anime": anime, "anime_id": 1, "kind": kind, "seq": 1,
        "song_title": f"{kind}曲", "artists": ["A"], "origin": "animethemes",
        "origin_label": "数据源", "note": "",
    }


def push_row(name: str, status: str, song_id: str, included: bool = True) -> dict:
    return {
        "included": included, "name": name, "artists": "A",
        "netease_song_id": song_id, "playlist": "26年10月", "status": status,
        "theme_id": int(song_id), "anime": "番", "kind": "OP",
    }


@unittest.skipUnless(HAS_QT, "需要 PySide6")
class FilterTestBase(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def _pump(self, n: int = 4):
        for _ in range(n):
            self.app.processEvents()

    # ---- 夹具页面（都带一份**每一项都能筛出严格子集**的数据，
    #      这样「装饰品」筛选项会被 test_every_option_actually_narrows 抓到）
    def match_page(self) -> "MatchPage":
        page = MatchPage([match_row(i, s) for i, s in enumerate(MATCH_STATUSES, 1)])
        self.logs = []
        page.log.connect(self.logs.append)
        self._pump()
        return page

    def anime_page(self) -> "AnimePage":
        page = AnimePage([
            anime_row("当季番", "target", True),
            anime_row("跨季番", "cross_season", True),
            anime_row("排除番", "other", False),
        ])
        self.logs = []
        page.log.connect(self.logs.append)
        self._pump()
        return page

    def theme_page(self) -> "ThemePage":
        # 夹具必须让**每一个**筛选项都能筛出严格子集（下面那条守卫测试会检查），
        # 所以这里刻意覆盖：OP/ED、已排除（曲目自己取消的）、手工补充、
        # 以及"它的番在第①步被排除"那种（anime_included=False）。
        rows = [
            theme_row("番A", "OP"),
            theme_row("番A", "ED"),
            {**theme_row("番B", "OP"), "included": False, "anime_included": False},
            {**theme_row("番C", "OP"), "included": False},
            {**theme_row("番D", "OP"), "origin": "manual", "origin_label": "手工"},
        ]
        page = ThemePage(rows, [])
        self.logs = []
        page.log.connect(self.logs.append)
        self._pump()
        return page

    def push_page(self) -> "PushPage":
        page = PushPage([
            push_row("待推送歌", "待推送", "1"),
            push_row("已推送歌", "已推送", "2"),
            push_row("失败歌", "失败", "3"),
        ])
        self.logs = []
        page.log.connect(self.logs.append)
        self._pump()
        return page

    # ---- 工具
    @staticmethod
    def button(page, text: str):
        for i in range(page.actions_layout.count()):
            b = page.actions_layout.itemAt(i).widget()
            if b is not None and b.text() == text:
                return b
        raise AssertionError(f"没有按钮 {text!r}")

    @staticmethod
    def visible(page) -> int:
        return len(page.visible_rows())

    def badge_number(self, page) -> int:
        import re
        m = re.search(r"(\d+)", page.badge.text())
        return int(m.group(1)) if m else 0


# ==================================================================== ① 用户报的那条

class TestStep3ViewToggles(FilterTestBase):
    def test_only_review_then_show_all_works(self):
        """用户报的那条：**死路哨兵**。

        「只看待审核」筛了 → 「显示全部」必须真的回到全部。
        修之前第二步是空操作（它读的是已经被筛过的那批行）。
        """
        page = self.match_page()
        total = self.visible(page)
        self.assertEqual(total, len(MATCH_STATUSES))

        self.button(page, "只看待审核").click()
        self._pump()
        self.assertEqual(self.visible(page), 4, "需要处理 = 待审核 + 低分 + 待匹配 + 缺失")
        self.assertEqual(page.filter_combo.currentText(), "需要处理")

        self.button(page, "显示全部").click()
        self._pump()
        self.assertEqual(self.visible(page), total, "「显示全部」没回到全部（这就是那条死路）")
        self.assertEqual(page.filter_combo.currentText(), "全部")

    def test_toolbar_button_and_combo_never_disagree(self):
        """按钮与筛选框必须永远说同一件事 —— 两边各管一半正是这个 bug 的根因。"""
        page = self.match_page()
        for btn in ("只看待审核", "显示全部", "只看待审核"):
            self.button(page, btn).click()
            self._pump()
            expected = "需要处理" if btn == "只看待审核" else "全部"
            self.assertEqual(page.filter_combo.currentText(), expected,
                             f"点了「{btn}」之后筛选框还写着"
                             f"「{page.filter_combo.currentText()}」")
            self.assertEqual(page._view, expected, "视图状态与筛选框不一致")

    def test_clicking_the_same_button_twice_does_not_narrow_further(self):
        """点两次「只看待审核」不能越筛越窄（旧实现每点一次都在子集上再筛）。"""
        page = self.match_page()
        self.button(page, "只看待审核").click()
        first = self.visible(page)
        self.button(page, "只看待审核").click()
        self._pump()
        self.assertEqual(self.visible(page), first)

    def test_badge_number_equals_the_needs_attention_view(self):
        """徽标「⚠ N 条需要处理」与筛选项「需要处理」必须**同一个数**。

        否则「徽标写 3 条、点进去看见 4 条」，两个数字一起失去可信度 ——
        而这个界面里，用户就是靠这个数字决定要不要继续往下走的。
        """
        page = self.match_page()
        self.button(page, "只看待审核").click()
        self._pump()
        self.assertEqual(self.badge_number(page), self.visible(page))

    def test_skipped_is_not_counted_as_需要处理(self):
        """`skipped` 是用户的决定，不是待办 —— 它只出现在「全部」里。"""
        page = self.match_page()
        self.button(page, "只看待审核").click()
        self._pump()
        visible = {r["status"] for r in page.visible_rows()}
        self.assertNotIn("skipped", visible)


# ==================================================================== ② 装饰品筛选项

class TestNoDecorativeFilters(FilterTestBase):
    """四个页面里**每一个**筛选项都必须真的改变视图。

    为什么值得写：修之前 ①②④ 三页的筛选框只打日志、表格一行不动。
    这种「控件长得像能用」的失败是不会报错的 —— 用户只会觉得"点了没反应"，
    然后怀疑自己点错了。这条测试是它们的公共哨兵。
    """

    def test_every_option_actually_narrows(self):
        for name, page in (("① 番单", self.anime_page()),
                           ("② 曲目", self.theme_page()),
                           ("③ 匹配", self.match_page()),
                           ("④ 写入", self.push_page())):
            total = self.visible(page)
            self.assertGreater(total, 1, f"{name} 的夹具太小，测不出东西")
            items = [page.filter_combo.itemText(i) for i in range(page.filter_combo.count())]
            self.assertEqual(items[0], "全部", f"{name} 的第一个筛选项应当是「全部」")
            for text in items[1:]:
                page.filter_combo.setCurrentText(text)
                self._pump()
                shown = self.visible(page)
                self.assertLess(shown, total,
                                f"{name} 的筛选项「{text}」筛完还是 {shown} 行 —— 它没在筛")
                self.assertGreater(shown, 0,
                                   f"{name} 的筛选项「{text}」一条都筛不出来（夹具没覆盖？）")
            page.filter_combo.setCurrentText("全部")
            self._pump()
            self.assertEqual(self.visible(page), total, f"{name} 切不回全部")

    def test_filter_logs_the_real_counts(self):
        """日志里必须出现「显示 x / y 行」这类真数字，而不是只有一句「筛选：xx」。"""
        page = self.anime_page()
        page.filter_combo.setCurrentText("仅当季")
        self._pump()
        self.assertTrue(any("1 / 3" in line for line in self.logs),
                        f"日志没有如实报出筛掉了多少行：{self.logs}")

    def test_filter_spec_requires_an_all_entry(self):
        """`set_filter_spec` 不接受「没有全部入口」的表 —— 那会让视图无法复位。"""
        page = self.anime_page()
        with self.assertRaises(ValueError):
            page.set_filter_spec({"仅当季": lambda r: True})

    def test_match_has_no_pending_review_only_option(self):
        """「待审核」那一项已被改名成「需要处理」（同名不同义曾经坑过用户）。"""
        page = self.match_page()
        items = [page.filter_combo.itemText(i) for i in range(page.filter_combo.count())]
        self.assertIn("需要处理", items)
        self.assertNotIn("待审核", items)


# ==================================================================== ③ 筛选不改数据

class TestFilterNeverChangesData(FilterTestBase):
    def test_filtering_hides_rows_it_does_not_remove_them(self):
        """筛选是**隐藏行**，不是重建一个只含匹配行的表。

        这是整套实现的地基：`model.rows` 永远是全量，于是落库/推送/统计
        天然就是对的，不需要每个调用点各自记得「要读全量」。
        """
        page = self.match_page()
        page._apply_filter("低分 / 未匹配")
        self._pump()
        self.assertEqual(len(page.model.rows), len(MATCH_STATUSES))
        self.assertEqual(len(page.all_rows()), len(MATCH_STATUSES))
        self.assertIs(page.all_rows(), page.model.rows)

    def test_push_checked_rows_ignores_the_filter(self):
        """⚠️ 最重要的那条：筛选绝不能减少会写进账号的歌。

        如果 `checked_push_rows()` 跟着筛选走，那么第④步筛到「失败」再按推送，
        就会**静默只推失败那一首**（甚至一首都不推）—— 而用户以为筛的只是显示。
        """
        page = self.push_page()
        self.assertEqual(len(page.checked_push_rows()), 3)
        page._apply_filter("失败")
        self._pump()
        self.assertEqual(self.visible(page), 1, "夹具前提：筛「失败」只剩 1 行")
        self.assertEqual(len(page.checked_push_rows()), 3,
                         "推送范围被筛选改变了 —— 这会少推歌，而且不报错")

    def test_unchecking_all_visible_rows_leaves_hidden_ones_alone(self):
        page = self.push_page()
        page._apply_filter("失败")
        self._pump()
        page._set_all(False)          # 「全选 / 取消勾选」按按钮的作用域 = 看得见的行
        self._pump()
        self.assertEqual(len(page.checked_push_rows()), 2)
        self.assertTrue(any("当前显示 1 行" in line for line in self.logs),
                        f"批量操作的日志没写清作用域：{self.logs}")

    def test_playlist_rename_reaches_hidden_rows(self):
        """改「目标歌单」必须覆盖被筛掉的行 —— 否则那几首会推到旧歌单去。"""
        page = self.push_page()
        page._apply_filter("失败")
        self._pump()
        page.adopt_default_playlist("26年1月")
        self._pump()
        self.assertEqual({r["playlist"] for r in page.all_rows()}, {"26年1月"})

    def test_restore_included_reaches_hidden_rows(self):
        """刷新时恢复勾选也要覆盖被筛掉的行（否则「我没勾它却推了」会重演）。"""
        page = self.push_page()
        page._apply_filter("失败")
        self._pump()
        page.restore_included({1: False, 2: False, 3: False})
        self._pump()
        self.assertEqual(page.checked_push_rows(), [])

    def test_mark_results_reaches_hidden_rows(self):
        """推送结果要写回全量；只写看得见的，被筛掉的行会永远停在「待推送」。"""
        page = self.push_page()
        page._apply_filter("待推送")
        self._pump()
        page.mark_results({"1": "已推送", "2": "已推送", "3": "失败"})
        self._pump()
        statuses = {r["netease_song_id"]: r["status"] for r in page.all_rows()}
        self.assertEqual(statuses, {"1": "已推送", "2": "已推送", "3": "失败"})

    def test_accept_all_uses_the_full_set(self):
        """「全部采用自动结果」按字面意思是全量，并把这件事打进日志。"""
        page = self.match_page()
        page._apply_filter("需要处理")
        self._pump()
        self.button(page, "全部采用自动结果").click()
        self._pump()
        statuses = [r["status"] for r in page.all_rows()]
        # auto + pending_review 一共 2 条被采用；原来那条 confirmed 还是 confirmed
        self.assertEqual(statuses.count("confirmed"), 3)
        self.assertTrue(any("全量" in line for line in self.logs), self.logs)
        # 采用完视图要重算：被采用的那两条已经处理掉了，不该还赖在「需要处理」里。
        # 剩下 3 条是 low / pending / missing —— 它们本来就不是「自动结果」，还在待办里。
        self.assertEqual(self.visible(page), 3)
        self.assertNotIn("auto", {r["status"] for r in page.visible_rows()})

    def test_set_data_keeps_the_current_filter(self):
        """后台任务回来刷新后，筛选状态保留 —— 不该被踢回「全部」。"""
        page = self.match_page()
        page._apply_filter("仅自动")
        self._pump()
        page.set_data([match_row(9, "pending_review"), match_row(10, "auto")])
        self._pump()
        self.assertEqual(page.filter_combo.currentText(), "仅自动")
        self.assertEqual(self.visible(page), 1)

    def test_deleting_a_row_recomputes_the_filter(self):
        """删行会让后面的行号整体下移 —— 隐藏状态必须跟着重算。

        隐藏是**按行号**记的。删掉一行之后不重算，就会出现「本该看得见的行
        被藏着」或「本该藏着的行露出来」—— 两种都不会报错。

        走真实的 Delete 键路径（`keyPressEvent`），不是直接调模型 ——
        右键菜单那条路会 `menu.exec()`（模态），测试里不能碰它（AGENTS.md §2）。
        """
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        page = self.anime_page()          # 当季(可见) / 跨季(可见) / 排除(藏)
        # 第一页仅允许移除未保存的手动新增行，来源番剧统一通过勾选排除。
        page.model.rows[0]["origin"] = "manual"
        page._apply_filter("已排除")       # 只有「排除番」该露出来
        self._pump()
        self.assertEqual([r["title_cn"] for r in page.visible_rows()], ["排除番"])

        page.table.selectRow(0)           # 选中「当季番」并删掉它
        qev = QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier)
        page.table.keyPressEvent(qev)
        self._pump()
        self.assertEqual(len(page.all_rows()), 2)
        self.assertEqual([r["title_cn"] for r in page.visible_rows()], ["排除番"],
                         "删行之后隐藏状态没重算 —— 会藏错行")

    def test_adding_a_row_respects_the_current_filter(self):
        """新增一行后筛选要重算：在「仅当季」下新增的未知归属行应当被藏起来。"""
        page = self.anime_page()
        page._apply_filter("仅当季")
        self._pump()
        before = self.visible(page)
        page._add()
        self._pump()
        self.assertEqual(len(page.all_rows()), 4, "新行要进 model（落库才拿得到）")
        self.assertEqual(self.visible(page), before, "新行不符合筛选，不该露出来")


# ==================================================================== ④ 空视图

class TestEmptyView(FilterTestBase):
    def test_empty_view_clears_the_candidate_panel(self):
        """筛到 0 行时，候选面板不能还挂着上一次选中那一行的候选歌曲。

        表格里已经没有那一行了，下方却在显示它的候选 —— 界面在自相矛盾。
        """
        page = MatchPage([match_row(1, "auto"), match_row(2, "confirmed")])
        self._pump()
        page.table.selectRow(0)
        self._pump()
        self.assertIn("番1", page.cand_title.text())

        page._apply_filter("需要处理")
        self._pump()
        self.assertEqual(self.visible(page), 0)
        self.assertNotIn("番1", page.cand_title.text(), "候选面板还留着被筛掉那一行的内容")
        self.assertIn("没有曲目", page.cand_title.text())

    def test_empty_view_says_the_data_is_still_there(self):
        """筛成空时要说明「数据没丢，只是被筛掉了」，否则用户会以为库空了。"""
        page = MatchPage([match_row(1, "auto")])
        page._apply_filter("需要处理")
        self._pump()
        text = page.cand_title.text() + " " + " ".join(
            page.cand_layout.itemAt(i).widget().text()
            for i in range(page.cand_layout.count())
            if page.cand_layout.itemAt(i).widget() is not None
        )
        self.assertIn("一共 1 条", text, f"没有说清数据还在：{text!r}")

    def test_genuinely_empty_data_still_shows_the_neutral_hint(self):
        """真的没有数据时不该说「筛选」—— 那时用户需要听的是「还没有数据」。"""
        page = MatchPage([])
        self._pump()
        self.assertIn("选中", page.cand_title.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
