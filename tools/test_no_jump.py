"""验证「选候选后界面不乱跳」。

做法：记录选中前后的关键 UI 状态，逐项比对：
  · 表格当前选中行
  · 表格垂直滚动位置
  · 候选区滚动位置
  · 候选卡片数量（不应被重建）

用法：
    $env:PYTHONPATH = "<工作区>\vendor"
    python tools/test_no_jump.py
"""

from __future__ import annotations

import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "vendor"))

from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, _HERE)
from demo_env import make_env  # noqa: E402


def snapshot(mp) -> dict:
    sb = mp.table.verticalScrollBar()
    csb = mp.cand_scroll.verticalScrollBar()
    rows = mp.table.selectionModel().selectedRows()
    return {
        "表格选中行": rows[0].row() if rows else None,
        "表格滚动值": sb.value(),
        "候选区滚动值": csb.value(),
        "候选卡片数": len(mp.cand_items),
        "候选卡片对象 id": [id(w) for w in mp.cand_items],
    }


def main() -> int:
    app, win, session = make_env(seed_data=True, run_match=True, limit=6)

    mp = win.pages[2]
    win._goto(2)
    for _ in range(6):
        app.processEvents()

    # 挑一条有候选、且靠后的行，让滚动位置有意义
    target_idx = None
    for i, r in enumerate(mp.model.rows):
        if len(r.get("candidates") or []) >= 6:
            target_idx = i
    if target_idx is None:
        print("没有带候选的行，无法测试")
        return 1

    mp.table.selectRow(target_idx)
    for _ in range(6):
        app.processEvents()

    # 人为制造滚动偏移，验证会不会被重置
    mp.cand_scroll.verticalScrollBar().setValue(60)
    mp.table.verticalScrollBar().setValue(3)
    for _ in range(6):
        app.processEvents()

    before = snapshot(mp)
    row = mp.model.rows[target_idx]
    cands = row.get("candidates") or []
    # 选一个和当前不同的候选（最后一名的概率最大）
    pick = next((c for c in reversed(cands) if c["netease_song_id"] != row.get("chosen_id")), cands[-1])

    print("选中前:", before)
    print(f"选候选: {pick['name']} / {'、'.join(pick.get('artists') or [])}  (id={pick['netease_song_id']})")

    mp._pick(row, pick)
    for _ in range(8):
        app.processEvents()

    after = snapshot(mp)
    print("选中后:", after)
    print()

    checks = [
        ("表格选中行未变", before["表格选中行"] == after["表格选中行"]),
        ("表格滚动位置未变", before["表格滚动值"] == after["表格滚动值"]),
        ("候选区滚动位置未变", before["候选区滚动值"] == after["候选区滚动值"]),
        ("候选卡片数量一致", before["候选卡片数"] == after["候选卡片数"]),
        ("候选卡片未被重建（对象 id 一致）", before["候选卡片对象 id"] == after["候选卡片对象 id"]),
        ("行的选中歌曲已更新", row["chosen_id"] == pick["netease_song_id"]),
        ("该行状态变为人工确认", row["status"] == "confirmed"),
    ]
    ok = True
    for name, passed in checks:
        print(f"  {'✅' if passed else '❌'} {name}")
        ok = ok and passed

    print()
    print("结果:", "全部通过 —— 界面不会跳" if ok else "仍有跳动")
    win.close()
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
