"""向导的四个步骤页面。"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from . import style
from .session import STATUS_LABEL
from .widgets import CandidateItem, Column, DictTableModel, DictTableView
from ..models import SeasonBucket


def _btn(text: str, kind: str = "normal", tip: str = "") -> QPushButton:
    b = QPushButton(text)
    if kind == "primary":
        b.setObjectName("Primary")
    elif kind == "ghost":
        b.setObjectName("Ghost")
    if tip:
        b.setToolTip(tip)
    return b


class BasePage(QWidget):
    """每步的公共骨架：工具栏 / 筛选 / 主体 / 底部导航。"""

    log = Signal(str)
    next_requested = Signal()
    back_requested = Signal()

    def __init__(self, index: int, title: str, subtitle: str, columns: list[Column], parent=None):
        super().__init__(parent)
        self.index = index
        self.columns = columns

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # ---- 标题行
        head = QHBoxLayout()
        t = QLabel(title)
        t.setObjectName("H1")
        head.addWidget(t)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("Muted")
        head.addWidget(self.subtitle)
        head.addStretch()
        self.head_extra = QHBoxLayout()
        head.addLayout(self.head_extra)
        root.addLayout(head)

        # ---- 工具栏
        self.toolbar_frame = QFrame()
        self.toolbar_frame.setObjectName("Toolbar")
        tb = QHBoxLayout(self.toolbar_frame)
        tb.setContentsMargins(8, 6, 8, 6)
        tb.setSpacing(6)

        self.actions_layout = QHBoxLayout()
        self.actions_layout.setSpacing(6)
        tb.addLayout(self.actions_layout)
        tb.addStretch()

        self.filter_combo = QComboBox()
        self.filter_combo.setFixedWidth(140)
        tb.addWidget(QLabel("筛选"))
        tb.addWidget(self.filter_combo)

        self.badge = QLabel("")
        self.badge.setStyleSheet(f"color: {style.WARN}; font-weight: 600;")
        tb.addWidget(self.badge)
        root.addWidget(self.toolbar_frame)

        # ---- 筛选状态：**一个状态、一个入口**
        #
        # 踩过（用户报的）：工具栏按钮和这个下拉框各管一半 —— 按钮改了表格却不改
        # 下拉框，而下拉框显示的文本没变，于是 `currentTextChanged` **根本不发**，
        # 用户在框里再选一次「全部」毫无反应，卡在筛选结果里出不来。
        # 现在两边都只能通过 `_apply_filter()` 改视图，并且它负责把框同步过去。
        self._filters: dict[str, Callable[[dict], bool] | None] = {self.FILTER_ALL: None}
        self._view: str = self.FILTER_ALL
        self.filter_combo.currentTextChanged.connect(self._on_filter_changed)

        # ---- 主体
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        root.addLayout(self.body, 1)

        # ---- 底部导航
        foot = QHBoxLayout()
        self.back_btn = _btn("← 上一步", "ghost")
        self.back_btn.clicked.connect(self.back_requested.emit)
        foot.addWidget(self.back_btn)

        self.foot_hint = QLabel("")
        self.foot_hint.setObjectName("Dim")
        foot.addWidget(self.foot_hint)
        foot.addStretch()

        self.stat = QLabel("")
        self.stat.setObjectName("Muted")
        foot.addWidget(self.stat)

        self.next_btn = _btn("确认并下一步 →", "primary")
        self.next_btn.clicked.connect(self.next_requested.emit)
        foot.addWidget(self.next_btn)
        root.addLayout(foot)

    # ---- 工具方法
    def add_action(
        self,
        text: str,
        slot: Callable[[], None],
        kind: str = "normal",
        tip: str = "",
        index: int | None = None,
    ) -> QPushButton:
        """加一个工具栏按钮。

        `index` 用于把「主操作」插到最前面 —— MainWindow 接入真实动作时
        需要拉取/匹配按钮排在全选之类的前面。
        """
        b = _btn(text, kind, tip)
        b.clicked.connect(slot)
        if index is None:
            self.actions_layout.addWidget(b)
        else:
            self.actions_layout.insertWidget(index, b)
        return b

    def add_widget(self, w: QWidget) -> None:
        self.body.addWidget(w, 1)

    # ---------------------------------------------------------------- 筛选

    #: 「不过滤」那一项的名字。四个页面都用它。
    FILTER_ALL = "全部"

    def set_filter_spec(self, spec: dict[str, Callable[[dict], bool] | None]) -> None:
        """设置筛选项：`{显示名: 判定函数}`，`None` = 不过滤（必须有一个）。

        为什么不再用 `set_filters(["全部", "仅 OP", …])` 那种**字符串列表**：
        字符串和任何判据都没有绑定关系，于是「接错了」的表现就是「点了没反应」——
        而这个界面里 ①②④ 的筛选框**正是这么变成纯装饰品的**：
        它们只往日志里打一行「筛选：仅 OP」，表格一行都不动（四个页面里只有一个
        是真会筛的）。现在每个名字都必须带一个判定函数，写不出来就说明它不该在这儿。
        """
        if not spec or None not in spec.values():
            raise ValueError("筛选表必须包含一个判定为 None 的「全部」入口")
        self._filters = {str(k): v for k, v in spec.items()}
        self.filter_combo.blockSignals(True)
        self.filter_combo.clear()
        self.filter_combo.addItems(list(self._filters))
        self.filter_combo.blockSignals(False)
        if self._view not in self._filters:
            self._view = next(iter(self._filters))
        self._apply_filter(self._view, log=False)

    def _on_filter_changed(self, text: str) -> None:
        self._apply_filter(text)

    def _apply_filter(self, name: str, *, log: bool = True) -> None:
        """切到某个筛选视图 —— **唯一**入口（工具栏按钮也必须走这里）。

        ⚠️ 按钮**不能**只做 `filter_combo.setCurrentText(...)`：文本本来就等于
        目标值时 Qt **不发** `currentTextChanged`，于是「按钮改了视图、框没变」
        或者「框没变、什么都没发生」——用户看到的是一条回不去的死路。

        实现方式是**隐藏行**，不是重建一个只含匹配行的表：
        这样 `model.rows` 永远是**全量**，落库 / 推送 / 统计读它天然就是对的。
        反过来说，如果这里用「set_rows(筛选后的子集)」，那么第④步的
        `checked_push_rows()` 与四步的保存都会**静默只处理看得见的那些歌**——
        筛选就从「显示」变成了「改数据」。
        """
        if name not in self._filters:
            name = self.FILTER_ALL if self.FILTER_ALL in self._filters else next(iter(self._filters))
        self._view = name

        self.filter_combo.blockSignals(True)
        self.filter_combo.setCurrentText(name)
        self.filter_combo.blockSignals(False)

        pred = self._filters.get(name)
        shown = 0
        table = getattr(self, "table", None)
        for i, row in enumerate(self.model.rows):
            hit = True if pred is None else bool(pred(row))
            if table is not None and table.model() is not None:
                table.setRowHidden(i, not hit)
            shown += int(hit)

        self._after_filter()
        if log:
            total = len(self.model.rows)
            self.log.emit(
                f"筛选「{name}」：显示 {shown} / {total} 行"
                if shown != total else f"筛选「{name}」：{total} 行"
            )
            if shown == 0 and total:
                self.log.emit(
                    f"  注意：这里 0 行只是**筛选**的结果，数据一条都没少（共 {total} 行）。"
                    "把筛选切回「全部」即可看到。")

    def _after_filter(self) -> None:
        """筛选后要做的收尾（子类覆盖：例如重建候选面板、选中第一行）。"""
        self._refresh()

    def visible_rows(self) -> list[dict]:
        """当前**看得见**的行（受筛选影响）。

        用途：批量操作（全选 / 反选）—— 用户按下「全选」时想的是眼前这一屏。
        落库与推送**必须**用 `all_rows()`，不能用这个。
        """
        table = getattr(self, "table", None)
        if table is None or table.model() is None:
            return list(self.model.rows)
        return [r for i, r in enumerate(self.model.rows) if not table.isRowHidden(i)]

    def all_rows(self) -> list[dict]:
        """**全量**行（不看筛选）—— 落库、推送、统计只认这一份。

        存在的意义是把这个约定变成代码里的一个显式名字：
        筛选是「显示」，不是「数据」。
        """
        return self.model.rows

    def _scope_hint(self) -> str:
        """批量操作日志里那句「这次动的是哪一拨」。"""
        visible, total = len(self.visible_rows()), len(self.model.rows)
        if visible == total:
            return ""
        return f"（当前筛选「{self._view}」只看得到 {visible} / {total} 行）"

    def _emit_all_changed(self) -> None:
        """整表内容变了但**行数没变**时用它。

        Qt 里 `layoutChanged` 是「行数/结构变了」的信号，用它刷内容
        会让视图重置选中行和滚动位置 —— 表现为「点一下就跳」。
        只改单元格内容必须用 `dataChanged`。
        """
        n, c = self.model.rowCount(), self.model.columnCount()
        if n and c:
            self.model.dataChanged.emit(
                self.model.index(0, 0), self.model.index(n - 1, c - 1)
            )

    def _refresh(self) -> None:
        """统计条的刷新（子类覆盖）。"""

    def set_data(self, rows: list[dict]) -> None:
        """整体替换本页数据 —— 重新拉取/匹配之后由 MainWindow 调用。

        传进来的必须是**全量**（`model.rows` 就是全量，见 `_apply_filter`）。
        筛选状态**跨刷新保留**：用户筛了「需要处理」，后台任务回来后不该被踢回
        「全部」（那样他会以为数据变了）。
        """
        self.model.set_rows(rows)
        self._apply_filter(self._view, log=False)

    def make_table(self, model: DictTableModel) -> DictTableView:
        tv = DictTableView(self.columns)
        tv.set_model(model)
        tv.log.connect(self.log.emit)
        # 表格自己也能增删行（右键菜单 / Delete 键）。行号会整体位移，
        # 而「哪些行被筛选藏起来」是按行号记的 —— 所以必须重算，
        # 否则删掉一行之后会藏错行（本该看得见的被藏着，或反过来）。
        tv.rows_changed.connect(lambda: self._apply_filter(self._view, log=False))
        return tv


# ==================================================================== ① 番单

class AnimePage(BasePage):
    def __init__(self, rows: list[dict], parent=None):
        cols = [
            Column("included", "纳入", 52, "check", align=Qt.AlignCenter),
            Column("title_cn", "中文名", 300, "text", editable=True, stretch=True),
            Column("title_jp", "日文原名", 240, "text", editable=True),
            Column("air_date", "首播日", 96, "text", editable=True, align=Qt.AlignCenter),
            Column("bucket_label", "季度归属", 80, "readonly", align=Qt.AlignCenter),
            Column("origin_label", "来源", 88, "readonly", align=Qt.AlignCenter),
            Column("note", "备注", 240, "text", editable=True),
        ]
        super().__init__(0, "① 番单确认", "来自 Bangumi「在看」", cols, parent)
        self.next_btn.setText("确认番单，下一步 →")

        self.model = DictTableModel(cols, rows)
        self.table = self.make_table(self.model)
        self.add_widget(self.table)

        self.add_action("全选", lambda: self._set_all(True))
        self.add_action("反选", self._invert)
        self.add_action("+ 新增番剧", self._add, "primary", "手动补充 Bangumi 上漏标的番")

        self.set_filter_spec({
            self.FILTER_ALL: None,
            "仅当季": lambda r: r.get("bucket") == str(SeasonBucket.TARGET),
            "跨季 + 当季": lambda r: r.get("bucket") in (
                str(SeasonBucket.TARGET), str(SeasonBucket.CROSS_SEASON)),
            "已排除": lambda r: not r.get("included"),
        })
        self._refresh()

    def _set_all(self, v: bool) -> None:
        # 作用域 = **当前看得见的行**（用户按下「全选」时想的是眼前这一屏），
        # 并把这件事打进日志 —— 不然筛选开着的时候「全选」到底动了多少行没人知道。
        rows = self.visible_rows()
        for r in rows:
            r["included"] = v
        self._emit_all_changed()
        self.log.emit(f"全部{'纳入' if v else '排除'}：当前显示 {len(rows)} 行{self._scope_hint()}")
        self._apply_filter(self._view, log=False)

    def _invert(self) -> None:
        rows = self.visible_rows()
        for r in rows:
            r["included"] = not r.get("included")
        self._emit_all_changed()
        self.log.emit(f"已反选：当前显示 {len(rows)} 行{self._scope_hint()}")
        self._apply_filter(self._view, log=False)

    def _add(self) -> None:
        r = self.model.add_row(
            {
                "included": True, "title_cn": "", "title_jp": "", "air_date": "",
                "bucket": "unknown", "bucket_label": "未知", "origin": "manual",
                "origin_label": "手工", "note": "", "bgm_id": "",
            }
        )
        self.log.emit(f"新增第 {r + 1} 行 —— 请填写中文名（手工数据不会被重跑覆盖）")
        self.table.scrollToBottom()
        self.table.edit(self.model.index(r, 1))
        self._apply_filter(self._view, log=False)

    def _refresh(self) -> None:
        # 统计条一律是**全量**口径（筛选只影响看到哪几行，不影响这些事实）
        inc = [r for r in self.all_rows() if r.get("included")]
        man = [r for r in self.all_rows() if r.get("origin") == "manual"]
        self.stat.setText(f"共 {len(self.model.rows)} 部 · 纳入 {len(inc)} · 手工 {len(man)}")
        self.badge.setText("")


# ==================================================================== ② 曲目

class ThemePage(BasePage):
    def __init__(self, rows: list[dict], missing: list[str], parent=None):
        cols = [
            Column("included", "收录", 52, "check", align=Qt.AlignCenter),
            Column("anime", "番剧", 230, "readonly", stretch=True),
            Column("kind", "类型", 60, "choice", editable=True, choices=["OP", "ED"], align=Qt.AlignCenter),
            Column("seq", "序号", 52, "text", editable=True, align=Qt.AlignCenter),
            Column("song_title", "曲名", 240, "text", editable=True),
            Column("artists", "艺人", 180, "text", editable=True),
            Column("origin_label", "来源", 66, "readonly", align=Qt.AlignCenter),
            Column("note", "备注", 220, "text", editable=True),
        ]
        super().__init__(1, "② OP/ED 曲目确认", "来自 AnimeThemes", cols, parent)
        self.next_btn.setText("确认曲目，下一步 →")
        self.missing = missing

        self.model = DictTableModel(cols, rows)
        self.table = self.make_table(self.model)
        self.add_widget(self.table)

        self.add_action("全选", lambda: self._set_all(True))
        self.add_action("+ 新增曲目", self._add, "primary", "数据源未收录时手动补充")
        self.add_action("查看未收录的番", self._show_missing)

        self.set_filter_spec({
            self.FILTER_ALL: None,
            "仅 OP": lambda r: r.get("kind") == "OP",
            "仅 ED": lambda r: r.get("kind") == "ED",
            # 「已排除」= 这一行没打勾。两种原因都算：曲目自己取消了收录，
            # 或者它的番在第①步被排除了（那样勾选框也不该是勾着的）。
            "已排除": lambda r: not r.get("included"),
            "手工补充": lambda r: r.get("origin") == "manual",
        })
        self._refresh()

    def _set_all(self, v: bool) -> None:
        rows = self.visible_rows()
        for r in rows:
            r["included"] = v
        self._emit_all_changed()
        self.log.emit(f"全部{'收录' if v else '排除'}：当前显示 {len(rows)} 行{self._scope_hint()}")

    def _add(self) -> None:
        r = self.model.add_row(
            {
                "included": True, "anime": "", "kind": "OP", "seq": 1,
                "song_title": "", "artists": "", "origin": "manual",
                "origin_label": "手工", "note": "",
            }
        )
        self.log.emit(f"新增第 {r + 1} 行 —— 请选择番剧并填写曲名")
        self.table.scrollToBottom()
        self.table.edit(self.model.index(r, 1))
        self._apply_filter(self._view, log=False)

    def _show_missing(self) -> None:
        """列出「一条 OP/ED 都没有的番」。

        ⚠️ 这件事**没法做成一个筛选项**：本表的一行 = 一条曲目，而「未收录」
        说的是**番剧**（它连一行都产生不了）。所以筛选框里没有它，
        它是这里的一个动作 —— 原来筛选框里那个「数据源未收录」是假的。
        """
        if self.missing:
            self.log.emit("AnimeThemes 未收录：" + "、".join(self.missing))
        else:
            self.log.emit("没有未收录的番")

    def _refresh(self) -> None:
        # 统计条一律是**全量**口径（筛选只影响看到哪几行，不影响这些事实）。
        # 「已排除」再拆一层：有多少是因为**它的番在第①步被排除**（那种行不能
        # 靠在这里打勾救回来，得回第①步把番勾上）—— 不拆的话用户会以为
        # "我在这儿勾了却没反应"。
        all_rows = self.all_rows()
        inc = [r for r in all_rows if r.get("included")]
        ops = [r for r in inc if r["kind"] == "OP"]
        eds = [r for r in inc if r["kind"] == "ED"]
        off = [r for r in all_rows if not r.get("included")]
        by_anime = [r for r in off if not r.get("anime_included", True)]
        self.stat.setText(
            f"OP {len(ops)} · ED {len(eds)} · 合计 {len(inc)} 条"
            + (f"（另 {len(off)} 条已排除" +
               (f"，其中 {len(by_anime)} 条的番在第①步被排除）" if by_anime else "）")
               if off else "")
        )
        if self.missing:
            self.badge.setText(f"⚠ {len(self.missing)} 部番数据源未收录")
        else:
            self.badge.setText("")


# ==================================================================== ③ 匹配

#: 「需要处理」的判据 —— **筛选与徽标共用这一个函数**。
#:
#: 为什么必须共用：徽标写着「⚠ 3 条需要处理」，点进去却看见 4 条（或反过来），
#: 用户就再也不信这两个数字里的任何一个。而它们本来就是同一个问题。
#:
#: `skipped` **不算**需要处理：那是用户自己的决定，不是待办（见 HANDOFF 的
#: 「第③步要加显式跳过动作」）。它单独计数，免得隐身。
def _needs_attention(r: dict) -> bool:
    s = str(r.get("status") or "")
    return s in ("pending_review", "low", "pending") or s.startswith("missing")


def _low_or_missing(r: dict) -> bool:
    """「低分 / 未匹配」= 需要处理里去掉 pending_review 那一档。"""
    s = str(r.get("status") or "")
    return s in ("low", "pending") or s.startswith("missing")


class MatchPage(BasePage):
    def __init__(self, rows: list[dict], parent=None):
        cols = [
            Column("anime", "番剧", 210, "readonly", stretch=True),
            Column("kind", "类型", 52, "readonly", align=Qt.AlignCenter),
            Column("theme_title", "目标曲目", 190, "readonly"),
            Column("theme_artists", "目标艺人", 130, "readonly"),
            Column("chosen_name", "选中歌曲", 230, "text", editable=True),
            Column("chosen_artists", "选中艺人", 140, "readonly"),
            Column("score", "置信度", 68, "score", align=Qt.AlignCenter),
            Column("status_label", "状态", 132, "status", align=Qt.AlignCenter),
            Column("note", "备注", 190, "text", editable=True),
        ]
        super().__init__(2, "③ 网易云匹配确认", "自动结果已选中，你只需处理异常项", cols, parent)
        self.next_btn.setText("确认匹配，下一步 →")

        self.model = DictTableModel(cols, rows)
        for r in self.model.rows:
            r["_checked"] = r["status"] in ("auto", "pending_review")
        self._shown_row: int | None = None   # 避免重复重建候选面板
        self.cand_items: list = []           # 当前候选卡片，用于原地更新选中态

        split = QSplitter(Qt.Vertical)

        self.table = self.make_table(self.model)
        self.table.selectionModel().selectionChanged.connect(self._on_select)
        split.addWidget(self.table)

        # ---- 回退：**一条**历史栈，覆盖「点候选卡片」和「单元格编辑」两类动作
        #
        # 为什么需要它：候选卡片一击即定稿（`_pick` 直接写 confirmed + manual），
        # 而 `confirmed` 正是进第④步推送集的通行证（`session._match_rows` 只放
        # auto/confirmed 进去）。用户浏览候选时误点一下，那首歌就**既成事实**了 ——
        # 没有二次确认，也没有退路。
        # 更重的一层：`manual=True` 保存时会变成 `decided_by='human'`，
        # 被 `learn_from_season` 学进 `config/aliases.yaml` → 误点会**永久教坏别名表**。
        self._history: list[dict] = []
        self._history_limit = 32
        #: 「改完之后请把这一行还给我」的暗号（见 `_after_filter`）
        self._pending_focus: dict | None = None
        self.model.set_edit_hook(self._on_cell_edit)
        self.table.set_undo_handler(self._undo_last)
        self.table.set_context_menu_builder(self._menu_actions)
        # 页面级 Ctrl+Z，上下文用**默认的 WindowShortcut**。
        #
        # ⚠️ 这里踩过一次，是**实测**踩的，不是推理出来的（`.tmp/probe_shortcut2.py`）：
        #   · 一开始用的 `WidgetWithChildrenShortcut` —— 它要求**焦点**落在本页的
        #     子控件上。而用户点完候选卡片之后焦点可能在按钮/面板上，甚至没有焦点
        #     控件，于是按 Ctrl+Z **一次都不触发**（日志里什么都不打，看起来像死键）。
        #   · 换成 WindowShortcut 之后正常。而「多条同键快捷键会不会被判成歧义」
        #     也实测了：窗口里同时有 8 条启用的 Ctrl+Z（四页表格各 2 条），
        #     在第①步按 Ctrl+Z，生效的**只有当前可见那一页**的表格撤销 ——
        #     隐藏页面的快捷键不参与匹配，所以不存在歧义。
        # 本页表格自带的那 2 条已由 `set_undo_handler` 关掉，因此本页只剩这 2 条。
        for _seq in (QKeySequence.Undo, QKeySequence("Ctrl+Shift+Z")):
            QShortcut(_seq, self, activated=self._undo_last)

        # ---- 候选区
        cand_wrap = QFrame()
        cand_wrap.setObjectName("Card")
        cl = QVBoxLayout(cand_wrap)
        cl.setContentsMargins(8, 6, 8, 8)
        cl.setSpacing(6)

        ch = QHBoxLayout()
        self.cand_title = QLabel("候选")
        self.cand_title.setObjectName("SectionTitle")
        ch.addWidget(self.cand_title)

        # 「候选都不对」的归宿。原来没有任何入口 —— 用户只能挑一首错的凑合，
        # 或者把它永远留在「待审核」里。`DecisionStatus.SKIPPED` 在库里、映射表里、
        # 徽标计数里早就就位，只差这一个按钮（HANDOFF 待办 P1-11）。
        b_skip = _btn(
            "都不是，跳过此曲", "ghost",
            "当前选中这一行的候选里没有正确的 → 标记「已跳过」。\n"
            "它不会进第④步，也不算「需要处理」；点「↶ 撤销」可以退回。",
        )
        b_skip.clicked.connect(self._skip_current)
        ch.addWidget(b_skip)
        ch.addStretch()

        self.manual_input = QLineEdit()
        self.manual_input.setPlaceholderText("粘贴网易云链接或歌曲 ID，如 https://music.163.com/song?id=1375305989")
        self.manual_input.setMinimumWidth(360)
        ch.addWidget(self.manual_input)
        b_manual = _btn("手动指定", "primary")
        b_manual.clicked.connect(self._manual_pick)
        ch.addWidget(b_manual)
        cl.addLayout(ch)

        self.cand_scroll = QScrollArea()
        self.cand_scroll.setWidgetResizable(True)
        self.cand_scroll.setFrameShape(QFrame.NoFrame)
        self.cand_host = QWidget()
        self.cand_layout = QVBoxLayout(self.cand_host)
        self.cand_layout.setContentsMargins(0, 0, 0, 0)
        self.cand_layout.setSpacing(6)
        self.cand_layout.addStretch()
        self.cand_scroll.setWidget(self.cand_host)
        cl.addWidget(self.cand_scroll, 1)
        split.addWidget(cand_wrap)
        split.setSizes([420, 300])
        # ⚠️ 这两个最小高度是**整窗能不能拖日志框**的关键（用户真机报的
        # 「窗口缩小之后日志框拖不动」）：
        # `QStackedWidget` 的最小高度取**所有页里最大的那个**，而这两行原来各写 200
        # ⇒ 第③步的 minimumSizeHint 变成 514（①②④ 只有 180）⇒ 主分隔器最小需要
        # 514 + 日志框 56 = 570px。窗口一旦缩到比这还矮，分隔器比窗口还高，
        # 日志框和分隔条被挤出可视区，拖动自然"没反应"（实测：窗口高 ≤800 就拖不动）。
        # 降到 120/130 之后，页面栈最小约 364，主分隔器最小约 420 —— 小窗口下也拖得动。
        self.table.setMinimumHeight(120)
        cand_wrap.setMinimumHeight(130)
        self.add_widget(split)

        self.add_action("全部采用自动结果", self._accept_all, "primary")
        # 撤销按钮排在这里：它跟「全部采用」是一对 —— 一次批量采用之后，
        # 最常见的下一个动作就是「哎呀点早了，退回去」
        self.undo_btn = self.add_action(
            "↶ 撤销", self._undo_last,
            tip="撤销上一次改动（点候选 / 手工指定 / 全部采用 / 跳过 / 改单元格）。Ctrl+Z",
        )
        self.undo_btn.setEnabled(False)
        self.add_action("只看待审核", lambda: self._apply_filter("需要处理"))
        self.add_action("显示全部", lambda: self._apply_filter(self.FILTER_ALL))

        # ⚠️ 「需要处理」是与**徽标同一个判据**（`_needs_attention`）的筛选项。
        # 原来这里是「全部 / 待审核 / 低分/未匹配 / 仅自动」，而「待审核」只含
        # pending_review —— 与工具栏「只看待审核」按钮（含低分/缺失/待匹配）**同名不同义**，
        # 于是「徽标写 3 条、点进去看见 4 条」，两个数字都不可信了。
        self.set_filter_spec({
            self.FILTER_ALL: None,
            "需要处理": _needs_attention,
            "低分 / 未匹配": _low_or_missing,
            "仅自动": lambda r: r.get("status") in ("auto", "confirmed"),
        })
        self._refresh()

    # ---- 表格交互
    def _on_select(self, *_):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        self._show_candidates(self.model.rows[rows[0].row()])

    def _show_candidates(self, row: dict, force: bool = False) -> None:
        # 同一行重复选中就不用重建（实测重建一次约 27ms，键盘上下切换时会累积成卡顿）
        rid = id(row)
        if not force and rid == self._shown_row and self.cand_layout.count() > 1:
            return
        self._shown_row = rid

        while self.cand_layout.count() > 1:
            it = self.cand_layout.takeAt(0)
            w = it.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self.cand_items = []

        cands = row.get("candidates") or []
        if not row:
            # 空表（例如刚换季度、或还没跑匹配）—— 面板给个中性提示，
            # **不能直接取 row['anime']**，否则 KeyError 会把整个界面构造打断。
            #
            # 还要区分「真的一条都没有」和「只是被筛选藏起来了」：
            # 后者如果只提示「请在上方选中一行」，用户会以为数据没了。
            total = len(self.all_rows())
            if total and self._view != self.FILTER_ALL:
                self.cand_title.setText("候选 ——（当前筛选下没有曲目）")
                hint = QLabel(
                    f"筛选「{self._view}」下一条也没有，但数据没丢：一共 {total} 条。\n"
                    f"把右上角的筛选切回「{self.FILTER_ALL}」就能看到。"
                )
            else:
                self.cand_title.setText("候选 ——（请在上方选中一行）")
                hint = QLabel("选中上方任意一行，这里会列出它的候选歌曲。")
            hint.setStyleSheet(f"color: {style.TEXT_MUTED}; padding: 12px;")
            self.cand_layout.insertWidget(0, hint)
            return

        self.cand_title.setText(
            f"候选 —— {row.get('anime', '?')} · {row.get('kind', '')} · "
            f"{row.get('theme_title', '')}"
            f"（{len(cands)} 条，目标艺人：{row.get('theme_artists', '')}）"
        )
        if not cands:
            hint = QLabel(
                "没有候选。\n\n"
                "· 若为「网易云未上架」→ 等发售后再重跑，或手动指定\n"
                "· 若为「数据源未收录」→ 到上一步手动补充曲目"
            )
            hint.setStyleSheet(f"color: {style.TEXT_MUTED}; padding: 12px;")
            self.cand_layout.insertWidget(0, hint)
            return

        for c in cands:
            item = CandidateItem(c, selected=(c["netease_song_id"] == row.get("chosen_id")))
            item.picked.connect(lambda cd, r=row: self._pick(r, cd))
            self.cand_layout.insertWidget(self.cand_layout.count() - 1, item)
            self.cand_items.append(item)

    def _mark_row_changed(self, row: dict) -> None:
        """只刷新这一行 —— 不用 layoutChanged，否则表格会丢滚动位置和选中行。"""
        r = self._index_of(row)
        if r is None:
            return
        self.model.dataChanged.emit(
            self.model.index(r, 0),
            self.model.index(r, self.model.columnCount() - 1),
        )

    def _sync_candidate_selection(self, chosen_id: str) -> None:
        """原地更新候选卡片的选中态，不重建控件。"""
        for it in self.cand_items:
            it.set_selected(str(it.cand.get("netease_song_id")) == str(chosen_id))

    def _pick(self, row: dict, cand: dict) -> None:
        # 重复点同一张卡片是真正的空操作 —— 不记历史，免得撤销栈被噪声塞满
        # （误点一次要按好几下 Ctrl+Z 才能退回到误点**之前**）
        if (str(row.get("chosen_id") or "") == str(cand["netease_song_id"])
                and row.get("status") == "confirmed"):
            self.log.emit(f"已是当前选中：{cand['name']}（未改动）")
            return
        self._snapshot([row], f"选定《{cand['name']}》")
        row["chosen_name"] = cand["name"]
        row["chosen_artists"] = "、".join(cand.get("artists") or [])
        row["chosen_id"] = cand["netease_song_id"]
        row["score"] = cand.get("score", 0)
        row["detail"] = cand.get("detail")
        row["status"] = "confirmed"
        row["status_label"] = "🔵 人工确认"
        row["manual"] = True

        self._mark_row_changed(row)                       # 只刷这一行
        self._sync_candidate_selection(cand["netease_song_id"])  # 只改选中态
        self._refresh()
        self.log.emit(
            f"人工选定：{row['theme_title']} → {cand['name']} / {'、'.join(cand.get('artists') or [])}"
        )

    def _manual_pick(self) -> None:
        text = self.manual_input.text().strip()
        if not text:
            self.log.emit("请先粘贴网易云链接或歌曲 ID")
            return
        import re

        m = re.search(r"[?&]id=(\d+)", text) or re.search(r"^(\d+)$", text)
        song_id = m.group(1) if m else None
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self.log.emit("请先在表格里选中一行")
            return
        if not song_id:
            self.log.emit(f"无法从 {text[:50]!r} 解析出歌曲 ID")
            return
        r = self.model.rows[rows[0].row()]
        self._snapshot([r], f"手动指定 ID {song_id}")
        r["chosen_id"] = song_id
        r["chosen_name"] = r.get("chosen_name") or "(手动指定)"
        r["status"] = "confirmed"
        r["status_label"] = "🔵 人工指定"
        r["manual"] = True
        self._mark_row_changed(r)
        self._sync_candidate_selection(song_id)
        self.log.emit(f"人工指定网易云 ID {song_id} → {r['anime']} {r['kind']}")
        self.manual_input.clear()
        self._refresh()

    # ---- 批量
    def _accept_all(self) -> None:
        """「全部采用自动结果」—— 按字面意思作用于**全量**，不是看得见的那几行。

        （批量操作作用域在这里与 ①②④ 的「全选」故意不同：那个按钮叫「全选」，
        用户想的是眼前这一屏；这个按钮叫「全部采用」，就是全部。）
        """
        n = 0
        todo = [r for r in self.all_rows() if r["status"] in ("auto", "pending_review")]
        if todo:
            # 整批算**一条**历史：一次误点「全部采用」应该一次撤销就全退回去，
            # 而不是按到手酸
            self._snapshot(todo, f"全部采用自动结果（{len(todo)} 行）")
        for r in todo:
            r["status"] = "confirmed"
            r["status_label"] = "🔵 已采用"
            n += 1
        if self.model.rowCount():
            self.model.dataChanged.emit(
                self.model.index(0, 0),
                self.model.index(self.model.rowCount() - 1, self.model.columnCount() - 1),
            )
        self.log.emit(f"已采用 {n} 条自动结果（全量）")
        # 采用完要重算视图：在「需要处理」下这批行已经处理掉了，
        # 让它们留在屏幕上会让人以为还没处理
        self._apply_filter(self._view, log=False)

    # ---- 回退：撤销 / 清除匹配 / 跳过此曲

    #: 撤销时**必须原样恢复**的字段。少恢复一个就会留下痕迹：
    #: 例如只把 `status` 退回「待审核」却留着 `manual=True`，那么下次保存
    #: `save_decisions` 仍然记成 `decided_by='human'` —— 这个被用户否掉的
    #: 对应关系照样会被 `learn_from_season` 学进别名表。
    _UNDO_FIELDS = (
        "chosen_id", "chosen_name", "chosen_artists", "chosen_duration",
        "score", "detail", "status", "status_label", "manual", "note",
    )

    def _is_live(self, row: dict) -> bool:
        """这一行还在表里吗 —— 用**身份**（`is`）判断，不是内容相等。

        换季度 / 重跑匹配之后 `set_data` 会换上全新的 dict，旧快照指向的对象
        已经不在表里；硬恢复会出现「日志说撤销了、屏幕上一点没变」。
        """
        return any(row is r for r in self.model.rows)

    def _index_of(self, row: dict) -> int | None:
        """按**身份**找行号。

        ⚠️ 不能用 `list.index(row)`：那是字典内容比较，两行内容相同就会挑错
        （第③步里同一部番的 OP/ED 行结构很像，字段少时真会撞）。
        """
        return next((i for i, r in enumerate(self.model.rows) if r is row), None)

    def _snapshot(self, rows, label: str) -> None:
        """改之前先留底。留在表里的行才记，且超上限丢最旧的。"""
        live = [r for r in rows if self._is_live(r)]
        if not live:
            return
        self._history.append({
            "kind": "rows",
            "label": label,
            "rows": [(r, {k: r.get(k) for k in self._UNDO_FIELDS}) for r in live],
        })
        del self._history[:-self._history_limit]
        self._update_undo_button()

    def _on_cell_edit(self, row: dict, key: str, old, new) -> None:
        """单元格编辑也进同一条栈 —— 两条独立的历史栈会让 Ctrl+Z 顺序错乱。"""
        self._history.append(
            {"kind": "cell", "label": f"修改「{key}」", "row": row, "key": key, "old": old}
        )
        del self._history[:-self._history_limit]
        self._update_undo_button()

    def _update_undo_button(self) -> None:
        btn = getattr(self, "undo_btn", None)   # 构造过程中可能还没有它
        if btn is None:
            return
        btn.setEnabled(bool(self._history))
        top = self._history[-1]["label"] if self._history else ""
        btn.setToolTip(f"撤销：{top}（Ctrl+Z）" if top else "没有可撤销的改动")

    def _undo_last(self) -> None:
        while self._history:
            ent = self._history.pop()
            if ent["kind"] == "cell":
                row = ent["row"]
                if not self._is_live(row):
                    continue
                self.model.restore_cell(row, ent["key"], ent["old"])
                self.log.emit(
                    f"↶ 撤销：{row.get('anime', '?')} {row.get('kind', '')} 的"
                    f"「{ent['key']}」恢复为 {ent['old']!r}"
                )
                self._update_undo_button()
                return
            live = [(r, snap) for r, snap in ent["rows"] if self._is_live(r)]
            if not live:
                continue
            for r, snap in live:
                r.update(snap)
            self._update_undo_button()
            self.log.emit(f"↶ 撤销「{ent['label']}」：")
            for r, _ in live:
                self.log.emit(
                    f"    {r.get('anime', '?')} {r.get('kind', '')} "
                    f"{r.get('theme_title', '')} → {r.get('status_label', '')}"
                    f"（{r.get('chosen_name', '')}）"
                )
            self._after_rows_changed([r for r, _ in live])
            return
        self._update_undo_button()
        self.log.emit(
            "没有可撤销的改动。撤销只覆盖**本次进入第③步以来**的改动；"
            "已经点过「下一步」（已落库）的行，用右键菜单的「清除此行的匹配」退回。"
        )

    def _after_rows_changed(self, rows: list[dict], *, focus: bool = True) -> None:
        """改完一行（或一批）之后的收尾：刷内容、同步卡片、按需重算筛选并选中。

        重算筛选是必须的：状态变了，它属于哪个筛选视图就变了。撤销之后
        **要把这一行还到眼前**（`_pending_focus`），否则用户看不出撤销生效没有。
        """
        for r in rows:
            self._mark_row_changed(r)
            if id(r) == self._shown_row:
                self._sync_candidate_selection(str(r.get("chosen_id") or ""))
        self._pending_focus = rows[0] if (focus and rows) else None
        self._apply_filter(self._view, log=False)
        self._pending_focus = None

    def _selected_row(self) -> dict | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        i = rows[0].row()
        return self.model.rows[i] if 0 <= i < len(self.model.rows) else None

    def _clear_chosen(self, row: dict) -> None:
        """把「选中了哪首」清干净。

        ⚠️ 必须**同时**清 `chosen_id` 和 `manual`：`save_decisions` 里
        `human = bool(row['manual']) or status == CONFIRMED`，而
        `learn_from_season` 只学 `decided_by='human'` **且** 有
        `chosen_candidate_id` 的决策。留一个不改，用户明确否掉的那个对应关系
        照样会被学进 `config/aliases.yaml`。
        """
        row["chosen_id"] = ""
        row["chosen_name"] = "—"
        row["chosen_artists"] = "—"
        row["chosen_duration"] = None
        row["score"] = 0.0
        row["detail"] = None
        row["manual"] = False

    def _skip_row(self, row: dict) -> None:
        """「候选里没有正确的」的归宿。

        `skipped` 不在 `_needs_attention` 里（那是用户自己的决定，不是待办），
        也不会进第④步（`_match_rows` 只放 auto/confirmed 进推送集）。
        """
        self._snapshot([row], f"跳过《{row.get('theme_title', '')}》")
        self._clear_chosen(row)
        row["status"] = "skipped"
        row["status_label"] = STATUS_LABEL["skipped"]
        self._after_rows_changed([row], focus=False)
        self.log.emit(
            f"⬜ 已跳过：{row.get('anime', '?')} {row.get('kind', '')} "
            f"《{row.get('theme_title', '')}》—— 不会进第④步；"
            "它已经不算「需要处理」，切「全部」筛选仍能看到，点「↶ 撤销」可退回。"
        )

    def _skip_current(self) -> None:
        row = self._selected_row()
        if row is None:
            self.log.emit("请先在上方表格里选中一行，再点「都不是，跳过此曲」")
            return
        self._skip_row(row)

    def _clear_match(self, row: dict) -> None:
        """事后纠正：把这一行退回「待审核」。

        什么时候需要它：撤销栈在 `set_data`（换季度 / 重跑匹配 / 逐步向前）时清空，
        所以「误点 → 下一步 → 返回」这条路上没有撤销可点 —— 那时误点已经落库了。
        """
        self._snapshot([row], f"清除《{row.get('theme_title', '')}》的匹配")
        was_manual = bool(row.get("manual"))
        self._clear_chosen(row)
        row["status"] = "pending_review" if (row.get("candidates") or []) else "missing_netease"
        row["status_label"] = STATUS_LABEL[row["status"]]
        self._after_rows_changed([row])
        self.log.emit(
            f"已清除匹配：{row.get('anime', '?')} {row.get('kind', '')} "
            f"《{row.get('theme_title', '')}》→ {row['status_label']}"
        )
        if was_manual:
            # 不假装干净（§6）：人工确认如果已经保存过，别名表里就已经留下了这次
            # 对应关系，而 `learn_from_season` 只加不删 —— 程序不替用户回收它。
            self.log.emit(
                "  ⚠️ 这一行之前是人工确认过的：若已保存过，"
                "别名表 config/aliases.yaml 可能已学到那次对应关系，这里不会回收，请核对。"
            )
        self.log.emit(
            "  （已经推送到歌单的那首**不会**被自动移除 —— 那是一次写入操作，"
            "本程序不擅自动你的歌单。）"
        )

    def _menu_actions(self, menu, row: dict, idx: int) -> dict:
        """第③步专属的右键动作（接在 `DictTableView.set_context_menu_builder` 上）。"""
        out: dict = {}
        a_undo = menu.addAction("撤销上一次改动")
        a_undo.setEnabled(bool(self._history))
        out[a_undo] = self._undo_last
        a_clear = menu.addAction("清除此行的匹配（退回待审核）")
        out[a_clear] = lambda: self._clear_match(row)
        a_skip = menu.addAction("跳过此曲（候选都不对）")
        out[a_skip] = lambda: self._skip_row(row)
        return out

    def set_data(self, rows: list[dict]) -> None:
        """换数据（换季度 / 重跑匹配 / 重载）时**清空撤销历史**。

        旧条目指向的行对象已经不在表里，留着只会让「撤销」变成一条什么都没做的
        日志。规则简单、也能对用户说清：撤销只覆盖本次进入第③步以来的改动；
        更早的误点用右键「清除此行的匹配」纠正。
        """
        self._history.clear()
        super().set_data(rows)
        self._update_undo_button()

    # ---- 筛选

    def _after_filter(self) -> None:
        """筛选 / 行级改动之后的收尾：让候选面板跟着选中行走。

        筛到 0 行时**必须清掉面板**，否则它还挂着上一次选中那一行的候选 ——
        表格里没有那一行，而下方却在显示它的候选歌曲。

        ⚠️ 反方向也有个坑（**真机报的**：点「都不是，跳过此曲」之后界面自动置顶）：
        **没有 `_pending_focus` 时不许去挑「第一条可见行」**。选中行一变，Qt 就会
        把它滚进视野 —— 用户点一下跳过，他刚看完的那一行就被顶下去了。
        行级动作（跳过 / 清除匹配 / 改单元格）都不该移动视图；选中行确实离开了
        当前筛选时，也只能落到**最近的一条**可见行（见 `_nearest_visible_row_index`）。
        """
        self._refresh()
        shown = self._shown_row
        self._shown_row = None
        target = self._pending_focus
        if target is not None and self._is_live(target):
            i = self._index_of(target)
            if i is not None and not self.table.isRowHidden(i):
                self.table.selectRow(i)
                self.table.scrollTo(self.model.index(i, 0))
                return
            # 撤销回去的状态可能仍然被当前筛选藏着（例如在「仅自动」下撤销一次
            # 人工选定 → 退回待审核）。这时**别偷偷切换筛选**（那才是真正的意外），
            # 但必须说清它去哪了，否则用户看到的是「撤销之后行没了」。
            self.log.emit(
                f"  提示：这一行现在被筛选「{self._view}」藏起来了 —— "
                f"切回「{self.FILTER_ALL}」就能看到它。"
            )

        # 选中行被当前筛选藏起来了（**最常见的一路**：切「需要处理」顺着往下跳，
        # 跳过一行之后它就离开这个筛选了）→ 落到**离它最近的一条可见行**。
        # ⚠️ 这里**不能**用「第一条可见行」：用户是在顺着往下处理，把他顶回顶部
        # 等于把「我刚看到哪」弄丢了（真机原话：「我需要界面保持不动」）。
        sel = self.table.selectionModel().selectedRows()
        cur = sel[0].row() if sel else None
        if sel and not self.table.isRowHidden(cur):
            # 选中行还在、也没被藏起来 → **什么都别动**。面板显示的仍是这一行，
            # 所以连重建都不用（`_shown_row` 还原成原值），
            # 免得每次行级动作都白重建一次候选面板（实测一次约 27ms）。
            self._shown_row = shown
            return

        nxt = self._nearest_visible_row_index(cur)
        if nxt is None:
            self._show_candidates({}, force=True)
        else:
            self.table.selectRow(nxt)

    def _nearest_visible_row_index(self, cur: int | None) -> int | None:
        """离 `cur` 最近的可见行：**先往下找**，都没有了再往上找。

        为什么不是「第一条可见行」：跳过一行之后，用户要看的是**下一首**。
        只有跳过的正好是最后一条时才往回退一条 —— 无论如何都不回到顶部。
        """
        if cur is None:
            return self._first_visible_row_index()
        for i in range(cur, self.model.rowCount()):
            if not self.table.isRowHidden(i):
                return i
        for i in range(min(cur, self.model.rowCount()) - 1, -1, -1):
            if not self.table.isRowHidden(i):
                return i
        return None

    def _first_visible_row_index(self) -> int | None:
        for i in range(self.model.rowCount()):
            if not self.table.isRowHidden(i):
                return i
        return None

    def _refresh(self) -> None:
        # 统计条一律**全量**口径：筛选只决定看到哪几行，不改变这些事实。
        m = self.all_rows()
        auto = len([r for r in m if r["status"] == "auto"])
        rev = len([r for r in m if r["status"] == "pending_review"])
        low = len([r for r in m if r["status"] == "low"])
        miss = len([r for r in m if r["status"].startswith("missing")])
        conf = len([r for r in m if r["status"] == "confirmed"])
        # pending = 还没跑过匹配。不统计它的话，刚拉完番单时状态条会全是 0，
        # 看起来像是「都处理完了」，其实是一条都还没匹配。
        pend = len([r for r in m if r["status"] == "pending"])
        skipped = len([r for r in m if r["status"] == "skipped"])
        bits = [
            f"🟢 自动 {auto}", f"🟡 待审核 {rev}", f"🔴 低分 {low}",
            f"⬜ 缺失 {miss}", f"🔵 人工 {conf}",
        ]
        if pend:
            bits.append(f"⏳ 待匹配 {pend}")
        if skipped:
            bits.append(f"⬜ 已跳过 {skipped}")
        self.stat.setText(" · ".join(bits))
        # ⚠️ 这个数字与筛选项「需要处理」必须**完全一致**（同一个判据函数）——
        # 否则「⚠ N 条需要处理」点进去看到的条数对不上，两个数字一起失去可信度。
        n = len([r for r in m if _needs_attention(r)])
        self.badge.setText(f"⚠ {n} 条需要处理" if n else "✓ 无待处理项")


# ==================================================================== ④ 写入

class PushPage(BasePage):
    def __init__(self, rows: list[dict], parent=None):
        cols = [
            Column("included", "推送", 52, "check", align=Qt.AlignCenter),
            Column("name", "曲名", 260, "readonly", stretch=True),
            Column("artists", "艺人", 170, "readonly"),
            Column("netease_song_id", "网易云 ID", 110, "readonly", align=Qt.AlignCenter),
            Column("playlist", "目标歌单", 130, "text", editable=True, align=Qt.AlignCenter),
            Column("status", "状态", 120, "status", align=Qt.AlignCenter),
        ]
        super().__init__(3, "④ 写入歌单", "逐首加入，限速 ≤1 req/s", cols, parent)
        self.next_btn.setText("完成")
        self.next_btn.setEnabled(True)

        self.model = DictTableModel(cols, rows)
        self.table = self.make_table(self.model)
        self.add_widget(self.table)

        self.add_action("全选", lambda: self._set_all(True))
        self.add_action("+ 手动加歌", self._add)

        self.set_filter_spec({
            self.FILTER_ALL: None,
            "待推送": lambda r: r.get("status") == "待推送",
            "失败": lambda r: r.get("status") == "失败",
        })
        # 记住建表时的默认歌单名 —— 「目标歌单」输入框改动时，
        # 只有**还等于这个旧默认值**的行才跟着改（用户自己填过的不能被覆盖）
        self._default_playlist = (rows[0].get("playlist") if rows else "") or ""
        self._refresh()

    def _set_all(self, v: bool) -> None:
        # 作用域 = 当前看得见的行（并在日志里说明），见 BasePage.visible_rows 的注释
        rows = self.visible_rows()
        for r in rows:
            r["included"] = v
        self._emit_all_changed()
        self.log.emit(
            f"已{'勾选' if v else '取消勾选'}当前显示 {len(rows)} 行{self._scope_hint()}"
            + ("" if v else "（取消勾选只表示这次不推，不会记为「已跳过」）"))

    def _add(self) -> None:
        r = self.model.add_row(
            {"included": True, "name": "", "artists": "", "netease_song_id": "",
             "playlist": self._default_playlist, "status": "待推送"}
        )
        self.log.emit(f"新增第 {r + 1} 行")
        self.table.scrollToBottom()
        self.table.edit(self.model.index(r, 1))
        self._apply_filter(self._view, log=False)

    # ---- ④ 与 MainWindow / session 之间的接口（推送只认这里的勾选）

    def checked_push_rows(self) -> list[dict]:
        """勾选了「推送」且确实有网易云 ID 的行 —— **全量，不看筛选**。

        ⚠️ 两条都是硬要求：

        1. 推送**只能**用这个入口取数据：`orchestrator.pushable(season)` 是全量，
           完全不看这个复选框 —— 也就是「取消勾选」曾经对写入没有任何作用。
           没有 song_id 的行（手动加的空白行）直接跳过，不能拿空 ID 去写歌单。
        2. 必须读 `all_rows()`：如果读的是「当前看得见的行」，那么第④步一旦筛到
           「失败」，按推送就会**静默只推失败那几首**（甚至一首都不推），
           而用户以为筛的只是显示。筛选绝不能改变会写进账号的东西。
        """
        return [
            r for r in self.all_rows()
            if r.get("included") and str(r.get("netease_song_id") or "").strip()
        ]

    def adopt_default_playlist(self, name: str, old_default: str | None = None) -> None:
        """「目标歌单」输入框变了 → 同步到还没被用户自定义过的行。

        不同步的话，输入框显示 A、行里显示 B、推送用的是 C，
        用户按界面理解去核对，核对的是错的东西。

        ⚠️ 只统计**真的变了**的行。修之前 `cur == old` 时也照样 `n += 1`，
        于是每次刷新都会打「目标歌单改为「26年10月」，已同步 12 行」——
        而实际上一个字段都没动（因为 `cur` 本来就等于 `name`）。
        一条永远为真的日志比没有日志更糟：它让人以为自己看懂了发生了什么。

        ⚠️ 遍历 `all_rows()`：筛选只是没显示，那些行一样要跟着改 ——
        否则筛着「失败」的时候改歌单名，被藏起来的那几首会推到旧歌单去。
        """
        old = self._default_playlist if old_default is None else old_default
        self._default_playlist = name
        n = 0
        for r in self.all_rows():
            cur = str(r.get("playlist") or "")
            if cur == name:
                continue                     # 已经是目标值 —— 什么都不用做
            if cur == old or not cur:
                r["playlist"] = name
                n += 1
        if n:
            self._emit_all_changed()
            self.log.emit(f"目标歌单改为「{name}」，已同步 {n} 行")

    def restore_included(self, want: dict) -> None:
        """按 theme_id 恢复复选框状态（key = theme_id）。

        为什么需要：`set_data()` 每次都用 session 新造的行**整体替换**，
        用户在步骤④取消的勾选会在下一次 reload 时悄悄回来 —— 而推送只认勾选，
        「我看着没勾但推了」这种反复正是最危险的。已入库的行（有 theme_id）恢复；
        手动新增的行没有 theme_id，只能保留默认（勾选）。

        遍历 `all_rows()`：被筛选藏起来的行同样要恢复，否则刷新一次
        「我看着没勾但推了」就会从被藏起来的那几行里发生。
        """
        for r in self.all_rows():
            tid = r.get("theme_id")
            if tid in want:
                r["included"] = bool(want[tid])
        self._emit_all_changed()

    def mark_results(self, results: dict[str, str]) -> None:
        """把推送结果写回表格（song_id → 状态）。

        这里是**唯一**该改推送状态的地方：状态只能来自真实推送的返回值，
        页面自己不许编（早期原型在这里伪造过「成功」，那是很危险的行为 ——
        界面显示成功而歌其实没进歌单，用户不会再去核对）。

        遍历 `all_rows()`：推送推的是全量勾选，那么结果也要写回全量。
        只写看得见的那几行，会让被筛掉的行永远停在「待推送」。
        """
        for r in self.all_rows():
            sid = str(r.get("netease_song_id") or "")
            if sid in results:
                r["status"] = results[sid]
        if self.model.rowCount():
            self.model.dataChanged.emit(
                self.model.index(0, 0),
                self.model.index(self.model.rowCount() - 1, self.model.columnCount() - 1),
            )
        self._refresh()

    def _refresh(self) -> None:
        # 全量口径：这些是事实（一共几首、几首失败），不该被筛选改变
        m = self.all_rows()
        wait = len([r for r in m if r.get("status") == "待推送"])
        ok = len([r for r in m if r.get("status") == "已推送"])
        ex = len([r for r in m if r.get("status") == "已存在"])
        bad = len([r for r in m if r.get("status") == "失败"])
        self.stat.setText(
            f"待推送 {wait} · 已推送 {ok} · 已存在 {ex} · 失败 {bad} · 合计 {len(m)}"
        )
        self.badge.setText(f"⚠ {bad} 首推送失败" if bad else "")
