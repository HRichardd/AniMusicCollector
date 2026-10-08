"""浏览器家族识别 / profile 推导 / 对话框起始目录 —— **纯逻辑，不需要 Qt**。

为什么单独测：这三条规则同时被界面（「浏览…」「改…」「默认」）和 CLI 用，
而且里面有一条**绝不能错**的约定：

    Chrome 的 profile 目录必须仍然是 `browser-profile`

因为现有用户的 bgm.tv 登录态（Cookie）就在那个目录里 —— 一旦改名，
用户升级后就被迫重新登录一次，而他们根本不会知道为什么。

全部离线：不联网、不碰真实 profile 目录、**不创建任何目录**。
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault(
    "ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, "data", "_test_tmp", "config")
)

from animusic import browsers  # noqa: E402


class TestFamily(unittest.TestCase):
    def test_known_families(self):
        for exe, want in [
            (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "chrome"),
            # ⚠️ Edge 的 exe 叫 msedge.exe，但家族键必须是 **edge**
            # （profile 目录要叫 browser-profile-edge，不是 -msedge）
            (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "edge"),
            (r"C:\x\MicrosoftEdge.exe", "edge"),
            (r"C:\x\brave.exe", "brave"),
            (r"C:\x\Vivaldi.exe", "vivaldi"),
            (r"C:\x\chromium.exe", "chromium"),
            (r"C:\x\launcher.exe", "opera"),
        ]:
            self.assertEqual(browsers.family_of(exe), want, exe)

    def test_case_and_spaces(self):
        # 大小写不敏感、两端空白无所谓（用户从属性对话框复制路径时常带空格）
        self.assertEqual(browsers.family_of(r"  C:\x\MSEDGE.EXE  "), "edge")
        self.assertEqual(browsers.family_of(r"C:\a b\Brave.EXE"), "brave")

    def test_unknown_name_is_used_as_is(self):
        """不猜"是不是 Chromium" —— 名字本身就是键。"""
        self.assertEqual(browsers.family_of(r"D:\some\MyBrowser.exe"), "mybrowser")

    def test_empty_or_dir_only(self):
        """不是 `.exe` 结尾的一律"没有家族" —— 目录名不该被拼成 profile 名。"""
        for bad in ("", "   ", r"C:\Program Files\Google\Chrome\Application",
                    r"C:\x\brave", r"C:\x\some.bat"):
            self.assertEqual(browsers.family_of(bad), "", bad)


class TestProfileName(unittest.TestCase):
    def test_chrome_keeps_the_historical_name(self):
        """**防回归**：Chrome 的 profile 目录名不许改，否则现有用户丢登录态。"""
        self.assertEqual(browsers.profile_dir_name(r"C:\x\chrome.exe"),
                         "browser-profile")
        self.assertEqual(browsers.profile_dir_name(r"C:\x\CHROME.exe"),
                         "browser-profile")

    def test_other_families_get_a_suffix(self):
        self.assertEqual(browsers.profile_dir_name(r"C:\x\msedge.exe"),
                         "browser-profile-edge")
        self.assertEqual(browsers.profile_dir_name(r"C:\x\brave.exe"),
                         "browser-profile-brave")

    def test_no_browser_no_name(self):
        self.assertEqual(browsers.profile_dir_name(""), "")


class TestDerive(unittest.TestCase):
    def test_default_root(self):
        p = browsers.derive_profile(r"C:\x\msedge.exe")
        self.assertEqual(p, os.path.join(browsers.DEFAULT_PROFILE_ROOT,
                                         "browser-profile-edge"))

    def test_base_is_respected(self):
        """用户把 profile 放到别的盘 → 换浏览器后仍在他放的那附近。"""
        p = browsers.derive_profile(r"C:\x\msedge.exe", base=r"E:\profiles")
        self.assertEqual(p, os.path.join(r"E:\profiles", "browser-profile-edge"))

    def test_chrome_default_matches_the_constant(self):
        """Chrome 推导出来的必须是**现在正在用的那个路径**（升级无感）。"""
        from animusic import constants
        self.assertEqual(browsers.derive_profile(r"C:\x\chrome.exe"),
                         constants.BROWSER_PROFILE_DIR)

    def test_empty_exe(self):
        self.assertEqual(browsers.derive_profile(""), "")

    def test_derive_does_not_create_anything(self):
        """**不变量**：推导只管拼字符串，绝不创建目录（目录由浏览器自己建）。"""
        base = os.path.join(_ROOT, "data", "_test_tmp", "derive-probe")
        p = browsers.derive_profile(r"C:\x\msedge.exe", base=base)
        self.assertFalse(os.path.exists(p), "推导不该创建目录")
        self.assertFalse(os.path.exists(base), "连父目录也不该创建")


class TestDialogStartDir(unittest.TestCase):
    def test_prefers_current_dir(self):
        d = browsers.dialog_start_dir("browser", os.path.join(_ROOT, "src", "x.exe"))
        self.assertEqual(d, os.path.join(_ROOT, "src"))

    def test_falls_back_to_something_that_exists(self):
        d = browsers.dialog_start_dir("browser", "")
        self.assertTrue(d == "" or os.path.isdir(d), d)

    def test_candidates_cover_both_kinds(self):
        self.assertIn("browser", browsers.CANDIDATES)
        self.assertIn("netease", browsers.CANDIDATES)
        self.assertTrue(any("chrome.exe" in c.lower() for c in browsers.CANDIDATES["browser"]))
        self.assertTrue(any("cloudmusic.exe" in c.lower() for c in browsers.CANDIDATES["netease"]))

    def test_dialog_dir_never_fills_the_field(self):
        """候选位置**只**用来决定对话框从哪打开，不返回"要填的路径"。

        （返回的是目录；调用方拿它当 `getOpenFileName` 的起始目录。）
        """
        d = browsers.dialog_start_dir("netease", "")
        self.assertFalse(d.lower().endswith(".exe"), d)


if __name__ == "__main__":
    unittest.main()
