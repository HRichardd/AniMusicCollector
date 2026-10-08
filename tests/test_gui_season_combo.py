"""季度下拉框 —— 三件与「时间会流逝」有关的事。

起因是用户的一个问题：「季度下拉列表会动态更新吗？现在最远到 27年10月，
用了一年之后 28年1月怎么办？」

实测回答：**会**（列表按 `date.today()` 现算，覆盖前后各一年，2028-01-15 打开
就有 2028Q1）。但顺着这个问题查出两个真边界，这个文件把三件事都钉住：

  1. 列表确实跟着「今天」走（含 `28年1月` 那一天）；
  2. 列表只在**开窗口时**建一次 —— 窗口连续开着跨过年界就会少一季；
  3. ⚠️ **`--season` 落在列表外时，界面显示的和实际用的不是一个季度**
     （实测 `--season 2020Q1` → 下拉框写「2026 秋」、工具在处理「2020 冬」）——
     这正是本项目反复在修的「界面说 A、程序用 B」。

全部离线：offscreen、不 show、不进事件循环；**不起探测线程**（子类覆盖 probe），
模态输入框一律 stub 掉（AGENTS.md §2）。
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault(
    "ANIMUSIC_CONFIG_DIR", os.path.join(_ROOT, "data", "_test_tmp", "config")
)

try:
    from PySide6.QtWidgets import QApplication, QInputDialog

    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False

if HAS_QT:
    from animusic.gui import main_window as mw  # noqa: E402
    from animusic.gui.main_window import (  # noqa: E402
        CUSTOM_SEASON,
        CUSTOM_SEASON_TEXT,
        EnvPanel,
        season_choices,
    )
    from animusic.gui.session import WizardSession  # noqa: E402
    from animusic.models import Season  # noqa: E402
    from animusic.store import Store  # noqa: E402

_TMP = os.path.join(_ROOT, "data", "_test_tmp")


# ==================================================================== 纯函数：列表

class TestSeasonChoices(unittest.TestCase):
    def test_covers_previous_current_and_next_year(self):
        cs = season_choices(date(2026, 10, 7))
        self.assertEqual(len(cs), 12)
        self.assertEqual(cs[0][1], Season(2025, 1))
        self.assertEqual(cs[-1][1], Season(2027, 4), "今天在 2026 年，末项应是 2027Q4")

    def test_user_question_2028_is_there_when_the_time_comes(self):
        """用户的原问题：用了一年后，`28年1月` 怎么办。

        答案：**到那天打开程序就有**（列表按当天现算），不需要任何配置。
        """
        cs = season_choices(date(2028, 1, 15))
        keys = [s.key for _lab, s in cs]
        self.assertIn("2028Q1", keys, "到了 2028 年，28年1月 必须在列表里")
        self.assertEqual(cs[-1][1], Season(2029, 4), "并且要能看到 2029 年")

    def test_include_brings_a_far_away_season_in(self):
        """`include` 把在用的那个季度（及其整年）并进列表。"""
        cs = season_choices(date(2026, 10, 7), include=Season(2020, 1))
        keys = [s.key for _lab, s in cs]
        self.assertIn("2020Q1", keys)
        self.assertIn("2020Q4", keys, "那一整年都该能选（补番时不用来回改命令行）")
        self.assertIn("2027Q4", keys, "原来的「前后一年」不能被挤掉")
        self.assertEqual(keys, sorted(keys), "列表必须按时间有序")

    def test_include_is_optional(self):
        self.assertEqual(season_choices(date(2026, 10, 7)),
                         season_choices(date(2026, 10, 7), include=None))


# ==================================================================== 下拉框

@unittest.skipUnless(HAS_QT, "需要 PySide6")
class SeasonComboTestBase(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        os.makedirs(_TMP, exist_ok=True)
        self.db = os.path.join(_TMP, f"seasoncombo_{id(self)}.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)
        self.panel_obj = None
        self.store = None

    def tearDown(self):
        # ⚠️ 顺序要紧：面板先析构（它连着 session/store），再关库，最后删文件。
        # 反过来的话，Windows 会 `PermissionError: 另一个程序正在使用此文件`
        # —— 而且报在 tearDown 里，看起来像是测试本身坏了。
        if self.panel_obj is not None:
            self.panel_obj.setParent(None)
            self.panel_obj.deleteLater()
            QApplication.sendPostedEvents(None, 0x34)      # DeferredDelete
            self.panel_obj = None
        if self.store is not None:
            self.store.close()
            self.store = None
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)

    def panel(self, season: Season):
        """只建 EnvPanel（不建 MainWindow），并且**不起探测线程**。"""

        class Panel(EnvPanel):
            def probe(self):        # 测试里不探端口、不起 QThread
                pass

        self.store = Store(self.db)
        self.sess = WizardSession(store=self.store, season=season)
        p = Panel(self.sess)
        self.panel_obj = p
        self.emitted: list[Season] = []
        p.season_changed.connect(self.emitted.append)
        self.pump()
        return p

    def pump(self, n: int = 4):
        for _ in range(n):
            self.app.processEvents()

    def choose(self, panel, text: str) -> int:
        idx = panel.season.findText(text)
        self.assertGreaterEqual(idx, 0, f"列表里没有 {text!r}")
        panel.season.setCurrentIndex(idx)
        self.pump()
        return idx


class _FakeDate:
    """替掉 `main_window.date`，让「今天」可控。"""

    _today = date(2026, 10, 7)

    @classmethod
    def today(cls):
        return cls._today


class TestComboAlwaysShowsTheSeasonInUse(SeasonComboTestBase):
    def test_out_of_range_season_is_shown_as_itself(self):
        """⚠️ 核心回归：`--season 2020Q1`（补老番）时，下拉框必须显示 2020Q1。

        修之前它会**显示 2026Q4**（找不到就退回当前季度），
        而 session 实际用 2020Q1 —— 界面说 A、程序用 B。实测见
        `.tmp/diag_season_mismatch.py`。
        """
        p = self.panel(Season(2020, 1))
        self.assertEqual(p.season.currentData(), Season(2020, 1))
        self.assertEqual(p.season.currentText(), "2020 冬（1月）")

    def test_in_range_season_is_shown_as_itself(self):
        p = self.panel(Season(2027, 4))
        self.assertEqual(p.season.currentData(), Season(2027, 4))

    def test_far_future_season_is_shown_as_itself(self):
        p = self.panel(Season(2031, 2))
        self.assertEqual(p.season.currentData(), Season(2031, 2))


class TestPopupRebuildsTheList(SeasonComboTestBase):
    """列表要跟着「今天」重建 —— 但测试里**不真的弹出下拉框**。

    为什么不用 `showPopup()` 测：那会真的弹一个 popup（抢键盘、在没有字体的
    环境里刷一屏 `QFont::setPointSizeF` 警告），在用户桌面上还会闪一下 ——
    AGENTS.md §2 说的就是这种「无人值守的界面动作」。
    所以：逻辑用 `rebuild()` 直接测，`showPopup()` 只验证它确实调了 `rebuild()`
    （把基类那个方法挡掉，于是什么都不会弹出来）。
    """

    def test_rebuild_after_a_year_moves_the_window_forward(self):
        """窗口一直开着跨年界 → 重建后应当覆盖到新的年份范围。"""
        _FakeDate._today = date(2026, 10, 7)
        with patch.object(mw, "date", _FakeDate):
            p = self.panel(Season(2026, 4))
            self.assertEqual(p.season.itemText(p.season.count() - 2), "2027 秋（10月）")

            _FakeDate._today = date(2028, 1, 15)
            p.season.rebuild()
            self.pump()
            texts = [p.season.itemText(i) for i in range(p.season.count())]
            self.assertIn("2028 冬（1月）", texts, "重建没跟着「今天」走")
            self.assertIn("2029 秋（10月）", texts)
            self.assertEqual(p.season.currentData(), Season(2026, 4),
                             "重建不该动当前选中项")
            self.assertEqual(self.emitted, [],
                             "重建期间必须挡信号 —— 否则每次点开下拉框都会白白 reload 一遍")

    def test_rebuild_keeps_an_out_of_range_selection(self):
        """重建（例如跨年）不能把 `--season 2020Q1` 这种选择弄丢。"""
        p = self.panel(Season(2020, 1))
        p.season.rebuild()
        self.pump()
        self.assertEqual(p.season.currentData(), Season(2020, 1))

    def test_show_popup_rebuilds_the_list_first(self):
        """`showPopup()` 必须先重建（挡掉基类实现，于是不会真的弹出来）。"""
        from PySide6.QtWidgets import QComboBox

        p = self.panel(Season(2026, 4))
        calls: list[int] = []
        orig = p.season.rebuild

        def spy(*, select=None):
            calls.append(1)
            return orig(select=select)

        p.season.rebuild = spy                       # type: ignore[method-assign]
        with patch.object(QComboBox, "showPopup") as base:
            p.season.showPopup()
        self.assertEqual(len(calls), 1, "showPopup 没有先重建列表")
        self.assertTrue(base.called, "基类的 showPopup 应当仍然被调用（我们只是挡掉了它）")

    def test_custom_entry_is_last(self):
        p = self.panel(Season(2026, 4))
        last = p.season.count() - 1
        self.assertEqual(p.season.itemText(last), CUSTOM_SEASON_TEXT)
        self.assertEqual(p.season.itemData(last), CUSTOM_SEASON)


class TestCustomSeasonEntry(SeasonComboTestBase):
    def custom_index(self, p) -> int:
        return p.season.findText(CUSTOM_SEASON_TEXT)

    def test_accepts_a_typed_season(self):
        """选「其它季度…」→ 输入 2020Q1 → 真的切过去，并往上报一次。"""
        p = self.panel(Season(2026, 4))
        with patch.object(QInputDialog, "getText", return_value=("2020Q1", True)) as ask:
            p.season.setCurrentIndex(self.custom_index(p))
            self.pump()
        self.assertTrue(ask.called, "应当问过用户要哪个季度")
        self.assertEqual(p.season.currentData(), Season(2020, 1))
        self.assertEqual(self.emitted, [Season(2020, 1)])

    def test_accepts_the_dash_form(self):
        p = self.panel(Season(2026, 4))
        with patch.object(QInputDialog, "getText", return_value=("2031-2", True)):
            p.season.setCurrentIndex(self.custom_index(p))
            self.pump()
        self.assertEqual(p.season.currentData(), Season(2031, 2))

    def test_cancel_restores_the_previous_season(self):
        """取消时必须还原 —— 界面绝不能停在「其它季度…」上。"""
        p = self.panel(Season(2026, 4))
        with patch.object(QInputDialog, "getText", return_value=("", False)):
            p.season.setCurrentIndex(self.custom_index(p))
            self.pump()
        self.assertEqual(p.season.currentData(), Season(2026, 4))
        self.assertEqual(self.emitted, [], "取消不是「换季度」")

    def test_bad_input_restores_and_reports(self):
        p = self.panel(Season(2026, 4))
        logs: list[str] = []
        p.log.connect(logs.append)
        with patch.object(QInputDialog, "getText", return_value=("乱写的", True)):
            p.season.setCurrentIndex(self.custom_index(p))
            self.pump()
        self.assertEqual(p.season.currentData(), Season(2026, 4))
        self.assertEqual(self.emitted, [])
        self.assertTrue(any("格式" in line for line in logs), logs)

    def test_custom_season_then_switching_back_to_a_normal_one(self):
        p = self.panel(Season(2026, 4))
        with patch.object(QInputDialog, "getText", return_value=("2020Q1", True)):
            p.season.setCurrentIndex(self.custom_index(p))
        self.pump()
        self.choose(p, "2027 秋（10月）")
        self.assertEqual(p.season.currentData(), Season(2027, 4))
        self.assertEqual(self.emitted, [Season(2020, 1), Season(2027, 4)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
