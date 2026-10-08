"""打包相关的路径解析 —— 以及一条**离线验收命令**。

为什么单独测这个：把程序打成 exe 之后，`__file__` 指的是 PyInstaller bundle 内部
（onedir = 安装目录、onefile = 每次启动新建的临时目录）。而
`data/state.db`、`config/`、`reports/` 是**用户数据**，必须待在 bundle 外面：

    不区分的话，打包版会把歌单 id、全部匹配决策、别名表写进 bundle ——
    onefile 每次启动换一个临时目录，用户看到的就是「数据全丢了」。

这组测试钉住三件事：
  1. `constants.app_root()` 的三档优先级（`ANIMUSIC_HOME` > 冻结 → exe 同级 > 源码树根）
  2. 由它派生的目录常量确实跟着走；`config_dir()` 里更具体的
     `ANIMUSIC_CONFIG_DIR` 仍然优先于 `ANIMUSIC_HOME`
  3. `Store(":memory:")` 能建起来（离线自检用它，从而**不写任何文件**）

全部离线：不联网、不碰用户数据、不碰账号。
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

from animusic import constants  # noqa: E402
from animusic.store import Store  # noqa: E402


class TestAppRoot(unittest.TestCase):
    """`app_root()` 的三档优先级。"""

    def _clean_env(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ANIMUSIC_HOME", None)

    def test_source_tree_by_default(self):
        self._clean_env()
        self.assertFalse(getattr(sys, "frozen", False), "测试进程不该是冻结的")
        self.assertEqual(constants.app_root(), constants.ROOT)

    def test_home_env_wins_and_is_absolute(self):
        self._clean_env()
        os.environ["ANIMUSIC_HOME"] = os.path.join(_ROOT, "data", "_test_tmp", "home")
        self.assertEqual(
            constants.app_root(),
            os.path.join(_ROOT, "data", "_test_tmp", "home"),
        )

    def test_frozen_uses_exe_directory(self):
        """冻结时 = **exe 所在目录**（绿色便携：整个文件夹拷走就搬家）。

        这条是打包版不丢数据的**唯一**保障，所以单独钉住。
        """
        self._clean_env()
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable",
                               os.path.join("D:" + os.sep, "apps", "AniMusic",
                                            "AniMusicCollector.exe")):
            self.assertEqual(
                constants.app_root(),
                os.path.join("D:" + os.sep, "apps", "AniMusic"),
            )

    def test_home_env_still_wins_when_frozen(self):
        self._clean_env()
        os.environ["ANIMUSIC_HOME"] = os.path.join(_ROOT, "data", "_test_tmp", "home2")
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable",
                               os.path.join("D:" + os.sep, "apps", "x.exe")):
            self.assertEqual(
                constants.app_root(),
                os.path.join(_ROOT, "data", "_test_tmp", "home2"),
            )


class TestDerivedPaths(unittest.TestCase):
    """派生的目录常量必须真的跟着 `app_root()` 走（而不是又写死回 ROOT）。"""

    def test_constants_derive_from_app_root(self):
        home = constants._HOME
        self.assertEqual(constants.DATA_DIR, os.path.join(home, "data"))
        self.assertEqual(constants.CACHE_DIR, os.path.join(home, "data", "cache"))
        self.assertEqual(constants.REPORT_DIR, os.path.join(home, "reports"))
        self.assertEqual(constants.DB_PATH, os.path.join(home, "data", "state.db"))

    def test_module_constants_agree_with_current_process(self):
        """本次进程里 `_HOME` 就是 `app_root()` 的结果（没设 ANIMUSIC_HOME 时）。"""
        if os.environ.get("ANIMUSIC_HOME"):
            self.skipTest("本进程设了 ANIMUSIC_HOME，不能用默认值比对")
        self.assertEqual(constants._HOME, constants.app_root())

    def test_home_paths_reports_everything(self):
        p = constants.home_paths()
        self.assertEqual(
            set(p),
            {"程序根目录", "数据库", "配置目录", "报告目录", "缓存目录", "日志目录",
             "打包运行"},
        )
        self.assertEqual(p["数据库"], constants.DB_PATH)
        self.assertEqual(p["报告目录"], constants.REPORT_DIR)
        self.assertEqual(p["日志目录"], os.path.join(constants._HOME, "logs"))
        self.assertIn(p["打包运行"], ("是", "否"))

    def test_config_dir_is_more_specific_than_home(self):
        """`ANIMUSIC_CONFIG_DIR` 比 `ANIMUSIC_HOME` 更具体 → 优先。

        （测试隔离就靠前者：单测必须能避开用户的真实 `aliases.yaml`。）
        """
        with mock.patch.dict(os.environ) as env:
            env["ANIMUSIC_HOME"] = os.path.join(_ROOT, "data", "_test_tmp", "home3")
            env["ANIMUSIC_CONFIG_DIR"] = os.path.join(_ROOT, "data", "_test_tmp", "cfg3")
            self.assertEqual(
                constants.config_dir(),
                os.path.join(_ROOT, "data", "_test_tmp", "cfg3"),
            )
            env.pop("ANIMUSIC_CONFIG_DIR")
            self.assertEqual(
                constants.config_dir(),
                os.path.join(_ROOT, "data", "_test_tmp", "home3", "config"),
            )


class TestMemoryStore(unittest.TestCase):
    """`Store(":memory:")` —— 离线自检靠它做到"一个文件都不写"。

    踩过的：`Store.__init__` 里 `os.makedirs(os.path.dirname(path))` 对
    `:memory:` 会 `os.makedirs("")` → FileNotFoundError，内存库根本建不起来。
    """

    def test_memory_store_works(self):
        st = Store(":memory:")
        try:
            sid = st.get_season_id(2026, 4)
            self.assertIsInstance(sid, int)
        finally:
            st.close()

    def test_memory_store_leaves_no_file(self):
        """:memory: 建库不该在磁盘上留下任何东西。"""
        st = Store(":memory:")
        try:
            self.assertEqual(st.path, ":memory:")
        finally:
            st.close()
        self.assertFalse(os.path.exists(":memory:"))


try:
    from PySide6.QtWidgets import QApplication  # noqa: F401

    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False


@unittest.skipUnless(HAS_QT, "需要 PySide6")
class TestSelfcheckCommand(unittest.TestCase):
    """`animusic selfcheck` 自己也要被测 —— 它是打包验收的唯一依据。

    如果它自己坏了（例如抛异常、退出码不对、关键标记没打出来），
    那"构建脚本说打包成功"就一文不值。构建脚本的判据是
    「退出码 0 + 输出里有『全部通过』」，这里就按同一个判据断言。
    """

    def test_selfcheck_exits_zero_with_marker(self):
        import contextlib
        import io

        from animusic import cli

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["selfcheck"])
        text = buf.getvalue()

        self.assertEqual(code, 0, f"selfcheck 退出码 {code}：\n{text}")
        self.assertIn("全部通过", text, f"没有打出验收标记：\n{text}")
        # 同时确认它真的**用**了那两个最容易漏的依赖（而不是只 import）
        self.assertIn("pykakasi", text)
        self.assertIn("PySide6", text)


class TestTokenAndWarning(unittest.TestCase):
    """令牌事实 + 低完整性的"人话解释"。

    这一段是**一次真机排查的结论固化**：工作区带低完整性标签 ⇒ 双击启动的程序降级成
    低完整性 ⇒ 它拉起的网易云客户端写不了自己的数据目录（没托盘图标、没登录态，
    看起来像全新客户端）。所以"能不能识别出低完整性、并给出正确处置"必须有人守着。
    """

    def test_token_facts_shape(self):
        f = constants.token_facts()
        self.assertEqual(set(f), {"完整性", "受限", "AppContainer", "管理员", "错误"})
        # Windows 上应当真读到；读不到也必须给出错误说明 —— 不许静默为 None
        self.assertTrue(f["完整性"] or f["错误"], f)
        self.assertIn("完整性=", constants.token_summary(f))

    def test_warning_is_silent_for_a_normal_token(self):
        normal = {"完整性": "中", "受限": False, "AppContainer": False, "管理员": False,
                  "错误": None}
        self.assertIsNone(constants.low_integrity_warning(normal))

    def test_warning_explains_cause_and_fix_for_low_integrity(self):
        low = {"完整性": "低", "受限": False, "AppContainer": False, "管理员": False,
               "错误": None}
        w = constants.low_integrity_warning(low)
        self.assertIsNotNone(w)
        # 必须说清三件事：现象、原因（文件夹标签）、办法（搬出去）
        self.assertIn("低完整性", w)
        self.assertIn("标签", w)
        self.assertIn("拷到普通位置", w)
        self.assertIn("管理员", w)   # 并说明管理员只是绕过、不是正解
        # 这是**日志**，不是 Markdown：不许出现 ** 这种标记
        self.assertNotIn("**", w)

    def test_suggested_home_is_outside_the_app_tree(self):
        """建议的目标目录**不能**在程序目录里面 —— 标签是 (OI)(CI) 继承的，
        在工作区里新建的目录照样带标签，等于没搬（我自己第一版就写错过这个）。"""
        s = constants.suggested_home()
        self.assertFalse(
            os.path.normcase(s).startswith(os.path.normcase(constants._HOME) + os.sep),
            f"{s} 还在程序目录树里，拷过去照样带低完整性标签",
        )

    def test_warning_mentions_the_suggested_home(self):
        low = {"完整性": "低", "受限": False, "AppContainer": False, "错误": None}
        self.assertIn(constants.suggested_home(), constants.low_integrity_warning(low))

    def test_warning_covers_restricted_and_appcontainer(self):
        for f in ({"完整性": "中", "受限": True, "AppContainer": False, "错误": None},
                  {"完整性": "中", "受限": False, "AppContainer": True, "错误": None}):
            self.assertIsNotNone(constants.low_integrity_warning(f))

    def test_needed_write_dirs_cover_the_two_external_ones(self):
        """必须覆盖"外部程序要写的那两处" —— 它们是这次故障的真正现场。"""
        names = [n for n, _p, _w in constants.needed_write_dirs()]
        self.assertIn("程序数据目录", names)
        self.assertIn("浏览器 profile", names)
        self.assertIn("网易云数据目录", names)

    def test_write_probe_never_creates_dirs(self):
        """体检**不许**凭空造目录（那几处里有别的程序的数据目录）。"""
        missing = os.path.join(_ROOT, "data", "_test_tmp", "definitely-not-here-xyz")
        self.assertEqual(constants.write_probe(missing), "不存在，跳过")
        self.assertFalse(os.path.exists(missing))

    def test_write_probe_reports_writable_and_leaves_no_file(self):
        target = os.path.join(_ROOT, "data", "_test_tmp")
        os.makedirs(target, exist_ok=True)
        self.assertEqual(constants.write_probe(target), "可写")
        self.assertFalse(os.path.exists(os.path.join(target, ".animusic-write-probe")))


if __name__ == "__main__":
    unittest.main()
