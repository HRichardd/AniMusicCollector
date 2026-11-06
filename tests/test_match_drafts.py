"""候选暂选/确认的离线回归：不显示窗口、不进事件循环、不发网络请求。"""
import unittest
from copy import deepcopy
from unittest.mock import patch, Mock
from types import SimpleNamespace

from test_gui_undo import GuiTestBase, cand, mrow, confirm_candidate
from animusic.gui.main_window import MainWindow
from animusic.gui.session import WizardSession
from animusic.config import Config
from animusic.models import Anime, Candidate, Season, Theme, ThemeKind
from animusic.normalize import AliasTable
from animusic.store import Store


class TestDrafts(GuiTestBase):
    def setUp(self):
        super().setUp()
        for target in ('PySide6.QtNetwork.QNetworkAccessManager.get',
                       'PySide6.QtWidgets.QMessageBox.question',
                       'PySide6.QtWidgets.QMessageBox.warning',
                       'PySide6.QtWidgets.QMessageBox.information'):
            guard = patch(target, side_effect=AssertionError('禁止网络或模态框'))
            guard.start()
            self.addCleanup(guard.stop)

    def selected_page(self, status='pending_review'):
        a, b = cand('1', 'A'), cand('2', 'B')
        row = mrow(1, status, cands=[a, b], chosen=a)
        page = self.page([row])
        page.table.selectRow(0)
        return page, row, a, b

    def test_checkbox_toggle_is_consistent_without_changing_formal_result(self):
        page, row, a, b = self.selected_page()
        before = deepcopy(row)
        self.assertFalse(any(it.radio.isChecked() for it in page.cand_items))
        page.cand_items[0].radio.click()
        self.assertEqual([it.radio.isChecked() for it in page.cand_items], [True, False])
        page.cand_items[0].radio.click()
        self.assertEqual([it.radio.isChecked() for it in page.cand_items], [False, False])
        self.assertFalse(page.confirm_pick_btn.isEnabled())
        page.cand_items[1].radio.click()
        self.assertEqual([it.radio.isChecked() for it in page.cand_items], [False, True])
        self.assertEqual(row, before)
        self.assertFalse(page._history)

    def test_changing_choice_is_exclusive(self):
        page, row, a, b = self.selected_page()
        page._pick(row, a)
        page._pick(row, b)
        self.assertEqual([it.radio.isChecked() for it in page.cand_items], [False, True])
        self.assertTrue(page.confirm_pick_btn.isEnabled())
        self.assertIn('待确认选择', page.pick_hint.text())

    def test_confirm_hides_last_todo_and_clears_panel(self):
        page, row, a, b = self.selected_page()
        page._apply_filter('需要处理')
        page._pick(row, b)
        self.assertFalse(page.table.isRowHidden(0))
        page.confirm_pick_btn.click()
        self.assertTrue(page.table.isRowHidden(0))
        self.assertEqual(row['chosen_id'], '2')
        self.assertEqual(row['status'], 'confirmed')
        self.assertIsNone(page._candidate_row)
        self.assertEqual(page.cand_items, [])
        self.assertFalse(page.confirm_pick_btn.isEnabled())

    def test_confirm_keeps_all_view_and_supports_undo(self):
        page, row, a, b = self.selected_page()
        page._pick(row, b)
        page._confirm_pick()
        self.assertFalse(page.table.isRowHidden(0))
        self.assertEqual(row['chosen_id'], '2')
        self.assertEqual(len(page._history), 1)
        page._undo_last()
        self.assertEqual(row['chosen_id'], '1')
        self.assertEqual(row['status'], 'pending_review')
        self.assertIsNone(page._draft_for(row))

    def test_sequential_confirmation_refreshes_without_skip(self):
        rows = [mrow(i, 'pending_review', cands=[cand(str(i), str(i))]) for i in range(1, 4)]
        page = self.page(rows)
        page._apply_filter('需要处理')
        page.table.selectRow(1)
        confirm_candidate(page, rows[1], rows[1]['candidates'][0])
        self.assertTrue(page.table.isRowHidden(1))
        self.assertEqual(page._selected_row(), rows[2])
        confirm_candidate(page, rows[2], rows[2]['candidates'][0])
        self.assertTrue(page.table.isRowHidden(2))
        self.assertEqual(page._selected_row(), rows[0])

    def test_drafts_survive_row_and_filter_changes_including_empty(self):
        page, row, a, b = self.selected_page()
        page._pick(row, b)
        page._apply_filter('仅自动')
        page._apply_filter(page.FILTER_ALL)
        page.table.selectRow(0)
        self.assertEqual(page._draft_for(row)['netease_song_id'], '2')
        page._pick(row, b)
        page._show_candidates(row, force=True)
        self.assertIsNone(page._draft_for(row))
        self.assertFalse(any(it.radio.isChecked() for it in page.cand_items))

    def test_cancel_adopted_choice_preserves_formal_record(self):
        page, row, a, b = self.selected_page('confirmed')
        before = deepcopy(row)
        page.cand_items[0].radio.click()
        self.assertEqual(row, before)
        self.assertIn('原已采用结果仍保留', page.pick_hint.text())
        self.assertFalse(page.confirm_pick_btn.isEnabled())
        page.cand_items[0].radio.click()
        page._confirm_pick()
        self.assertFalse(page._history)
        self.assertTrue(page.cand_items[0].radio.isChecked())

    def test_manual_id_requires_confirmation_and_clears_old_metadata(self):
        page, row, a, b = self.selected_page()
        before = deepcopy(row)
        page.manual_input.setText('https://music.163.com/song?id=12345')
        page._manual_pick()
        self.assertEqual(row, before)
        self.assertIn('12345', page.pick_hint.text())
        page._confirm_pick()
        self.assertEqual(row['chosen_id'], '12345')
        self.assertIn('12345', row['chosen_name'])
        self.assertEqual(row['chosen_artists'], '')
        self.assertIsNone(row['detail'])
        self.assertEqual(row['score'], 0)

    def test_no_candidates_manual_id_still_confirmable(self):
        row = mrow(1, 'missing_netease')
        page = self.page([row])
        page.table.selectRow(0)
        page.manual_input.setText('12345')
        page._manual_pick()
        self.assertTrue(page.confirm_pick_btn.isEnabled())
        page._confirm_pick()
        self.assertEqual(row['chosen_id'], '12345')

    def test_invalid_manual_input_is_not_a_draft(self):
        page, row, a, b = self.selected_page()
        for text in ('invalid', '0'):
            page.manual_input.setText(text)
            page._manual_pick()
            self.assertIsNone(page._draft_for(row))
            self.assertFalse(page.confirm_pick_btn.isEnabled())

    def test_skip_clears_draft_and_undo_does_not_restore_it(self):
        page, row, a, b = self.selected_page()
        page._pick(row, b)
        page._skip_current()
        self.assertEqual(row['status'], 'skipped')
        self.assertIsNone(page._draft_for(row))
        page._undo_last()
        self.assertEqual(row['status'], 'pending_review')
        self.assertIsNone(page._draft_for(row))

    def test_bulk_uses_formal_recommendation_and_discards_draft(self):
        page, row, a, b = self.selected_page()
        page._pick(row, b)
        page._accept_all()
        self.assertEqual(row['chosen_id'], '1')
        self.assertEqual(page._draft_for(row)['netease_song_id'], '1')

    def test_reload_discards_draft_even_with_same_theme_id(self):
        page, row, a, b = self.selected_page()
        page._pick(row, b)
        page.set_data([deepcopy(row)])
        self.assertEqual(page._drafts, {})
        self.assertIsNone(page._draft_for(page.model.rows[0]))

    def test_leaving_page_discards_without_confirming(self):
        page, row, a, b = self.selected_page()
        page._pick(row, b)
        stub = SimpleNamespace(_closing=False, worker=None, pages=[Mock(), Mock(), page, Mock()],
                               stack=Mock(), stepper=Mock(), log=Mock(), _log_step_scope=Mock())
        stub.stack.currentIndex.return_value = 2
        MainWindow._goto(stub, 3)
        self.assertEqual(page._drafts, {})
        self.assertEqual(row['status'], 'pending_review')
        self.assertEqual(row['chosen_id'], '1')
        self.assertTrue(self.log_has('未确认的暂选未采用'))

    def test_draft_save_does_not_learn_and_confirmation_does(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        sess = WizardSession(store=store, config=Config(), aliases=AliasTable(), season=Season(2026, 4))
        aid = store.upsert_anime(sess.season_id, Anime(season_key='2026Q4', title_cn='番'))
        tid = store.upsert_theme(aid, Theme(anime_id=aid, kind=ThemeKind.OP, song_title='曲', artists=['人']))
        store.upsert_candidate(tid, Candidate(theme_id=tid, netease_song_id='1', name='曲', artists=['人']))
        page = self.page(sess.build()['match'])
        page.table.selectRow(0)
        row = page.model.rows[0]
        page._pick(row, row['candidates'][0])
        sess.save_decisions(page.all_rows())
        self.assertNotIn(tid, store.decision_map(sess.season_id))
        self.assertEqual(sess.build()['push'], [])
        page._confirm_pick()
        stat = sess.save_decisions(page.all_rows())
        decision = store.decision_map(sess.season_id)[tid]
        self.assertEqual(decision['status'], 'confirmed')
        self.assertEqual(decision['decided_by'], 'human')
        self.assertEqual(stat['checked'], 1)
        self.assertEqual(len(sess.build()['push']), 1)

    def test_card_double_click_does_not_toggle_twice(self):
        from PySide6.QtCore import Qt
        page, row, a, b = self.selected_page()
        event = Mock()
        event.button.return_value = Qt.LeftButton
        card = page.cand_items[0]
        card.mouseReleaseEvent(event)
        card.mouseDoubleClickEvent(event)
        card.mouseReleaseEvent(event)
        self.assertTrue(card.radio.isChecked())
        self.assertEqual(page._draft_for(row)['netease_song_id'], '1')

    def test_removed_candidate_cannot_be_confirmed(self):
        page, row, a, b = self.selected_page()
        page._pick(row, b)
        row['candidates'] = [a]
        page._confirm_pick()
        self.assertEqual(row['status'], 'pending_review')
        self.assertIsNone(page._draft_for(row))
        self.assertFalse(page.confirm_pick_btn.isEnabled())

    def test_no_row_disables_confirmation_and_clears_cards(self):
        page, row, a, b = self.selected_page()
        page._pick(row, b)
        page.table.clearSelection()
        self.assertFalse(page.confirm_pick_btn.isEnabled())
        self.assertIsNone(page._candidate_row)
        self.assertEqual(page.cand_items, [])

    def test_checkbox_double_click_does_not_toggle_twice(self):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        page, row, a, b = self.selected_page()
        checkbox = page.cand_items[0].radio
        checkbox.click()
        event = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(1, 1), QPointF(1, 1),
                            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        checkbox.mouseDoubleClickEvent(event)
        release = QMouseEvent(QEvent.MouseButtonRelease, QPointF(1, 1), QPointF(1, 1),
                              Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
        checkbox.mouseReleaseEvent(release)
        self.assertTrue(checkbox.isChecked())
        self.assertEqual(page._draft_for(row)['netease_song_id'], '1')

    def test_busy_page_disables_confirmation(self):
        page, row, a, b = self.selected_page()
        page._pick(row, b)
        self.assertTrue(page.confirm_pick_btn.isEnabled())
        page.setEnabled(False)
        self.assertFalse(page.confirm_pick_btn.isEnabled())
        page.setEnabled(True)
        self.assertTrue(page.confirm_pick_btn.isEnabled())


if __name__ == '__main__':
    unittest.main()
