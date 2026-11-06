"""第二步菜单和勾选日志回归：offscreen、假菜单、临时数据库。"""

import gc
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtWidgets import QApplication, QInputDialog

from animusic.config import Config
from animusic.gui.pages import ThemePage, MatchPage, PushPage
from animusic.gui.session import WizardSession
from animusic.gui.widgets import DictTableModel, DictTableView
from animusic.models import Anime, Theme, ThemeKind, Season
from animusic.normalize import AliasTable
from animusic.store import Store


def song(title, kind='OP', **values):
    return dict(anime='番A', anime_id=1, included=True, song_title=title, kind=kind,
                artists='歌手', origin='search', seq=1, **values)


class TestThemeControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.pages = []
        self.logs = []
        self.page = self.make_page([
            song('片头', theme_id=1, theme_included=True, anime_included=True),
            {**song('片尾', 'ED', theme_id=2), 'included': False, 'theme_included': False},
            {**song('插入曲', 'IN', theme_id=3), 'origin': 'manual', 'theme_included': True},
            {**song(''), 'origin': 'manual'},
            {**song('番剧已排除曲目', theme_id=4), 'included': False,
             'theme_included': True, 'anime_included': False},
        ])

    def make_page(self, rows):
        page = ThemePage(rows, [])
        self.pages.append(page)
        page.log.connect(self.logs.append)
        return page

    def tearDown(self):
        for page in self.pages:
            page.close()
            page.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.pages.clear()
        self.page = None
        gc.collect()

    def menu(self, table, row_index, choose=None):
        actions = []
        menu = Mock()
        def add_action(text):
            action = Mock()
            action.text.return_value = text
            actions.append(action)
            return action
        menu.addAction.side_effect = add_action
        menu.exec.side_effect = lambda _pos: next((a for a in actions if a.text() == choose), None)
        event = SimpleNamespace(pos=lambda: QPoint(), globalPos=lambda: QPoint())
        with patch.object(table, 'indexAt', return_value=table.model().index(row_index, 0)), \
                patch('animusic.gui.theme_table.QMenu', return_value=menu), \
                patch('animusic.gui.widgets.QMenu', return_value=menu):
            table.contextMenuEvent(event)
        return [a.text() for a in actions], menu

    def check(self, row, state):
        return self.page.model.setData(self.page.model.index(row, 0), state, Qt.CheckStateRole)

    def test_menu_only_adds_for_saved_manual_unsaved_excluded_and_blank(self):
        before = [dict(r) for r in self.page.all_rows()]
        for index in range(-1, len(before)):
            with self.subTest(index=index):
                texts, menu = self.menu(self.page.table, index)
                self.assertEqual(texts, ['新增一行'])
                menu.addSeparator.assert_not_called()
        self.assertEqual(self.page.all_rows(), before)
        self.assertEqual(self.logs, [])

    def test_context_add_uses_existing_anime_selection_dialog(self):
        self.page.anime_choices = [(1, '番A'), (2, '番B')]
        self.page.selected_anime_id = 2
        with patch.object(QInputDialog, 'getItem', return_value=('番B（选项2）', True)) as dialog:
            self.menu(self.page.table, 0, '新增一行')
        dialog.assert_called_once()
        self.assertEqual(dialog.call_args.args[4], 1)
        row = self.page.all_rows()[-1]
        self.assertEqual((row['anime_id'], row['anime'], row['origin']), (2, '番B', 'manual'))
        self.assertIn('新增第 6 行', self.logs[-1])

    def test_cancel_add_does_not_change_rows(self):
        self.page.anime_choices = [(1, '番A'), (2, '番B')]
        with patch.object(QInputDialog, 'getItem', return_value=('', False)) as dialog:
            self.menu(self.page.table, -1, '新增一行')
        dialog.assert_called_once()
        self.assertEqual(len(self.page.all_rows()), 5)
        self.assertEqual(self.logs, [])

    def test_boolean_and_qt_checkbox_values_log_only_real_changes(self):
        for state in (Qt.Unchecked, False, 0):
            self.assertTrue(self.check(0, state))
        self.assertEqual(self.logs, ['已排除曲目：番A｜OP｜片头（第 1 行，待保存）'])
        self.assertEqual(len(self.page.model._undo), 1)
        row = self.page.all_rows()[0]
        self.assertFalse(row['included'])
        self.assertFalse(row['theme_included'])
        for state in (Qt.Checked, True, 2):
            self.assertTrue(self.check(0, state))
        self.assertEqual(len(self.logs), 2)
        self.assertEqual(self.logs[-1], '已收录曲目：番A｜OP｜片头（第 1 行，待保存）')
        self.assertEqual(len(self.page.model._undo), 2)

    def test_all_three_kinds_are_identifiable_in_logs(self):
        for index, kind, title in [(0, 'OP', '片头'), (1, 'ED', '片尾'), (2, '插入曲', '插入曲')]:
            self.check(index, True)
            self.check(index, False)
            self.assertIn(f'番A｜{kind}｜{title}', self.logs[-1])
            self.assertIn('待保存', self.logs[-1])

    def test_unnamed_row_has_readable_fallback(self):
        self.check(3, False)
        self.assertIn('未命名曲目（第 4 行', self.logs[-1])
        self.assertNotIn('None', self.logs[-1])

    def test_undo_restores_own_flag_filter_and_statistics(self):
        self.page._apply_filter('已排除', log=False)
        self.check(1, True)
        self.assertNotIn(self.page.all_rows()[1], self.page.visible_rows())
        self.page.table._do_undo()
        row = self.page.all_rows()[1]
        self.assertFalse(row['included'])
        self.assertFalse(row['theme_included'])
        self.assertIn(row, self.page.visible_rows())
        self.assertEqual(self.logs[-1], '撤销：已排除曲目：番A｜ED｜片尾（第 2 行，待保存）')
        self.assertIn('合计 3 条', self.page.stat.text())

    def test_undo_new_row_and_text_edit_keep_independent_history(self):
        self.check(3, False)
        self.page.model.setData(self.page.model.index(3, 4), '临时曲名')
        self.page.table._do_undo()
        self.assertEqual(self.page.all_rows()[3]['song_title'], '')
        self.assertFalse(self.page.all_rows()[3]['included'])
        self.page.table._do_undo()
        row = self.page.all_rows()[3]
        self.assertTrue(row['included'])
        self.assertNotIn('theme_included', row)
        self.assertIn('撤销：已收录曲目', self.logs[-1])

    def test_excluded_anime_cannot_be_reincluded_by_song_checkbox(self):
        row = self.page.all_rows()[4]
        self.check(4, False)
        self.check(4, True)
        self.assertFalse(row['included'])
        self.assertFalse(row['anime_included'])
        self.assertTrue(row['theme_included'])
        self.assertIn('所属番剧已排除', self.logs[-1])
        self.page.table._do_undo()
        self.assertFalse(row['included'])
        self.assertFalse(row['theme_included'])

    def test_bulk_changes_only_visible_rows_and_noop_is_silent(self):
        self.page._apply_filter('仅 OP', log=False)
        self.page._set_all(False)
        self.assertEqual(sum(line.startswith('已排除曲目') for line in self.logs), 3)
        self.assertIn('变更 3 行（待保存）', self.logs[-1])
        self.assertTrue(self.page.all_rows()[2]['included'])
        count = len(self.logs)
        self.page._set_all(False)
        self.assertEqual(len(self.logs), count)

    def test_bulk_reinclude_survives_rows_disappearing_from_filter(self):
        self.check(2, False)
        self.page._apply_filter('已排除', log=False)
        self.logs.clear()
        self.page._set_all(True)
        self.assertEqual(sum(line.startswith('已收录曲目') for line in self.logs), 2)
        self.assertIn('变更 2 行', self.logs[-1])
        self.assertTrue(self.page.all_rows()[1]['included'])
        self.assertTrue(self.page.all_rows()[2]['included'])
        self.assertEqual(self.page.visible_rows(), [self.page.all_rows()[4]])

    def test_bulk_respects_selected_anime(self):
        self.page.all_rows()[2]['anime_id'] = 2
        self.page.selected_anime_id = 2
        self.page._apply_filter('全部', log=False)
        self.page._set_all(False)
        self.assertEqual(sum(line.startswith('已排除曲目') for line in self.logs), 1)
        self.assertTrue(self.page.all_rows()[0]['included'])
        self.page.table._do_undo()
        self.assertTrue(self.page.all_rows()[2]['included'])

    def test_reload_and_filters_do_not_log_fake_checkbox_changes(self):
        self.page.set_data([dict(r) for r in self.page.all_rows()])
        self.page._apply_filter('已排除', log=False)
        self.page._clear_anime_selection()
        self.assertFalse(any(line.startswith(('已收录曲目', '已排除曲目', '撤销：')) for line in self.logs))
        self.assertFalse(self.page.model._undo)

    def test_removing_unsaved_row_does_not_redirect_undo_to_another_song(self):
        self.page.set_data([song('未保存'), song('已保存', theme_id=7)])
        self.check(1, False)
        self.page.model.remove_rows([0])
        self.page.table._do_undo()
        self.assertEqual([r['song_title'] for r in self.page.all_rows()], ['已保存'])
        self.assertTrue(self.page.all_rows()[0]['included'])
        self.assertIn('已保存', self.logs[-1])

    def test_other_steps_keep_generic_menus_and_matching_actions(self):
        match = MatchPage([dict(theme_id=1, anime='番A', kind='OP', seq=1, candidates=[], status='pending_review')])
        push = PushPage([dict(theme_id=1, included=True, name='曲', playlist='仅测试', netease_song_id='1')])
        self.pages.extend([match, push])
        for page in (match, push):
            self.assertIs(type(page.model), DictTableModel)
            self.assertIs(type(page.table), DictTableView)
        texts, _ = self.menu(match.table, 0)
        self.assertIn('清除此行的匹配（退回待审核）', texts)
        self.assertIn('移除此行', texts)
        texts, _ = self.menu(push.table, 0)
        self.assertIn('排除此行（保留历史）', texts)

    def test_filtered_save_and_reload_preserves_hidden_edits_and_parent_exclusion(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        session = WizardSession(config=Config(), store=store, season=Season(2026, 4), aliases=AliasTable())
        sid = session.season_id
        aid = store.upsert_anime(sid, Anime(season_key='2026Q4', title_cn='番A'))
        tids = [store.upsert_theme(aid, Theme(anime_id=aid, kind=kind, song_title=str(kind), source='wikipedia'))
                for kind in (ThemeKind.OP, ThemeKind.IN)]
        self.page.set_data(session.build()['theme'])
        index = next(i for i, r in enumerate(self.page.all_rows()) if r['theme_id'] == tids[0])
        self.check(index, False)
        self.page._apply_filter('仅插入曲', log=False)
        session.save_themes(self.page.all_rows(), confirm=False)
        self.page.set_data(session.build()['theme'])
        self.assertFalse(next(r['included'] for r in self.page.all_rows() if r['theme_id'] == tids[0]))
        self.assertEqual(len(self.page.all_rows()), 2)
        store.set_anime_included(aid, False)
        self.page.set_data(session.build()['theme'])
        index = next(i for i, r in enumerate(self.page.all_rows()) if r['theme_id'] == tids[0])
        self.check(index, True)
        session.save_themes(self.page.all_rows(), confirm=False)
        self.assertFalse(store.anime_list(sid, only_included=False)[0]['included'])
        self.assertTrue(all(not r['included'] for r in session.build()['theme']))
