"""`Store` 的跨线程行为 —— 直击真机上「拉取报错 / 推送准备歌单失败」那个 bug。

事故原样（离线可复现，见 `.tmp/repro_bugs.py`）：

    ProgrammingError: SQLite objects created in a thread can only be used in
    that same thread. The object was created in thread id 8196 and this is
    thread id 4284.

`run_gui.py` 在主线程建 `Store`，而 GUI 的拉取 / 匹配 / 推送都跑在
`gui/workers.py` 的 `TaskWorker`（QThread）里。共用一条连接 → 必炸：
    · 「从 Bangumi 拉取」直接弹错误框；
    · 推送时先是歌单建成了，然后「准备歌单失败」整批中止，一首歌都没进去。

为什么这些测试值得存在：**它们在修之前全是红的**，而且这个 bug
在纯顺序调用的单测里永远看不见（离线跑 CLI 一切正常）。

全部离线，库开在 workspace 内的临时目录（不用 tempfile —— 见附录 G.2）。
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

from animusic.models import Anime, SeasonBucket  # noqa: E402
from animusic.store import Store  # noqa: E402

_TMP = os.path.join(_ROOT, "data", "_test_tmp")


class StoreThreadBase(unittest.TestCase):
    def setUp(self):
        os.makedirs(_TMP, exist_ok=True)
        self.db = os.path.join(_TMP, f"threads_{id(self)}.db")
        self._rm()
        self.store = Store(self.db)
        self.sid = self.store.get_season_id(2026, 4)

    def tearDown(self):
        self.store.close()
        self._rm()

    def _rm(self):
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)

    # ---- 工具：在**另一个线程**里跑一段代码，把异常原样带回来
    def in_thread(self, fn):
        """模拟 `TaskWorker`：在别的线程里跑 `fn`，收工时收掉自己的连接。

        收尾那一下（`close_thread`）就是 `gui/workers.py` 里 `cleanup` 干的事 ——
        不收的话，Windows 上连接要等 GC 才释放，`tearDown` 删库文件会
        `PermissionError: [WinError 32] 另一个程序正在使用此文件`。
        """
        box: dict = {}

        def run():
            try:
                box["ok"] = fn()
            except BaseException as e:      # noqa: BLE001 - 要把异常原文带回主线程
                box["err"] = e
            finally:
                self.store.close_thread()

        t = threading.Thread(target=run, name="fake-TaskWorker")
        t.start()
        t.join(timeout=30)
        self.assertFalse(t.is_alive(), "后台线程没结束（可能死锁）")
        if "err" in box:
            raise box["err"]
        return box.get("ok")


class TestCrossThread(StoreThreadBase):
    def test_read_from_worker_thread(self):
        """后台线程读 —— 修复前这里抛 ProgrammingError。"""
        aid = self.store.upsert_anime(self.sid, Anime(
            season_key="2026Q4", title_cn="番A", bgm_id=1,
        ))
        rows = self.in_thread(lambda: self.store.anime_list(self.sid))
        self.assertEqual([r["id"] for r in rows], [aid])

    def test_write_from_worker_thread_is_visible_in_main(self):
        """后台线程写、主线程读 —— 拉取就是这条路径（fetch_season 在工作线程）。"""
        def job():
            aid = self.store.upsert_anime(self.sid, Anime(
                season_key="2026Q4", title_cn="后台写的番", bgm_id=2,
            ))
            self.store.set_stage(self.sid, "anime", "fetched", fetched=True)
            return aid

        aid = self.in_thread(job)
        rows = self.store.anime_list(self.sid)
        self.assertEqual([r["id"] for r in rows], [aid])
        self.assertEqual(rows[0]["title_cn"], "后台写的番")
        self.assertEqual(str(self.store.get_stage(self.sid, "anime")), "fetched")

    def test_each_thread_gets_its_own_connection(self):
        main_conn = self.store.conn
        self.assertIs(self.store.conn, main_conn, "同一线程必须复用同一条连接")
        other = self.in_thread(lambda: self.store.conn)
        self.assertIsNot(other, main_conn, "不同线程必须各自一条连接")

    def test_concurrent_writers_do_not_lock_each_other_out(self):
        """两个线程同时写 —— 不能出现 `database is locked`。

        加了 `busy_timeout` 就是为了这个：WAL 下写是串行的，
        没有超时的话第二个写者会立刻失败（而 GUI 里那会变成一条突兀的报错）。
        """
        n = 12
        errors: list[BaseException] = []

        def writer(tag: str):
            def job():
                try:
                    for i in range(n):
                        self.store.upsert_anime(self.sid, Anime(
                            season_key="2026Q4", title_cn=f"{tag}-{i}",
                            animethemes_slug=f"{tag}-{i}",
                        ))
                except BaseException as e:      # noqa: BLE001
                    errors.append(e)
                finally:
                    self.store.close_thread()
            return job

        threads = [threading.Thread(target=writer(tag)) for tag in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
            self.assertFalse(t.is_alive(), "写线程没结束（可能死锁）")
        self.assertEqual(errors, [], f"并发写报错：{errors}")
        names = {r["title_cn"] for r in self.store.anime_list(self.sid)}
        self.assertEqual(len(names), n * 2, f"应有 {n * 2} 条，实际 {len(names)}")

    def test_foreign_keys_are_enabled_in_new_threads(self):
        """外键开关是**每连接**的 —— 新线程的连接必须自己再打开一次。

        踩点：`SCHEMA` 里那句 `PRAGMA foreign_keys = ON` 只对跑
        `executescript` 的那条连接生效。要是新连接漏了这句，
        约束会在工作线程里**静默失效**（不报错、脏数据照样写进去）。
        """
        def bad_insert():
            self.store.conn.execute(
                "INSERT INTO anime (season_id, title_cn) VALUES (?,?)",
                (999999, "不存在的季度"),
            )

        with self.assertRaises(Exception) as ctx:
            self.in_thread(bad_insert)
        self.assertIn("FOREIGN KEY", str(ctx.exception).upper())

    def test_session_build_works_from_worker_thread(self):
        """GUI 里 `reload()` 走的就是 `session.build()` —— 也要能跨线程。"""
        try:
            from animusic.gui.session import WizardSession
        except ImportError:  # pragma: no cover
            self.skipTest("需要 PySide6 才能 import gui.session")
        from animusic.models import Season

        sess = WizardSession(store=self.store, season=Season(2026, 4))
        data = self.in_thread(sess.build)
        self.assertEqual(set(data), {"anime", "theme", "match", "push", "missing_source"})


class TestStoreLifecycle(StoreThreadBase):
    def test_conn_is_a_property_not_an_instance_attribute(self):
        """回归哨兵：`conn` 必须一直是 property。

        一旦有人改回 `self.conn = sqlite3.connect(...)`，
        跨线程 bug 就会**静默复活**（离线单测依然全绿，只有真机点界面才会炸），
        所以这里直接钉住实现方式。
        """
        self.assertIsInstance(type(self.store).conn, property)
        self.assertNotIn("conn", self.store.__dict__,
                         "连接不能存在实例属性上（那等于把所有线程绑死到一条连接）")

    def test_close_thread_unregisters_its_connection(self):
        """收工后连接要从登记表里摘掉 —— 否则 `close()` 会去关别的线程的连接。"""
        self.in_thread(lambda: self.store.conn)
        # 主线程那条还在，工作线程那条已经收掉了
        self.assertEqual(len(self.store._conns), 1)
        self.store.close()          # 不能再抛（关一条已关的连接）
        self.store.close()

    def test_use_after_close_raises_a_clear_error(self):
        """关闭后要用，必须给一句人看得懂的话。

        原来会抛 `sqlite3.ProgrammingError: Cannot operate on a closed database`
        —— 用户看到的是一个和「我做错了什么」毫无关系的底层报错。
        """
        self.store.close()
        with self.assertRaises(RuntimeError) as ctx:
            _ = self.store.conn
        self.assertIn("已关闭", str(ctx.exception))
        # tearDown 会再 close 一次，必须幂等（不能因为关了两次就抛）
        self.store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
