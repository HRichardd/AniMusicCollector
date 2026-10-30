"""第一步的纳入和移除规则，独立于其它步骤的通用表格行为。"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QMenu

from .widgets import DictTableModel, DictTableView


def anime_name(row: dict, index: int) -> str:
    return (row.get("title_cn") or row.get("title_jp") or row.get("title_romaji")
            or f"未命名番剧（第 {index + 1} 行）")


class AnimeTableModel(DictTableModel):
    log = Signal(str)

    @staticmethod
    def can_remove(row: dict) -> bool:
        return row.get("origin") == "manual" and not row.get("anime_id")

    def inclusion_message(self, row: dict, index: int, included: bool) -> str:
        return f"已{'纳入' if included else '排除'}番剧：{anime_name(row, index)}（待保存）"

    def setData(self, index, value, role=Qt.EditRole):  # noqa: N802
        if (index.isValid() and role == Qt.CheckStateRole
                and self.columns[index.column()].key == "included"):
            row = self.rows[index.row()]
            included = value != Qt.Unchecked and bool(value)
            if bool(row.get("included")) == included:
                return True
            changed = super().setData(index, included, role)
            if changed:
                self.log.emit(self.inclusion_message(row, index.row(), included))
            return changed
        return super().setData(index, value, role)

    def undo(self) -> str | None:
        entry = self._undo[-1] if self._undo else None
        message = super().undo()
        if message and entry is not None:
            r, key, old, _ = entry
            if key == "included":
                return "撤销：" + self.inclusion_message(self.rows[r], r, bool(old))
        return message

    def remove_rows(self, idxs: list[int]) -> None:
        for i in sorted(set(idxs), reverse=True):
            if not 0 <= i < len(self.rows) or not self.can_remove(self.rows[i]):
                continue
            super().remove_rows([i])
            # 移除新增行会改变后续行号，撤销不能因此改到另一部番。
            self._undo = [(r - int(r > i), key, old, new)
                          for r, key, old, new in self._undo if r != i]


class AnimeTableView(DictTableView):
    def contextMenuEvent(self, e):  # noqa: N802
        idx = self.indexAt(e.pos())
        menu = QMenu(self)
        remove = None
        if idx.isValid() and self.model().can_remove(self.model().rows[idx.row()]):
            remove = menu.addAction("移除此行")
        add = menu.addAction("新增一行") if self.allow_add else None
        if menu.isEmpty():
            return
        chosen = menu.exec(e.globalPos())
        if chosen is not None and chosen == remove:
            self._remove_unsaved([idx.row()])
        elif chosen is not None and chosen == add:
            self._add_row()

    def _remove_unsaved(self, indexes: list[int]) -> None:
        removable = [i for i in sorted(set(indexes))
                     if self.model().can_remove(self.model().rows[i])]
        names = [anime_name(self.model().rows[i], i) for i in removable]
        if not removable:
            return
        self.model().remove_rows(removable)
        self.rows_changed.emit()
        for name in names:
            self.log.emit(f"已移除新增番剧：{name}（未保存）")

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key_Delete:
            self._remove_unsaved([i.row() for i in self.selectionModel().selectedRows()])
            return
        super().keyPressEvent(e)
