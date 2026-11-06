"""第三步诊断与取消的离线回归；所有网络均为假件。"""
import io
import json
import sys
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'vendor')]
from animusic.http import HttpClient, HttpError, RateLimited, safe_diagnostic
from animusic.collector.api_search import ApiSearchCollector, SearchError
from animusic.config import Config
from animusic.models import Anime, Candidate, DecisionStatus, Season, Stage, StageStatus, Theme, ThemeKind
from animusic.normalize import AliasTable
from animusic.orchestrator import Orchestrator, OperationCancelled
from animusic.store import Store


class Reply:
    status = 200
    headers = {'content-type': 'application/json'}
    def __init__(self, body=b'{"code":200,"result":{"songs":[]}}', on_read=None):
        self.body, self.on_read = body, on_read
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        if self.on_read:
            self.on_read()
        return self.body


class TestSearchHttp(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.client = HttpClient(rps=0, retries=1, diagnostics=True, on_event=self.logs.append)
        self.collector = ApiSearchCollector(self.client)

    def test_candidates_and_empty(self):
        song = {'id': 1, 'name': '曲', 'artists': [{'name': '歌手'}]}
        with patch('urllib.request.urlopen', side_effect=[Reply(json.dumps(
                {'code': 200, 'result': {'songs': [song]}}).encode()), Reply()]) as opened:
            self.assertEqual(self.collector.search('曲')[0].netease_song_id, '1')
            self.assertEqual(self.collector.search('空'), [])
        self.assertEqual(opened.call_count, 2)
        self.assertIn('候选 1', '\n'.join(self.logs))
        self.assertIn('HTTP 200', '\n'.join(self.logs))

    def test_business_rejection_and_redaction(self):
        with patch('urllib.request.urlopen', return_value=Reply(json.dumps({
                'code': -462, 'msg': 'token=TOPSECRET Cookie: PRIVATE'}).encode())):
            with self.assertRaises(SearchError) as error:
                self.collector.search('曲')
        self.assertEqual(error.exception.category, '服务拒绝')
        self.assertEqual(error.exception.code, -462)
        self.assertNotIn('TOPSECRET', str(error.exception))
        self.assertNotIn('PRIVATE', str(error.exception))

    def test_invalid_json_has_http_status_without_body(self):
        with patch('urllib.request.urlopen', return_value=Reply(b'<html>PRIVATE</html>')):
            with self.assertRaises(SearchError) as error:
                self.collector.search('曲')
        self.assertEqual(error.exception.http_status, 200)
        self.assertEqual(error.exception.category, '解析错误')
        self.assertNotIn('PRIVATE', str(error.exception))

    def test_network_retry_and_exhaustion(self):
        with patch('urllib.request.urlopen', side_effect=[TimeoutError('timeout'), Reply()]) as opened, \
                patch.object(self.client, '_wait_retry'):
            self.collector.search('曲')
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(self.client.stats.retried, 1)
        self.assertIn('退避', '\n'.join(self.logs))
        with patch('urllib.request.urlopen', side_effect=TimeoutError('timeout')), \
                patch.object(self.client, '_wait_retry'):
            with self.assertRaises(HttpError):
                self.collector.search('曲')
        self.assertEqual(self.client.stats.requests, 4)

    def test_429_wait_and_retry_count(self):
        err = urllib.error.HTTPError('https://music.163.com/x', 429, 'limited',
                                     {'Retry-After': '0'}, io.BytesIO(b''))
        self.addCleanup(err.close)
        with patch('urllib.request.urlopen', side_effect=[err, Reply()]) as opened:
            self.collector.search('曲')
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(self.client.stats.rate_limit_retries, 1)
        self.assertIn('HTTP 429', '\n'.join(self.logs))

    def test_cancel_before_request_and_after_response(self):
        def cancel():
            raise OperationCancelled('停止')
        self.client.check_cancelled = cancel
        with patch('urllib.request.urlopen') as opened:
            with self.assertRaises(OperationCancelled):
                self.collector.search('曲')
            opened.assert_not_called()
        stopped = [False]
        self.client.check_cancelled = lambda: cancel() if stopped[0] else None
        with patch('urllib.request.urlopen', return_value=Reply(on_read=lambda: stopped.__setitem__(0, True))) as opened:
            with self.assertRaises(OperationCancelled):
                self.collector.search('曲')
            self.assertEqual(opened.call_count, 1)

    def test_cancel_during_backoff_does_not_count_retry(self):
        stopped = [False]
        def check():
            if stopped[0]:
                raise OperationCancelled('停止')
        self.client.check_cancelled = check
        with patch('urllib.request.urlopen', side_effect=TimeoutError('offline')) as opened, \
                patch('animusic.http.time.sleep', side_effect=lambda _: stopped.__setitem__(0, True)):
            with self.assertRaises(OperationCancelled):
                self.collector.search('曲')
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self.client.stats.retried, 0)

    def test_cancel_during_limiter_wait_sends_nothing(self):
        self.client.limiter.min_interval = 10
        self.client.limiter._last['music.163.com'] = time.monotonic()
        stopped = [False]
        def check():
            if stopped[0]:
                raise OperationCancelled('停止')
        self.client.check_cancelled = check
        with patch('urllib.request.urlopen') as opened, patch('animusic.http.time.sleep',
                side_effect=lambda _: stopped.__setitem__(0, True)):
            with self.assertRaises(OperationCancelled):
                self.collector.search('曲')
        opened.assert_not_called()
        self.assertEqual(self.client.stats.requests, 0)

    def test_cancel_during_429_wait_does_not_retry(self):
        stopped = [False]
        def check():
            if stopped[0]:
                raise OperationCancelled('停止')
        self.client.check_cancelled = check
        err = urllib.error.HTTPError('https://music.163.com/x', 429, 'limited',
                                     {'Retry-After': '1'}, io.BytesIO(b''))
        self.addCleanup(err.close)
        with patch('urllib.request.urlopen', side_effect=err) as opened, patch('animusic.http.time.sleep',
                side_effect=lambda _: stopped.__setitem__(0, True)):
            with self.assertRaises(OperationCancelled):
                self.collector.search('曲')
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self.client.stats.rate_limit_retries, 0)

    def test_cancel_after_429_keeps_cooldown(self):
        stopped = [False]
        def check():
            if stopped[0]:
                raise OperationCancelled('停止')
        self.client.check_cancelled = check
        self.client.on_rate_limit = Mock()
        err = urllib.error.HTTPError('https://music.163.com/x', 429, 'limited',
                                     {'Retry-After': '25'}, io.BytesIO(b''))
        self.addCleanup(err.close)
        def reject(*args, **kwargs):
            stopped[0] = True
            raise err
        with patch('urllib.request.urlopen', side_effect=reject):
            with self.assertRaises(OperationCancelled):
                self.collector.search('曲')
        self.client.on_rate_limit.assert_called_once()
        self.assertEqual(self.client.stats.rate_limited, 1)

    def test_wait_watcher_is_joined_on_success_and_error(self):
        # Event驱动等待，不用十秒真睡眠，也不启动网络或GUI。
        for fail in (False, True):
            notice = threading.Event()
            logs = []
            def record(message):
                logs.append(message)
                if '尚未返回' in message:
                    notice.set()
            self.client._on_event = record
            self.client.wait_notice_interval = .001
            def open_reply(*args, **kwargs):
                self.assertTrue(notice.wait(1))
                if fail:
                    raise TimeoutError('offline')
                return Reply()
            self.client.retries = 0
            with patch('urllib.request.urlopen', side_effect=open_reply):
                if fail:
                    with self.assertRaises(HttpError):
                        self.collector.search('曲')
                else:
                    self.collector.search('曲')
            self.assertFalse(any(t.name == 'search-wait-notice' for t in threading.enumerate()))
            self.assertTrue(any('尚未返回' in line for line in logs))

    def test_safe_message(self):
        for raw in ('error https://user:PASS@proxy/x?token=ABC Authorization: Bearer XYZ',
                    '\"Cookie\": \"PASS\"', '\"Authorization\": \"Bearer XYZ\"',
                    'Bearer XYZ', '\"token\": \"ABC\"'):
            with self.subTest(raw=raw):
                message = safe_diagnostic(raw)
                for secret in ('PASS', 'ABC', 'XYZ'):
                    self.assertNotIn(secret, message)

    def test_cancelled_wait_records_actual_time(self):
        with patch('animusic.http.time.monotonic', side_effect=[100., 100.25]), \
                patch.object(self.client, '_wait_retry', side_effect=OperationCancelled('停止')):
            with self.assertRaises(OperationCancelled):
                self.client._accounted_wait(10., 'backoff')
        self.assertEqual(self.client.stats.backoff, .25)

    def test_malformed_candidates_are_parse_error(self):
        with patch('urllib.request.urlopen', return_value=Reply(b'{"code":200,"result":{"songs":[42]}}')):
            with self.assertRaises(SearchError) as error:
                self.collector.search('曲')
        self.assertEqual(error.exception.category, '解析错误')


class TestMatchDiagnostics(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.season = Season(2026, 4)
        self.sid = self.store.get_season_id(2026, 4)
        aid = self.store.upsert_anime(self.sid, Anime(season_key='2026Q4', title_cn='番'))
        self.tid = self.store.upsert_theme(aid, Theme(anime_id=aid, kind=ThemeKind.OP,
                    song_title='曲', artists=['歌手'], source='manual'))
        self.logs = []
        self.search = Mock()
        self.search.search.return_value = []
        self.orch = Orchestrator(config=Config(), store=self.store, aliases=AliasTable(),
                                 search=self.search, progress=self.logs.append)

    def test_empty_result_logs_and_confirmation(self):
        self.orch.match_season(self.season)
        logs = '\n'.join(self.logs)
        for text in ('正在准备', '开始 1/1', '查询1', '搜索开始', '空结果', '结束：', '正常完成'):
            self.assertIn(text, logs)
        self.assertEqual(self.store.get_stage(self.sid, Stage.MATCH), StageStatus.CONFIRMED)

    def test_failures_do_not_confirm_or_write_decision(self):
        self.store.set_stage(self.sid, Stage.MATCH, StageStatus.CONFIRMED, confirmed=True)
        self.search.search.side_effect = SearchError('服务拒绝', 'offline', http_status=200, code=-462)
        self.orch.match_season(self.season)
        self.assertNotIn(self.tid, self.store.decision_map(self.sid))
        self.assertEqual(self.store.get_stage(self.sid, Stage.MATCH), StageStatus.DIRTY)
        self.assertIn('存在搜索失败', '\n'.join(self.logs))

    def test_partial_failure_does_not_confirm_stage(self):
        self.search.search.side_effect = [TimeoutError('offline'), [], [], []]
        self.orch.match_season(self.season)
        self.assertEqual(self.store.get_stage(self.sid, Stage.MATCH), StageStatus.DIRTY)
        self.assertIn(self.tid, self.store.decision_map(self.sid))

    def test_cancel_is_not_swallowed_and_preserves_candidate(self):
        cid = self.store.upsert_candidate(self.tid, Candidate(theme_id=self.tid,
                   netease_song_id='1', name='曲', artists=['歌手']))
        self.search.search.side_effect = OperationCancelled('停止')
        with self.assertRaises(OperationCancelled):
            self.orch.match_season(self.season)
        self.assertEqual(self.search.search.call_count, 1)
        self.assertNotIn(self.tid, self.store.decision_map(self.sid))
        self.assertEqual(self.store.get_stage(self.sid, Stage.MATCH), StageStatus.DIRTY)
        self.assertIsNotNone(self.store.conn.execute('SELECT id FROM candidate WHERE id=?', (cid,)).fetchone())

    def test_noop_preserves_manual_decision(self):
        self.store.set_decision(self.tid, DecisionStatus.SKIPPED, None, decided_by='human')
        self.orch.match_season(self.season)
        self.search.search.assert_not_called()
        self.assertIn('没有待处理', '\n'.join(self.logs))

    def test_unexpected_error_leaves_dirty(self):
        with patch.object(self.orch, '_match_one', side_effect=ValueError('offline')):
            with self.assertRaises(ValueError):
                self.orch.match_season(self.season)
        self.assertEqual(self.store.get_stage(self.sid, Stage.MATCH), StageStatus.DIRTY)
        self.assertIn('匹配异常终止', '\n'.join(self.logs))

    def test_default_search_http_is_wired_to_cancellation_and_query_context(self):
        orch = Orchestrator(config=Config(), store=self.store, aliases=AliasTable(), progress=self.logs.append)
        orch._search_context = '曲目1/查询1'
        orch.search.http._emit('HTTP开始')
        self.assertIn('[曲目1/查询1]', self.logs[-1])
        orch.should_stop = lambda: True
        with patch('urllib.request.urlopen') as opened:
            with self.assertRaises(OperationCancelled):
                orch.search.search('曲')
            opened.assert_not_called()


class TestStopMessage(unittest.TestCase):
    def test_search_stop_uses_network_message_without_constructing_gui(self):
        from animusic.gui.main_window import MainWindow
        worker = SimpleNamespace(isRunning=lambda: True, request_stop=Mock(), _search_match=True)
        stub = SimpleNamespace(worker=worker, btn_stop=Mock(), status=Mock(), log=Mock(),
                               pages=[None, SimpleNamespace(btn_stop=Mock())])
        MainWindow._stop_task(stub)
        worker.request_stop.assert_called_once()
        self.assertIn('等待当前网络调用返回', stub.log.call_args.args[0])
        stub.btn_stop.setEnabled.assert_called_once_with(False)


if __name__ == '__main__':
    unittest.main()
