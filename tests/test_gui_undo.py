"""第③步的**回退**：撤销 / 清除匹配 / 跳过此曲。

用户报的：需要人工确认的条目，在浏览可选项时误点了其中一首歌，
但那些候选**其实没有一个是正确的** —— 需要一个回退手段。

读代码先把「误点为什么是既成事实」摊开（全部可静态核对）：

  1. 候选卡片有两个地方会选中：复选框 `radio.clicked`，以及**整张卡片的双击**
     （`widgets.py`）。而 `_pick()` 一击就写 `status="confirmed"` + `manual=True`——
     没有中间态、没有二次确认。
  2. `confirmed` 正是进第④步推送集的通行证（`session._match_rows` 只放
     auto/confirmed 进去）。误点 = 这首歌会被推。
  3. 更重的一层：`manual=True` 存库时变成 `decided_by='human'`，然后被
     `learn_from_season(confirm_only=True)` 学进 `config/aliases.yaml`。
     也就是说误点会**永久教坏别名表** —— 而别名表**不能**靠撤销回滚。
  4. 原来唯一的撤销（`DictTableModel._undo`）只记单元格文本编辑，完全不覆盖
     `_pick`；快捷键还绑在表格自己身上，焦点在候选卡片上时按 Ctrl+Z 没反应。

所以这里测三件事：
  · 档 1 撤销：一条历史栈（卡片点击 + 单元格编辑**共用**，否则 Ctrl+Z 不是后进先出）
  · 档 2 清除匹配：已落库之后的纠正路径（撤销栈在换数据时就清空了）
  · 档 3 跳过此曲：让 `DecisionStatus.SKIPPED` 第一次真的可达，且**不**留下
    人工信号（留了的话，用户明确否掉的那个对应关系照样被学进别名表）

全部离线：offscreen、不 show、不进事件循环、不联网、不碰账号
（构造窗口与断言 —— 见 AGENTS.md §2）。
⚠️ 夹具里的候选 `cover` **必须**留空：`CoverLoader` 见到非空 URL 会立刻发
真实网络请求（`QNetworkAccessManager.get`）。
"""

from __future__ import annotations

import gc
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

# ⚠️ 必须在 import animusic **之前**设置：别名自学习会回写配置目录。
os.environ.setdefault(
    "ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, "data", "_test_tmp", "config")
)

try:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMenu

    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

if HAS_QT:
    from animusic.gui.main_window import MainWindow  # noqa: E402
    from animusic.gui.pages import MatchPage  # noqa: E402
    from animusic.gui.session import STATUS_LABEL, WizardSession  # noqa: E402
    from animusic.gui.widgets import Column, DictTableModel  # noqa: E402

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
from animusic.normalize import AliasTable  # noqa: E402
from animusic.store import Store  # noqa: E402

_TMP = os.path.join(_ROOT, "data", "_test_tmp")


# ==================================================================== 夹具

def cand(song_id: str, name: str, *, score: float = 0.9, artists=("A",),
         status: str = "pending_review") -> dict:
    """一条候选。`cover` 故意为空 —— 非空会触发真实网络请求。"""
    return {
        "netease_song_id": song_id, "name": name, "artists": list(artists),
        "album": "", "cover": "", "duration_ms": 240_000, "tags": [], "alias": [],
        "score": score, "detail": {"title": 1.0, "artist": 1.0}, "rank": 0,
        "status": status,
    }


def mrow(i: int, status: str = "pending_review", *, cands=None, chosen: dict | None = None,
         manual: bool = False, note: str = "") -> dict:
    chosen = chosen or {}
    return {
        "theme_id": i, "anime": f"番{i}", "kind": "OP", "seq": 1,
        "theme_title": f"曲{i}", "theme_artists": "A",
        "chosen_name": chosen.get("name") or "—",
        "chosen_artists": "、".join(chosen.get("artists") or []) or "—",
        "chosen_id": str(chosen.get("netease_song_id") or ""),
        "chosen_duration": chosen.get("duration_ms"),
        "score": chosen.get("score") or 0.0,
        "status": status, "status_label": STATUS_LABEL.get(status, status),
        "candidates": list(cands or []), "note": note, "manual": manual,
        "detail": chosen.get("detail"),
    }


#: 撤销必须原样恢复的字段 —— 与 `MatchPage._UNDO_FIELDS` 对齐。
#: 故意**在这里再抄一份**：如果哪天实现里少恢复一个字段，这个测试要能发现，
#: 而不是跟着实现一起改口。
UNDO_FIELDS = (
    "chosen_id", "chosen_name", "chosen_artists", "chosen_duration",
    "score", "detail", "status", "status_label", "manual", "note",
)


def selected_index(page) -> "int | None":
    """表格当前选中第几行（没选中 = None）。

    ⚠️ 「界面有没有自己乱动」就用它来断言：选中行一变，Qt 会把它滚进视野
    —— 真机报的「点跳过之后自动置顶」就是这个链条。
    """
    rows = page.table.selectionModel().selectedRows()
    return rows[0].row() if rows else None


@unittest.skipUnless(HAS_QT, "需要 PySide6")
class GuiTestBase(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        os.makedirs(_TMP, exist_ok=True)
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self._pages: list = []
        self.logs: list[str] = []

    def tearDown(self):
        # ⚠️ 控件必须**在主线程里当场析构**，不能丢给 GC ——
        # 踩过：留给 GC 的话会在某个后台线程里析构，Qt 报
        # "Timers cannot be stopped from another thread" 并把进程带走（0xC0000409）。
        for p in self._pages:
            p.setParent(None)
            p.deleteLater()
        self._pages.clear()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        gc.collect()

    def _pump(self, n: int = 4):
        for _ in range(n):
            self.app.processEvents()

    def page(self, rows: list[dict]) -> "MatchPage":
        p = MatchPage(rows)
        self._pages.append(p)
        p.log.connect(self.logs.append)
        self._pump()
        return p

    def log_has(self, needle: str) -> bool:
        return any(needle in m for m in self.logs)


# ==================================================================== 档 1：撤销

class TestUndo(GuiTestBase):
    def test_undo_restores_every_field(self):
        """误点一首 → 撤销 → 10 个字段**全部**回到原样。

        只恢复 `status` 是不够的：留着 `manual=True`，下次保存仍然记成
        `decided_by='human'`，被否掉的对应关系照样进别名表。
        """
        c1, c2 = cand("1", "自动选的"), cand("2", "误点的")
        row = mrow(1, "pending_review", cands=[c1, c2], chosen=c1)
        page = self.page([row])
        before = {k: row[k] for k in UNDO_FIELDS}

        page._pick(row, c2)
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["chosen_id"], "2")
        self.assertTrue(row["manual"])
        self.assertEqual(len(page._history), 1)

        page._undo_last()
        self.assertEqual({k: row[k] for k in UNDO_FIELDS}, before)
        self.assertEqual(len(page._history), 0)

    def test_undo_is_lifo_across_cards_and_cells(self):
        """卡片点击和单元格编辑**共用一条**历史栈。

        两条独立栈的老毛病：点错一首歌、马上按撤销，撤掉的却是更早那次备注修改，
        错的歌还留着 —— 那正好是这个功能存在的理由，等于没修。
        """
        c1 = cand("1", "候选一")
        row = mrow(1, "pending_review", cands=[c1], note="旧备注")
        page = self.page([row])
        page._pick(row, c1)
        # 改备注（走 model.setData —— 真实界面上是双击单元格编辑）
        note_col = next(i for i, c in enumerate(page.model.columns) if c.key == "note")
        page.model.setData(page.model.index(0, note_col), "新备注", Qt.EditRole)
        self.assertEqual(row["note"], "新备注")
        self.assertEqual(len(page._history), 2)

        page._undo_last()          # 先撤**最后**那件事：备注
        self.assertEqual(row["note"], "旧备注")
        self.assertEqual(row["chosen_id"], "1", "撤销顺序错了：应该先撤备注")
        page._undo_last()          # 再撤误点
        self.assertEqual(row["chosen_id"], "")
        self.assertEqual(row["status"], "pending_review")

    def test_accept_all_is_one_undo_step(self):
        """「全部采用自动结果」整批算**一条**历史：一次撤销全退回去。"""
        rows = [mrow(1, "auto"), mrow(2, "pending_review"), mrow(3, "low")]
        page = self.page(rows)
        page._accept_all()
        self.assertTrue(all(r["status"] == "confirmed" for r in rows[:2]))
        self.assertEqual(rows[2]["status"], "low", "低分不该被「全部采用」动到")
        self.assertEqual(len(page._history), 1)

        page._undo_last()
        self.assertEqual([r["status"] for r in rows], ["auto", "pending_review", "low"])

    def test_pick_same_card_twice_is_noop(self):
        """重复点同一张卡片不该往历史栈里灌噪声。

        不拦的话：误点一次之后要按好几下 Ctrl+Z 才能退回到误点**之前**。
        """
        c1 = cand("1", "候选一")
        row = mrow(1, "pending_review", cands=[c1])
        page = self.page([row])
        page._pick(row, c1)
        page._pick(row, c1)
        self.assertEqual(len(page._history), 1)

    def test_undo_empty_says_where_to_go(self):
        """没有可撤销的改动时，必须告诉用户还有别的路（否则等于没说）。"""
        page = self.page([mrow(1, "auto")])
        page._undo_last()
        self.assertTrue(self.log_has("没有可撤销的改动"))
        self.assertTrue(self.log_has("清除此行的匹配"), "空栈时必须指向档 2")

    def test_history_is_bounded(self):
        row = mrow(1, "auto", note="n")
        page = self.page([row])
        note_col = next(i for i, c in enumerate(page.model.columns) if c.key == "note")
        for i in range(page._history_limit + 8):
            page.model.setData(page.model.index(0, note_col), f"n{i}", Qt.EditRole)
        self.assertEqual(len(page._history), page._history_limit)

    def test_undo_after_set_data_does_not_lie(self):
        """换数据（换季度/重跑匹配）后旧条目必须作废。

        旧条目指向的 dict 已经不在表里：硬恢复会打印「已撤销」但屏幕上一动不动 ——
        正是 §6 禁止的那种假指标。
        """
        c1 = cand("1", "候选一")
        row = mrow(1, "pending_review", cands=[c1])
        page = self.page([row])
        page._pick(row, c1)
        self.assertTrue(page.undo_btn.isEnabled())

        page.set_data([mrow(9, "auto")])
        self.assertEqual(len(page._history), 0)
        self.assertFalse(page.undo_btn.isEnabled())
        page._undo_last()                       # 不该炸
        self.assertTrue(self.log_has("没有可撤销的改动"))

    def test_undo_button_state_and_tooltip(self):
        page = self.page([mrow(1, "pending_review", cands=[cand("1", "候选一")])])
        self.assertFalse(page.undo_btn.isEnabled())
        page._pick(page.model.rows[0], page.model.rows[0]["candidates"][0])
        self.assertTrue(page.undo_btn.isEnabled())
        self.assertIn("选定", page.undo_btn.toolTip())
        page._undo_last()
        self.assertFalse(page.undo_btn.isEnabled())

    def test_undo_puts_the_row_back_in_front_of_you(self):
        """撤销后这一行要**回到眼前**（并回到「需要处理」视图里）。

        误点的行已经不是「待审核」了；撤销之后如果它还藏着，用户看不出撤销生效没有。
        """
        c1 = cand("1", "候选一")
        row = mrow(1, "pending_review", cands=[c1])
        page = self.page([row])
        page._apply_filter("需要处理")
        page.table.selectRow(0)
        page._pick(row, c1)
        page._apply_filter("需要处理")          # 手动重筛：这一行消失了
        self.assertTrue(page.table.isRowHidden(0))

        page._undo_last()
        self.assertEqual(page._view, "需要处理", "撤销不该偷偷切换筛选")
        self.assertFalse(page.table.isRowHidden(0), "撤销后该行应重新可见")
        sel = page.table.selectionModel().selectedRows()
        self.assertEqual([i.row() for i in sel], [0], "撤销后应选中该行")

    def test_undo_hidden_row_says_where_it_went(self):
        """撤销回去的状态仍被当前筛选藏着时，**不许偷偷切筛选**，但要说清它去哪了。"""
        c1 = cand("1", "候选一")
        row = mrow(1, "auto", cands=[c1], chosen=c1)
        page = self.page([row])
        page._pick(row, c1)                     # auto → confirmed
        page._apply_filter("需要处理")
        self.assertTrue(page.table.isRowHidden(0), "人工确认的不算「需要处理」")
        page._undo_last()                       # 退回 auto → 仍然不在「需要处理」里
        self.assertEqual(page._view, "需要处理", "撤销不该偷偷切换筛选")
        self.assertTrue(self.log_has("藏起来了"))

    def test_right_click_menu_exposes_all_three(self):
        row = mrow(1, "pending_review", cands=[cand("1", "候选一")])
        page = self.page([row])
        menu = QMenu(page)
        acts = page._menu_actions(menu, row, 0)
        texts = [a.text() for a in acts]
        self.assertEqual(len(acts), 3)
        self.assertTrue(any("撤销" in t for t in texts))
        self.assertTrue(any("清除此行的匹配" in t for t in texts))
        self.assertTrue(any("跳过此曲" in t for t in texts))
        undo_act = next(a for a in acts if "撤销" in a.text())
        self.assertFalse(undo_act.isEnabled(), "没有历史时菜单里的撤销应置灰")


# ==================================================================== 档 3：跳过此曲

class TestSkip(GuiTestBase):
    def test_skip_clears_the_choice_and_the_human_flag(self):
        """跳过后必须**同时**清 `chosen_id` 和 `manual`。

        留一个不改 → `save_decisions` 仍然算 human、`learn_from_season` 仍然能拿到
        chosen_candidate_id → 用户刚否掉的对应关系照样被学进别名表。
        """
        c1 = cand("1", "错的那首")
        row = mrow(1, "pending_review", cands=[c1], chosen=c1, manual=True)
        page = self.page([row])
        page._skip_row(row)
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["status_label"], STATUS_LABEL["skipped"])
        self.assertEqual(row["chosen_id"], "")
        self.assertEqual(row["chosen_name"], "—")
        self.assertFalse(row["manual"])
        self.assertEqual(row["score"], 0.0)

    def test_skipped_row_is_not_a_todo_and_disappears_from_that_filter(self):
        c1 = cand("1", "候选一")
        row = mrow(1, "pending_review", cands=[c1])
        page = self.page([row])
        page._apply_filter("需要处理")
        self.assertFalse(page.table.isRowHidden(0))
        page.table.selectRow(0)
        page._skip_current()
        self.assertTrue(self.log_has("已跳过"))
        self.assertTrue(page.table.isRowHidden(0), "跳过的行不该继续算「需要处理」")
        self.assertIn("已跳过 1", page.stat.text())

    def test_skip_button_without_selection_says_so(self):
        page = self.page([mrow(1, "pending_review")])
        page.table.clearSelection()
        page._skip_current()
        self.assertTrue(self.log_has("请先在上方表格里选中一行"))
        self.assertEqual(page.model.rows[0]["status"], "pending_review")

    def test_skip_does_not_move_the_view(self):
        """⚠️ **真机报的**：点「都不是，跳过此曲」之后界面**自动置顶**。

        用户的原话是「我需要界面保持不动」。根因在 `_after_filter`：
        没有 `_pending_focus` 时它兜底选中「第一条可见行」，而**选中行一变，
        表格就会把它滚进视野** —— 用户刚看完的那一行被顶到下面去了。
        行级动作（跳过 / 清除匹配 / 改单元格）都不该移动视图。
        """
        rows = [mrow(i, "pending_review", cands=[cand(str(i), f"候选{i}")])
                for i in range(1, 5)]
        page = self.page(rows)
        page._apply_filter(page.FILTER_ALL)
        page.table.selectRow(2)                 # 用户停在第 3 行
        self._pump()
        self.assertEqual(selected_index(page), 2)

        page._skip_current()
        self._pump()

        self.assertEqual(selected_index(page), 2,
                         "跳过后选中行被挪走了 —— 界面会跟着置顶")
        self.assertEqual(page._shown_row, id(rows[2]),
                         "候选面板也不该切到别的行")
        self.assertEqual(rows[2]["status"], "skipped", "前提：这一行真的被跳过了")

    def test_filter_change_still_lands_on_a_visible_row(self):
        """⚠️ 别为了「不动」把这个兜底删掉：筛选把选中行藏起来之后，
        必须有**一条可见的**选中行，否则表格选中与下方候选面板会对不上。"""
        rows = [mrow(i, "auto" if i % 2 else "pending_review",
                     cands=[cand(str(i), f"候选{i}")]) for i in range(1, 5)]
        page = self.page(rows)
        page._apply_filter(page.FILTER_ALL)
        page.table.selectRow(1)                 # 第 2 行：pending_review
        self._pump()

        page._apply_filter("仅自动")             # 第 2 行被藏起来了

        i = selected_index(page)
        self.assertIsNotNone(i, "筛选后必须还有一条选中行")
        self.assertFalse(page.table.isRowHidden(i))

    def test_filter_change_keeps_the_row_when_it_is_still_visible(self):
        """选中行在新筛选下**还在** —— 那就别动它（原来会跳到第一条，
        等于用户每切一次筛选就被顶到最上面）。"""
        rows = [mrow(i, "auto", cands=[cand(str(i), f"候选{i}")]) for i in range(1, 5)]
        page = self.page(rows)
        page._apply_filter(page.FILTER_ALL)
        page.table.selectRow(3)
        self._pump()

        page._apply_filter("仅自动")             # 第 4 行仍然可见
        self.assertEqual(selected_index(page), 3)

    def test_skip_inside_the_todo_filter_lands_on_the_next_row(self):
        """⚠️ **真机上最常见的那一路**：切「需要处理」，顺着往下跳。

        跳过之后这一行就离开「需要处理」了 —— 此时必须落到**下一行**，
        而不是从第一行重来。用户的原话是「我需要界面保持不动」，
        而顶回顶部等于把「我刚看到哪」弄丢了。
        """
        rows = [mrow(i, "pending_review", cands=[cand(str(i), f"候选{i}")])
                for i in range(1, 6)]
        page = self.page(rows)
        page._apply_filter("需要处理")
        page.table.selectRow(2)
        self._pump()

        page._skip_current()
        self._pump()

        self.assertTrue(page.table.isRowHidden(2), "前提：跳过的行离开了这个筛选")
        self.assertEqual(selected_index(page), 3, "应当落到下一行，而不是被顶回顶部")
        self.assertEqual(page._shown_row, id(rows[3]), "候选面板跟着下一行走")

    def test_skip_the_last_one_steps_back_one_row_not_to_the_top(self):
        """跳过的正好是最后一条可见行 → 往回退一条，仍然**不回顶部**。"""
        rows = [mrow(i, "pending_review", cands=[cand(str(i), f"候选{i}")])
                for i in range(1, 5)]
        page = self.page(rows)
        page._apply_filter("需要处理")
        page.table.selectRow(3)
        self._pump()

        page._skip_current()
        self._pump()

        self.assertTrue(page.table.isRowHidden(3))
        self.assertEqual(selected_index(page), 2)

    def test_skip_is_undoable(self):
        c1 = cand("1", "候选一")
        row = mrow(1, "pending_review", cands=[c1], chosen=c1)
        page = self.page([row])
        before = {k: row[k] for k in UNDO_FIELDS}
        page._skip_row(row)
        page._undo_last()
        self.assertEqual({k: row[k] for k in UNDO_FIELDS}, before)


# ==================================================================== 档 2：清除匹配

class TestClearMatch(GuiTestBase):
    def test_clear_returns_to_pending_review(self):
        c1 = cand("1", "误点的")
        row = mrow(1, "confirmed", cands=[c1], chosen=c1, manual=True)
        page = self.page([row])
        page._clear_match(row)
        self.assertEqual(row["status"], "pending_review")
        self.assertEqual(row["status_label"], STATUS_LABEL["pending_review"])
        self.assertEqual(row["chosen_id"], "")
        self.assertFalse(row["manual"])
        self.assertEqual(len(page._history), 1, "清除本身也要可撤销")

    def test_clear_without_candidates_is_missing_netease(self):
        row = mrow(1, "confirmed", chosen=cand("1", "x"), manual=True)
        page = self.page([row])
        page._clear_match(row)
        self.assertEqual(row["status"], "missing_netease")

    def test_clear_warns_about_alias_and_playlist(self):
        """两件必须说实话的事（§6）：

        · 别名表已经学到的对应关系**不会**被回收（`learn_from_season` 只加不删）
        · 已经推进歌单的那首**不会**被自动移除（那是一次写入操作）
        """
        c1 = cand("1", "误点的")
        row = mrow(1, "confirmed", cands=[c1], chosen=c1, manual=True)
        page = self.page([row])
        page._clear_match(row)
        self.assertTrue(self.log_has("aliases.yaml"))
        self.assertTrue(self.log_has("不会"), "必须说明歌单里的那首不会被自动移除")

        # 从没人工确认过的行（纯自动结果）不该吓唬用户
        self.logs.clear()
        row2 = mrow(2, "confirmed", cands=[cand("2", "自动的")])
        page._clear_match(row2)
        self.assertFalse(self.log_has("aliases.yaml"))


# ==================================================================== 共用的表格控件不能被改坏

class TestSharedWidgetUnchanged(unittest.TestCase):
    """没设 hook 的页面（①②④）行为必须一字不变。"""

    def test_default_model_undo_still_works(self):
        cols = [Column("note", "备注", 120, "text", editable=True)]
        model = DictTableModel(cols, [{"note": "旧"}])
        model.setData(model.index(0, 0), "新", Qt.EditRole)
        self.assertEqual(model.rows[0]["note"], "新")
        msg = model.undo()
        self.assertEqual(model.rows[0]["note"], "旧")
        self.assertIn("撤销", msg or "")

    def test_edit_hook_takes_over_the_history(self):
        """设了 hook 之后模型**不再**自己记 —— 否则两条栈会让顺序错乱。"""
        cols = [Column("note", "备注", 120, "text", editable=True)]
        model = DictTableModel(cols, [{"note": "旧"}])
        seen: list = []
        model.set_edit_hook(lambda row, key, old, new: seen.append((key, old, new)))
        model.setData(model.index(0, 0), "新", Qt.EditRole)
        self.assertEqual(seen, [("note", "旧", "新")])
        self.assertIsNone(model.undo())


# ==================================================================== 落库（真 Store，离线）

class TestPersist(GuiTestBase):
    """「界面上点了」→「库里/别名表里成了什么」—— 这一层才是真正的后果。"""

    def setUp(self):
        super().setUp()
        self.db = os.path.join(_TMP, f"undo_{id(self)}.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)
        self.store = Store(self.db)
        self.table = AliasTable()               # 不落盘，避免碰真实 config
        self.sess = WizardSession(store=self.store, season=Season.parse("2026Q4"),
                                  aliases=self.table)
        self.sid = self.sess.season_id

    def tearDown(self):
        super().tearDown()
        self.store.close()
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)

    def _seed(self, title="Urusiren", cand_name="ウルサイレン",
              cand_artist="Umeda Cypher", theme_artist="梅田サイファー"):
        """一部番 + 一条曲目 + 一条候选。

        ⚠️ 曲名/艺人用的是 `test_alias_learning.py` 里**验证过会被学走**的那对
        （跨书写系统的相似度 ≥ 艺人学习下限 0.75）。随便编一对，「正对照」
        可能本来就学不到，于是「跳过没学到」那条测试会变成恒真的。
        """
        aid = self.store.upsert_anime(self.sid, Anime(
            season_key="2026Q4", title_cn="番A", title_jp="ばんA",
            animethemes_slug=f"s{title}", season_bucket=SeasonBucket.TARGET,
            origin=Origin.BANGUMI,
        ))
        tid = self.store.upsert_theme(aid, Theme(
            anime_id=aid, kind=ThemeKind.OP, song_title=title, artists=[theme_artist],
            sequence=1, source="animethemes", origin=Origin.ANIMETHEMES,
        ))
        # cover 留空 —— 非空会让 CandidateItem 立刻发真实网络请求
        self.store.upsert_candidate(tid, Candidate(
            theme_id=tid, netease_song_id=f"id{title}", name=cand_name,
            artists=[cand_artist], duration_ms=240_000, score=0.9,
            origin=Origin.SEARCH,
        ))
        return tid

    def test_skip_persists_as_skipped_without_human_signal(self):
        tid = self._seed()
        page = self.page(self.sess.build()["match"])
        row = page.model.rows[0]
        page._skip_row(row)

        stat = self.sess.save_decisions(page.all_rows())
        self.assertEqual(stat["written"], 1)
        self.assertEqual(stat["skipped"], [], "状态原文必须能映射进库，不许被静默丢掉")
        d = self.store.decision_map(self.sid)[tid]
        self.assertEqual(d["status"], DecisionStatus.SKIPPED)
        self.assertIsNone(d["chosen_candidate_id"])
        self.assertNotEqual(d["decided_by"], "human", "跳过不是人工确认")

    def test_skip_is_not_learned_as_an_alias(self):
        """**最重要的一条**：跳过之后别名表必须一个都不学。

        跳过的语义就是「这些候选都不对」。如果实现只清了 status 而留着
        chosen_id/manual，`learn_from_season` 会把那个错的对应关系学进去，
        之后「确认一次、以后自动通过」会照错的放行 —— 而且很难排查。
        """
        tid = self._seed()
        page = self.page(self.sess.build()["match"])
        page._skip_row(page.model.rows[0])
        self.sess.save_decisions(page.all_rows())

        stat = self.sess.orchestrator().learn_from_season(self.sess.season,
                                                          confirm_only=True)
        self.assertEqual(stat["checked"], 0)
        self.assertFalse(self.table.same_group("artist", "梅田サイファー", "Umeda Cypher"))
        self.assertFalse(self.table.same_group("title", "Urusiren", "ウルサイレン"))
        self.assertEqual(self.table.stats(), {"anime": 0, "artist": 0, "title": 0})
        self.assertIsNotNone(self.store.decision_map(self.sid)[tid])

    def test_confirmed_is_learned_positive_control(self):
        """正对照：**人工选定**确实会被学走。

        没有这一条，上面那条「没学到」有可能是恒真的（§6：这个数字有可能是
        恒真的吗？）—— 例如别名表根本就没接上。
        """
        self._seed()
        page = self.page(self.sess.build()["match"])
        page._pick(page.model.rows[0], page.model.rows[0]["candidates"][0])
        self.sess.save_decisions(page.all_rows())

        stat = self.sess.orchestrator().learn_from_season(self.sess.season,
                                                          confirm_only=True)
        self.assertEqual(stat["checked"], 1)
        self.assertTrue(self.table.same_group("artist", "梅田サイファー", "Umeda Cypher"))
        self.assertTrue(self.table.same_group("title", "Urusiren", "ウルサイレン"))

    def test_skipped_row_never_reaches_step_four(self):
        self._seed()
        page = self.page(self.sess.build()["match"])
        row = page.model.rows[0]
        page._pick(row, row["candidates"][0])           # 先误点
        self.sess.save_decisions(page.all_rows())
        self.assertEqual(len(self.sess.build()["push"]), 1, "确认过的行应该进第④步")

        page._clear_match(row)                          # 再纠正
        self.sess.save_decisions(page.all_rows())
        self.assertEqual(self.sess.build()["push"], [], "退回待审核后不该再进第④步")

    def test_clear_match_overwrites_the_persisted_decision(self):
        """档 2 的全部意义：**已经落库**之后还能纠正。

        （撤销栈在换数据时就清空了，所以「误点 → 下一步 → 返回」这条路上
        只能靠它；这里同时验证 `set_decision` 的 upsert 真的覆盖了旧行。）
        """
        tid = self._seed()
        page = self.page(self.sess.build()["match"])
        row = page.model.rows[0]
        page._pick(row, row["candidates"][0])
        self.sess.save_decisions(page.all_rows())
        self.assertEqual(self.store.decision_map(self.sid)[tid]["status"],
                         DecisionStatus.CONFIRMED)

        # 模拟「离开第③步再回来」：撤销栈清空，页面数据是重新建的
        page2 = self.page(self.sess.build()["match"])
        self.assertEqual(len(page2._history), 0)
        page2._clear_match(page2.model.rows[0])
        self.sess.save_decisions(page2.all_rows())

        d = self.store.decision_map(self.sid)[tid]
        self.assertEqual(d["status"], DecisionStatus.PENDING_REVIEW)
        self.assertIsNone(d["chosen_candidate_id"])
        self.assertEqual(d["decided_by"], "auto")

    def test_skipped_row_reloads_without_a_phantom_selection(self):
        """重载后不许把第一首候选又显示成「选中歌曲」。

        用户刚说过「候选都不对」，而 `_match_rows` 原来会在没有 chosen 时
        回退到 `cands[0]` —— 状态列写「已跳过」、选中列写着第一首，两列互相打脸。
        """
        tid = self._seed()
        page = self.page(self.sess.build()["match"])
        page._skip_row(page.model.rows[0])
        self.sess.save_decisions(page.all_rows())

        again = self.sess.build()["match"]
        row = next(r for r in again if r["theme_id"] == tid)
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["chosen_id"], "")
        self.assertEqual(row["chosen_name"], "—")
        self.assertEqual(self.sess.build()["push"], [])


class TestUndoShortcutWiring(GuiTestBase):
    """Ctrl+Z 到底会不会被触发 —— **实测**，不是推理。

    这一条是被自己写错的代码逼出来的：第一版用的是
    `Qt.WidgetWithChildrenShortcut`，它要求**焦点**落在本页的子控件上。
    实际用的时候，用户点完候选卡片，焦点可能在按钮上、也可能没有焦点控件 ——
    于是 Ctrl+Z 一次都不触发，日志里什么都不打，看起来就是个死键。
    离线单测全绿（测试是直接调 `_undo_last()` 的，绕过了快捷键系统），
    真按键盘才发现。

    这里连**真实按键投递**一起测（`QTest.keyClick` 发给窗口），
    并且先验证「对照组」= Ctrl+L 能触发：对照组不成立就 skip，
    **绝不**在没有对照组的情况下把「没触发」当成通过。
    """

    def setUp(self):
        super().setUp()
        self.db = os.path.join(_TMP, f"undo_key_{id(self)}.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)
        self.store = Store(self.db)
        self.sess = WizardSession(store=self.store, season=Season.parse("2026Q4"),
                                  aliases=AliasTable())
        sid = self.sess.season_id
        aid = self.store.upsert_anime(sid, Anime(
            season_key="2026Q4", title_cn="探针番", title_jp="プローブ",
            animethemes_slug="probe", season_bucket=SeasonBucket.TARGET,
            origin=Origin.BANGUMI,
        ))
        tid = self.store.upsert_theme(aid, Theme(
            anime_id=aid, kind=ThemeKind.OP, song_title="Probe Song", artists=["A"],
            sequence=1, source="animethemes", origin=Origin.ANIMETHEMES,
        ))
        self.store.upsert_candidate(tid, Candidate(
            theme_id=tid, netease_song_id="123", name="Probe Song", artists=["A"],
            duration_ms=240_000, score=0.9, score_detail={"title": 1.0},
            origin=Origin.SEARCH,
        ))
        self.win = MainWindow(self.sess)
        self.win.resize(1200, 800)
        self.win.show()                 # offscreen：没人看着，也不弹任何模态框（§2）
        self.win.activateWindow()
        self._pump(8)

    def tearDown(self):
        self.win.close()
        self._pump()
        self.win.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.win = None
        super().tearDown()
        self.store.close()
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)

    def _control_ok(self) -> bool:
        """对照组：Ctrl+L 是窗口自己的一条已知可用的快捷键。"""
        before = self.win.btn_log.isChecked()
        QTest.keyClick(self.win, Qt.Key_L, Qt.ControlModifier)
        self._pump()
        ok = self.win.btn_log.isChecked() != before
        QTest.keyClick(self.win, Qt.Key_L, Qt.ControlModifier)   # 还原
        self._pump()
        return ok

    def test_ctrl_z_reaches_the_page_history(self):
        """第③步按 Ctrl+Z → 走页面统一历史（不是「按了没反应」）。"""
        if not self._control_ok():
            self.skipTest("这个环境里快捷键事件送不到窗口（对照组 Ctrl+L 不成立）")

        self.win._goto(2)
        self._pump()
        page = self.win.pages[2]
        row = page.model.rows[0]
        page._pick(row, {"netease_song_id": "999999", "name": "探针曲",
                         "artists": ["探针"], "score": 0.5, "detail": {}})
        self.assertEqual(len(page._history), 1)

        QTest.keyClick(self.win, Qt.Key_Z, Qt.ControlModifier)
        self._pump(6)
        self.assertEqual(len(page._history), 0, "Ctrl+Z 没被触发（快捷键是死的）")
        self.assertEqual(row["chosen_id"], "123", "撤销应恢复到原来的候选")

    def test_ctrl_z_on_another_page_does_not_hit_the_match_page(self):
        """反向：在第①步按 Ctrl+Z 走的是那一页表格自己的撤销。

        这一条同时说明「多条同键快捷键」不会被 Qt 判成歧义 —— 实测窗口里
        同时有 8 条启用的 Ctrl+Z（四页表格各 2 条），隐藏页面的不参与匹配。
        """
        if not self._control_ok():
            self.skipTest("这个环境里快捷键事件送不到窗口（对照组 Ctrl+L 不成立）")

        self.win._goto(0)
        self._pump()
        p0 = self.win.pages[0]
        logs: list[str] = []
        p0.log.connect(logs.append)
        col = next(i for i, c in enumerate(p0.model.columns) if c.key == "note")
        p0.model.setData(p0.model.index(0, col), "探针备注", Qt.EditRole)
        self.assertEqual(p0.model.rows[0]["note"], "探针备注")

        QTest.keyClick(self.win, Qt.Key_Z, Qt.ControlModifier)
        self._pump(6)
        self.assertEqual(p0.model.rows[0]["note"], "", "第①步的单元格撤销应生效")
        self.assertTrue(any("撤销" in m for m in logs))


if __name__ == "__main__":
    unittest.main()