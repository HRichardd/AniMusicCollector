"""环境面板：程序路径「浏览…」+ 浏览器 profile 行 —— 以及它们背后的三条约定。

需求来自用户（逐条对着实现测）：
  · 浏览器/网易云的路径**默认空**，首次启动由用户自己「浏览…」选，然后**持久化**
  · 浏览器那栏要有「需 Chromium 内核」的提示
  · profile 行要有 ⓘ 说明、**支持自行修改**；换浏览器时按家族自动切
  · profile 目录**只指向，不创建、不删除**；用户手动改过就不自动覆盖

全部离线：offscreen、不 show、不进事件循环、**不弹任何真实对话框**
（`QFileDialog` / `QMessageBox` 一律 stub —— AGENTS.md §2），
而且配置一律写在临时目录里，绝不碰用户真实的 `config/settings.yaml`。
"""

from __future__ import annotations

import gc
import os
import shutil
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import yaml
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

if HAS_QT:
    from animusic import browsers, constants  # noqa: E402
    from animusic.config import Config, ensure_default_config  # noqa: E402
    from animusic.gui import main_window as mw  # noqa: E402
    from animusic.gui.session import WizardSession  # noqa: E402
    from animusic.models import Season  # noqa: E402
    from animusic.normalize import AliasTable  # noqa: E402
    from animusic.store import Store  # noqa: E402

_TMP = os.path.join(_ROOT, "data", "_test_tmp", "env_paths")


@unittest.skipUnless(HAS_QT, "需要 PySide6")
class EnvPathsTestBase(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        # 每个测试一份独立的配置目录（绝不碰真实 config/）
        self.home = os.path.join(_TMP, f"h{id(self)}")
        shutil.rmtree(self.home, ignore_errors=True)
        os.makedirs(self.home, exist_ok=True)
        self._old_cfg_env = os.environ.get("ANIMUSIC_CONFIG_DIR")
        os.environ["ANIMUSIC_CONFIG_DIR"] = os.path.join(self.home, "config")
        ensure_default_config()                      # 落一份默认 settings.yaml
        self.cfg_path = os.path.join(self.home, "config", "settings.yaml")
        # profile 的 base 定在临时目录里 —— 免得推导写到真实的 %LOCALAPPDATA%
        self.base = os.path.join(self.home, "profiles")
        os.makedirs(self.base, exist_ok=True)
        self._panels: list = []
        self.logs: list[str] = []
        # 不在测试里起探测线程（这个项目吃过线程/GC 的亏）
        self._probe_patch = mock.patch.object(mw.EnvPanel, "probe", lambda self: None)
        self._probe_patch.start()

    def tearDown(self):
        self._probe_patch.stop()
        for p in self._panels:
            p.setParent(None)
            p.deleteLater()
        self._panels.clear()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        gc.collect()
        if self._old_cfg_env is None:
            os.environ.pop("ANIMUSIC_CONFIG_DIR", None)
        else:
            os.environ["ANIMUSIC_CONFIG_DIR"] = self._old_cfg_env
        shutil.rmtree(self.home, ignore_errors=True)

    def _pump(self, n: int = 4):
        for _ in range(n):
            self.app.processEvents()

    def panel(self) -> "mw.EnvPanel":
        sess = WizardSession(
            config=Config.load(), store=Store(":memory:"),
            season=Season.parse("2026Q4"), aliases=AliasTable(),
        )
        p = mw.EnvPanel(sess)
        self._panels.append(p)
        p.log.connect(self.logs.append)
        self._pump()
        # profile 字段指向临时 base —— 推导出来的目录就落在临时目录里
        p.browser.set_profile_text(os.path.join(self.base, "browser-profile"))
        return p

    def disk(self) -> dict:
        with open(self.cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def write_disk(self, data: dict) -> None:
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    def assertLogged(self, needle: str) -> None:
        self.assertTrue(any(needle in m for m in self.logs), f"日志里没有 {needle!r}：{self.logs}")


class TestDefaults(EnvPathsTestBase):
    def test_config_defaults_are_empty(self):
        """两个程序路径**默认为空** —— 机器特有的路径不该猜。"""
        c = Config()
        self.assertEqual(c.netease.client_path, "")
        self.assertEqual(c.browser.exe_path, "")
        self.assertFalse(c.netease.configured)
        self.assertFalse(c.browser.configured)

    def test_generated_settings_has_empty_paths(self):
        d = self.disk()
        self.assertEqual(d["netease"]["client_path"], "")
        self.assertEqual(d["browser"]["exe_path"], "")

    def test_example_template_matches_defaults(self):
        """`config/settings.example.yaml` 必须与 `Config()` 默认值**一字不差**。

        理由：模板是给人复制/对照的，一旦漂移就是"文档骗人"，而且没人会去核对。
        （这条测试第一次跑就抓到我手写的模板与默认值差在 `user_data_dir` 上：
        模板写的是可搬移的 `%LOCALAPPDATA%\\...`，而默认值是展开后的绝对路径 ——
        修的是默认值那一侧：配置文件里本来就不该出现 `C:\\Users\\<某个人>`。）
        """
        from dataclasses import fields
        path = os.path.join(_ROOT, "config", "settings.example.yaml")
        with open(path, encoding="utf-8") as f:
            example = yaml.safe_load(f)
        c = Config()
        for sec in ("netease", "browser", "bangumi", "fetch", "match"):
            want = {f.name: getattr(getattr(c, sec), f.name)
                    for f in fields(getattr(c, sec))}
            self.assertEqual(example.get(sec), want, f"模板的 {sec} 节与默认值不一致")

    def test_fields_start_empty_with_a_hint(self):
        p = self.panel()
        self.assertEqual(p.netease.path_edit.text(), "")
        self.assertEqual(p.browser.path_edit.text(), "")
        self.assertIn("浏览", p.browser.path_edit.placeholderText())

    def test_browser_hint_says_chromium(self):
        """浏览器那栏必须常驻提示「需 Chromium 内核」，且 tooltip 说明哪些不行。"""
        p = self.panel()
        self.assertEqual(p.browser.hint.text(), "(需 Chromium 内核)")
        tip = p.browser.hint.toolTip()
        self.assertIn("Chrome", tip)
        self.assertIn("Firefox", tip)
        self.assertIn("不行", tip)

    def test_both_rows_have_a_browse_button(self):
        p = self.panel()
        self.assertEqual(p.netease.btn_browse.text(), "浏览…")
        self.assertEqual(p.browser.btn_browse.text(), "浏览…")

    def test_profile_row_has_info_icon_with_explanation(self):
        p = self.panel()
        tip = p.browser.profile.info.toolTip()
        for needle in ("登录态", "bgm.tv", "不新建", "不删除"):
            self.assertIn(needle, tip)
        self.assertFalse(p.browser.profile.info.icon().isNull(), "ⓘ 图标不该是空的")

    def test_profile_row_disabled_until_browser_chosen(self):
        p = self.panel()
        self.assertFalse(p.browser.profile.isEnabled(), "没选浏览器时 profile 行该置灰")
        p.browser.path_edit.setText(r"C:\x\chrome.exe")
        p.browser.path_committed.emit("浏览器", r"C:\x\chrome.exe")
        self.assertTrue(p.browser.profile.isEnabled())


class TestPersistence(EnvPathsTestBase):
    def test_browse_fills_field_and_saves(self):
        p = self.panel()
        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(r"D:\Apps\Chrome\chrome.exe", "")):
            p.browser.btn_browse.click()
        self.assertEqual(p.browser.path_edit.text(), r"D:\Apps\Chrome\chrome.exe")
        self.assertEqual(self.disk()["browser"]["exe_path"], r"D:\Apps\Chrome\chrome.exe")
        self.assertLogged("已保存")

    def test_browse_cancel_changes_nothing(self):
        p = self.panel()
        before = self.disk()
        with mock.patch.object(QFileDialog, "getOpenFileName", return_value=("", "")):
            p.browser.btn_browse.click()
        self.assertEqual(p.browser.path_edit.text(), "")
        self.assertEqual(self.disk(), before)

    def test_typing_a_path_persists_on_editing_finished(self):
        p = self.panel()
        p.netease.path_edit.setText(r"D:\CloudMusic\cloudmusic.exe")
        p.netease.path_edit.editingFinished.emit()
        self.assertEqual(self.disk()["netease"]["client_path"],
                         r"D:\CloudMusic\cloudmusic.exe")

    def test_other_sections_survive(self):
        """写路径只能改这几个 key —— 用户开着程序时改过的其它项不能被冲掉。"""
        d = self.disk()
        d["match"]["auto_threshold"] = 0.5
        d["bangumi"]["uid"] = "1000001"
        self.write_disk(d)
        p = self.panel()
        p.netease.path_edit.setText(r"D:\CloudMusic\cloudmusic.exe")
        p.netease.path_edit.editingFinished.emit()
        after = self.disk()
        self.assertEqual(after["match"]["auto_threshold"], 0.5)
        self.assertEqual(after["bangumi"]["uid"], "1000001")

    def test_write_failure_is_reported_not_swallowed(self):
        p = self.panel()
        with mock.patch.object(Config, "save", side_effect=OSError("只读目录")):
            p.netease.path_edit.setText(r"D:\CloudMusic\cloudmusic.exe")
            p.netease.path_edit.editingFinished.emit()
        self.assertLogged("没能写回配置")
        self.assertLogged("重新选一次")
        # 字段本身仍然生效（只是没落盘）
        self.assertEqual(p.netease.path_edit.text(), r"D:\CloudMusic\cloudmusic.exe")


class TestProfileSwitching(EnvPathsTestBase):
    def _commit(self, panel, exe: str) -> None:
        panel.browser.path_edit.setText(exe)
        panel.browser.path_committed.emit("浏览器", exe)

    def test_chrome_keeps_the_original_profile(self):
        p = self.panel()
        self._commit(p, r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        self.assertEqual(p.browser.profile.path(), os.path.join(self.base, "browser-profile"))

    def test_switch_to_edge_derives_a_new_profile(self):
        p = self.panel()
        self._commit(p, r"C:\x\chrome.exe")
        self._commit(p, r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
        self.assertEqual(p.browser.profile.path(), os.path.join(self.base, "browser-profile-edge"))
        self.assertEqual(self.disk()["browser"]["user_data_dir"],
                         os.path.join(self.base, "browser-profile-edge"))
        self.assertLogged("自动切到")
        self.assertLogged("登录一次")

    def test_switch_back_to_chrome_returns_to_the_original(self):
        p = self.panel()
        self._commit(p, r"C:\x\chrome.exe")
        self._commit(p, r"C:\x\msedge.exe")
        self._commit(p, r"C:\x\chrome.exe")
        self.assertEqual(p.browser.profile.path(), os.path.join(self.base, "browser-profile"))

    def test_manual_profile_is_not_overwritten(self):
        p = self.panel()
        self._commit(p, r"C:\x\chrome.exe")
        p.browser.set_profile_text(r"E:\my-own-profile")     # 用户手动改
        self._commit(p, r"C:\x\msedge.exe")
        self.assertEqual(p.browser.profile.path(), r"E:\my-own-profile")
        self.assertLogged("没有自动改")

    def test_reset_button_rederives(self):
        """「默认」= 回到"自动生成的那个"：按当前浏览器推导，**留在当前 profile 所在目录**。

        （与换浏览器时的自动切换同一套基准 —— 否则两个入口会给出不同结果：
        用户把 profile 放在 E 盘，点「默认」却被拽回 `%LOCALAPPDATA%`。）
        """
        p = self.panel()
        self._commit(p, r"C:\x\msedge.exe")
        p.browser.set_profile_text(r"E:\my-profiles\browser-profile-edge")
        p.browser.profile.btn_default.click()
        self.assertEqual(p.browser.profile.path(),
                         r"E:\my-profiles\browser-profile-edge")

    def test_reset_after_manual_pick_stays_in_that_area(self):
        p = self.panel()
        self._commit(p, r"C:\x\msedge.exe")
        p.browser.set_profile_text(r"E:\whatever")
        p.browser.profile.btn_default.click()
        self.assertEqual(p.browser.profile.path(), r"E:\browser-profile-edge")

    def test_profile_dir_picker_persists(self):
        p = self.panel()
        self._commit(p, r"C:\x\chrome.exe")
        target = os.path.join(self.home, "picked-profile")
        with mock.patch.object(QFileDialog, "getExistingDirectory", return_value=target):
            p.browser.profile.btn_pick.click()
        self.assertEqual(p.browser.profile.path(), target)
        self.assertEqual(self.disk()["browser"]["user_data_dir"], target)

    def test_nothing_is_created_or_deleted(self):
        """**不变量**：整个过程只改字符串 —— 不创建、不删除、不移动任何目录。"""
        p = self.panel()
        before = sorted(os.listdir(self.base))
        self._commit(p, r"C:\x\chrome.exe")
        self._commit(p, r"C:\x\msedge.exe")
        self._commit(p, r"C:\x\brave.exe")
        after = sorted(os.listdir(self.base))
        self.assertEqual(before, after, "profile 推导不该动文件系统")
        self.assertFalse(os.path.exists(os.path.join(self.base, "browser-profile-edge")))

    def test_spec_uses_the_field_not_a_constant(self):
        """`--user-data-dir=` 必须取界面字段（可改），不能再写死。"""
        p = self.panel()
        self._commit(p, r"C:\x\chrome.exe")
        p.browser.set_profile_text(r"E:\custom-profile")
        _exe, args, _port = p._browser_spec()
        self.assertIn(r"--user-data-dir=E:\custom-profile", args)

    def test_spec_uses_config_start_url(self):
        """起始页面听配置的（`browser.start_url`）—— 原来界面里又写死了一次。

        这是硬编码体检的 B1：`start_url` 之前是**死配置**，改了界面不认。
        """
        p = self.panel()
        p.session.config.browser.start_url = "https://bgm.tv/anime/list/1000001/do"
        self._commit(p, r"C:\x\chrome.exe")
        _exe, args, _port = p._browser_spec()
        self.assertIn("https://bgm.tv/anime/list/1000001/do", args)
        self.assertNotIn("https://bgm.tv/", args)

    def test_ports_come_from_constants(self):
        """端口不许再写字面量：行里的初值、spec 的兜底都应是 constants 的值（B2）。"""
        p = self.panel()
        self.assertEqual(p.netease.port_edit.text(), str(constants.NETEASE_DEBUG_PORT))
        self.assertEqual(p.browser.port_edit.text(), str(constants.BROWSER_DEBUG_PORT))
        p.netease.port_edit.setText("")
        p.browser.port_edit.setText("")
        _e1, a1, port1 = p._netease_spec()
        _e2, a2, port2 = p._browser_spec()
        self.assertEqual(port1, constants.NETEASE_DEBUG_PORT)
        self.assertEqual(port2, constants.BROWSER_DEBUG_PORT)
        self.assertIn(f"--remote-debugging-port={constants.NETEASE_DEBUG_PORT}", a1)
        self.assertIn(f"--remote-debugging-port={constants.BROWSER_DEBUG_PORT}", a2)


class TestEmptyPathFeedback(EnvPathsTestBase):
    def test_launch_without_path_prompts(self):
        """还没选路径就点「启动并接管」→ 弹一次轻提示 + 写日志（不能静默）。"""
        p = self.panel()
        with mock.patch.object(QMessageBox, "information") as info:
            p.browser.launched.emit("浏览器")
        self.assertEqual(info.call_count, 1)
        self.assertIn("浏览", info.call_args[0][2] if len(info.call_args[0]) > 2 else "")
        self.assertLogged("还没设置")

    def test_netease_prompt_mentions_netease(self):
        p = self.panel()
        with mock.patch.object(QMessageBox, "information") as info:
            p.netease.launched.emit("网易云客户端")
        self.assertEqual(info.call_count, 1)
        self.assertLogged("网易云客户端")

    def test_copy_command_without_path_says_so(self):
        p = self.panel()
        p._copy_browser_cmd("浏览器")
        self.assertLogged("还没设置程序路径")


if __name__ == "__main__":
    unittest.main()
