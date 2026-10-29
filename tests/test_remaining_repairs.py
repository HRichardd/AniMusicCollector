"""R05/R07/R08/R09：假客户端、假HTTP、无窗口对象；不启动GUI或真实账号路径。"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from dataclasses import FrozenInstanceError
from threading import Event
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, ".tmp", "remaining-test-config"))

from animusic.config import Config
from animusic.gui.main_window import MainWindow
from animusic.gui.pages import PushPage, ThemePage, QInputDialog
from animusic.gui.session import WizardSession
from animusic.gui.workers import TaskWorker
from animusic.http import HttpClient, RateLimited
from animusic.models import Anime, Candidate, HealthStatus, PlaylistRef, Season, Theme, ThemeKind
from animusic.normalize import AliasTable
from animusic.orchestrator import OperationCancelled, Orchestrator
from animusic.store import Store, SCHEMA
from animusic import spawn


class MemoryCollector:
    def __init__(self):
        self.calls = []
        self.alive = True
        self.last_write_action = "added"
        self.last_write_error = ""

    def health(self):
        self.calls.append(("health",))
        return HealthStatus(self.alive, "假客户端已断开" if not self.alive else "fake")

    def find_playlist(self, name):
        return PlaylistRef(name=name, netease_id="1")

    def ensure_playlist(self, name):
        self.calls.append(("ensure", name))
        return PlaylistRef(name=name, netease_id="1")

    def add_to_playlist(self, song, playlist):
        self.calls.append(("add", playlist.name, song))
        return True


class TestPushPlan(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.season = Season(2026, 4)
        self.sid = self.store.get_season_id(2026, 4)
        self.aid = self.store.upsert_anime(self.sid, Anime(season_key="2026Q4", title_cn="番"))
        self.orch = Orchestrator(store=self.store, config=Config(), aliases=AliasTable())
        self.tids = [self.song("1", "A"), self.song("2", "B")]

    def song(self, song, title):
        tid = self.store.upsert_theme(self.aid, Theme(anime_id=self.aid, kind=ThemeKind.OP, song_title=title))
        cid = self.store.upsert_candidate(tid, Candidate(theme_id=tid, netease_song_id=song, name=title))
        self.store.set_decision(tid, "auto", cid)
        return tid

    def choice(self, tid, song, target="", included=True):
        return {"theme_id": tid, "netease_song_id": song, "playlist": target, "included": included}

    def test_cli_and_gui_share_saved_selection(self):
        choices = [self.choice(self.tids[0], "1", "自选"), self.choice(self.tids[1], "2", included=False)]
        self.store.save_push_selection(self.sid, choices)
        cli = self.orch.build_push_plan(self.season)
        gui = self.orch.build_push_plan(self.season, selections=choices)
        self.assertEqual(cli, gui)
        self.assertEqual([(i.song_id, i.playlist) for i in cli.items], [("1", "自选")])

    def test_gui_adapter_uses_current_rows_and_same_preview_executor(self):
        choices = [self.choice(self.tids[0], "1", "自选"), self.choice(self.tids[1], "2", included=False)]
        sess = SimpleNamespace(playlist_name="26年10月", season=self.season, orchestrator=lambda: self.orch)
        stub = SimpleNamespace(session=sess, env=SimpleNamespace(playlist=SimpleNamespace(text=lambda: "默认")),
                               pages=[None, None, None, SimpleNamespace(all_rows=lambda: choices)], log=Mock())
        stub._current_push_plan = MethodType(MainWindow._current_push_plan, stub)
        self.assertEqual(stub._current_push_plan(), self.orch.build_push_plan(self.season, selections=choices))
        with patch.object(self.orch, "execute_push_plan", wraps=self.orch.execute_push_plan) as execute:
            MainWindow._push_dry_run(stub)
        self.assertTrue(execute.call_args.kwargs["dry_run"])

    def test_no_selection_record_defaults_and_explicit_empty_means_none(self):
        plan = self.orch.build_push_plan(self.season)
        self.assertEqual(len(plan.items), 2)
        self.assertEqual({i.playlist for i in plan.items}, {"26年10月"})
        self.assertEqual(self.orch.build_push_plan(self.season, selections=[]).items, ())
        with self.assertRaises(FrozenInstanceError):
            plan.items[0].song_id = "changed"

    def test_same_song_different_targets_survive_save_reload_and_dedupe(self):
        third = self.song("1", "C")
        choices = [self.choice(self.tids[0], "1", "X"), self.choice(third, "1", "Y")]
        self.store.save_push_selection(self.sid, choices)
        session = WizardSession(config=Config(), store=self.store, season=self.season, aliases=AliasTable())
        rows = [r for r in session.build()["push"] if r["netease_song_id"] == "1"]
        self.assertEqual({r["playlist"] for r in rows}, {"X", "Y"})
        plan = self.orch.build_push_plan(self.season, selections=choices + choices)
        self.assertEqual([(i.playlist, i.song_id) for i in plan.items], [("X", "1"), ("Y", "1")])
        choices[1]["playlist"] = "X"
        self.assertEqual(len(self.orch.build_push_plan(self.season, selections=choices).items), 1)

    def test_limit_after_selection_and_invalid_limits(self):
        choices = [self.choice(self.tids[0], "1", included=False), self.choice(self.tids[1], "2")]
        plan = self.orch.build_push_plan(self.season, selections=choices, limit=1)
        self.assertEqual(plan.items[0].song_id, "2")
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.orch.build_push_plan(self.season, limit=limit)

    def test_stale_plan_skips_without_any_collector_calls(self):
        plan = self.orch.build_push_plan(self.season)
        self.store.set_anime_included(self.aid, False)
        collector = MemoryCollector()
        rep = self.orch.execute_push_plan(plan, collector)
        self.assertEqual(rep.skipped, 2)
        self.assertEqual(collector.calls, [])
        self.assertTrue(all("过期" in item.note for item in rep.items))

    def test_decision_changed_while_preparing_playlist_is_revalidated(self):
        plan = self.orch.build_push_plan(self.season, limit=1)
        collector = MemoryCollector()
        def prepare(name):
            self.store.set_decision(self.tids[0], "pending_review")
            return PlaylistRef(name=name, netease_id="1")
        collector.ensure_playlist = prepare
        rep = self.orch.execute_push_plan(plan, collector)
        self.assertEqual(rep.skipped, 1)
        self.assertFalse(any(c[0] == "add" for c in collector.calls))

    def test_dry_run_does_not_construct_or_touch_collector(self):
        with patch("animusic.collector.desktop_cdp.DesktopCDPCollector", side_effect=AssertionError("真实collector")):
            rep = self.orch.execute_push_plan(self.orch.build_push_plan(self.season), dry_run=True)
        self.assertTrue(rep.dry_run)
        self.assertEqual(len(rep.items), 2)
        self.assertFalse(self.store.pushed_songs("26年10月"))

    def test_multi_target_preview_counts_actual_planned_targets(self):
        choices = [self.choice(self.tids[0], "1", "X"), self.choice(self.tids[1], "2", "Y")]
        rep = self.orch.execute_push_plan(self.orch.build_push_plan(self.season, selections=choices), dry_run=True)
        text = "\n".join(self.orch.describe_push(rep))
        self.assertIn("多个目标歌单（2个）", text)
        self.assertIn("计划 2 首", text)
        self.assertFalse(rep.playlists, "预览不应假装已读取过远端歌单元数据")

    def test_stop_before_first_song_does_not_prepare_playlist(self):
        collector = MemoryCollector()
        rep = self.orch.execute_push_plan(self.orch.build_push_plan(self.season), collector, should_stop=lambda: True)
        self.assertTrue(rep.stopped)
        self.assertFalse(rep.ok)
        self.assertEqual(len(rep.untried), 2)
        self.assertEqual(collector.calls, [])

    def test_stop_during_current_song_records_it_and_does_not_start_next_target(self):
        choices = [self.choice(self.tids[0], "1", "X"), self.choice(self.tids[1], "2", "Y")]
        plan = self.orch.build_push_plan(self.season, selections=choices)
        collector, stop = MemoryCollector(), Event()
        add = collector.add_to_playlist
        def adding(song, target):
            stop.set()
            return add(song, target)
        collector.add_to_playlist = adding
        rep = self.orch.execute_push_plan(plan, collector, should_stop=stop.is_set)
        self.assertTrue(self.store.is_pushed("X", "1"))
        self.assertEqual(rep.pushed, 1)
        self.assertEqual(len(rep.untried), 1)
        self.assertNotIn(("ensure", "Y"), collector.calls)

    def test_client_loss_marks_all_remaining_targets_untried(self):
        collector = MemoryCollector()
        def fail(song, target):
            collector.alive = False
            collector.last_write_error = "模拟掉线"
            return False
        collector.add_to_playlist = fail
        rep = self.orch.execute_push_plan(self.orch.build_push_plan(self.season), collector)
        self.assertTrue(rep.fatal)
        self.assertEqual(len(rep.failed), 1)
        self.assertEqual(len(rep.untried), 1)
        self.assertFalse(self.store.is_pushed("26年10月", "1"))

    def test_manual_theme_to_confirmed_song_to_push_plan(self):
        from animusic.gui.widgets import Column, DictTableModel
        model = DictTableModel([Column("song_title", "曲名")])
        stub = SimpleNamespace(anime_choices=[(self.aid, "番")], model=model, log=SimpleNamespace(emit=Mock()),
                               table=SimpleNamespace(scrollToBottom=Mock(), edit=Mock()), _view="全部", _apply_filter=Mock())
        ThemePage._add(stub)
        model.rows[0].update(song_title="数据源没有的歌", artists=["测试艺人"])
        session = WizardSession(config=Config(), store=self.store, season=self.season, aliases=AliasTable())
        session.save_themes(model.rows)
        tid = model.rows[0]["theme_id"]
        row = next(r for r in session.build()["match"] if r["theme_id"] == tid)
        row.update(chosen_id="99", chosen_name="人工歌", chosen_artists="测试艺人", status="confirmed", manual=True)
        session.save_decisions([row])
        plan = session.orchestrator().build_push_plan(self.season)
        self.assertTrue(any(i.theme_id == tid and i.song_id == "99" for i in plan.items))


class TestChoiceMigration(unittest.TestCase):
    def test_legacy_choices_survive_repeated_open(self):
        tmp_root = os.path.join(_ROOT, ".tmp")
        os.makedirs(tmp_root, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as folder:
            path = os.path.join(folder, "legacy.db")
            legacy = SCHEMA.replace("UNIQUE (season_id, theme_id, netease_song_id)", "UNIQUE (season_id, netease_song_id)")
            with sqlite3.connect(path) as conn:
                conn.executescript(legacy)
                conn.execute("INSERT INTO season VALUES(1,2026,4,'目标','old')")
                conn.execute("INSERT INTO push_selection(season_id,netease_song_id,target_playlist,included) VALUES(1,'1','自选',0)")
            conn.close()
            for _ in range(2):
                with Store(path) as store:
                    self.assertEqual(store.push_selection_map(1), {"1": {"included": False, "playlist": "自选"}})
                    store.save_push_selection(1, [{"netease_song_id": "1", "playlist": "自选", "included": False}])
                    self.assertEqual(len(store.push_selection_rows(1)), 1)


class TestRetryBudgets(unittest.TestCase):
    def run_requests(self, outcomes, **kwargs):
        client = HttpClient(rps=0, **kwargs)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status, response.headers = 200, {}
        response.read.return_value = b'{}'
        prepared = []
        for value in outcomes:
            if value == 429:
                error = urllib.error.HTTPError("https://example.invalid", 429, "limited", {"Retry-After": "2"}, io.BytesIO())
                self.addCleanup(error.close)
                prepared.append(error)
            elif value == "network":
                prepared.append(urllib.error.URLError("fake"))
            else:
                prepared.append(response)
        with patch("animusic.http.urllib.request.urlopen", side_effect=prepared) as call, patch("animusic.http.time.sleep") as sleep:
            result = client.get("https://example.invalid")
        return client, result, call.call_count, sleep.call_args_list

    def test_network_budget_zero_still_allows_429_retry(self):
        client, _, calls, _ = self.run_requests([429, "ok"], retries=0)
        self.assertEqual(calls, 2)
        self.assertEqual(client.stats.retried, 0)
        self.assertEqual(client.stats.rate_limit_retries, 1)

    def test_mixed_failures_use_independent_budgets(self):
        client, _, calls, _ = self.run_requests(["network", 429, "ok"], retries=1)
        self.assertEqual(calls, 3)
        self.assertEqual((client.stats.retried, client.stats.rate_limit_retries), (1, 1))

    def test_budget_insufficient_does_not_sleep_retry_or_lose_429(self):
        client = HttpClient(rps=0, retries=0, rate_limit_wait_budget=1)
        error = urllib.error.HTTPError("https://example.invalid", 429, "limited", {"Retry-After": "2"}, io.BytesIO())
        self.addCleanup(error.close)
        with patch("animusic.http.urllib.request.urlopen", side_effect=error) as call, patch("animusic.http.time.sleep") as sleep:
            with self.assertRaises(RateLimited) as caught:
                client.get("https://example.invalid")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(call.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual((client.stats.requests, client.stats.rate_limit_retries), (1, 0))

    def test_cancellation_between_wait_and_retry_does_not_count_unsent_retry(self):
        client = HttpClient(rps=0, retries=0)
        stop = Event()
        def check():
            if stop.is_set():
                raise OperationCancelled("stop")
        client.check_cancelled = check
        error = urllib.error.HTTPError("https://example.invalid", 429, "limited", {"Retry-After": "2"}, io.BytesIO())
        self.addCleanup(error.close)
        with patch("animusic.http.urllib.request.urlopen", side_effect=error) as call, patch(
                "animusic.http.time.sleep", side_effect=lambda duration: stop.set()):
            with self.assertRaises(OperationCancelled):
                client.get("https://example.invalid")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(client.stats.rate_limit_retries, 0)


class TestCooperativeWorker(unittest.TestCase):
    def test_plain_callable_executes_once_and_cleans_before_success(self):
        order = []
        worker = TaskWorker(lambda: order.append("job") or 42, cleanup=lambda: order.append("cleanup"))
        worker.succeeded.connect(lambda result: order.append(("result", result)))
        worker.run()
        self.assertEqual(order, ["job", "cleanup", ("result", 42)])

    def test_internal_typeerror_does_not_reexecute_function(self):
        calls = []
        def job(progress=None):
            calls.append("write")
            raise TypeError("progress 内部错误")
        worker = TaskWorker(job)
        errors = []
        worker.failed.connect(lambda msg, tb: errors.append(msg))
        worker.run()
        self.assertEqual(calls, ["write"])
        self.assertEqual(len(errors), 1)

    def test_stop_before_start_does_not_call_job_but_cleans(self):
        job, cleanup = Mock(), Mock()
        worker = TaskWorker(job, cleanup=cleanup)
        cancelled = []
        worker.cancelled.connect(cancelled.append)
        worker.request_stop()
        worker.run()
        job.assert_not_called()
        cleanup.assert_called_once()
        self.assertEqual(len(cancelled), 1)

    def test_cooperative_callback_is_injected(self):
        seen = []
        def job(should_stop=None, progress=None):
            seen.append(should_stop())
            self.assertTrue(callable(progress))
        worker = TaskWorker(job)
        worker.run()
        self.assertEqual(seen, [False])

    def test_finished_is_required_to_unlock_ui(self):
        worker = object()
        stub = SimpleNamespace(worker=worker, _busy=Mock(), _finish_close_when_ready=Mock())
        MainWindow._task_finished(stub, worker)
        stub._busy.assert_called_once_with(False)
        stub._finish_close_when_ready.assert_called_once()

    def test_task_result_does_not_unlock_before_finished(self):
        stub = SimpleNamespace(_busy=Mock(), log=Mock(), reload=Mock(), _update_status=Mock())
        MainWindow._task_done(stub, "fetch", None, None)
        stub._busy.assert_not_called()

    def test_business_error_still_cleans_and_cleanup_error_is_reported(self):
        cleanup, failed = Mock(), []
        def fail():
            raise RuntimeError("业务失败")
        worker = TaskWorker(fail, cleanup=cleanup)
        worker.failed.connect(lambda msg, tb: failed.append(msg))
        worker.run()
        cleanup.assert_called_once()
        self.assertIn("业务失败", failed[0])
        worker = TaskWorker(lambda: None, cleanup=Mock(side_effect=RuntimeError("清理失败")))
        worker.failed.connect(lambda msg, tb: failed.append(msg))
        worker.run()
        self.assertIn("清理失败", failed[-1])

    def test_close_waits_for_task_probe_and_launcher_without_terminate(self):
        workers = []
        for _ in range(3):
            w = SimpleNamespace(running=True, requestInterruption=Mock(), finished=SimpleNamespace(connect=Mock()),
                                _close_connected=False, terminate=Mock())
            w.isRunning = lambda current=w: current.running
            workers.append(w)
        stub = SimpleNamespace(worker=workers[0], env=SimpleNamespace(_worker=workers[1], _launch_worker=workers[2]),
                               _timer=SimpleNamespace(stop=Mock()), _busy=Mock(), status=SimpleNamespace(showMessage=Mock()),
                               session=SimpleNamespace(close=Mock()), close=Mock(), _closing=False)
        stub._active_workers = MethodType(MainWindow._active_workers, stub)
        stub._finish_close_when_ready = MethodType(MainWindow._finish_close_when_ready, stub)
        event = SimpleNamespace(ignore=Mock(), accept=Mock())
        MainWindow.closeEvent(stub, event)
        event.ignore.assert_called_once()
        stub.session.close.assert_not_called()
        for w in workers:
            w.requestInterruption.assert_called_once()
            w.terminate.assert_not_called()
            w.running = False
        stub._finish_close_when_ready()
        stub.close.assert_called_once()
        MainWindow.closeEvent(stub, event)
        event.accept.assert_called_once()
        stub.session.close.assert_called_once()

    def test_launch_wait_can_be_cancelled_without_polling_or_killing_process(self):
        proc = Mock()
        with patch("animusic.spawn._port_open", side_effect=AssertionError("不应继续探测")):
            result = spawn.wait_for_port(proc, 9999, 100, should_stop=lambda: True)
        self.assertTrue(result["cancelled"])
        proc.terminate.assert_not_called()


class TestManualFlowWithoutWindows(unittest.TestCase):
    def test_push_add_is_guidance_instead_of_empty_row(self):
        signal = Mock()
        stub = SimpleNamespace(manual_requested=SimpleNamespace(emit=signal), model=SimpleNamespace(add_row=Mock()))
        PushPage._add(stub)
        signal.assert_called_once()
        stub.model.add_row.assert_not_called()

    def test_guidance_routes_to_missing_target_or_matching(self):
        for rows, target in (([], 1), ([{"theme_id": 1}], 2)):
            stub = SimpleNamespace(pages=[None, None, SimpleNamespace(all_rows=lambda: rows)], _goto=Mock(), log=Mock())
            MainWindow._manual_song_help(stub)
            stub._goto.assert_called_once_with(target)

    def test_new_theme_resolves_same_name_anime_by_id_and_stubs_picker(self):
        model = SimpleNamespace(add_row=Mock(return_value=0), index=lambda *a: None)
        stub = SimpleNamespace(anime_choices=[(1, "同名番"), (2, "同名番")], model=model, log=SimpleNamespace(emit=Mock()),
                               table=SimpleNamespace(scrollToBottom=Mock(), edit=Mock()), _view="全部", _apply_filter=Mock())
        with patch.object(QInputDialog, "getItem", return_value=("同名番（选项2）", True)) as picker:
            ThemePage._add(stub)
        picker.assert_called_once()
        self.assertEqual(model.add_row.call_args[0][0]["anime_id"], 2)
        self.assertEqual(model.add_row.call_args[0][0]["anime"], "同名番")

    def test_push_view_cannot_add_from_generic_menu_path(self):
        from animusic.gui.widgets import DictTableView
        stub = SimpleNamespace(allow_add=False, model=Mock(side_effect=AssertionError("不能生成空行")))
        DictTableView._add_row(stub)


if __name__ == "__main__":
    unittest.main()
