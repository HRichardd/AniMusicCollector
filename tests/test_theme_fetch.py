"""第二步状态、重试与缓存的离线验收；所有来源均显式替换。"""
import os
import tempfile
import time
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from animusic.http import HttpClient, RateLimited
from animusic.models import Anime, Candidate, DecisionStatus, Origin, Season, Theme
from animusic.orchestrator import OperationCancelled, Orchestrator
from animusic.sources.wikipedia import WikipediaSource, ThemeFetchResult, WikiTheme
from animusic.store import Store, ThemeIdentityConflict


def song(title='曲A'):
    return WikiTheme(kind='OP', song_title=title, artists=['艺人'], sequence=1)


class TestThemeFetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'state.db')
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.season = Season.parse('2026Q4')
        self.sid = self.store.get_season_id(2026, 4)
        self.ids = [self.store.upsert_anime(self.sid, Anime(
            season_key=self.season.key, title_jp=name, bgm_id=i))
            for i, name in enumerate(('番A', '番B', '番C'), 1)]
        self.logs = []
        self.orch = Orchestrator.__new__(Orchestrator)
        self.orch.store = self.store
        self.orch.progress = self.logs.append
        self.orch.should_stop = None
        self.orch.wikipedia = SimpleNamespace(fetch_result=Mock(return_value=ThemeFetchResult(themes=[song()])))
        self.orch.animethemes = Mock()
        self.orch._at_pool = Mock(side_effect=AssertionError('不许查询 AnimeThemes'))
        self.orch._anilist_pool = Mock(side_effect=AssertionError('不许查询 AniList'))
        self.orch.match_season = Mock(side_effect=AssertionError('不许执行第三步'))

    def states(self):
        return {r['anime_id']: r for r in self.store.theme_fetch_rows(self.sid)}

    def test_all_requeries_success_but_never_excluded(self):
        self.store.set_anime_included(self.ids[2], False)
        self.store.set_theme_fetch(self.ids[0], 'success', saved_count=1)
        self.orch.refresh_themes(self.season)
        self.assertEqual(self.orch.wikipedia.fetch_result.call_count, 2)
        self.assertEqual([r['status'] for r in self.states().values()], ['success', 'success', 'pending'])
        self.assertEqual(len(self.store.anime_list(self.sid, False)), 3)
        self.orch._at_pool.assert_not_called()
        self.orch._anilist_pool.assert_not_called()
        self.orch.match_season.assert_not_called()

    def test_each_targeted_button_only_queries_its_state(self):
        self.store.set_theme_fetch(self.ids[0], 'success', saved_count=1)
        self.store.set_theme_fetch(self.ids[1], 'failed', reason_code='timeout')
        self.orch.fetch_pending_themes(self.season)
        self.orch.wikipedia.fetch_result.assert_called_once_with(['番C'])
        self.orch.wikipedia.fetch_result.reset_mock()
        self.orch.retry_failed_themes(self.season)
        self.orch.wikipedia.fetch_result.assert_called_once_with(['番B'])

    def test_breaker_current_pending_remaining_pending_and_can_resume(self):
        self.orch.wikipedia.fetch_result.side_effect = [
            ThemeFetchResult(themes=[song()]),
            ThemeFetchResult(reason_code='rate_limited', message='限流', stopped=True)]
        self.orch.refresh_themes(self.season)
        states = self.states()
        self.assertEqual([r['status'] for r in states.values()], ['success', 'pending', 'pending'])
        self.assertEqual([r['attempts'] for r in states.values()], [1, 1, 0])
        self.orch.wikipedia.fetch_result.side_effect = None
        self.orch.wikipedia.fetch_result.reset_mock()
        self.orch.fetch_pending_themes(self.season)
        self.assertEqual(self.orch.wikipedia.fetch_result.call_args_list,
                         [unittest.mock.call(['番B']), unittest.mock.call(['番C'])])
        self.orch.wikipedia.fetch_result.reset_mock()
        self.orch.retry_failed_themes(self.season)
        self.orch.wikipedia.fetch_result.assert_not_called()

    def test_old_rate_limit_migration_preserves_data_and_wait(self):
        self.orch.refresh_themes(self.season)
        tid = self.store.theme_list(self.sid)[0]['id']
        self.store.upsert_candidate(tid, Candidate(theme_id=tid, netease_song_id='123',
            name='历史候选', artists=['艺人'], duration_ms=1000, score=0.8, score_detail={}, origin=Origin.SEARCH))
        self.store.set_decision(tid, DecisionStatus.SKIPPED, decided_by='human')
        self.store.mark_pushed('历史歌单', '123', tid)
        deadline = time.time() + 3600
        self.store.set_theme_fetch(self.ids[1], 'failed', reason_code='rate_limited',
                                   message='原始限流原因', retry_not_before=deadline, start=True,
                                   diagnostics={'query_stage': '读取正文', 'source_language': 'ja'})
        self.store.set_theme_fetch(self.ids[2], 'failed', reason_code='timeout', message='原始超时')
        self.store.save_source_wait(deadline)
        original = self.states()[self.ids[1]]
        before = {name: [tuple(r) for r in self.store.conn.execute(f'SELECT * FROM {name}')]
                  for name in ('anime', 'theme', 'candidate', 'decision', 'pushed')}
        for _ in range(2):
            self.store.close()
            self.store = Store(self.path)
            self.orch.store = self.store
            self.assertEqual(self.states()[self.ids[1]], dict(original, status='pending'))
            self.assertEqual(self.states()[self.ids[2]]['status'], 'failed')
            self.assertEqual(before, {name: [tuple(r) for r in self.store.conn.execute(f'SELECT * FROM {name}')]
                                      for name in before})
        self.orch.wikipedia.fetch_result.reset_mock()
        self.orch.fetch_pending_themes(self.season)
        self.orch.wikipedia.fetch_result.assert_not_called()

    def test_actual_count_includes_excluded_and_is_not_saved_count(self):
        self.orch.refresh_themes(self.season)
        self.store.conn.execute('UPDATE theme SET included=0 WHERE anime_id=?', (self.ids[0],))
        self.store.set_theme_fetch(self.ids[0], 'pending')
        row = self.states()[self.ids[0]]
        self.assertEqual((row['theme_count'], row['saved_count']), (1, 0))

    def test_cancel_current_and_remaining_pending_finished_kept(self):
        stop = [False]
        def query(names):
            stop[0] = names == ['番B']
            return ThemeFetchResult(themes=[song()])
        self.orch.should_stop = lambda: stop[0]
        self.orch.wikipedia.fetch_result.side_effect = query
        with self.assertRaises(OperationCancelled):
            self.orch.refresh_themes(self.season)
        states = self.states()
        self.assertEqual([r['status'] for r in states.values()], ['success', 'pending', 'pending'])
        self.assertEqual([r['attempts'] for r in states.values()], [1, 1, 0])
        self.assertEqual(len(self.store.theme_list(self.sid)), 1)

    def test_cancel_failed_retry_returns_unfinished_targets_to_pending(self):
        for aid in self.ids:
            self.store.set_theme_fetch(aid, 'failed')
        self.orch.should_stop = lambda: True
        with self.assertRaises(OperationCancelled):
            self.orch.retry_failed_themes(self.season)
        self.assertTrue(all(r['status'] == 'pending' and r['attempts'] == 0 for r in self.states().values()))

    def test_conflict_rolls_back_whole_anime_and_retries_idempotently(self):
        self.store.set_anime_included(self.ids[1], False)
        self.store.set_anime_included(self.ids[2], False)
        self.orch.wikipedia.fetch_result.return_value = ThemeFetchResult(themes=[song('一'), song('二')])
        real = self.store.upsert_theme
        def save(aid, theme):
            if theme.song_title == '二':
                raise ThemeIdentityConflict('来源身份歧义')
            return real(aid, theme)
        with patch.object(self.store, 'upsert_theme', side_effect=save):
            self.orch.refresh_themes(self.season)
        self.assertEqual(self.store.theme_list(self.sid), [])
        self.assertEqual(self.states()[self.ids[0]]['reason_code'], 'save_conflict')
        self.orch.retry_failed_themes(self.season)
        self.orch.refresh_themes(self.season)
        self.assertEqual(len(self.store.theme_list(self.sid)), 2)
        self.assertEqual(self.states()[self.ids[0]]['saved_count'], 2)

    def test_manual_edit_selection_and_decisions_survive_repeat(self):
        aid = self.ids[0]
        self.orch.refresh_themes(self.season)
        row = self.store.theme_list(self.sid)[0]
        tid = row['id']
        self.store.conn.execute("UPDATE theme SET song_title='人工曲名',artists='[\"人工艺人\"]',"
                                "included=0,origin='manual' WHERE id=?", (tid,))
        self.store._commit()
        self.store.set_decision(tid, DecisionStatus.SKIPPED, decided_by='human')
        before = dict(self.store.decision_map(self.sid)[tid])
        self.orch.refresh_themes(self.season)
        row = next(r for r in self.store.theme_list(self.sid, False) if r['id'] == tid)
        self.assertEqual((row['song_title'], row['included']), ('人工曲名', 0))
        self.assertEqual(dict(self.store.decision_map(self.sid)[tid]), before)
        self.assertEqual(len(self.store.theme_list(self.sid, False)), 3)

    def test_manual_song_does_not_make_wiki_success(self):
        self.store.upsert_theme(self.ids[0], Theme(anime_id=self.ids[0], kind='OP',
            song_title='手动补曲', artists=[], source='manual', origin=Origin.MANUAL))
        self.orch.wikipedia.fetch_result.return_value = ThemeFetchResult(reason_code='article_not_found', message='未找到词条')
        rep = self.orch.refresh_themes(self.season)
        self.assertEqual(rep.subjects_with_themes, 0)
        self.assertTrue(all(r['status'] == 'failed' for r in self.states().values()))

    def test_restart_recovers_running_and_preserves_failure_and_deadline(self):
        self.store.set_theme_fetch(self.ids[0], 'running', start=True)
        self.store.set_theme_fetch(self.ids[1], 'failed', reason_code='timeout', message='超时')
        deadline = time.time() + 3600
        self.store.save_source_wait(deadline)
        self.store.close()
        self.store = Store(self.path)
        self.orch.store = self.store
        self.assertEqual(self.states()[self.ids[0]]['status'], 'pending')
        self.assertEqual(self.states()[self.ids[1]]['message'], '超时')
        self.orch.refresh_themes(self.season)
        self.orch.wikipedia.fetch_result.assert_not_called()
        self.assertEqual(self.store.source_wait_until(), deadline)
        self.assertEqual(self.states()[self.ids[0]]['attempts'], 1)

    def test_rename_invalidates_query_without_touching_themes(self):
        self.orch.refresh_themes(self.season)
        rows = [dict(r) for r in self.store.conn.execute('SELECT * FROM theme')]
        self.store.conn.execute("UPDATE anime SET title_jp='新名称' WHERE id=?", (self.ids[0],))
        self.store._commit()
        self.assertEqual(self.states()[self.ids[0]]['status'], 'pending')
        self.assertEqual([dict(r) for r in self.store.conn.execute('SELECT * FROM theme')], rows)

    def test_empty_selection_no_requests_or_attempts(self):
        self.orch.retry_failed_themes(self.season)
        self.orch.wikipedia.fetch_result.assert_not_called()
        self.assertTrue(all(r['attempts'] == 0 for r in self.states().values()))

    def test_old_source_entry_disabled_before_any_read(self):
        with self.assertRaises(NotImplementedError):
            self.orch.fetch_season_from_animethemes(self.season)
        self.orch.animethemes.assert_not_called()

    def test_old_database_migration_is_repeatable_and_preserves_user_data(self):
        tid = self.store.upsert_theme(self.ids[0], Theme(anime_id=self.ids[0], kind='OP',
            song_title='历史曲目', artists=['历史艺人'], source='animethemes', origin=Origin.ANIMETHEMES))
        self.store.set_decision(tid, DecisionStatus.SKIPPED, decided_by='human')
        self.store.mark_pushed('历史歌单', '123', tid)
        before = {name: [tuple(r) for r in self.store.conn.execute(f'SELECT * FROM {name}')]
                  for name in ('anime', 'theme', 'decision', 'pushed')}
        # 在独立测试库中模拟尚无第二步状态表的旧版本。
        self.store.conn.executescript('DROP TRIGGER anime_theme_fetch_insert; '
                                     'DROP TRIGGER anime_theme_fetch_rename; '
                                     'DROP TABLE theme_fetch; DROP TABLE source_wait;')
        for _ in range(2):
            self.store.close()
            self.store = Store(self.path)
            self.orch.store = self.store
            self.assertTrue(all(r['status'] == 'pending' and r['attempts'] == 0 for r in self.states().values()))
            after = {name: [tuple(r) for r in self.store.conn.execute(f'SELECT * FROM {name}')]
                     for name in before}
            self.assertEqual(before, after)


class TestWikiFetchResult(unittest.TestCase):
    text = '== 主題歌 ==\n; 「歌A」\n: [[艺人]]によるオープニングテーマ。\n'

    def source(self, text=None):
        src = WikipediaSource(client=HttpClient(rps=0, retries=0))
        calls = []
        def api(url, **kwargs):
            calls.append((url, kwargs))
            params = parse_qs(urlparse(url).query)
            if params.get('list') == ['search']:
                return {'query': {'search': []}}
            titles = params['titles'][0].split('|')
            pages = [{'title': name, 'missing': True} for name in titles]
            for page in pages:
                if page['title'] == '作品A (アニメ)':
                    page.pop('missing')
                    page['revisions'] = [{'slots': {'main': {'content': self.text if text is None else text}}}]
            return {'query': {'pages': pages}}
        src.http.get_json = Mock(side_effect=api)
        return src, calls

    def test_new_round_fetches_again_same_round_reuses_body(self):
        src, calls = self.source()
        src.begin_round()
        first = src.fetch_result(['作品A'])
        n = len(calls)
        second = src.fetch_result(['作品A'])
        self.assertTrue(first.themes)
        self.assertEqual(first.themes, second.themes)
        self.assertEqual(len(calls), n)
        self.assertTrue(all(not kw['use_cache'] for _, kw in calls))
        src.begin_round()
        self.assertTrue(src.fetch_result(['作品A']).themes)
        self.assertGreater(len(calls), n)

    def test_no_section_and_unparsed_section_are_distinct(self):
        for text, expected in [('== あらすじ ==\n正文', 'no_theme_section'),
                               ('== 主題歌 ==\n未知格式', 'no_usable_themes')]:
            with self.subTest(expected=expected):
                src, _ = self.source(text)
                src.begin_round()
                result = src.fetch_result(['作品A'])
                self.assertEqual(result.reason_code, expected)
                self.assertTrue(result.article_url)

    def test_missing_article(self):
        src, _ = self.source()
        src.begin_round()
        self.assertEqual(src.fetch_result(['未知作品']) .reason_code, 'article_not_found')

    def test_failed_request_not_cached_and_new_round_resets_breaker(self):
        src, calls = self.source()
        real = src.http.get_json.side_effect
        src.begin_round()
        src.http.get_json.side_effect = RateLimited('限流', retry_after=10, breaker=True)
        self.assertEqual(src.fetch_result(['作品A']).reason_code, 'rate_limited')
        self.assertFalse(src._wt_cache)
        src.http._rl_tripped.add('ja.wikipedia.org')
        src.begin_round()
        self.assertFalse(src.http._rl_tripped)
        src.http.get_json.side_effect = real
        self.assertTrue(src.fetch_result(['作品A']).themes)

    def test_timeout_network_and_parse_errors_are_distinct(self):
        for error, expected in [(TimeoutError(), 'timeout'),
                                (urllib.error.URLError('断网'), 'network_error'),
                                (ValueError('异常响应'), 'parse_error')]:
            with self.subTest(expected=expected):
                src, _ = self.source()
                src.begin_round()
                src.http.get_json.side_effect = error
                self.assertEqual(src.fetch_result(['作品A']).reason_code, expected)
                self.assertEqual(src.stats['titles_asked'], src.stats['titles_unanswered'])

    def test_malformed_batch_counts_all_unanswered_candidates(self):
        for payload in ({'query': {}}, {'query': {'pages': []}},
                        {'query': {'pages': [{'title': '作品A (アニメ)', 'revisions': []}]}}):
            with self.subTest(payload=payload):
                src, _ = self.source()
                src.begin_round()
                src.http.get_json.side_effect = None
                src.http.get_json.return_value = payload
                self.assertEqual(src.fetch_result(['作品A']).reason_code, 'parse_error')
                self.assertEqual(src.stats['titles_asked'], src.stats['titles_unanswered'])

    def test_cancellation_propagates(self):
        src, _ = self.source()
        src.begin_round()
        src.http.get_json.side_effect = OperationCancelled('停止')
        src.http.check_cancelled = Mock(side_effect=OperationCancelled('停止'))
        with self.assertRaises(OperationCancelled):
            src.fetch_result(['作品A'])

    def test_readable_diagnostics_distinguish_responses_and_persist(self):
        payloads = [({'error': {'code': 'badvalue', 'info': '无效参数'}}, 'api_error', '无效参数'),
                    ({'query': {}}, 'invalid_pages', '词条列表'),
                    ({'query': {'pages': [{'title': '作品A (アニメ)', 'revisions': []}]}},
                     'missing_content', '缺少正文')]
        for payload, code, message in payloads:
            with self.subTest(code=code):
                src, _ = self.source()
                src.begin_round()
                src.http.get_json = Mock(return_value=payload)
                result = src.fetch_result(['作品A'])
                self.assertIn(message, result.message)
                self.assertEqual(result.diagnostics['error_code'], code)
                self.assertEqual(result.diagnostics['query_stage'], '读取词条正文')
                self.assertEqual(result.diagnostics['source_language'], 'ja')
                if code == 'missing_content':
                    self.assertTrue(result.article_url)
                with tempfile.TemporaryDirectory() as folder:
                    store = Store(os.path.join(folder, 'state.db'))
                    sid = store.get_season_id(2026, 4)
                    aid = store.upsert_anime(sid, Anime(season_key='2026Q4', title_jp='作品A'))
                    store.set_theme_fetch(aid, 'failed', reason_code=result.reason_code,
                                          message=result.message, diagnostics=result.diagnostics)
                    self.assertEqual(store.theme_fetch_rows(sid)[0]['diagnostics'], result.diagnostics)
                    store.close()

    def test_cancel_during_rate_limit_wait_prevents_retry(self):
        client = HttpClient(rps=0, retries=0, rate_limit_retries=1, rate_limit_wait_budget=30)
        stopped = [False]
        def check():
            if stopped[0]:
                raise OperationCancelled('停止')
        client.check_cancelled = check
        client.on_rate_limit = Mock()
        limited = urllib.error.HTTPError('https://ja.wikipedia.org/test', 429, 'limit', {'Retry-After': '5'}, None)
        try:
            with patch('animusic.http.urllib.request.urlopen', side_effect=limited) as request, \
                    patch('animusic.http.time.sleep', side_effect=lambda _: stopped.__setitem__(0, True)):
                with self.assertRaises(OperationCancelled):
                    client.get_json('https://ja.wikipedia.org/test')
            request.assert_called_once()
            client.on_rate_limit.assert_called_once()
            self.assertGreater(client.on_rate_limit.call_args.args[0], time.time())
        finally:
            limited.close()

    def test_real_http_wrapper_preserves_empty_timeout_cause(self):
        for error, expected in [(TimeoutError(), 'timeout'),
                                (urllib.error.URLError(TimeoutError()), 'timeout'),
                                (urllib.error.URLError(OSError('断网')), 'network_error')]:
            with self.subTest(expected=expected):
                src = WikipediaSource(client=HttpClient(rps=0, retries=0))
                src.begin_round()
                with patch('animusic.http.urllib.request.urlopen', side_effect=error):
                    result = src.fetch_result(['作品A'])
                self.assertEqual(result.reason_code, expected)

    def test_wait_requirement_persisted_before_automatic_wait(self):
        client = HttpClient(rps=0, retries=0, rate_limit_retries=1, rate_limit_wait_budget=30)
        client.on_rate_limit = Mock()
        client.check_cancelled = Mock()
        response = SimpleNamespace(status=200, headers={}, read=lambda: b'{}')
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *args: None
        from contextlib import nullcontext
        limited = urllib.error.HTTPError('https://ja.wikipedia.org/test', 429, 'limit', {'Retry-After': '5'}, None)
        with patch('animusic.http.urllib.request.urlopen', side_effect=[limited, nullcontext(response)]), \
                patch('animusic.http.time.sleep') as sleep:
            client.get_json('https://ja.wikipedia.org/test')
        limited.close()
        self.assertEqual(client.stats.requests, 2)
        self.assertEqual(client.stats.rate_limit_retries, 1)
        client.on_rate_limit.assert_called_once()
        self.assertAlmostEqual(sum(call.args[0] for call in sleep.call_args_list), 5.0)

