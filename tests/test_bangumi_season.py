"""当季全量来源：假浏览器页面，不联网、不启动浏览器。"""

import json
import unittest
from datetime import date
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote, urlsplit

from animusic.models import Season
from animusic.sources.bangumi import BangumiSource, BangumiReadError


class TestBangumiSeason(unittest.TestCase):
    def setUp(self):
        self.bridge = Mock()
        self.tab, self.page = Mock(), Mock()
        self.bridge.open_tab_and_wait.return_value = self.tab, self.page
        self.bridge.navigate_and_wait.return_value = True
        self.source = BangumiSource(self.bridge)
        self.season = Season(2026, 4)
        sleep = patch('animusic.sources.bangumi.time.sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def payload(self, month=10, page=1, rows=None, next_page=False, **overrides):
        data = dict(url=self.source.season_browser_url(2026, month, page), valid=True,
                    current_page=page, rows=rows or [], has_next=next_page)
        data.update(overrides)
        return json.dumps(data)

    @staticmethod
    def row(sid, info='', **overrides):
        return dict(bgm_id=sid, title_cn='测试番', title_jp='テスト', info=info, **overrides)

    def read(self, pages, **kwargs):
        self.bridge.eval_in_tab.side_effect = pages
        return self.source.read_season_all(self.season, **kwargs)

    def assert_closed(self):
        self.page.close.assert_called_once()
        self.bridge.close_tab.assert_called_once_with(self.tab)

    def test_quarters_and_page_urls(self):
        for quarter in range(1, 5):
            self.bridge.reset_mock()
            season = Season(2027, quarter)
            self.bridge.eval_in_tab.side_effect = [json.dumps(dict(
                url=self.source.season_browser_url(2027, m), valid=True,
                current_page=None, has_next=False, rows=[]))
                for m in range(season.start_month, season.start_month + 3)]
            self.source.read_season_all(season)
            urls = [self.bridge.open_tab_and_wait.call_args.args[0]] + [
                c.args[1] for c in self.bridge.navigate_and_wait.call_args_list]
            self.assertEqual(len(urls), 3)
            for month, url in zip(range(season.start_month, season.start_month + 3), urls):
                self.assertEqual(unquote(urlsplit(url).path),
                                 f'/anime/browser/tv/日本/airtime/2027-{month}')
                self.assertEqual(parse_qs(urlsplit(url).query), {'sort': ['title'], 'page': ['1']})
        url = self.source.season_browser_url(2026, 10, 2)
        self.assertIn('airtime/2026-10', url)
        self.assertEqual(parse_qs(urlsplit(url).query)['page'], ['2'])

    def test_months_and_partial_page_overlap_keep_distinct_ids(self):
        result = self.read([
            self.payload(rows=[self.row(1, '12话 / 2026年10月1日')], next_page=True),
            self.payload(page=2, rows=[self.row(1), self.row(2, '2026-10-15')]),
            self.payload(month=11, rows=[self.row(3, '2026年11月')]),
            self.payload(month=12, rows=[self.row(4, '2026年12月1日')]),
        ])
        self.assertEqual([s.bgm_id for s in result.subjects], [1, 2, 3, 4])
        self.assertEqual([s.air_date for s in result.subjects],
                         [date(2026, 10, 1), date(2026, 10, 15), None, date(2026, 12, 1)])
        self.assertEqual(result.path, 'season_browser')
        self.assert_closed()

    def test_cross_month_dedup_does_not_stop_month_paging(self):
        result = self.read([
            self.payload(rows=[self.row(1)]),
            self.payload(month=11, rows=[self.row(1)], next_page=True),
            self.payload(month=11, page=2, rows=[self.row(2)]),
            self.payload(month=12),
        ])
        self.assertEqual([s.bgm_id for s in result.subjects], [1, 2])

    def test_fake_eleven_pages_stop_on_valid_empty_page(self):
        result = self.read([
            self.payload(rows=[self.row(1)], next_page=True),
            self.payload(page=2, next_page=True),
            self.payload(month=11), self.payload(month=12),
        ])
        self.assertEqual(len(result.subjects), 1)
        self.assertEqual(self.bridge.eval_in_tab.call_count, 4)

    def test_empty_month_does_not_skip_later_months(self):
        result = self.read([self.payload(), self.payload(month=11, rows=[self.row(1)]),
                            self.payload(month=12)])
        self.assertEqual(len(result.subjects), 1)

    def test_valid_empty_season(self):
        result = self.read([self.payload(month=m) for m in (10, 11, 12)])
        self.assertEqual(result.subjects, [])
        self.assert_closed()

    def test_uses_existing_site_domain_without_navigating_existing_tab(self):
        self.bridge.find_bangumi_tab.return_value.url = 'https://bangumi.tv/anime'
        self.read([self.payload(month=m, url=self.source.season_browser_url(
            2026, m, host='bangumi.tv')) for m in (10, 11, 12)])
        urls = [self.bridge.open_tab_and_wait.call_args.args[0]] + [
            c.args[1] for c in self.bridge.navigate_and_wait.call_args_list]
        self.assertTrue(all(url.startswith('https://bangumi.tv/') for url in urls))
        self.assertTrue(all(c.args[0] is self.page for c in self.bridge.navigate_and_wait.call_args_list))
        self.assert_closed()

    def test_conflicting_original_names_fail(self):
        conflict = self.row(1)
        conflict['title_jp'] = '別の作品'
        with self.assertRaisesRegex(BangumiReadError, '原名冲突'):
            self.read([self.payload(rows=[self.row(1)]),
                       self.payload(month=11, rows=[conflict])])
        self.assert_closed()

    def test_repeated_page_is_failure_not_complete(self):
        with self.assertRaisesRegex(BangumiReadError, '重复'):
            self.read([self.payload(rows=[self.row(1)], next_page=True),
                       self.payload(page=2, rows=[self.row(1)])])
        self.assert_closed()

    def test_invalid_page_or_schema_is_failure(self):
        for value in ['403 Forbidden', 'null', '[]', self.payload(valid=False),
                      self.payload(rows='bad'), self.payload(has_next=None),
                      self.payload(url='https://bgm.tv/login'),
                      self.payload(url=self.source.season_browser_url(2026, 11)),
                      self.payload(current_page=2)]:
            with self.subTest(value=value), self.assertRaises(BangumiReadError):
                self.source._season_page(value, 2026, 10, 1)

    def test_wrong_first_air_date_or_invalid_rows_fail(self):
        for row in [None, {}, self.row(0), self.row(1, '12话 / 2026年9月1日'),
                    self.row(1, '2025年'), self.row(1, '2026年10月32日')]:
            with self.subTest(row=row), self.assertRaises(BangumiReadError):
                self.source._season_subject(row, 2026, 10)

    def test_partial_dates_and_missing_dates_are_not_invented(self):
        for text in ['', '2026年', '2026年10月', '12话 / 未定 / 制作于2025年']:
            with self.subTest(text=text):
                self.assertIsNone(self.source._season_subject(self.row(1, text), 2026, 10).air_date)

    def test_later_month_failure_does_not_return_partial_list(self):
        with self.assertRaises(BangumiReadError):
            self.read([self.payload(rows=[self.row(1)]), self.payload(month=11, valid=False)])
        self.assert_closed()

    def test_navigation_timeout(self):
        self.bridge.navigate_and_wait.return_value = False
        with self.assertRaisesRegex(BangumiReadError, '超时'):
            self.read([self.payload(rows=[self.row(1)], next_page=True)])
        self.assert_closed()

    def test_page_limit_is_failure(self):
        with patch('animusic.sources.bangumi.SEASON_MAX_PAGES', 2):
            with self.assertRaisesRegex(BangumiReadError, '保护上限'):
                self.read([self.payload(rows=[self.row(1)], next_page=True),
                           self.payload(page=2, rows=[self.row(2)], next_page=True)])
        self.assert_closed()

    def test_cancellation_closes_only_temporary_tab(self):
        class Cancelled(Exception):
            pass

        check = Mock(side_effect=[None, None, Cancelled()])
        with self.assertRaises(Cancelled):
            self.read([], check_cancelled=check)
        self.assert_closed()


if __name__ == '__main__':
    unittest.main()
