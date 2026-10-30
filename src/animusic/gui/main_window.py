"""主窗口：环境面板 + 步骤指示器 + 向导页面 + 日志。"""

from __future__ import annotations

import json
import os
import socket
import urllib.request
from datetime import date

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import browsers, constants, spawn
from ..config import Config
from ..models import Season
from ..orchestrator import PushReport
from .session import WizardSession
from .workers import TaskWorker
from . import style
from .pages import AnimePage, MatchPage, PushPage, ThemePage
from .widgets import LogPane, StatusLed, Stepper

#: ⚠️ 这里**刻意没有任何兜底路径**。
#: 原来 `NETEASE_EXE` / `CHROME_EXE` 写死了本机的 `D:\CloudMusic\cloudmusic.exe`
#: 与 Chrome 的安装路径 —— 换台机器就是一个"看似有值实则错误"的路径，
#: 比空着更坏。现在路径默认空，由用户在界面上「浏览…」选一次并写回 settings.yaml。
#: profile 目录的推导规则在 `animusic/browsers.py`（Chrome 保持历史目录名，
#: 这样现有用户的 bgm.tv 登录态不会因为升级而丢）。
PROBE_CONNECT_TIMEOUT = 0.35   # 端口连通性
PROBE_HTTP_TIMEOUT = 1.0       # 端口开着时才发 HTTP
PROBE_INTERVAL_MS = 15000      # 状态变化不频繁，15 秒足够


def _port_open(port: int, timeout: float = PROBE_CONNECT_TIMEOUT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_json(port: int, path: str, timeout: float = PROBE_HTTP_TIMEOUT):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


class ProbeWorker(QThread):
    """在后台线程探测两个 CDP 目标。

    为什么必须后台：实测单个端口的探测（连接挂到超时）就要 1.2 秒，
    两个端口 2.4 秒。放在 UI 线程上会让界面每隔几秒冻结一次 —— 表现为「点起来卡」。
    """

    done = Signal(object)

    def __init__(self, netease_port: int, browser_port: int, parent=None):
        super().__init__(parent)
        self.netease_port = netease_port
        self.browser_port = browser_port

    def run(self) -> None:  # noqa: D102
        out: dict[str, tuple] = {}
        if self.isInterruptionRequested():
            return

        if _port_open(self.netease_port):
            ver = _http_json(self.netease_port, "/json/version")
            out["netease"] = ((ver or {}).get("Browser"), None)
        else:
            out["netease"] = (None, None)

        if self.isInterruptionRequested():
            return
        if _port_open(self.browser_port):
            ver = _http_json(self.browser_port, "/json/version")
            tabs = _http_json(self.browser_port, "/json/list") or []
            logged = bool(tabs) and any("bgm.tv" in (t.get("url") or "") for t in tabs)
            out["browser"] = ((ver or {}).get("Browser"), logged)
        else:
            out["browser"] = (None, None)

        self.done.emit(out)


class LaunchWorker(QThread):
    """启动外部程序并**轮询调试端口**确认是否真的接管成功。

    为什么必须轮询：只调 `Popen` 是不够的 —— 光看"进程还在不在"会得出错的结论，
    唯一可信的证据是**调试端口真的开了**。这里踩过两次、方向相反的坑：

      · 一开始只看 `Popen` 成不成功 ⇒ 失败时用户只看到状态灯不亮，没有任何提示；
      · 后来改成"退出码非 0 就算失败" ⇒ **误报**：Chrome 的 `chrome.exe` 只是
        交接用的桩，把命令行交给真正的浏览器进程后就退出（退出码 0x80000003），
        而浏览器其实已经起来了、端口一两秒后就开 —— 用户看到的是一个假报错弹窗。

    现在的判据只有一条：**端口开没开**。子进程退出只记下来、继续等（见
    `spawn.wait_for_port`）。
    """

    progress = Signal(str)
    done = Signal(object)

    def __init__(self, label: str, exe: str, args: list[str], port: int,
                 timeout: float = 25.0, parent=None):
        super().__init__(parent)
        self.label = label
        self.exe = exe
        self.args = args
        self.port = port
        self.timeout = timeout

    def run(self) -> None:  # noqa: D102
        if self.isInterruptionRequested():
            return
        if _port_open(self.port):
            self.done.emit({"ok": True, "msg": f"端口 {self.port} 已在监听，直接复用"})
            return

        self.progress.emit(f"启动 {self.label} …")
        try:
            proc = spawn.external(self.exe, self.args, label=self.label)
        except Exception as e:
            self.done.emit({"ok": False, "stage": "spawn",
                            "msg": f"无法启动进程：{type(e).__name__}: {e}"})
            return

        # ⚠️ **子进程退出 ≠ 启动失败**（实测误报，用户真机报的）。
        # Chrome 的 `chrome.exe` 只是交接用的桩：它把命令行交给真正的浏览器进程
        # 之后就退出，退出码 0x80000003（2147483651）。原实现一见退出码就报
        # 「进程启动后立即退出」，而浏览器其实起来了、端口一两秒后就开 ——
        # 用户看到的是一个假报错弹窗。现在退出只记下来，继续等端口。
        res = spawn.wait_for_port(
            proc, self.port, self.timeout, on_waited=self.progress.emit,
            should_stop=self.isInterruptionRequested
        )
        if res["ok"]:
            self.done.emit({"ok": True, "msg": res["msg"]})
            return
        self.done.emit({
            "ok": False,
            "stage": "cancelled" if res.get("cancelled") else "timeout" if res["exit_code"] is None else "exited",
            "exit_code": res["exit_code"],
            "alive": res["exit_code"] is None,
            "port": self.port,
            "msg": res["msg"],
        })


def _command_text(exe: str, args: list[str]) -> str:
    quoted = " ".join(f'"{a}"' if " " in a else a for a in args)
    return f'& "{exe}" {quoted}'


def _netease_exe(cfg: Config | None) -> str:
    """客户端路径 —— 直接听配置的，**没有兜底**（空 = 还没配，见上面那段注释）。

    `strip()` 是必需的：yaml 里写 `client_path: ""` 会得到一个空串，
    空值必须当作「没配」处理（界面会提示"点浏览…"，而不是弹"找不到文件："）。
    """
    return (getattr(getattr(cfg, "netease", None), "client_path", "") or "").strip()


def _browser_exe(cfg: Config | None) -> str:
    return (getattr(getattr(cfg, "browser", None), "exe_path", "") or "").strip()


def _profile_from_config(cfg: Config | None) -> str:
    """配置里的 profile 目录；没配就按当前浏览器推导一个（不落盘）。"""
    p = (getattr(getattr(cfg, "browser", None), "user_data_dir", "") or "").strip()
    if p:
        return os.path.expandvars(p)
    return browsers.derive_profile(_browser_exe(cfg))


def wikipedia_attribution() -> str:
    """维基百科的 CC BY-SA 署名文本 —— **唯一来源**在 `sources/wikipedia.py`。

    署名是许可证义务（见 AGENTS.md §7），所以它必须出现在界面上，
    而不只是躺在导出报告里。

    ⚠️ 这里**刻意不写兜底字符串**：写一份就是两份真相，改一处忘一处不会报错，
    只会让界面显示的署名和导出报告里的悄悄不一致。
    `sources/wikipedia.py` 只依赖标准库与内部模块（无第三方依赖），
    顶层导入本来就安全 —— 真要为了「只读环境」兜底，那也应该是导入失败时
    **显式报错**，而不是显示一条可能已经过期的署名。
    """
    from ..sources.wikipedia import ATTRIBUTION

    return ATTRIBUTION


def _combo_index_of(combo, target) -> int:
    """在下拉框里按**等于** target 的 itemData 找下标（找不到返回 -1）。

    ⚠️ 为什么不用 `QComboBox.findData()`：实测（PySide6 6.x）对自定义对象返回 -1，
    即使 `itemData(i) == target` 逐项比较是 True —— 它内部并不是用 Python 的 `==`。
    踩过的后果很隐蔽：`findData` 恒 -1 → 判定为「季度不在列表里」→ 下拉框
    停在未选中（currentIndex == -1），用户随便一碰就把季度换成了别的。
    `Season` 是 frozen dataclass，`==` 语义正确，所以这里自己扫一遍。
    """
    for i in range(combo.count()):
        if combo.itemData(i) == target:
            return i
    return -1


def season_choices(today: date | None = None,
                   include: Season | None = None) -> list[tuple[str, Season]]:
    """下拉框候选项：**当前年份前后各一年**的四季，按时间顺序。

    为什么不再写死 2026：原来那个列表是四个字符串字面量，跨到 2027 年
    就会「只能选 2026」—— 而且界面看起来一切正常，属于静默失效。
    前后各一年是为了照顾补去年番、或提前准备下一季的人。

    `include`：**必须出现在列表里**的那个季度（它所在的整年都会并进来）。
    为什么需要它（实测踩过）：列表只覆盖前后一年，而 `--season 2020Q1`
    这种「补老番」的用法会落在列表之外。原来的处理是「找不到就显示当前季度」，
    于是**界面写着 2026 秋、工具实际在处理 2020 冬** —— 界面说 A、程序用 B，
    正是这个项目反复在修的那类毛病（证据：`.tmp/diag_season_mismatch.py`）。
    现在把在用的年份并进年份集合，界面永远说真话。
    顺带好处：补 2020 年时，2020~2027 每一季都在列表里，不用来回改命令行。
    """
    d = today or date.today()
    years = {d.year - 1, d.year, d.year + 1}
    if include is not None:
        years.add(include.year)
    return [
        (f"{y} {constants.SEASON_CN[q]}（{constants.QUARTER_MONTH[q]}月）", Season(y, q))
        for y in sorted(years)
        for q in (1, 2, 3, 4)
    ]


#: 下拉框最后那一项：「其它季度…」—— 让用户输入列表之外的季度。
#: 存在理由：列表是「前后各一年」的**封闭集合**，而界面上没有别的入口
#: 能选到其它季度（补很老的番、或提前准备很远的季度就得出命令行）。
CUSTOM_SEASON_TEXT = "其它季度…"
#: 那一项的 itemData 哨兵（不是 Season，所以能一眼区分开）
CUSTOM_SEASON = "__custom_season__"


class SeasonCombo(QComboBox):
    """季度下拉框 —— 三件与「时间会流逝」有关的事。

    1. **永远包含正在用的那个季度**（见 `season_choices` 的 `include`）；
    2. **每次弹出前重建列表**：列表按「今天」算，只在开窗口时建一次的话，
       窗口连续开着跨过年界就会少一季（实测要连续开满一年才触发，是真边界）；
    3. 最后多一项「其它季度…」，用来输入列表之外的任何季度。
    """

    def __init__(self, required: Season | None = None, parent=None):
        super().__init__(parent)
        self._required = required
        self.rebuild()

    def rebuild(self, *, select: Season | None = None) -> None:
        """按「今天」重建列表，尽量保住当前选中项。

        ⚠️ 重建期间必须 `blockSignals`：`clear()`/`addItem()` 会让
        `currentIndexChanged` 乱发（先是 -1，再是新下标），而它已经接到
        「换季度 → 整个界面 reload」上 —— 不挡信号的话，用户每点开一次下拉框
        都会白白重载一遍数据。
        """
        want = select if select is not None else self.currentData()
        if isinstance(want, Season):
            self._required = want
        items = season_choices(include=self._required)
        labels = [lab for lab, _s in items] + [CUSTOM_SEASON_TEXT]
        if labels == [self.itemText(i) for i in range(self.count())]:
            return                      # 内容没变就别动（免得每次点开都重置选中）
        keep = want if isinstance(want, Season) else self._required
        self.blockSignals(True)
        self.clear()
        for lab, s in items:
            self.addItem(lab, s)
        self.addItem(CUSTOM_SEASON_TEXT, CUSTOM_SEASON)
        idx = _combo_index_of(self, keep)
        self.setCurrentIndex(idx if idx >= 0 else 0)
        self.blockSignals(False)

    def showPopup(self) -> None:  # noqa: N802 - Qt 命名
        self.rebuild()
        super().showPopup()


class CommandDialog(QDialog):
    """失败时展示可复制的命令行。"""

    def __init__(self, title: str, detail: str, command: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 400)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        head = QLabel(detail)
        head.setWordWrap(True)
        head.setTextFormat(Qt.RichText)
        lay.addWidget(head)

        hint = QLabel("也可以在你自己的终端里直接运行下面这条命令：")
        hint.setObjectName("Muted")
        lay.addWidget(hint)

        self.editor = QPlainTextEdit(command)
        self.editor.setReadOnly(True)
        lay.addWidget(self.editor, 1)

        row = QHBoxLayout()
        row.addStretch()
        b_copy = QPushButton("复制命令")
        b_copy.setObjectName("Primary")
        b_copy.clicked.connect(lambda: self._copy(command))
        row.addWidget(b_copy)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        row.addWidget(b_close)
        lay.addLayout(row)

    def _copy(self, command: str) -> None:
        QApplication.clipboard().setText(command)
        self.editor.setPlainText(command + "\n\n（已复制到剪贴板）")


class TargetRow(QWidget):
    """一个被接管的进程：路径 / 端口 / 启动按钮 / 状态灯（浏览器那行外加 profile 行）。"""

    launched = Signal(str)
    copy_requested = Signal(str)
    #: 点了「浏览…」—— 由 EnvPanel 弹文件对话框（TargetRow 自己不管配置与持久化）
    browse_requested = Signal(str)
    #: 路径被确定下来（回车/失焦/选完文件）—— 用于写回配置 + 推导 profile
    path_committed = Signal(str, str)          # (label, 新路径)

    def __init__(self, label: str, path: str, port: int, button_text: str,
                 hint: str = "", hint_tip: str = "", with_profile: bool = False,
                 parent=None):
        super().__init__(parent)
        self.label = label
        self.port = port

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)

        name = QLabel(label)
        name.setFixedWidth(84)
        name.setStyleSheet(f"color: {style.TEXT_MUTED};")
        lay.addWidget(name)

        self.path_edit = QLineEdit(path)
        self.path_edit.setMinimumWidth(300)
        self.path_edit.setPlaceholderText("（未设置，点右边「浏览…」选择）")
        self.path_edit.setToolTip("要接管的程序 exe 的完整路径")
        # `editingFinished` 在回车与失焦时都会发 —— 手敲路径也能落库
        self.path_edit.editingFinished.connect(
            lambda: self.path_committed.emit(self.label, self.path_edit.text().strip())
        )
        lay.addWidget(self.path_edit, 1)

        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.setObjectName("Ghost")
        self.btn_browse.setToolTip("选择一个可执行文件")
        self.btn_browse.clicked.connect(lambda: self.browse_requested.emit(self.label))
        lay.addWidget(self.btn_browse)

        if hint:
            # ⚠️ 用**独立标签**而不是 placeholder：placeholder 只在输入框为空时可见，
            # 而这里永远有值（或显示"未设置"），等于没提示。
            self.hint = QLabel(hint)
            self.hint.setObjectName("Dim")
            if hint_tip:
                self.hint.setToolTip(hint_tip)
            lay.addWidget(self.hint)

        lay.addWidget(QLabel("端口"))
        self.port_edit = QLineEdit(str(port))
        self.port_edit.setFixedWidth(58)
        lay.addWidget(self.port_edit)

        self.btn = QPushButton(button_text)
        self.btn.setObjectName("Ghost")
        self.btn.clicked.connect(lambda: self.launched.emit(label))
        lay.addWidget(self.btn)

        self.btn_cmd = QPushButton("复制命令")
        self.btn_cmd.setObjectName("Ghost")
        self.btn_cmd.setToolTip("复制在终端里手动启动所需的确切命令行")
        self.btn_cmd.clicked.connect(lambda: self.copy_requested.emit(label))
        lay.addWidget(self.btn_cmd)

        self.led = StatusLed()
        lay.addWidget(self.led)
        outer.addWidget(row)

        #: 只有浏览器那行有（profile 依赖浏览器路径，所以它的可用性跟着上面走）
        self.profile: "ProfileRow | None" = None
        if with_profile:
            self.profile = ProfileRow(parent=self)
            outer.addWidget(self.profile)
        self._sync_profile_enabled()

    def _sync_profile_enabled(self) -> None:
        """浏览器路径没选之前，profile 行置灰（推导需要它）。"""
        if self.profile is not None:
            self.profile.setEnabled(bool(self.path_edit.text().strip()))

    def set_profile_text(self, text: str) -> None:
        if self.profile is not None:
            self.profile.set_path(text)

    def set_status(self, color: str, text: str) -> None:
        self.led.set_state(color, text)


class ProfileRow(QWidget):
    """浏览器的「用户数据目录」一行：显示 + 改… + 默认 + ⓘ 说明。

    ⚠️ 这一行**只负责显示与选择**：改不改、什么时候改，由 EnvPanel 决定
    （它才知道"浏览器路径变了没有""用户有没有手动设过"）。
    """

    TIP = (
        "浏览器「用户数据目录」(profile)\n"
        "浏览器把 Cookie / 登录态 / 缓存 / 扩展都放在这里。本程序用它开一个**独立**的\n"
        "浏览器实例 —— 读 Bangumi「在看」列表靠的就是这里存的 bgm.tv 登录态。\n"
        "· 换浏览器时本程序会换成对应目录：Chrome → browser-profile、"
        "Edge → browser-profile-edge …\n"
        "· 换目录 = 放弃当前这份登录态，需要重新登录一次 bgm.tv\n"
        "· 目录由浏览器自己创建；本程序不新建、不删除、不移动它"
    )

    dir_pick_requested = Signal()
    reset_requested = Signal()
    dir_committed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 2)
        lay.setSpacing(8)

        lab = QLabel("profile")
        lab.setFixedWidth(84)
        lab.setStyleSheet(f"color: {style.TEXT_MUTED};")
        lay.addWidget(lab)

        self.path_edit = QLineEdit("")
        self.path_edit.setPlaceholderText("（选完浏览器后自动生成，也可自行修改）")
        self.path_edit.editingFinished.connect(
            lambda: self.dir_committed.emit(self.path_edit.text().strip())
        )
        lay.addWidget(self.path_edit, 1)

        self.btn_pick = QPushButton("改…")
        self.btn_pick.setObjectName("Ghost")
        self.btn_pick.setToolTip("选一个目录作为浏览器的用户数据目录")
        self.btn_pick.clicked.connect(self.dir_pick_requested.emit)
        lay.addWidget(self.btn_pick)

        self.btn_default = QPushButton("默认")
        self.btn_default.setObjectName("Ghost")
        self.btn_default.setToolTip("按当前浏览器重新推导目录名（Chrome → browser-profile，"
                                    "其它 → -<名字>）；放在当前 profile 所在的目录下")
        self.btn_default.clicked.connect(self.reset_requested.emit)
        lay.addWidget(self.btn_default)

        # ⓘ 用 Qt 自带的图标，不往仓库加图片资源
        self.info = QToolButton()
        self.info.setObjectName("Ghost")
        self.info.setAutoRaise(True)
        self.info.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MessageBoxInformation))
        self.info.setToolTip(self.TIP)
        self.info.setCursor(Qt.WhatsThisCursor)
        lay.addWidget(self.info)
        # 端口/启动那几列在上一行，这里补一点右边距对齐
        lay.addSpacing(160)

    def set_path(self, text: str) -> None:
        if self.path_edit.text() != text:
            self.path_edit.setText(text)

    def path(self) -> str:
        return self.path_edit.text().strip()


class EnvPanel(QFrame):
    log = Signal(str)
    #: 用户在下拉框里换了季度（携带是 Season 对象）
    season_changed = Signal(object)

    def __init__(self, session: WizardSession, parent=None, *, probe_enabled: bool = True):
        super().__init__(parent)
        #: 保存 session 引用：下面的 `_save_launcher_config` 要改 session.config
        #: （路径/profile 选完就落库 + 同步内存，后续行为与界面一致）
        self.session = session
        self._probe_enabled = probe_enabled
        self.setObjectName("Card")
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(4)

        head = QHBoxLayout()
        t = QLabel("环境")
        t.setObjectName("SectionTitle")
        head.addWidget(t)
        head.addStretch()

        head.addWidget(QLabel("季度"))
        # ⚠️ 这个下拉框此前是**装饰品**：造出来只列了硬编码的 2026 四季，
        # 全项目没有一处读它、也没有 currentIndexChanged —— 用户改了季度
        # 界面毫无反应，而所有业务逻辑读的是 sess.season（来自 run_gui.py --season）。
        # 现在候选项按「今天」算、**保证含在用的那个季度**、弹出前重建，
        # 并在 MainWindow 里接进 session。
        self._last_season: Season = session.season
        self.season = SeasonCombo(session.season)
        # 150 装得下「2026 秋（10月）」，但装不下「其它季度…」这一项
        self.season.setFixedWidth(168)
        self.season.setToolTip(
            f"覆盖「今天」前后各一年。要选其它季度就用最后一项「{CUSTOM_SEASON_TEXT}」")
        # 界面控件自己把「选择」翻译成 Season 再往上抛：MainWindow 就不必
        # 知道下拉框里存的是 data 还是字符串（findData/currentData 的细节留在面板内）
        self.season.currentIndexChanged.connect(self._emit_season)
        head.addWidget(self.season)

        head.addWidget(QLabel("目标歌单"))
        self.playlist = QLineEdit(session.playlist_name)
        self.playlist.setFixedWidth(110)
        head.addWidget(self.playlist)
        root.addLayout(head)

        self.netease = TargetRow("网易云客户端", _netease_exe(session.config),
                                 session.config.netease.debug_port, "启动并接管")
        self.netease.launched.connect(self._launch_netease)
        self.netease.copy_requested.connect(self._copy_netease_cmd)
        self.netease.browse_requested.connect(lambda _l: self._pick_exe("netease"))
        self.netease.path_committed.connect(self._on_netease_path)
        self.netease.port_edit.editingFinished.connect(lambda: self._on_port_edited("netease"))
        root.addWidget(self.netease)

        self.browser = TargetRow(
            "浏览器", _browser_exe(session.config), session.config.browser.debug_port,
            "启动并打开 Bangumi",
            # 用**独立标签**常驻显示；tooltip 里讲清哪些行、哪些不行
            hint="(需 Chromium 内核)",
            hint_tip="Chrome / Edge / Brave / Vivaldi / Chromium 都可以。\n"
                     "Firefox / Safari 不行 —— 它们的远程调试不是 CDP，程序读不到 /json/version。",
            with_profile=True,
        )
        self.browser.launched.connect(self._launch_browser)
        self.browser.copy_requested.connect(self._copy_browser_cmd)
        self.browser.browse_requested.connect(lambda _l: self._pick_exe("browser"))
        self.browser.path_committed.connect(self._on_browser_path)
        self.browser.port_edit.editingFinished.connect(lambda: self._on_port_edited("browser"))
        self.browser.profile.dir_pick_requested.connect(self._pick_profile_dir)
        self.browser.profile.reset_requested.connect(self._reset_profile)
        self.browser.profile.dir_committed.connect(self._on_profile_edited)
        #: 上一次生效的浏览器路径 —— 用来判断"换了浏览器没有"（家族推导要用旧值）
        self._last_browser_exe = self.browser.path_edit.text().strip()
        self.browser.set_profile_text(
            _profile_from_config(session.config) or ""
        )
        self.browser._sync_profile_enabled()
        root.addWidget(self.browser)

        self._worker: ProbeWorker | None = None
        self._launch_worker: LaunchWorker | None = None
        self.probe()

    # ---------------------------------------------------------------- 路径与 profile
    #
    # 这一段的三条规则（都有实测/明确要求作依据）：
    #   1. 路径**默认为空**，由用户「浏览…」选，选完立刻写回 settings.yaml
    #   2. 换浏览器时 profile **按家族自动切**（Chrome → browser-profile 保持不变，
    #      否则现有用户的 bgm.tv 登录态就白丢了），但**只指向，不创建不删除不移动**
    #   3. 用户**手动改过** profile 之后，换浏览器不再覆盖它，只提示
    def _pick_exe(self, kind: str) -> None:
        """「浏览…」：选可执行文件。

        ⚠️ 模态文件对话框 —— 用户自己点出来的，没问题；测试必须 stub
        `QFileDialog.getOpenFileName`（AGENTS.md §2）。
        起始目录由 `browsers.dialog_start_dir()` 给（看候选安装位置），
        **但绝不自动往输入框里填** —— 用户明确要求不做自动检测。
        """
        row = self.netease if kind == "netease" else self.browser
        title = ("选择网易云音乐客户端（cloudmusic.exe）" if kind == "netease"
                 else "选择 Chromium 内核的浏览器（Chrome / Edge / Brave …）")
        path, _ = QFileDialog.getOpenFileName(
            self, title, browsers.dialog_start_dir(kind, row.path_edit.text()),
            "可执行文件 (*.exe);;所有文件 (*)",
        )
        if not path:
            return
        row.path_edit.setText(path)
        # 走同一条提交路径（校验 + 落库 + 推导 profile），避免两处逻辑
        row.path_committed.emit(row.label, path)

    def _on_netease_path(self, _label: str, path: str) -> None:
        self._save_launcher_config(netease=path, log_what="网易云客户端路径")

    def _on_browser_path(self, _label: str, path: str) -> None:
        """浏览器路径变了：先看看要不要把 profile 也切过去，再落库。"""
        old = self._last_browser_exe
        cur_profile = self.browser.profile.path() if self.browser.profile else ""
        new_profile = self._derive_profile_for(path, cur_profile)
        switched = False
        if path and new_profile and new_profile != cur_profile:
            # 只在"当前这份是自动给的"（或还没设）时才自动切，不覆盖用户自己的选择
            expected_old = self._derive_profile_for(old, cur_profile) if old else ""
            if not cur_profile or (expected_old and cur_profile == expected_old):
                self.browser.set_profile_text(new_profile)
                switched = True
                self.log.emit(f"浏览器换成 {os.path.basename(path)} → profile 目录自动切到：")
                self.log.emit(f"    {new_profile}")
                self.log.emit("    这是按浏览器家族给的目录；第一次用它需要在这个窗口里登录一次"
                         " bgm.tv。原来的 profile 保留着，换回去还能用。")
            else:
                self.log.emit(f"浏览器换成 {os.path.basename(path)}，但 profile 目录是你手动设的"
                         f"（{cur_profile}），没有自动改。")
                self.log.emit("    ⚠️ 不同内核共用同一个 profile 目录可能互相干扰；"
                         "要换可以点 profile 那行的「默认」。")
        self._last_browser_exe = path
        self.browser._sync_profile_enabled()
        self._save_launcher_config(
            browser=path,
            profile=self.browser.profile.path() if self.browser.profile else "",
            log_what="浏览器路径" + ("（profile 已同步切换）" if switched else ""),
        )

    def _derive_profile_for(self, exe: str, current_profile: str) -> str:
        """按家族推导 profile；base 取**当前 profile 的父目录**，尊重用户的摆放位置。"""
        base = None
        cur = (current_profile or "").strip().rstrip("\\/")
        if cur:
            base = os.path.dirname(cur) or None
        return browsers.derive_profile(exe, base=base)

    def _pick_profile_dir(self) -> None:
        """profile 的「改…」：选一个目录（模态，测试要 stub）。"""
        cur = self.browser.profile.path() if self.browser.profile else ""
        d = QFileDialog.getExistingDirectory(
            self, "选择浏览器的用户数据目录（profile）", cur or ""
        )
        if not d:
            return
        self.browser.set_profile_text(d)
        self._on_profile_edited(d)

    def _reset_profile(self) -> None:
        """profile 的「默认」：按当前浏览器重新推导。

        基准取**当前 profile 所在目录**（与换浏览器时的自动切换一致）——
        否则两个入口会给出不同结果：用户把 profile 放在 E 盘，点「默认」却被
        拽回 `%LOCALAPPDATA%`，那才是真的意外。
        """
        exe = self.browser.path_edit.text().strip()
        if not exe:
            self.log.emit("请先选浏览器路径，profile 目录是按浏览器家族推导的。")
            return
        cur = self.browser.profile.path() if self.browser.profile else ""
        d = self._derive_profile_for(exe, cur)
        self.browser.set_profile_text(d)
        self._on_profile_edited(d)
        self.log.emit(f"profile 目录已按「{os.path.basename(exe)}」重置为：{d}")

    def _on_profile_edited(self, path: str) -> None:
        was = self.browser.path_edit.text().strip()
        self._save_launcher_config(
            browser=was, profile=path, log_what="浏览器 profile 目录"
        )
        if path and was:
            # 用户手动改过 → 记下来（后续换浏览器不再自动覆盖它，靠 `_on_browser_path`
            # 里那个"expected_old"判断实现，这里不需要额外状态）

            self.log.emit("      （换浏览器时不会再自动改这个目录；要恢复默认点「默认」）")

    def _on_port_edited(self, kind: str) -> None:
        row = self.netease if kind == "netease" else self.browser
        section = getattr(self.session.config, kind)
        text = row.port_edit.text().strip()
        if not text:
            row.port_edit.setText(str(section.debug_port))
            return
        try:
            port = int(text)
            if not 1 <= port <= 65535:
                raise ValueError
            other = "browser" if kind == "netease" else "netease"
            if port == getattr(self.session.config, other).debug_port:
                raise ValueError
        except ValueError:
            self.log.emit(f"{row.label}端口无效：请输入1—65535，且不能与另一程序相同。已恢复原值。")
            row.port_edit.setText(str(section.debug_port))
            return
        row.port_edit.setText(str(port))
        if port == section.debug_port:
            return
        self._save_launcher_config(**{kind + "_port": port}, log_what=row.label + "调试端口")
        row.port = port
        # 已构造的编排器带着旧端口的 BrowserBridge，后续任务必须重建它。
        self.session._orch = None

    def _save_launcher_config(self, *, netease: str | None = None,
                              browser: str | None = None,
                              profile: str | None = None,
                              netease_port: int | None = None,
                              browser_port: int | None = None, log_what: str = "") -> None:
        """把路径/profile/端口写回配置，并同步内存。

        **先重读磁盘、再只改这几个 key**：用户在程序开着的时候手工改过 settings.yaml
        的其它项（阈值、歌单模板…）不能被我们整体覆盖掉。
        """
        cfg = self.session.config
        changes = (("netease", "client_path", netease), ("browser", "exe_path", browser),
                   ("browser", "user_data_dir", profile), ("netease", "debug_port", netease_port),
                   ("browser", "debug_port", browser_port))
        for section, key, value in changes:
            if value is not None:
                setattr(getattr(cfg, section), key, value)
        try:
            fresh = Config.load(cfg.path or None)
            for section, key, value in changes:
                if value is not None:
                    setattr(getattr(fresh, section), key, value)
            fresh.save()
        except Exception as e:  # noqa: BLE001
            # 写不进去（只读目录 / 受限环境）→ **如实说**，不假装保存成功
            self.log.emit(f"⚠️ {log_what}已生效，但没能写回配置：{type(e).__name__}: {e}")
            self.log.emit("    下次启动需要重新选一次（可以把程序放到可写目录，或设 ANIMUSIC_CONFIG_DIR）")
            return
        self.log.emit(f"已保存{log_what} → {fresh.path}")

    def _emit_season(self, _index: int) -> None:
        s = self.season.currentData()
        if s == CUSTOM_SEASON:
            # 用户点了「其它季度…」→ 问他要哪一个
            self._prompt_custom_season()
            return
        if isinstance(s, Season):
            self._last_season = s
            self.season_changed.emit(s)

    def _prompt_custom_season(self) -> None:
        """「其它季度…」→ 输入任意季度（补很老的番、或提前准备很远的季度）。

        ⚠️ 这里会弹**模态**输入框。是用户自己点出来的，所以没问题；
        但测试里必须把 `QInputDialog.getText` 整个替换掉（AGENTS.md §2：
        不许让测试走到模态框前面）。

        ⚠️ 取消或解析失败时**必须把选中项还原到上一季**：否则界面会停在
        「其它季度…」这一项上，而工具实际还在处理上一季 ——
        那正是这次要消灭的那种谎（界面说 A、程序用 B）。
        """
        text, ok = QInputDialog.getText(
            self, "其它季度", "形如 2028Q1 / 2028-1 / 2026q4：",
            text=self._last_season.key,
        )
        if not ok:
            self._restore_season()
            return
        try:
            s = Season.parse(text)
        except ValueError as e:
            self.log.emit(f"季度格式不对：{e}")
            self._restore_season()
            return
        # 先把它并进列表，再选中它 —— 顺序反了就会选到一个不存在的项。
        # ⚠️ `rebuild()` 内部是**挡着信号**改选中项的（否则每次重建都会触发换季度），
        # 所以这一次切换必须**由这里显式报上去**，不能指望 setCurrentIndex 发信号
        # —— 实测：rebuild 已经把它选好了，后面的 setCurrentIndex 下标没变，
        # `currentIndexChanged` 根本不发，于是用户输了季度却什么都没发生。
        self.season.rebuild(select=s)
        idx = _combo_index_of(self.season, s)
        if idx < 0:                      # 理论上不可能，但绝不留下不一致的界面
            self.log.emit(f"无法在下拉框里选中 {s.key} —— 保持原季度不变")
            self._restore_season()
            return
        self.season.blockSignals(True)
        self.season.setCurrentIndex(idx)
        self.season.blockSignals(False)
        self._last_season = s
        self.season_changed.emit(s)

    def _restore_season(self) -> None:
        """把选中项退回上一个真实季度（挡信号：还原不是「换季度」）。"""
        idx = _combo_index_of(self.season, self._last_season)
        self.season.blockSignals(True)
        self.season.setCurrentIndex(idx if idx >= 0 else 0)
        self.season.blockSignals(False)

    def set_playlist_text(self, name: str) -> None:
        """程序性设置目标歌单名 —— 用 setText 而非 setCurrentText 之类，
        因为它只发 textChanged、**不发** textEdited，不会反过来触发
        `_on_playlist_edited`（避免「改季度 → 改歌单 → 又改回去」的回环）。"""
        if self.playlist.text() != name:
            self.playlist.setText(name)

    # ---- 非阻塞探测
    def probe(self) -> None:
        if not self._probe_enabled or getattr(self, "_closing", False):
            return
        if self._worker is not None and self._worker.isRunning():
            return  # 不重入，避免线程堆积
        self.netease.set_status(style.TEXT_DIM, "检测中…")
        self.browser.set_status(style.TEXT_DIM, "检测中…")

        self._worker = ProbeWorker(
            self.session.config.netease.debug_port,
            self.session.config.browser.debug_port,
            self,
        )
        self._worker.done.connect(self._on_probe)
        self._worker.start()

    def _on_probe(self, res: dict) -> None:
        if getattr(self, "_closing", False):
            return
        browser_ver, _ = res.get("netease", (None, None))
        if browser_ver:
            self.netease.set_status(style.OK, f"已连接 ({browser_ver.split('/')[-1]})")
        else:
            self.netease.set_status(style.TEXT_DIM, "未启动")

        bver, logged = res.get("browser", (None, None))
        if not bver:
            self.browser.set_status(style.TEXT_DIM, "未启动")
        elif logged is True:
            self.browser.set_status(style.OK, "已连接 · 已登录")
        elif logged is False:
            self.browser.set_status(style.WARN, "已连接 · 未登录")
        else:
            self.browser.set_status(style.OK, "已连接")

    # ---- 启动参数（启动与「复制命令」共用，避免两处不一致）
    #
    # ⚠️ 这两份 spec 就是**启动参数的唯一来源**（CLI 走 `Config.*_launch_args()`，
    # 那份在这里也要保持一致）。原来 `constants` 里还躺着一份**零引用**的
    # `NETEASE_LAUNCH_ARGS`/`BROWSER_LAUNCH_ARGS`，2026-10-07 已删 ——
    # 同一份参数有三处，改一处不生效。
    def _netease_spec(self) -> tuple[str, list[str], int]:
        exe = self.netease.path_edit.text().strip()
        self._on_port_edited("netease")
        port = self.session.config.netease.debug_port
        return exe, [
            f"--remote-debugging-port={port}",
            "--disable-features=CalculateNativeWinOcclusion",
        ], port

    def _browser_spec(self) -> tuple[str, list[str], int]:
        exe = self.browser.path_edit.text().strip()
        self._on_port_edited("browser")
        port = self.session.config.browser.debug_port
        # profile 取**界面字段**（可自行修改），不再是写死的常量；
        # 字段空着就按家族推导一个（不落盘，只为让"复制命令"仍然可用）。
        profile = ""
        if self.browser.profile is not None:
            profile = self.browser.profile.path()
        # 字段空 → 按家族推导；连家族都推不出来（例如用户填了个 `.bat` 包装脚本）
        # → 退回历史默认目录。**绝不能拼出 `--user-data-dir=` 这种空值**：
        # 那会让 Chrome 用默认 profile，而 ≥136 在默认 profile 上直接拒绝开远程调试。
        profile = profile or browsers.derive_profile(exe) or constants.BROWSER_PROFILE_DIR
        # 起始页面也听配置的（`config.browser.start_url`）—— 原来这里又写死了一次
        # `https://bgm.tv/`，于是"改了 start_url 界面不认"（它一直是死配置）。
        start_url = (getattr(self.session.config.browser, "start_url", "") or "").strip()
        return exe, [
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            start_url or "https://bgm.tv/",
        ], port

    def _copy_cmd(self, label: str, exe: str, args: list[str]) -> None:
        if not exe:
            self.log.emit(f"「{label}」还没设置程序路径 —— 请先点「浏览…」选一个")
            return
        cmd = _command_text(exe, args)
        QApplication.clipboard().setText(cmd)
        self.log.emit(f"已复制「{label}」的启动命令到剪贴板（可直接粘到终端运行）")

    def _copy_netease_cmd(self, _l: str) -> None:
        self._copy_cmd("网易云客户端", *self._netease_spec()[:2])

    def _copy_browser_cmd(self, _l: str) -> None:
        self._copy_cmd("浏览器", *self._browser_spec()[:2])

    # ---- 启动（带轮询与诊断）
    def _launch_netease(self, _label: str) -> None:
        exe, args, port = self._netease_spec()
        if not exe:
            self._need_path("网易云客户端", "netease")
            return
        if not os.path.exists(exe):
            QMessageBox.warning(self, "路径无效", f"找不到文件：\n{exe}")
            return
        self._run_launch("网易云客户端", self.netease, exe, args, port)

    def _launch_browser(self, _label: str) -> None:
        exe, args, port = self._browser_spec()
        if not exe:
            self._need_path("浏览器（需 Chromium 内核）", "browser")
            return
        if not os.path.exists(exe):
            QMessageBox.warning(self, "路径无效", f"找不到文件：\n{exe}")
            return
        self._run_launch("浏览器", self.browser, exe, args, port)

    def _need_path(self, what: str, kind: str) -> None:
        """路径还没配就点了启动 —— 给一次轻提示（只写日志的话用户会以为程序坏了）。

        ⚠️ 模态框：是用户点出来的，没问题；测试必须 stub `QMessageBox.information`（§2）。
        """
        extra = ("（浏览器要选 Chromium 内核的：Chrome / Edge / Brave / Vivaldi …）"
                 if kind == "browser" else "")
        self.log.emit(f"还没设置{what}的程序路径 —— 请点那一行的「浏览…」选一个{extra}")
        QMessageBox.information(
            self, "还没设置路径",
            f"还没设置{what}的程序路径。\n\n"
            f"请点那一行的「浏览…」选择 {what} 的可执行文件（.exe）。\n"
            f"选好之后会自动记住，下次启动不用再选。{extra}",
        )

    def _run_launch(self, label: str, row: "TargetRow", exe: str, args: list[str], port: int) -> None:
        if getattr(self, "_closing", False):
            return
        if getattr(self, "_launch_worker", None) is not None and self._launch_worker.isRunning():
            self.log.emit("已有启动任务在进行中，请等待完成")
            return

        command = _command_text(exe, args)
        row.set_status(style.WARN, "启动中…")
        self.log.emit(f"启动 {label}：{command}")

        self._launch_worker = LaunchWorker(label, exe, args, port, parent=self)
        self._launch_worker.progress.connect(lambda m: row.set_status(style.WARN, m))
        self._launch_worker.done.connect(
            lambda res, l=label, r=row, c=command: self._on_launch_done(l, r, c, res)
        )
        self._launch_worker.start()

    def _on_launch_done(self, label: str, row: "TargetRow", command: str, res: dict) -> None:
        if getattr(self, "_closing", False) or res.get("stage") == "cancelled":
            return
        if res.get("ok"):
            self.log.emit(f"{label}：{res['msg']}")
            self.probe()
            return

        row.set_status(style.ERR, "启动失败")
        stage = res.get("stage")
        if stage == "spawn":
            why = "进程无法创建。"
        elif stage == "exited":
            # 走到这里 = 启动器进程退了**而且**端口一直没开。
            # 不能再写"进程启动后立即退出"了 —— 那句话以前会把"Chrome 已交接成功"
            # 也报成失败（用户真机报过）。exit_code 只作为线索列出来。
            why = (
                f"启动器进程退出（code {res.get('exit_code')}）后，"
                f"调试端口 {res.get('port', '')} 一直没有开。<br><br>"
                "常见原因："
                "<ul>"
                "<li>目标<b>已经在运行</b>了 —— 调试端口只能在启动时指定，"
                "必须先从托盘/任务管理器完全退出再重启（记下这个 exit code："
                "Chrome 的 <code>chrome.exe</code> 只是交接用的桩，"
                "它退出<b>不代表</b>失败）</li>"
                "<li>目标程序自身启动失败。看它自己的输出："
                f"<code>{spawn.log_path(label)}</code></li>"
                "</ul>"
            )
        else:
            why = (
                f"超时：{res.get('msg')}。<br><br>"
                "可能是程序还在初始化，或调试端口被其他程序占用。"
            )

        self.log.emit(f"{label} 启动失败：{res.get('msg')}")
        dlg = CommandDialog(
            f"{label} 启动失败",
            f"<b>{why}</b>",
            command,
            self,
        )
        dlg.exec()
        self.probe()


class MainWindow(QMainWindow):
    def __init__(self, session: WizardSession, *, probe_enabled: bool = True):
        super().__init__()
        self.session = session
        self.data: dict[str, list[dict]] = {
            "anime": [], "theme": [], "match": [], "push": [], "missing_source": [],
        }
        self.worker: TaskWorker | None = None
        self._closing = False
        self.setWindowTitle("AniMusicCollector —— 新番 OP/ED → 网易云歌单")
        self.resize(1500, 980)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 8)
        root.setSpacing(8)

        self.env = EnvPanel(session, probe_enabled=probe_enabled)
        self.env.log.connect(self.log)
        root.addWidget(self.env)

        self.stepper = Stepper()
        self.stepper.step_clicked.connect(self._goto)
        root.addWidget(self.stepper)

        self.stack = QStackedWidget()
        self.pages = [
            AnimePage([]),
            ThemePage([], []),
            MatchPage([]),
            PushPage([]),
        ]
        for p in self.pages:
            p.log.connect(self.log)
            p.next_requested.connect(self._next)
            p.back_requested.connect(self._back)
            self.stack.addWidget(p)

        self.logpane = LogPane()
        # ⚠️ 表格区与日志区之间必须是**可拖动的分界**。
        # 原来日志是 `addWidget` 平铺在下面的，而 LogPane 自己写着
        # `setFixedHeight(72)`（同时钉死 min 与 max）—— 用户既拖不动、
        # 也没有任何地方能改，日志永远只有 4 行高，报错时看不见上下文。
        # 装进 QSplitter 后：可以拖、默认给到 170px、窗口变大时优先长表格
        # （stretch 0=1 / 1=0）。
        self.split = QSplitter(Qt.Vertical)
        self.split.addWidget(self.stack)
        self.split.addWidget(self.logpane)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.setCollapsible(0, False)
        self.split.setCollapsible(1, False)
        self.split.setSizes([760, 170])
        root.addWidget(self.split, 1)

        self.status = self.statusBar()
        # 日志面板的折叠开关。为什么需要它：分界一旦被拖到最底下，
        # 用户没有别的办法把日志弄回来（也不该靠「重开程序」）。
        self.btn_log = QPushButton("日志 ▾")
        self.btn_log.setObjectName("Ghost")
        self.btn_log.setCheckable(True)
        self.btn_log.setChecked(True)
        self.btn_log.setToolTip("显示 / 隐藏日志面板（Ctrl+L）")
        self.btn_log.toggled.connect(self._set_log_visible)
        self.status.addWidget(self.btn_log)
        self.btn_stop = QPushButton("停止任务")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_task)
        self.status.addWidget(self.btn_stop)
        # 留住引用：见下面 QTimer 那段注释 —— 这个项目已经因为「匿名 QObject 交给 GC」
        # 吃过一次间歇性崩溃（0xC0000409）。QShortcut 没有定时器、风险低得多，
        # 但留着引用是免费的，而且下次读代码的人不会再来一遍这个推理。
        self._shortcut_log = QShortcut(QKeySequence("Ctrl+L"), self,
                                      activated=self.btn_log.toggle)

        # 数据来源署名**常驻**状态栏右侧。
        # 为什么不只放在启动日志里：日志会被后续输出顶走，而署名是 CC BY-SA 的
        # **许可证义务** —— 只要界面开着、只要展示的是维基来的曲目，它就得在场。
        self.credit = QLabel(wikipedia_attribution())
        self.credit.setToolTip(
            "OP/ED 曲目的主数据源是日文维基百科的「主題歌」栏，正文以 CC BY-SA 4.0 授权。\n"
            "转发本程序的界面截图或导出的报告时，请一并保留这行署名。")
        self.status.addPermanentWidget(self.credit)

        self._wire_actions()
        self.reload()

        # 状态灯低频刷新即可；探测本身在后台线程，不阻塞 UI。
        #
        # ⚠️ 必须**留住引用**，并在 `closeEvent` 里 `stop()`。
        # 原来它是个匿名临时对象：窗口关掉之后定时器还在跑，而一个带**活动**
        # 定时器的 QObject 一旦在**别的线程**里被析构（Python 的 GC 可能在任何
        # 线程触发），Qt 会报 "Timers cannot be stopped from another thread"，
        # 并且实测见过整个进程被杀掉（退出码 0xC0000409，全量测试跑完不打汇总）。
        # 关窗口就该停掉它 —— 这本来也是对的：窗口都没了，不该再探测。
        self._timer = QTimer(self, timeout=self._tick, interval=PROBE_INTERVAL_MS)
        if probe_enabled:
            self._timer.start()

        self._goto(0)
        self._intro()

    # ---------------------------------------------------------------- 真实动作接线
    def _wire_actions(self) -> None:
        """把四个页面的主操作接到真实的 Orchestrator 上（index=0 → 排在最前）。"""
        anime, theme, match, push = self.pages

        anime.add_action("从 Bangumi 拉取「在看」", self._fetch_bangumi, "primary",
                         "读取已接管浏览器里的 Bangumi 在看列表", index=0)
        all_anime = anime.add_action("当季全部（来源待定）", lambda: None, "normal",
                                    "番单来源尚未确定，暂不可用", index=1)
        all_anime.setEnabled(False)

        theme.add_action("拉取 / 更新 OP/ED", self._fetch_themes, "primary",
                         "只更新**番单里已纳入**的那些番的曲目；\n"
                         "不会把当季全部番拉进来（那是第①步「仅从 AnimeThemes 拉当季」的事）",
                         index=0)

        match.add_action("开始匹配网易云", self._run_match, "primary",
                         "对每条曲目搜索并打分（后台执行）", index=0)

        push.add_action("推送到歌单", self._push_songs, "primary",
                         "把自动通过 + 人工确认的歌加入季度歌单（会先让你确认）", index=0)
        push.add_action("试运行", self._push_dry_run, "normal",
                         "只列出会推哪些歌，不写入", index=1)
        push.add_action("检查目标歌单", self._check_playlist, "normal", "", index=2)
        push.manual_requested.connect(self._manual_song_help)
        self.env.season_changed.connect(self._on_season_changed)
        self.env.playlist.textEdited.connect(self._on_playlist_edited)

    def _manual_song_help(self) -> None:
        if not self.pages[2].all_rows():
            self._goto(1)
            self.log("请先在第②步新增曲目，确认后到第③步手动指定网易云歌曲")
        else:
            self._goto(2)
            self.log("请选择目标曲目，使用「手动指定」填写网易云歌曲ID；缺少曲目时先回②新增")

    # ---------------------------------------------------------------- 日志面板
    def _set_log_visible(self, on: bool) -> None:
        self.logpane.setVisible(bool(on))
        self.btn_log.setText("日志 ▾" if on else "日志 ▸")

    # ---------------------------------------------------------------- 环境面板接线

    def _on_season_changed(self, s: Season) -> None:
        """下拉框换季度 —— 必须真的换掉 session 的季度。

        这是之前缺的那条接线：下拉框和 `sess.season` 曾经是两个互不相干的
        变量，用户在界面上换季度等于什么都没发生。
        """
        if self._closing or (self.worker is not None and self.worker.isRunning()):
            return
        self.session.set_season(s)
        self.log(f"季度切换为 {s.key}（{s.label}）")
        # 「目标歌单」和表格都在 reload 里按新季度重建（含名字由季度推出，②）；
        # 这里是主动换数据，所以不保留旧勾选
        self.reload(preserve_included=False)

    def _on_playlist_edited(self, text: str) -> None:
        """用户手改「目标歌单」 —— 同步到还没有自定义过歌单的表格行。

        不这么做的话，输入框显示 A、每行显示 B，而推送用的是 A：
        用户按界面理解去核对，核对的是错的东西。
        """
        self.pages[3].adopt_default_playlist(text)
        self._update_status()

    def _refresh_playlist(self) -> None:
        """把「目标歌单」设为当前季度应有的名字，并同步到推送表格。

        `EnvPanel.set_playlist_text` 用的是 setText，只发 textChanged
        而**不发** textEdited，所以不会绕回来触发 `_on_playlist_edited`
        （否则会「改季度 → 改歌单 → 又把歌单改回去」来回打转）。
        """
        panel = self.env
        old = panel.playlist.text()
        new = self.session.playlist_name
        panel.set_playlist_text(new)
        self.pages[3].adopt_default_playlist(new, old_default=old)
        self._update_status()

    # ---------------------------------------------------------------- 刷新

    def reload(self, reason: str = "", preserve_included: bool = True) -> None:
        """从数据库重建四个页面的数据。

        不在 UI 线程里做任何网络 I/O（§8.6 硬规则）——
        这里只读本地 SQLite。

        `preserve_included=False` 用于导航（上一步/下一步/换季度）：
        那是用户主动要求「换一批数据」，勾选留着只会误导。
        默认 True 是给后台任务回来后的刷新用的 —— 那些刷新不该动用户的勾选。
        """
        push_page = self.pages[3]
        # 勾选状态是**用户意图**，而 set_data 会整表替换把意图冲掉。
        # 推送只认勾选，所以「刷新一下勾选就回来了」比「丢一次写入」危险得多。
        # 读全量（all_rows）：被筛选藏起来的行，勾选意图一样要保住。
        want = ({r.get("theme_id"): bool(r.get("included")) for r in push_page.all_rows()}
                if preserve_included else {})
        self.data = self.session.build()
        self.pages[0].set_data(self.data["anime"])
        self.pages[1].set_data(self.data["theme"])
        self.pages[1].missing = self.data["missing_source"]
        self.pages[1].anime_choices = [(r["anime_id"], " · ".join(x for x in (
            r["title_cn"], r["title_jp"] if r["title_jp"] != r["title_cn"] else "", r["air_date"]) if x))
            for r in self.data["anime"] if r["included"]]
        self.pages[2].set_data(self.data["match"])
        push_page.set_data(self.data["push"])
        push_page.restore_included(want)
        self._refresh_playlist()
        self._update_status()
        if reason:
            st = self.session.stats(self.data)
            self.log(
                f"已刷新（{reason}）：番剧 {st['anime']} · {self._theme_text(st)} · "
                f"自动 {st['auto']} · 待审核 {st['review']} · 缺失 {st['missing']}"
            )

    # ---------------------------------------------------------------- 后台任务

    def _busy(self, on: bool, label: str = "") -> None:
        for p in self.pages:
            p.setEnabled(not on and not self._closing)
        self.env.setEnabled(not on and not self._closing)
        self.stepper.setEnabled(not on and not self._closing)
        self.btn_stop.setEnabled(on and not self._closing)
        if on:
            self.status.showMessage(f"  ⏳ {label}…")
            self.log(f"▶ {label}（后台执行，界面不会卡）")

    def _run_task(self, fn, label: str, on_done=None, *, refresh_on_success: bool = True) -> None:
        if self._closing:
            return
        if self.worker is not None and self.worker.isRunning():
            self.log(f"已有任务在跑，忽略本次「{label}」")
            return
        self._busy(True, label)
        # `cleanup` 在那个线程里收掉它自己的 sqlite 连接 —— GUI 主线程
        # 关不掉别的线程建的连接（sqlite 的线程检查，实测确认）。
        w = TaskWorker(fn, label=label, parent=self,
                       cleanup=self.session.store.close_thread)
        w._ui_done = on_done
        w._ui_refresh = refresh_on_success
        # QObject槽明确在主线程执行，不能让工作线程直接更新或销毁界面。
        w.progressed.connect(self._task_progress)
        w.failed.connect(self._worker_failed)
        w.cancelled.connect(self.log)
        w.succeeded.connect(self._worker_succeeded)
        w.finished.connect(self._task_finished)
        self.worker = w
        w.start()

    @Slot(str)
    def _task_progress(self, message: str) -> None:
        self.log(f"    {message}")

    @Slot(str, str)
    def _worker_failed(self, message: str, traceback_text: str) -> None:
        self._task_failed(self.sender().label, message, traceback_text)

    @Slot(object)
    def _worker_succeeded(self, result) -> None:
        worker = self.sender()
        self._task_done(worker.label, result, worker._ui_done, refresh_on_success=worker._ui_refresh)

    def _task_failed(self, label: str, msg: str, tb: str) -> None:
        self.log(f"✗ {label} 失败：{msg}")
        self.logpane.log(tb.splitlines()[-1] if tb else "")
        if not self._closing:
            QMessageBox.warning(self, f"{label} 失败", msg)

    def _task_done(self, label: str, result, on_done, *, refresh_on_success: bool = True) -> None:
        self.log(f"✓ {label} 完成")
        if on_done is not None:
            on_done(result)
        if getattr(self, "_closing", False):
            return
        if refresh_on_success:
            self.reload(label)
        else:
            self._update_status()

    def _stop_task(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.request_stop()
            self.btn_stop.setEnabled(False)
            self.log("已请求停止，正在等待当前操作完成回查与记账")

    @Slot()
    def _task_finished(self, worker=None) -> None:
        if worker is None:
            worker = self.sender()
        if worker is self.worker:
            self._busy(False)
        self._finish_close_when_ready()

    # ---- 具体动作

    def _fetch_bangumi(self) -> None:
        sess = self.session
        season = sess.season

        def job(progress=None, should_stop=None):
            return sess.run_operation('fetch_anime_list', season, progress=progress, should_stop=should_stop)

        self._run_task(job, f"从 Bangumi 拉取 {season.key}")

    def _fetch_animethemes(self) -> None:
        sess = self.session
        season = sess.season

        def job(progress=None, should_stop=None):
            return sess.run_operation('fetch_season_from_animethemes', season, progress=progress, should_stop=should_stop)

        self._run_task(job, f"从 AnimeThemes 拉取 {season.key}")

    def _update_season(self) -> None:
        """季度内增量重跑 —— 只处理新的与值得重试的。

        赛季持续十几周，新番歌曲通常**开播后 2~8 周才上架**，
        所以这个按钮的预期用法是每周点一次，而不是反复全量重跑。
        """
        sess = self.session
        season = sess.season

        def job(progress=None, should_stop=None):
            return sess.run_operation('update_season', season, progress=progress, should_stop=should_stop)

        def done(rep):
            for line in sess.orchestrator().describe_update(rep):
                self.log(line)

        self._run_task(job, f"{season.key} 增量重跑", on_done=done)

    def _fetch_themes(self) -> None:
        """第②步的「拉取 / 更新 OP/ED」= **只更新当前番单的曲目**。

        ⚠️ 这里原来接的是 `fetch_season_from_animethemes`，也就是第①步那个
        "仅从 AnimeThemes 拉当季" —— 按设计它会把**整季**番写进番单，
        于是用户点"更新曲目"却看到番单里凭空多出几十部没在看的番（实测报的 bug②）。
        两件事现在彻底分开：
          · 第①步「仅从 AnimeThemes 拉当季」→ 有意地把整季收进来
          · 第②步「拉取 / 更新 OP/ED」→ 只碰番单里已纳入的那几部
        """
        sess = self.session
        season = sess.season

        def job(progress=None, should_stop=None):
            return sess.run_operation('refresh_themes', season, progress=progress, should_stop=should_stop)

        self._run_task(job, f"更新曲目 {season.key}")

    def _exclude_supplemental(self) -> None:
        """第①步：把「仅从 AnimeThemes 拉当季」带进来的番一键取消纳入。

        只改 `included`（**不删除**），所以曲目、候选、匹配决策、推送记录都还在，
        想恢复就把「纳入」勾回来。
        """
        sess = self.session
        if self.worker is not None and self.worker.isRunning():
            self.log("已有任务在跑，忽略本次「排除补充的番」")
            return
        try:
            names = sess.store.exclude_supplemental_anime(sess.season_id)
        except Exception as e:  # noqa: BLE001
            self.log(f"✗ 排除失败：{type(e).__name__}: {e}")
            return
        if not names:
            self.log("没有需要排除的番（番单里没有「AnimeThemes 补充」的番）")
            return
        preview = "、".join(names[:5]) + ("…" if len(names) > 5 else "")
        self.log(f"已排除 {len(names)} 部「AnimeThemes 补充」的番（只取消纳入，数据保留）：{preview}")
        self.log("    它们不是你在看的番，也不是手工新增的；要恢复就把「纳入」勾回来。")
        self.reload("排除补充的番", preserve_included=False)

    def _run_match(self) -> None:
        sess = self.session
        season = sess.season

        def job(progress=None, should_stop=None):
            return sess.run_operation('match_season', season, progress=progress, should_stop=should_stop)

        self._run_task(job, f"匹配 {season.key}")

    # ---- 推送（唯一会改动账号的动作）

    _PUSH_STATUS = {"pushed": "已推送", "existed": "已存在", "failed": "失败", "pending": "未尝试", "skipped": "已跳过"}

    def _current_push_plan(self):
        default = self.env.playlist.text().strip() or self.session.playlist_name
        choices = [{**r, "playlist": str(r.get("playlist") or "").strip() or default}
                   for r in self.pages[3].all_rows()]
        return self.session.orchestrator().build_push_plan(self.session.season, selections=choices)

    def _push_dry_run(self) -> None:
        orch = self.session.orchestrator()
        report = orch.execute_push_plan(self._current_push_plan(), dry_run=True)
        for line in orch.describe_push(report):
            self.log(line)

    def push_selected_rows(self) -> list[dict]:
        from dataclasses import asdict
        return [asdict(it) for it in self._current_push_plan().items]

    def _push_songs(self) -> None:
        plan = self._current_push_plan()
        if not plan.items:
            QMessageBox.information(self, "没有可推送的曲目", "请先到第③步确认匹配，并在第④步勾选要推送的歌曲。")
            return
        groups = {}
        for it in plan.items:
            groups[it.playlist] = groups.get(it.playlist, 0) + 1
        detail = "\n".join(f"· 歌单「{name}」{count} 首" for name, count in groups.items())
        ok = QMessageBox.question(
            self, "确认推送", f"将把勾选的 {len(plan.items)} 首歌加入 {len(groups)} 个歌单：\n\n{detail}\n\n"
            "已在歌单里的会回查后跳过；停止时会先完成当前歌曲的确认与记账。\n继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ok != QMessageBox.Yes:
            self.log("已取消推送")
            return
        # 确认的是冻结的计划，不从后台线程再读取会变化的页面或季度。
        orch = self.session.orchestrator()
        def job(progress=None, should_stop=None):
            return orch.execute_push_plan(plan, self._make_push_collector(), progress=progress, should_stop=should_stop)
        self._run_task(job, f"推送到 {len(groups)} 个歌单", on_done=self._push_done, refresh_on_success=False)

    @staticmethod
    def _group_by_playlist(rows: list[dict], default: str) -> dict[str, list[dict]]:
        """勾选的行按「目标歌单」分组，保持首现顺序。

        空歌单名视为「用环境面板里的默认值」—— 手动新增行时那一格本来就是空的，
        不能因为空就把它塞进一个叫 "" 的歌单里。
        """
        out: dict[str, list[dict]] = {}
        for r in rows:
            name = str(r.get("playlist") or "").strip() or default
            out.setdefault(name, []).append(r)
        return out

    def _make_push_collector(self):
        """推送用的 collector —— 单独一个方法是为了测试能替换它。

        测试只允许离线：换掉这里就不需要真的客户端了。
        """
        from ..collector.desktop_cdp import DesktopCDPCollector

        return DesktopCDPCollector(self.session.config.netease.debug_port)

    def _push_groups(self, groups: dict[str, list[dict]], progress=None, should_stop=None, collector=None) -> PushReport:
        """兼容旧接线入口，执行规则只有编排层的一份。"""
        orch = self.session.orchestrator()
        choices = [{**r, "playlist": name} for name, rows in groups.items() for r in rows]
        plan = orch.build_push_plan(self.session.season, selections=choices)
        col = collector if collector is not None else self._make_push_collector()
        return orch.execute_push_plan(plan, col, progress=progress, should_stop=should_stop)

    def _push_group(self, collector, name: str, rows: list[dict], progress=None) -> PushReport:
        return self._push_groups({name: rows}, progress=progress, collector=collector)

    def _push_done(self, rep: PushReport) -> None:
        for line in self.session.orchestrator().describe_push(rep):
            self.log(line)
        # 推送完立刻把「这次的勾选与逐行歌单」存下来：用户推完直接关窗口
        # （没点「完成」）也不该丢掉这次的选择。读全量 —— 见 `_next` 的注释。
        try:
            self.session.save_push_selection(self.pages[3].all_rows())
        except Exception as e:  # noqa: BLE001 - 存选择失败不影响推送结果本身
            self.log(f"（推送选择未能保存：{type(e).__name__}: {e}）")
        # ⚠️ 这里**刻意不 reload()**。reload 会用 session 新造的行整体替换表格，
        # 于是刚刚由 mark_results 写上去的「失败」标记会当场被抹掉 ——
        # 而失败项不在台账里（只有成功才入库），reload 后再也回不来。
        # 结果就是：用户看到一句「部分失败」的弹窗，然后表格里一切正常，
        # 根本不知道是哪几首。失败状态必须留在表格上让人能核对。
        self.pages[3].mark_results({
            (it.playlist or rep.playlist, str(it.song_id)): self._PUSH_STATUS.get(it.status, it.status)
            for it in rep.items + rep.untried
        })
        if rep.failed and not getattr(self, "_closing", False):
            QMessageBox.warning(
                self, "部分失败",
                f"{len(rep.failed)} 首没能加入歌单，原因已列在日志和表格里。\n\n"
                f"常见原因：网易云未上架、无版权、或客户端不在该歌单页面。",
            )

    def _check_playlist(self) -> None:
        name = self.session.playlist_name
        try:
            # 延迟导入：这个模块依赖客户端状态，导入失败不应影响整个 GUI
            from ..collector.desktop_cdp import DesktopCDPCollector

            col = DesktopCDPCollector(self.session.config.netease.debug_port)
            st = col.health()
            if not st.ok:
                self.log(f"网易云客户端不可用：{st.detail}")
                QMessageBox.information(self, "网易云客户端", st.detail)
                return
            found = col.find_playlist(name)
            self.log(f"目标歌单「{name}」：{'已存在 → ' + str(found.netease_id) if found else '不存在，推送时会自动创建'}")
        except Exception as e:  # noqa: BLE001
            self.log(f"检查歌单失败：{type(e).__name__}: {e}")
            QMessageBox.warning(self, "检查歌单失败", f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 日志 / 状态
    @Slot(str)
    def log(self, msg: str) -> None:
        self.logpane.log(msg)

    def _intro(self) -> None:
        st = self.session.stats(self.data)
        s = self.session.season
        self.log(f"AniMusicCollector 启动 · 季度 {s.key}（{s.label}） · 目标歌单「{self.session.playlist_name}」")
        self.log(f"  数据库 {self.session.store.path}")
        # 打包版最容易搞错、又最难自己发现的一件事：「到底在用哪个库、哪份配置」。
        # 仓库里的 data/state.db 和 exe 旁边的 data/ 是两个不同的文件，界面长得一模一样。
        # 与其让人猜，直接打出来。（数据库那行仍以 `store.path` 为准 ——
        # `--db` 能覆盖它，而覆盖值只有 store 自己知道。）
        p = constants.home_paths()
        self.log(
            f"  配置 {p['配置目录']} · 报告 {p['报告目录']} · "
            f"程序根目录 {p['程序根目录']}（打包运行：{p['打包运行']}）"
        )
        # 令牌：一次真机反馈逼出来的 ——「双击打开后，本程序拉起的网易云客户端写不了
        # 自己的数据目录（没托盘图标、没登录态）；同一个命令行在终端里手工跑就正常」。
        # 子进程继承**本进程的令牌**，所以"我这个进程是不是中完整性、有没有受限"
        # 直接决定它能不能写 %LOCALAPPDATA%。正常应该是「完整性=中 受限=否」。
        self.log(f"  进程令牌 {constants.token_summary()}")
        # 外部程序要写的那几处，本进程现在就量一遍 —— 这是上面那条反馈的**现场证据**：
        # 如果这里显示"不可写"，而同一台机器的终端里手工跑却正常，
        # 那问题就在本进程的令牌/权限上，而不是程序代码。
        self.log(f"  环境 {constants.env_facts()}")
        for _name, _path, _why in constants.needed_write_dirs():
            _r = constants.write_probe(_path)
            self.log(f"    {_name} {_r}" + ("" if _r == "可写" else f"  ← {_why}"))
        # 令牌被降级时，把**为什么**和**怎么办**一次说清（这段实测结论写死在
        # constants.low_integrity_warning 里，命令行自检打印同一段，避免两处说法不一致）。
        _warn = constants.low_integrity_warning()
        if _warn:
            for _line in _warn.splitlines():
                self.log(_line)
        self.log(
            f"  当前：番剧 {st['anime']} · {self._theme_text(st)} · "
            f"匹配 {st['match_total']} · 待推送 {st['push']}"
        )
        if not self.data["anime"]:
            self.log("  还没有数据 —— 点步骤① 的「从 Bangumi 拉取」开始，")
            self.log("  或者用「仅从 AnimeThemes 拉当季」跳过 Bangumi 直接看整季。")
        self.log("提示：手工改过的行会标记为「手工」，重跑拉取时不会被覆盖。")
        # 数据来源署名 —— CC BY-SA 4.0 的**署名义务**，必须出现在界面上。
        # 文本只有一处定义（`sources/wikipedia.py:ATTRIBUTION`），这里引用不抄写；
        # 日志会滚走，所以同一个字符串还常驻在状态栏（见 __init__ 的 self.credit）。
        self.log(f"曲目来源：{wikipedia_attribution()}")

    def _tick(self) -> None:
        self.env.probe()

    def _update_status(self) -> None:
        st = self.session.stats(self.data)
        s = self.session.season
        self.status.showMessage(
            f"  季度 {s.year} {s.label.split()[-1]} · 歌单 {self.session.playlist_name}   |   "
            f"番剧 {st['anime']} · {self._theme_text(st)} · "
            f"匹配 {st['match_total']} · 待推送 {st['push']}"
        )

    @staticmethod
    def _theme_text(st: dict) -> str:
        """曲目数的**两个口径都写出来**（只在它们不一样时）。

        为什么：界面上的「曲目 14」只数**勾上**的行，而拉取报告说的是
        「新增曲目 15 条」——差的那一条是用户在步骤②取消勾选的。
        两个数字都对，但只显示一个就会让人以为丢了数据（实测被问过一次）。

        ⚠️ 差额的措辞**不许写「未收录」**（原来写的就是它）。本项目里
        「未收录」= **数据源里没有**（`MISSING_SOURCE`，界面上是 ⬜ 未收录），
        而这儿的差额是**用户自己没勾**：要么在②把这条曲目取消了，要么它
        所属的番在①被排除了。写成「未收录」会让人去手动补几行**本来就有
        数据**的曲目 —— 措辞指错方向就是让人白干活（§6）。

        两个原因也分开说，因为**去处不同**：前者回②勾上，后者回①勾番。
        """
        n = int(st.get("theme", 0))
        total = st.get("theme_total")
        if total is None or int(total) == n:
            return f"曲目 {n}"
        gap = int(total) - n
        unchecked = int(st.get("theme_unchecked", 0))
        by_anime = int(st.get("theme_anime_excluded", 0))
        why: list[str] = []
        if unchecked:
            why.append(f"{unchecked} 条在②取消勾选")
        if by_anime:
            # 与第②③步统计条的措辞保持一致（那边写的是「其中 N 条的番在第①步被排除」）
            why.append(f"{by_anime} 条的番在①被排除")
        if not why:
            # 手工拼出来的 stats（测试/旧调用方）算不出原因时**只说实话**，
            # 不编一个来源：差额确实都是「没勾上」，这是能确定的。
            why.append(f"{gap} 条未勾选")
        return f"曲目 {n}（共 {total}：{' · '.join(why)}）"

    # ---------------------------------------------------------------- 导航
    def _goto(self, i: int) -> None:
        if self._closing or (self.worker is not None and self.worker.isRunning()):
            return
        i = max(0, min(len(self.pages) - 1, i))
        self.stack.setCurrentIndex(i)
        self.stepper.set_current(i)
        page = self.pages[i]
        page.back_btn.setEnabled(i > 0)
        for j in range(len(self.pages)):
            if j < i:
                self.stepper.set_state(j, "confirmed")
        self.log(f"→ 步骤 {i + 1}：{page.__class__.__name__}")
        self._log_step_scope(i)

    def _log_step_scope(self, i: int) -> None:
        """进入每一步时说清「这一步现在的实际情况」与「它管什么」。

        为什么要有：第①步与第②步各自有一个勾选框（「纳入」/「收录」），
        用户实测时看不出第②步在管什么、也不知道它跟第①步什么关系（"显得意义不明"）。
        这几行把口径摊开，而且是**从当前行里数出来的**（不数流程计数器）。

        口径：第①步管"哪些番"，第②步管"哪些曲"；两处的排除都会影响③④，
        但**排除不等于删除** —— 数据一直留着，勾回来就恢复。
        """
        if i == 0:
            rows = self.pages[0].all_rows()
            inc = [r for r in rows if r.get("included")]
            self.log(f"    第①步只管「哪些番」：共 {len(rows)} 部 · 纳入 {len(inc)} 部"
                     f"（这个选择决定第③步会出现哪些番）")
        elif i == 1:
            rows = self.pages[1].all_rows()
            inc = [r for r in rows if r.get("included")]
            off = [r for r in rows if not r.get("included")]
            by_anime = [r for r in off if not r.get("anime_included", True)]
            manual = [r for r in rows if r.get("origin") == "manual"]
            self.log(f"    第②步只管「哪些曲」：共 {len(rows)} 条 · 收录 {len(inc)} 条 · "
                     f"已排除 {len(off)} 条"
                     + (f"（其中 {len(by_anime)} 条是因为它的番在第①步被排除了）"
                        if by_anime else "")
                     + f" · 手工补充 {len(manual)} 条")
            self.log("    这里取消「收录」的曲目不会进入匹配与推送；手工改过的行重跑拉取不会被覆盖。")
        elif i == 2:
            rows = self.pages[2].all_rows()
            todo = [r for r in rows if r.get("status") in ("auto", "pending_review")]
            self.log(f"    第③步「确认匹配」：共 {len(rows)} 条 · 待你过目的 {len(todo)} 条"
                     f"（🟢自动通过的不必逐条看；拿不准可以点候选或「都不是，跳过此曲」）")
        elif i == 3:
            rows = self.pages[3].all_rows()
            inc = [r for r in rows if r.get("included")]
            self.log(f"    第④步「推送」：可推 {len(rows)} 条 · 已勾选 {len(inc)} 条"
                     f"（只推勾选的；每行的「目标歌单」可以让它们进不同歌单）")

    def _next(self) -> None:
        if self._closing or (self.worker is not None and self.worker.isRunning()):
            return
        i = self.stack.currentIndex()
        # ← 先把本步的人工修改落库，再往下走（手工值优先于拉取值，§8.2）
        #
        # ⚠️ 每一步都读 `page.all_rows()`（**全量**），不是 `model.rows` 里"看得见的"。
        # 界面上现在有四页的「筛选」；如果落库跟着筛选走，那么筛着「已排除」
        # 点下一步，其余番剧的编辑就会被**静默丢掉**。筛选是显示层的事，
        # 落库必须永远是全量 —— `all_rows()` 存在的唯一理由就是把这件事写成代码。
        try:
            if i == 0:
                self.session.save_anime(self.pages[0].all_rows())
                self.log("番单保存完成（含纳入选择）")
            elif i == 1:
                self.session.save_themes(self.pages[1].all_rows())
            elif i == 2:
                stat = self.session.save_decisions(self.pages[2].all_rows())
                self._report_learned(stat)
            elif i == 3:
                # 第④步的勾选/每行歌单**曾经从不落库** —— 用户在最后一步
                # 精挑细选完，点「完成」，然后那些选择原地蒸发（推送读的是
                # 数据库里的决策，不是表格）。所以这里必须存 ——
                # 但**存的是 push_selection**，不是 decision。
                #
                # ⚠️ 这里原来调的是 `save_decisions()`，那是第③步的 API：
                # 它把行里的 `status` 直接写进 `decision.status`，而推送页那一列
                # 是「待推送」这种给人看的文案 → 撞 CHECK 约束 →
                # `IntegrityError`，第④步的保存从第一次点击起就必失败。
                sel = self.session.save_push_selection(self.pages[3].all_rows())
                self.log(
                    f"已保存本步选择：{sel['saved']} 行（勾选 {sel['checked']}）"
                    + (f"，{sel['unsaved']} 行因缺网易云 ID 未保存" if sel["unsaved"] else "")
                )
        except Exception as e:  # noqa: BLE001
            self.log(f"✗ 保存本步修改失败：{type(e).__name__}: {e}")
            QMessageBox.warning(self, "保存失败", f"{type(e).__name__}: {e}")
            return

        self.stepper.set_state(i, "confirmed")
        if i < len(self.pages) - 1:
            self._goto(i + 1)
            self.reload(preserve_included=False)
        else:
            self.log("向导流程完成。")
            QMessageBox.information(self, "完成", self._completion_text())

    def _completion_text(self) -> str:
        """第④步「完成」时如实说明状态。

        ⚠️ 这里原来写的是「写入歌单（M3）尚未接入」—— 写入早就真机跑通了
        （`favorite` 动作 + 幂等，服务端 `trackIds` 核对过，见 DESIGN.md G.15）。
        对着一个能用的功能说「尚未接入」，会让用户绕远路去手动导入。
        现在改成按推送台账说实话，同时保留 reports/ 兜底那条信息。
        """
        rows = self.pages[3].all_rows()
        pushed = len([r for r in rows if r.get("status") in ("已推送", "已存在")])
        head = (
            f"四步都走完了，已经推送 {pushed} 首。\n\n"
            "写入歌单走的是网易云客户端的真实写入（幂等：已在歌单里的会跳过）。\n"
            if pushed else
            "四步都走完了，本次还没有推送成功任何歌曲。\n\n"
            "歌单写入需要客户端在线：回到本步点「推送到歌单」即可，"
            "失败的单首会在日志和表格里写明原因。\n"
        )
        return (
            head
            + "\n兜底（推送失败或想手工核对时用）：\n"
            "· reports/<季度>_import.txt —— 导入清单\n"
            "· reports/<季度>_match_missing.md —— 没匹配上的曲目\n"
            "两者都还在，推送没成功时可以照旧在客户端里手动导入。"
        )

    def _report_learned(self, stat: dict | None) -> None:
        """把别名学习与**落库情况**告诉用户 —— 这是「越用越准」的正反馈。

        ⚠️ `skipped` 必须显示出来：它代表用户在这一步做的选择**没有被保存**。
        静默丢掉用户的点击，比报错更糟（他以为存了）。
        """
        if not stat:
            return
        for tid, raw in (stat.get("skipped") or [])[:5]:
            self.log(f"⚠ 有一条曲目的状态无法落库，已跳过：{raw!r}（theme {tid}）")
        if len(stat.get("skipped") or []) > 5:
            self.log(f"   （共 {len(stat['skipped'])} 条被跳过）")
        if stat.get("error"):
            self.log(f"（别名学习跳过：{stat['error']}）")
            return
        n = stat.get("title", 0) + stat.get("artist", 0) + stat.get("anime", 0)
        if n:
            self.log(
                f"🧠 学到 {n} 条别名（曲名 {stat.get('title', 0)} · "
                f"艺人 {stat.get('artist', 0)} · 番剧 {stat.get('anime', 0)}）"
                f"→ {self.session.aliases.path}"
            )
            self.log("   以后这些组合会直接自动通过，不需要再确认。")

    def _back(self) -> None:
        i = self.stack.currentIndex()
        if i > 0:
            self.stepper.set_state(i - 1, "dirty")
            self.stepper.set_state(i, "pending")
            self._goto(i - 1)
            self.log("↑ 回退。下游步骤已标记为「需重跑」，但数据保留（不清空）。")

    def _active_workers(self):
        candidates = (self.worker, getattr(self.env, "_worker", None), getattr(self.env, "_launch_worker", None))
        return [w for w in candidates if w is not None and w.isRunning()]

    @Slot()
    def _finish_close_when_ready(self) -> None:
        if self._closing and not self._active_workers():
            self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closing = True
        self.env._closing = True
        self._timer.stop()
        self._busy(False)
        active = self._active_workers()
        if active:
            event.ignore()
            self.status.showMessage("正在等待当前操作完成，随后自动关闭…")
            for worker in active:
                if hasattr(worker, "request_stop"):
                    worker.request_stop()
                else:
                    worker.requestInterruption()
                if not getattr(worker, "_close_connected", False):
                    worker.finished.connect(self._finish_close_when_ready)
                    worker._close_connected = True
            return
        self.session.close()
        event.accept()
