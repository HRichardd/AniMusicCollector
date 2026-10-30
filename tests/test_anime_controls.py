"""第一步交互简化的离线回归；菜单执行被替换，不展示窗口或进入事件循环。"""

import gc
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent, QItemSelectionModel, QPoint, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from animusic.gui.pages import AnimePage, ThemePage
from animusic.gui.widgets import DictTableModel, DictTableView


def anime(name, **values):
    return dict(title_cn=name, title_jp="", included=True, origin="manual", **values)


class TestAnimeControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.page = AnimePage([
            {**anime("来源番", anime_id=1), "origin": "bangumi"},
            anime("已保存手工番", anime_id=2),
            anime(""),
        ])
        self.logs = []
        self.page.log.connect(self.logs.append)

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.page = None
        gc.collect()

    def menu(self, table, row_index, choose=None):
        actions = []

        def add_action(text):
            action = Mock()
            action.text.return_value = text
            actions.append(action)
            return action

        # PySide的实例可能保留原生方法，替换整个菜单，确保不进入模态等待。
        menu = Mock()
        menu.addAction.side_effect = add_action
        menu.isEmpty.side_effect = lambda: not actions
        menu.exec.side_effect = lambda _pos: next((a for a in actions if a.text() == choose), None)

        event = SimpleNamespace(pos=lambda: QPoint(), globalPos=lambda: QPoint())
        with patch.object(table, "indexAt", return_value=table.model().index(row_index, 0)), \
                patch("animusic.gui.anime_table.QMenu", return_value=menu), \
                patch("animusic.gui.widgets.QMenu", return_value=menu):
            table.contextMenuEvent(event)
        return [a.text() for a in actions]

    def delete(self, rows):
        table = self.page.table
        table.clearSelection()
        for row in rows:
            table.selectionModel().select(table.model().index(row, 0),
                                          QItemSelectionModel.Select | QItemSelectionModel.Rows)
        table.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))

    def test_checkbox_logs_changes_noops_and_undo(self):
        model = self.page.model
        idx = model.index(0, 0)
        model.setData(idx, Qt.Unchecked, Qt.CheckStateRole)
        self.assertEqual(self.logs, ["已排除番剧：来源番（待保存）"])
        self.assertFalse(model.rows[0]["included"])
        model.setData(idx, Qt.Unchecked, Qt.CheckStateRole)
        self.assertEqual(len(self.logs), 1)
        self.page.table._do_undo()
        self.assertTrue(model.rows[0]["included"])
        self.assertEqual(self.logs[-1], "撤销：已纳入番剧：来源番（待保存）")
        model.setData(idx, Qt.Unchecked, Qt.CheckStateRole)
        model.setData(idx, Qt.Checked, Qt.CheckStateRole)
        self.assertEqual(self.logs[-1], "已纳入番剧：来源番（待保存）")

    def test_unnamed_row_logging_and_filter_statistics(self):
        model = self.page.model
        model.setData(model.index(2, 0), 0, Qt.CheckStateRole)
        self.assertIn("未命名番剧（第 3 行）", self.logs[-1])
        self.assertIn("纳入 2", self.page.stat.text())
        self.page.filter_combo.setCurrentText("已排除")
        self.assertEqual(len(self.page.visible_rows()), 1)
        model.setData(model.index(2, 0), True, Qt.CheckStateRole)
        self.assertEqual(len(self.page.visible_rows()), 0)

    def test_bulk_changes_are_logged_and_undoable(self):
        self.page._set_all(False)
        self.assertEqual(sum(s.startswith("已排除番剧") for s in self.logs), 3)
        count = len(self.logs)
        self.page._set_all(False)
        self.assertEqual(len(self.logs), count)
        self.page.table._do_undo()
        self.assertTrue(self.page.model.rows[2]["included"])
        self.page._invert()
        self.assertEqual([r["included"] for r in self.page.all_rows()], [True, True, False])

    def test_saved_rows_offer_no_exclude_remove_or_restore_menu(self):
        for i in (0, 1):
            self.assertEqual(self.menu(self.page.table, i), ["新增一行"])
        self.assertEqual(self.menu(self.page.table, 2), ["移除此行", "新增一行"])

    def test_context_menu_removes_only_unsaved_row(self):
        self.menu(self.page.table, 2, "移除此行")
        self.assertEqual(len(self.page.all_rows()), 2)
        self.assertIn("已移除新增番剧：未命名番剧", self.logs[-1])
        self.assertTrue(all(r["included"] for r in self.page.all_rows()))

    def test_saved_delete_is_noop_mixed_delete_removes_only_new_row(self):
        self.delete([0, 1])
        self.assertEqual(len(self.page.all_rows()), 3)
        self.assertTrue(all(r["included"] for r in self.page.all_rows()))
        self.assertEqual(self.logs, [])
        self.delete([0, 1, 2])
        self.assertEqual(len(self.page.all_rows()), 2)
        self.assertTrue(all(r["included"] for r in self.page.all_rows()))
        self.assertEqual(len(self.logs), 1)

    def test_unsaved_row_becomes_protected_after_save_identity_assigned(self):
        self.page.model.rows[2]["anime_id"] = 3
        self.assertEqual(self.menu(self.page.table, 2), ["新增一行"])
        self.delete([2])
        self.assertEqual(len(self.page.all_rows()), 3)
        self.assertTrue(self.page.model.rows[2]["included"])

    def test_removing_new_row_keeps_undo_target_on_correct_anime(self):
        self.page.set_data([anime("新增"), anime("保存", anime_id=10)])
        self.page.model.setData(self.page.model.index(1, 0), False, Qt.CheckStateRole)
        self.page.model.remove_rows([0, 1])
        self.assertEqual([r["title_cn"] for r in self.page.all_rows()], ["保存"])
        self.page.table._do_undo()
        self.assertTrue(self.page.model.rows[0]["included"])
        self.assertIn("保存", self.logs[-1])

    def test_context_add_creates_a_removable_manual_row(self):
        self.menu(self.page.table, 0, "新增一行")
        self.assertEqual(len(self.page.all_rows()), 4)
        self.assertTrue(self.page.model.can_remove(self.page.model.rows[3]))

    def test_second_step_keeps_existing_table_and_menu(self):
        page = ThemePage([dict(theme_id=1, anime_id=1, included=True, song_title="曲",
                               origin="manual", kind="OP", artists="人")], [])
        try:
            self.assertIs(type(page.model), DictTableModel)
            self.assertIs(type(page.table), DictTableView)
            texts = self.menu(page.table, 0)
            self.assertIn("排除此行（保留历史）", texts)
            self.assertIn("从结果中排除（不删除）", texts)
            self.assertIn("恢复为数据源值", texts)
        finally:
            page.deleteLater()
            QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
