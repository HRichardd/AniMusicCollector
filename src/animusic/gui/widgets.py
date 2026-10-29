"""可复用控件：状态灯、步骤指示器、可编辑表格、候选卡片、日志面板。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from . import style
from .. import constants


# ==================================================================== 状态灯

class StatusLed(QWidget):
    """● 文字 形式的状态指示。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._color = style.TEXT_DIM
        self._text = "未检测"
        self.setMinimumWidth(120)
        self.setFixedHeight(20)

    def set_state(self, color: str, text: str) -> None:
        self._color = color
        self._text = text
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(self._color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(2, 7, 8, 8)
        p.setPen(QColor(style.TEXT if self._color != style.TEXT_DIM else style.TEXT_MUTED))
        f = self.font()
        f.setPointSize(9)
        p.setFont(f)
        p.drawText(QRect(16, 0, self.width() - 16, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self._text)


# ==================================================================== 步骤指示器

class Stepper(QWidget):
    """向导步骤指示器，可点击回退。"""

    step_clicked = Signal(int)

    STEPS = ["① 番单", "② 曲目", "③ 匹配", "④ 写入"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._current = 0
        self._states: list[str] = ["confirmed", "confirmed", "pending", "pending"]
        self._hover = -1
        self.setMouseTracking(True)

    def set_current(self, i: int) -> None:
        self._current = i
        self.update()

    def set_states(self, states: list[str]) -> None:
        self._states = states
        self.update()

    def set_state(self, i: int, state: str) -> None:
        self._states[i] = state
        self.update()

    def _color(self, i: int) -> str:
        if self._states[i] == "dirty":
            return style.WARN
        if i == self._current:
            return style.ACCENT
        if self._states[i] == "confirmed":
            return style.OK
        return "#4a4e54"

    def _cx(self, i: int) -> int:
        seg = self.width() / len(self.STEPS)
        return int(seg * i + seg / 2)

    def mouseMoveEvent(self, e):  # noqa: N802
        seg = self.width() / len(self.STEPS)
        idx = int(e.position().x() / seg)
        idx = max(0, min(len(self.STEPS) - 1, idx))
        if idx != self._hover:
            self._hover = idx
            self.setCursor(Qt.PointingHandCursor if idx < self._current else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, e):  # noqa: N802
        self._hover = -1
        self.update()

    def mousePressEvent(self, e):  # noqa: N802
        seg = self.width() / len(self.STEPS)
        idx = max(0, min(len(self.STEPS) - 1, int(e.position().x() / seg)))
        if idx != self._current:
            self.step_clicked.emit(idx)

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(style.PANEL))

        n = len(self.STEPS)
        cy = 20
        r = 11

        # 连接线
        p.setPen(Qt.NoPen)
        for i in range(n - 1):
            x1, x2 = self._cx(i) + r + 4, self._cx(i + 1) - r - 4
            done = self._states[i] == "confirmed" and i < self._current
            p.setBrush(QColor(style.OK if done else "#3a3d42"))
            p.drawRoundedRect(QRect(x1, cy - 1, max(0, x2 - x1), 2), 1, 1)

        f = self.font()
        for i, label in enumerate(self.STEPS):
            cx = self._cx(i)
            col = QColor(self._color(i))
            p.setBrush(QColor(style.PANEL))
            p.setPen(col)
            p.drawEllipse(cx - r, cy - r, r * 2, r * 2)

            if self._states[i] == "confirmed" and i != self._current:
                # 已确认打勾
                p.setPen(col)
                f2 = QFont(f)
                f2.setPointSize(10)
                f2.setBold(True)
                p.setFont(f2)
                p.drawText(QRect(cx - r, cy - r, r * 2, r * 2), Qt.AlignCenter, "✓")
            else:
                p.setBrush(col)
                p.setPen(Qt.NoPen)
                p.drawEllipse(cx - 4, cy - 4, 8, 8)

            f2 = QFont(f)
            f2.setPointSize(9)
            f2.setBold(i == self._current)
            p.setFont(f2)
            p.setPen(QColor(style.TEXT if i == self._current else style.TEXT_MUTED))
            p.drawText(QRect(cx - 60, cy + r + 2, 120, 18), Qt.AlignCenter, label)

            # 状态小字
            st = self._states[i]
            sub = {"confirmed": "已确认", "pending": "未开始", "fetched": "待确认", "dirty": "需重跑"}.get(st, st)
            f3 = QFont(f)
            f3.setPointSize(8)
            p.setFont(f3)
            p.setPen(QColor(style.WARN if st == "dirty" else style.TEXT_DIM))
            p.drawText(QRect(cx - 60, cy + r + 20, 120, 14), Qt.AlignCenter, sub)

        p.setPen(QColor(style.BORDER_SOFT))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)


# ==================================================================== 表格

@dataclass
class Column:
    key: str
    title: str
    width: int = 120
    kind: str = "text"          # check | text | choice | readonly | score | status
    editable: bool = False
    choices: list[str] | None = None
    color_fn: Callable[[dict], str | None] | None = None
    align: Qt.AlignmentFlag = Qt.AlignLeft | Qt.AlignVCenter
    stretch: bool = False


class ComboDelegate(QStyledItemDelegate):
    def __init__(self, choices: list[str], parent=None):
        super().__init__(parent)
        self.choices = choices

    def createEditor(self, parent, option, index):  # noqa: N802
        cb = QComboBox(parent)
        cb.addItems(self.choices)
        return cb

    def setEditorData(self, editor, index):  # noqa: N802
        val = index.data(Qt.EditRole)
        i = editor.findText(str(val))
        editor.setCurrentIndex(max(0, i))

    def setModelData(self, editor, model, index):  # noqa: N802
        model.setData(index, editor.currentText(), Qt.EditRole)


class DictTableModel(QAbstractTableModel):
    """以 dict 列表为后端的可编辑表格模型。"""

    def __init__(self, columns: list[Column], rows: list[dict] | None = None, parent=None):
        super().__init__(parent)
        self.columns = columns
        self.rows: list[dict] = rows or []
        self._undo: list[tuple[int, str, Any, Any]] = []
        #: 设了它 = 由页面接管编辑历史（见 `set_edit_hook`）。默认 None，
        #: 也就是四页里只有第③步用得上，其余页面行为一字不变。
        self._edit_hook: Callable[[dict, str, Any, Any], None] | None = None

    # ---- Qt 接口
    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.columns[section].title
        return None

    def flags(self, index):  # noqa: N802
        if not index.isValid():
            return Qt.NoItemFlags
        col = self.columns[index.column()]
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled
        if col.kind == "check":
            return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
        if col.editable:
            return base | Qt.ItemIsEditable
        return base

    def data(self, index, role=Qt.DisplayRole):  # noqa: N802
        if not index.isValid():
            return None
        col = self.columns[index.column()]
        row = self.rows[index.row()]
        val = row.get(col.key)

        if role == Qt.CheckStateRole and col.kind == "check":
            return Qt.Checked if val else Qt.Unchecked

        if role in (Qt.DisplayRole, Qt.EditRole):
            if col.kind == "check":
                return None
            if col.key in ("score",) or col.kind == "score":
                try:
                    return f"{float(val):.2f}"
                except (TypeError, ValueError):
                    return "—"
            if col.key == "duration_ms":
                try:
                    ms = int(val)
                    return f"{ms // 60000}:{ms % 60000 // 1000:02d}"
                except (TypeError, ValueError):
                    return "—"
            if isinstance(val, (list, tuple)):
                return "、".join(str(x) for x in val)
            return "" if val is None else str(val)

        if role == Qt.TextAlignmentRole:
            return int(col.align)

        if role == Qt.ForegroundRole:
            if col.color_fn:
                c = col.color_fn(row)
                if c:
                    return QColor(c)
            if col.kind == "status":
                return QColor(style.SCORE_COLORS.get(row.get("status", ""), style.TEXT))
            if col.key == "score":
                return QColor(style.SCORE_COLORS.get(row.get("status", ""), style.TEXT))
            if col.kind == "readonly":
                return QColor(style.TEXT_MUTED)

        if role == Qt.BackgroundRole:
            # 手工数据行优先（重跑不会被覆盖的那类）
            if row.get("origin") == "manual" or row.get("manual"):
                return QColor(style.MANUAL)
            return QColor(style.PANEL_ALT if index.row() % 2 else style.PANEL)

        if role == Qt.ToolTipRole:
            parts = []
            if row.get("note"):
                parts.append(f"备注：{row['note']}")
            if row.get("origin") == "manual":
                parts.append("手工数据 —— 重跑拉取时不会被覆盖")
            if col.key == "score" and row.get("detail"):
                d = row["detail"]
                parts.append(
                    "分项得分：\n"
                    f"  曲名相似   {d.get('title')}\n"
                    f"  艺人匹配   {d.get('artist')}\n"
                    f"  番剧标注   {d.get('anime_alias')}\n"
                    f"  时长合理   {d.get('duration')}\n"
                    f"  基础分     {d.get('base')}\n"
                    f"  降权       {d.get('penalties') or '—'}\n"
                    f"  加权       {d.get('bonuses') or '—'}"
                )
            return "\n".join(parts) if parts else None

        if role == Qt.FontRole and index.column() == 0:
            f = QFont()
            f.setBold(True)
            return f
        return None

    def setData(self, index, value, role=Qt.EditRole):  # noqa: N802
        if not index.isValid():
            return False
        col = self.columns[index.column()]
        row = self.rows[index.row()]

        if role == Qt.CheckStateRole and col.kind == "check":
            self._record(index.row(), row, col.key, row.get(col.key), bool(value))
            row[col.key] = bool(value)
            if col.key == "included" and "theme_included" in row:
                row["theme_included"] = bool(value)
            self.dataChanged.emit(index, index, [Qt.CheckStateRole, Qt.DisplayRole])
            return True

        if role == Qt.EditRole and col.editable:
            self._record(index.row(), row, col.key, row.get(col.key), value)
            row[col.key] = value
            self.dataChanged.emit(index, index)
            return True
        return False

    def set_edit_hook(self, fn: Callable[[dict, str, Any, Any], None] | None) -> None:
        """把「单元格编辑的历史」交给页面接管，回调签名 `fn(row, key, old, new)`。

        为什么必须能接管：第③步有两类要撤销的动作 —— 单元格编辑（改备注/曲名），
        和「点候选卡片选定一首」。两者各留一条历史栈的话，`Ctrl+Z` 就**不是**
        后进先出：刚点错一首歌、马上按撤销，很可能撤掉的是更早那次备注修改，
        而错的歌还留着 —— 那正是这个功能存在的理由，等于没修。
        页面用这个 hook 把两类动作塞进同一条栈，顺序才是对的。
        """
        self._edit_hook = fn

    def _record(self, r: int, row: dict, key: str, old: Any, new: Any) -> None:
        """记一次编辑。有 hook 就交出去，没有就保持老行为（自己的 `_undo`）。"""
        if self._edit_hook is not None:
            self._edit_hook(row, key, old, new)
        else:
            self._undo.append((r, key, old, new))

    def restore_cell(self, row: dict, key: str, old: Any) -> bool:
        """撤销专用：把某一格恢复成旧值，只发这一格的 `dataChanged`。

        用**身份**（`is`）找行号，不用 `list.index` —— 后者是字典内容比较，
        两个内容相同的行会挑错第一个（第③步里同番剧 OP/ED 的行结构很像）。
        """
        r = next((i for i, x in enumerate(self.rows) if x is row), None)
        if r is None:
            return False
        row[key] = old
        for c, col in enumerate(self.columns):
            if col.key == key:
                self.dataChanged.emit(
                    self.index(r, c), self.index(r, c),
                    [Qt.CheckStateRole, Qt.DisplayRole, Qt.EditRole],
                )
                return True
        return False

    # ---- 业务操作
    def add_row(self, row: dict | None = None) -> int:
        self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows))
        self.rows.append(row or {c.key: (False if c.kind == "check" else "") for c in self.columns})
        self.endInsertRows()
        return len(self.rows) - 1

    def remove_rows(self, idxs: list[int]) -> None:
        for i in sorted(set(idxs), reverse=True):
            row = self.rows[i]
            # 已保存的①②行只排除；物理移出模型会让保存端丢失用户意图。
            if "included" in row and (row.get("theme_id") or
                                       (row.get("anime_id") and "song_title" not in row)):
                row["included"] = False
                if "theme_included" in row:
                    row["theme_included"] = False
                self.dataChanged.emit(self.index(i, 0), self.index(i, len(self.columns) - 1))
                continue
            self.beginRemoveRows(QModelIndex(), i, i)
            self.rows.pop(i)
            self.endRemoveRows()

    def undo(self) -> str | None:
        if not self._undo:
            return None
        r, k, old, _new = self._undo.pop()
        if 0 <= r < len(self.rows):
            self.rows[r][k] = old
            tl = self.index(r, 0)
            br = self.index(r, len(self.columns) - 1)
            self.dataChanged.emit(tl, br)
            return f"撤销：第 {r + 1} 行 {k} 恢复为 {old!r}"
        return None

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self.rows = rows
        self._undo.clear()
        self.endResetModel()


class DictTableView(QTableView):
    """带右键菜单、快捷键、拖拽粘贴的表格视图。"""

    log = Signal(str)
    add_requested = Signal()
    #: 增删行之后发一次（行号会整体位移）。页面靠它重算「哪些行该被筛选藏起来」——
    #: 隐藏状态是**按行号**记的，删掉一行之后不重算就会藏错行
    #: （本该看得见的行被藏着，或反过来）。
    rows_changed = Signal()

    def __init__(self, columns: list[Column], parent=None):
        super().__init__(parent)
        self.columns = columns
        self.allow_add = True
        self.add_handler = None
        self.setAlternatingRowColors(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSortingEnabled(False)
        self.verticalHeader().setDefaultSectionSize(30)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setStretchLastSection(True)
        self.setEditTriggers(
            QAbstractItemView.DoubleClicked
            | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.AnyKeyPressed
        )

        for i, c in enumerate(columns):
            if c.kind == "choice" and c.choices:
                self.setItemDelegateForColumn(i, ComboDelegate(c.choices, self))

        #: 页面接管撤销时的回调（见 `set_undo_handler`）
        self._undo_handler: Callable[[], None] | None = None
        #: 页面往右键菜单里加自己的动作（见 `set_context_menu_builder`）
        self._menu_builder: Callable[[QMenu, dict, int], dict] | None = None

        # ⚠️ 这两条快捷键**必须留着引用**：页面接管撤销时要把它们 `setEnabled(False)`，
        # 让那一次按键只有一个处理器（谁先谁后本来也说不清）。
        #
        # 顺带记一条**实测推翻过的猜测**（`.tmp/probe_shortcut2.py`）：窗口里同时有
        # 8 条启用的 Ctrl+Z（四页表格各 2 条）**不会**被 Qt 判成歧义 ——
        # 在第①步按 Ctrl+Z，生效的是当前可见那一页；隐藏页面的不参与匹配。
        # 真正会把 Ctrl+Z 变成死键的是**上下文**：`WidgetWithChildrenShortcut`
        # 要求焦点落在该页的子控件上，焦点在按钮上时完全不触发。
        self._undo_shortcuts = [
            QShortcut(QKeySequence.Undo, self, activated=self._do_undo),
            QShortcut(QKeySequence("Ctrl+Shift+Z"), self, activated=self._do_undo),
        ]

    def set_undo_handler(self, fn: Callable[[], None] | None) -> None:
        """把本表格的 Ctrl+Z 交给页面（页面有跨「卡片点击 + 单元格编辑」的统一历史）。

        传 None 恢复默认：撤销本表格自己的单元格编辑历史。
        """
        self._undo_handler = fn
        for sc in self._undo_shortcuts:
            sc.setEnabled(fn is None)

    def set_context_menu_builder(
        self, fn: Callable[[QMenu, dict, int], dict] | None
    ) -> None:
        """往右键菜单顶部加页面自己的动作。

        `fn(menu, row, row_index)` 返回 `{QAction: 回调}`；本视图负责在用户选中后调用。
        需要这个口子是因为行级动作（清除匹配 / 跳过此曲）只有第③步有语义，
        而 `DictTableView` 是四页共用的。
        """
        self._menu_builder = fn

    def set_model(self, model: DictTableModel) -> None:
        self.setModel(model)
        hh = self.horizontalHeader()
        for i, c in enumerate(model.columns):
            if c.stretch:
                hh.setSectionResizeMode(i, QHeaderView.Stretch)
            else:
                hh.setSectionResizeMode(i, QHeaderView.Interactive)
                self.setColumnWidth(i, c.width)

    # ---- 右键菜单
    def contextMenuEvent(self, e):  # noqa: N802
        idx = self.indexAt(e.pos())
        menu = QMenu(self)
        extra: dict = {}
        if idx.isValid():
            row = self.model().rows[idx.row()]
            if self._menu_builder is not None:
                # 页面动作放在最上面：它们是这一行的**正事**（改匹配），
                # 删除/新增是通用操作，排在后面免得误点
                extra = dict(self._menu_builder(menu, row, idx.row()) or {})
                if extra:
                    menu.addSeparator()
            saved = "included" in row and (row.get("theme_id") or
                                             (row.get("anime_id") and "song_title" not in row))
            act_del = menu.addAction("排除此行（保留历史）" if saved else "移除此行")
            act_excl = None
            if "included" in row:
                act_excl = menu.addAction("从结果中排除（不删除）" if row.get("included") else "重新纳入")
            act_restore = None
            if row.get("origin") == "manual":
                menu.addSeparator()
                act_restore = menu.addAction("恢复为数据源值")
            menu.addSeparator()
            act_add = menu.addAction("新增一行") if self.allow_add else None

            chosen = menu.exec(e.globalPos())
            if chosen is None:
                return
            if chosen in extra:
                extra[chosen]()
            elif chosen == act_del:
                self.model().remove_rows([idx.row()])
                self.rows_changed.emit()
                self.log.emit(f"已{'排除' if saved else '移除'}第 {idx.row() + 1} 行")
            elif act_excl is not None and chosen == act_excl:
                row["included"] = not row.get("included")
                if "theme_included" in row:
                    row["theme_included"] = row["included"]
                self.model().dataChanged.emit(self.model().index(idx.row(), 0),
                                              self.model().index(idx.row(), len(self.columns) - 1))
                self.rows_changed.emit()
                self.log.emit(f"第 {idx.row() + 1} 行 included → {row['included']}")
            elif act_restore is not None and chosen == act_restore:
                row["origin"] = "animethemes"
                row["origin_label"] = "数据源"
                self.model().dataChanged.emit(
                    self.model().index(idx.row(), 0),
                    self.model().index(idx.row(), len(self.columns) - 1),
                )
                self.log.emit(f"第 {idx.row() + 1} 行已恢复为数据源值（下次重跑会覆盖）")
            elif chosen == act_add:
                self._add_row()
        else:
            if not self.allow_add:
                return
            act_add = menu.addAction("新增一行")
            if menu.exec(e.globalPos()) == act_add:
                self._add_row()

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key_Delete:
            rows = sorted({i.row() for i in self.selectionModel().selectedRows()}, reverse=True)
            if rows:
                self.model().remove_rows(rows)
                self.rows_changed.emit()
                self.log.emit(f"已排除已有行或移除未保存行：{len(rows)} 行")
            return
        super().keyPressEvent(e)

    def _add_row(self) -> None:
        if not self.allow_add:
            return
        if self.add_handler is not None:
            self.add_handler()
            return
        r = self.model().add_row()
        self.rows_changed.emit()
        self.log.emit(f"已新增第 {r + 1} 行 —— 请填写内容")
        self.scrollToBottom()
        for c, col in enumerate(self.columns):
            if col.editable or col.kind == "check":
                self.edit(self.model().index(r, c))
                break

    def _do_undo(self) -> None:
        if self._undo_handler is not None:
            self._undo_handler()
            return
        msg = self.model().undo()
        self.log.emit(msg or "没有可撤销的操作")


# ==================================================================== 封面板

class CoverLoader(QWidget):
    """异步加载封面，失败则显示占位色块。"""

    _cache: dict[str, QPixmap] = {}
    _nam: QNetworkAccessManager | None = None

    def __init__(self, url: str = "", size: int = 44, parent=None):
        super().__init__(parent)
        self._url = url or ""
        self._size = size
        self.setFixedSize(size, size)
        self._pix: QPixmap | None = self._cache.get(self._url)
        if self._url and self._pix is None:
            self._fetch()

    @classmethod
    def _manager(cls) -> QNetworkAccessManager:
        if cls._nam is None:
            cls._nam = QNetworkAccessManager()
        return cls._nam

    def _fetch(self) -> None:
        req = QNetworkRequest(QUrl(self._url))
        req.setAttribute(QNetworkRequest.FollowRedirectsAttribute, True)
        reply = self._manager().get(req)
        reply.finished.connect(lambda: self._on_done(reply))

    def _on_done(self, reply: QNetworkReply) -> None:
        try:
            if reply.error() == QNetworkReply.NoError:
                pm = QPixmap()
                if pm.loadFromData(reply.readAll()):
                    self._pix = pm.scaled(
                        self._size, self._size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
                    )
                    CoverLoader._cache[self._url] = self._pix
                    self.update()
        finally:
            reply.deleteLater()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._pix and not self._pix.isNull():
            p.drawPixmap(0, 0, self._pix)
        else:
            p.setBrush(QColor(style.PANEL_ALT))
            p.setPen(QColor(style.BORDER))
            p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 4, 4)


# ==================================================================== 候选卡片

class CandidateItem(QFrame):
    """一条候选：封面 / 曲名 / 艺人 / 专辑 / 时长 / 标签 / 番剧标注 / 分数。"""

    picked = Signal(dict)

    def __init__(self, cand: dict, selected: bool = False, parent=None):
        super().__init__(parent)
        self.cand = cand
        # 用 objectName + 全局 QSS，不逐个 setStyleSheet（性能）
        self.setObjectName("CandCardSel" if selected else "CandCard")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)

        lay.addWidget(CoverLoader(cand.get("cover") or "", 44))

        mid = QVBoxLayout()
        mid.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.radio = QCheckBox()
        self.radio.setChecked(selected)
        self.radio.clicked.connect(lambda: self.picked.emit(self.cand))
        top.addWidget(self.radio)

        name = QLabel(str(cand.get("name") or "")[:60])
        name.setObjectName("CandName")
        top.addWidget(name)

        for tag in cand.get("tags") or []:
            t = QLabel(tag)
            t.setObjectName("TagVip" if tag == "VIP" else "Tag")
            top.addWidget(t)

        if cand.get("alias") or []:
            a = QLabel(f"《{cand['alias'][0]}》")
            a.setObjectName("CandAlias")
            top.addWidget(a)

        top.addStretch()
        mid.addLayout(top)

        sub_bits = ["、".join(cand.get("artists") or [])]
        if cand.get("album"):
            sub_bits.append(str(cand["album"])[:40])
        ms = cand.get("duration_ms") or 0
        if ms:
            sub_bits.append(f"{ms // 60000}:{ms % 60000 // 1000:02d}")
        sub = QLabel("   ·   ".join(sub_bits))
        sub.setObjectName("CandSub")
        mid.addWidget(sub)

        lay.addLayout(mid, 1)

        # 分数
        sc = float(cand.get("score") or 0)
        # ⚠️ 这里的兜底阈值必须和**决策口径**一致（`constants.AUTO_THRESHOLD` /
        # `REVIEW_THRESHOLD`，`gui/session.py` 造候选时用的就是它们）。
        # 原来写的是 0.85 / 0.55 —— 与 0.80 / 0.60 不一致：正常路径走不到这个兜底
        # （所有候选都带 `status`），可一旦有谁传了不带 status 的候选，
        # 卡片颜色就会和"自动通过/待复核"的真实判定对不上。
        st = cand.get("status") or (
            "auto" if sc >= constants.AUTO_THRESHOLD
            else "pending_review" if sc >= constants.REVIEW_THRESHOLD
            else "low"
        )
        right = QVBoxLayout()
        right.setSpacing(0)
        sl = QLabel(f"{sc:.2f}")
        sl.setObjectName("ScoreBig")
        sl.setProperty("state", st)
        sl.setAlignment(Qt.AlignRight)
        right.addWidget(sl)
        d = cand.get("detail") or {}
        dtxt = QLabel(
            f"曲{d.get('title', 0):.2f} 人{d.get('artist', 0):.2f} "
            f"番{d.get('anime_alias', 0):.2f} 时{d.get('duration', 0):.2f}"
        )
        dtxt.setObjectName("ScoreDetail")
        dtxt.setAlignment(Qt.AlignRight)
        dtxt.setToolTip("分项得分：曲名 / 艺人 / 番剧标注 / 时长")
        right.addWidget(dtxt)
        lay.addLayout(right)

    def set_selected(self, sel: bool) -> None:
        """原地更新选中态 —— 不重建控件，因此不会重置滚动位置。"""
        self.radio.setChecked(sel)
        target = "CandCardSel" if sel else "CandCard"
        if self.objectName() != target:
            self.setObjectName(target)
            # objectName 变了要重新 polish，QSS 才会生效
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        self.picked.emit(self.cand)


# ==================================================================== 日志

class LogPane(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(2000)
        # ⚠️ 原来是 `setFixedHeight(72)` —— 它同时钉死 minimum 和 maximum，
        # 于是这块日志**永远只有 4 行高、且任何布局都改不了它**。
        # 报错时看不全上下文（用户反馈的第一条问题就是这个）。
        # 现在：只给一个下限（防被拖没），高度交给 QSplitter 的分界决定。
        #
        # 下限取 40 而不是更大：这个值会**叠加**到主分隔器的最小高度上。窗口小的时候，
        # 它和页面栈的最小高度一起把分隔条挤出可视区 —— 表现为"日志框拖不动"
        # （实测：见 pages.py 里 MatchPage 那段注释）。
        self.setMinimumHeight(40)

    def log(self, msg: str) -> None:
        from datetime import datetime

        self.appendPlainText(f"[{datetime.now():%H:%M:%S}] {msg}")
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
