"""第二步专用菜单和收录日志，避免改变其它步骤的共用表格。"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QMenu

from .widgets import DictTableModel, DictTableView


class ThemeTableModel(DictTableModel):
    log = Signal(str)

    @staticmethod
    def _inclusion(row: dict) -> tuple[bool, bool, bool]:
        return (bool(row.get('included')), 'theme_included' in row,
                bool(row.get('theme_included', row.get('included'))))

    @staticmethod
    def _restore_inclusion(row: dict, state: tuple[bool, bool, bool]) -> None:
        row['included'] = state[0]
        if state[1]:
            row['theme_included'] = state[2]
        else:
            row.pop('theme_included', None)

    @staticmethod
    def inclusion_message(row: dict, index: int, included: bool) -> str:
        anime = row.get('anime') or '未命名番剧'
        kind = {'OP': 'OP', 'ED': 'ED', 'IN': '插入曲'}.get(row.get('kind'), '未指定类型')
        title = row.get('song_title') or '未命名曲目'
        parent = '；所属番剧已排除，需在第①步纳入番剧' if included and not row.get('anime_included', True) else ''
        return f"已{'收录' if included else '排除'}曲目：{anime}｜{kind}｜{title}（第 {index + 1} 行，待保存{parent}）"

    def setData(self, index, value, role=Qt.EditRole):  # noqa: N802
        if (index.isValid() and role == Qt.CheckStateRole
                and self.columns[index.column()].key == 'included'):
            row = self.rows[index.row()]
            included = value != Qt.Unchecked and bool(value)
            before = self._inclusion(row)
            # 曲目自身的选择与有效收录分开；勾曲目不能越过第一步的番剧排除。
            effective = included and bool(row.get('anime_included', True))
            if before[0] == effective and before[2] == included:
                return True
            after = (effective, True, included)
            self._record(index.row(), row, 'included', before, after)
            self._restore_inclusion(row, after)
            self.dataChanged.emit(index, index, [Qt.CheckStateRole, Qt.DisplayRole])
            self.log.emit(self.inclusion_message(row, index.row(), included))
            return True
        return super().setData(index, value, role)

    def undo(self) -> str | None:
        if self._undo and self._undo[-1][1] == 'included':
            r, _, before, _ = self._undo.pop()
            if 0 <= r < len(self.rows):
                self._restore_inclusion(self.rows[r], before)
                self.dataChanged.emit(self.index(r, 0), self.index(r, len(self.columns) - 1))
                return '撤销：' + self.inclusion_message(self.rows[r], r, before[2])
            return None
        return super().undo()

    def remove_rows(self, idxs: list[int]) -> None:
        for i in sorted(set(idxs), reverse=True):
            if not 0 <= i < len(self.rows):
                continue
            row = self.rows[i]
            if row.get('theme_id'):
                self.setData(self.index(i, 0), False, Qt.CheckStateRole)
            else:
                super().remove_rows([i])
                # Delete仍可移除未保存行；必须修正历史行号，撤销不能恢复到另一首歌。
                self._undo = [(r - int(r > i), key, old, new)
                              for r, key, old, new in self._undo if r != i]


class ThemeTableView(DictTableView):
    def contextMenuEvent(self, e):  # noqa: N802
        if not self.allow_add:
            return
        menu = QMenu(self)
        add = menu.addAction('新增一行')
        if menu.exec(e.globalPos()) == add:
            self._add_row()
