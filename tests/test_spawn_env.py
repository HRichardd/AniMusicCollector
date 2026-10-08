"""启动第三方程序的那条路径 —— 由真机反馈逼出来的三条约束。

用户真机反馈（打包之后）：**同一个命令行，终端里手工启动正常，
程序用 subprocess 拉起来就坏**。用假"浏览器"探针（`.tmp/probe_child.py`）
把子进程拿到的上下文 dump 出来逐项对比，差别只有三处，全在 `spawn` 模块收口：

  1. `PATH` 被 PyInstaller 插进了 bundle 的两段（`_internal\\PySide6`、`_internal`），
     另有 `QT_PLUGIN_PATH` / `QML2_IMPORT_PATH` / `PYSIDE6_OPTION_PYTHON_ENUM`
  2. 三个标准句柄是 **NULL**
  3. `DETACHED_PROCESS` 让子进程连控制台都没有

另一条是**误报**：Chrome 的 `chrome.exe` 只是交接用的桩，把命令行交给真正的
浏览器进程后就退出（退出码 0x80000003 = 2147483651）。原来的实现一见退出码
就判失败，于是"浏览器明明起来了、却弹一个报错框"。

全部离线：不联网、不启动任何真实浏览器/客户端、不碰账号。
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault(
    "ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, "data", "_test_tmp", "config")
)

from animusic import spawn  # noqa: E402

#: 打包后 PyInstaller 会插进 PATH 的两段（实测值，见 HANDOFF）
_BUNDLE = r"D:\apps\AniMusic\AniMusicCollector"
_MEIPASS = os.path.join(_BUNDLE, "_internal")


class TestCleanEnv(unittest.TestCase):
    """`clean_env()`：给第三方程序的环境，要和"手工在终端启动"一致。"""

    def _base(self):
        return {
            "PATH": os.pathsep.join([
                os.path.join(_MEIPASS, "PySide6"),          # ← 打包版插的
                _MEIPASS,                                    # ← 打包版插的
                r"C:\WINDOWS\system32",
                r"D:\tools\bin",
            ]),
            "QT_PLUGIN_PATH": os.path.join(_MEIPASS, "PySide6", "plugins"),
            "QML2_IMPORT_PATH": os.path.join(_MEIPASS, "PySide6", "qml"),
            "PYSIDE6_OPTION_PYTHON_ENUM": "1",
            "QT_QPA_PLATFORM": "offscreen",
            "_PYI_ARCHIVE_FILE": os.path.join(_BUNDLE, "AniMusicCollector.exe"),
            "_PYI_PARENT_PROCESS_LEVEL": "1",
            "PYTHONPATH": os.path.join(_BUNDLE, "src"),
            "SYSTEMROOT": r"C:\WINDOWS",
            "USERPROFILE": r"C:\Users\someone",
        }

    def _clean(self):
        with mock.patch.object(sys, "_MEIPASS", _MEIPASS, create=True), \
             mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable",
                               os.path.join(_BUNDLE, "AniMusicCollector.exe")):
            return spawn.clean_env(self._base())

    def test_bundle_path_entries_are_stripped(self):
        """bundle 的两段必须从 PATH 里消失 —— 否则子进程会从我们这儿找 DLL。"""
        env = self._clean()
        parts = env["PATH"].split(os.pathsep)
        self.assertNotIn(os.path.join(_MEIPASS, "PySide6"), parts)
        self.assertNotIn(_MEIPASS, parts)

    def test_user_path_entries_survive(self):
        """只摘我们自己的，用户原本的 PATH 一项都不许动。"""
        env = self._clean()
        self.assertIn(r"C:\WINDOWS\system32", env["PATH"])
        self.assertIn(r"D:\tools\bin", env["PATH"])

    def test_qt_and_python_vars_are_stripped(self):
        env = self._clean()
        for k in ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "PYSIDE6_OPTION_PYTHON_ENUM",
                  "QT_QPA_PLATFORM", "PYTHONPATH",
                  "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL"):
            self.assertNotIn(k, env, f"{k} 不该传给第三方程序")

    def test_unrelated_vars_survive(self):
        env = self._clean()
        self.assertEqual(env["SYSTEMROOT"], r"C:\WINDOWS")
        self.assertEqual(env["USERPROFILE"], r"C:\Users\someone")

    def test_source_run_changes_nothing(self):
        """源码运行（没有 _MEIPASS、不是 frozen）时**一个字都不改**。

        否则开发时的行为和打包后不一致，等于换了个环境测 —— 这正是这次
        真机问题的成因（打包版和源码版给子进程的环境不同）。
        """
        base = self._base()
        with mock.patch.object(sys, "frozen", False, create=True):
            env = spawn.clean_env(base)
        # PATH 里那两段虽然"看起来像 bundle"，但源码运行时我们并不知道
        # 哪个目录是 bundle ⇒ 不动 PATH；但 Qt/Python 那几个变量照删
        self.assertEqual(env["PATH"], base["PATH"])
        self.assertNotIn("QT_PLUGIN_PATH", env)

    def test_empty_path_entry_is_preserved(self):
        """PATH 里的空项语义是「当前目录」，不替用户改语义。"""
        with mock.patch.object(sys, "_MEIPASS", _MEIPASS, create=True):
            env = spawn.clean_env({"PATH": f"{_MEIPASS}{os.pathsep}{os.pathsep}C:\\x"})
        parts = env["PATH"].split(os.pathsep)
        self.assertNotIn(_MEIPASS, parts)
        self.assertIn("", parts, "空项（当前目录）必须留着")
        self.assertIn("C:\\x", parts)


class TestExternalSpawn(unittest.TestCase):
    """`spawn.external()` 到底把什么传给了 Popen —— 不看代码看调用。"""

    def _spawn(self, **kw):
        calls = {}

        class FakeProc:
            def poll(self):
                return None

        def fake_popen(cmd, **kwargs):
            calls["cmd"] = cmd
            calls.update(kwargs)
            return FakeProc()

        with mock.patch.object(spawn.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(spawn, "log_path",
                               return_value=os.path.join(_ROOT, "data", "_test_tmp",
                                                         "launch-test.log")):
            proc = spawn.external(r"C:\chrome.exe", ["--remote-debugging-port=9223"],
                                  label="浏览器", **kw)
        return proc, calls

    def test_env_is_cleaned(self):
        _, calls = self._spawn()
        self.assertIn("env", calls, "必须显式传 env，否则会继承 bundle 的 PATH")
        self.assertNotIn("QT_PLUGIN_PATH", calls["env"])

    def test_std_handles_are_valid_not_null(self):
        """标准句柄要指向**有效**的东西（日志文件），不能是 NULL。

        NULL 句柄是打包版与"终端手工启动"的实测差异之一。
        """
        _, calls = self._spawn()
        for k in ("stdin", "stdout", "stderr"):
            self.assertIn(k, calls, f"{k} 必须显式指定")
        self.assertIsNot(calls["stdout"], None)
        self.assertIsNot(calls["stderr"], None)

    def test_does_not_detach(self):
        """不再用 DETACHED_PROCESS —— 它让子进程连控制台都没有，
        而"子进程活过父进程"并不需要它（Windows 本来就不杀）。"""
        _, calls = self._spawn()
        self.assertEqual(calls.get("creationflags", 0), 0)

    def test_cwd_defaults_to_exe_directory(self):
        _, calls = self._spawn()
        self.assertEqual(calls["cwd"], "C:" + os.sep)

    def test_log_path_is_under_app_root(self):
        p = spawn.log_path("浏览器")
        self.assertTrue(p.startswith(_ROOT))
        self.assertIn("logs", p)
        self.assertTrue(p.endswith(".log"))


class TestWaitForPort(unittest.TestCase):
    """**误报的回归**：子进程退出 ≠ 启动失败，唯一判据是端口。"""

    def _proc(self, rc_after: int | None):
        class FakeProc:
            def __init__(self):
                self.calls = 0

            def poll(self):
                self.calls += 1
                # 第一次问就说"已退出"（模拟 Chrome 的桩）
                return rc_after if self.calls >= 1 else None

        return FakeProc()

    def test_exited_stub_but_port_opens_is_success(self):
        """桩进程退出（0x80000003）之后端口才开 —— 必须判**成功**。

        这正是用户真机报的那条：Chrome 起来了、Bangumi 打开了，
        我们的弹窗却说"进程启动后立即退出"。
        """
        proc = self._proc(0x80000003 - (1 << 32))  # → -2147483645，与真机同值
        waited: list[str] = []
        with mock.patch.object(spawn, "_port_open", side_effect=[False, True]):
            res = spawn.wait_for_port(proc, 9223, timeout=5, on_waited=waited.append)
        self.assertTrue(res["ok"], res)
        self.assertIn("就绪", res["msg"])
        self.assertIn("正常交接", res["msg"], "要说明白桩进程退出是正常的")
        self.assertTrue(any("继续等" in m for m in waited))

    def test_port_never_opens_is_failure_with_exit_code(self):
        proc = self._proc(3)
        with mock.patch.object(spawn, "_port_open", return_value=False), \
             mock.patch.object(spawn.time, "sleep", return_value=None):
            res = spawn.wait_for_port(proc, 9223, timeout=0.01)
        self.assertFalse(res["ok"])
        self.assertEqual(res["exit_code"], 3)
        self.assertIn("未开放调试端口", res["msg"])

    def test_alive_process_timeout_has_no_exit_code(self):
        class Alive:
            def poll(self):
                return None

        with mock.patch.object(spawn, "_port_open", return_value=False), \
             mock.patch.object(spawn.time, "sleep", return_value=None):
            res = spawn.wait_for_port(Alive(), 9223, timeout=0.01)
        self.assertFalse(res["ok"])
        self.assertIsNone(res["exit_code"])
        self.assertIn("进程仍存活", res["msg"])


if __name__ == "__main__":
    unittest.main()
