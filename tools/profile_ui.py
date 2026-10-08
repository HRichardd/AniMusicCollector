"""UI 性能测量：找出点击卡顿的元凶。

用法：
    $env:PYTHONPATH = "<工作区>\vendor"
    python tools/profile_ui.py
"""

from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "vendor"))

from PySide6.QtCore import QModelIndex, Qt  # noqa: E402

sys.path.insert(0, _HERE)
from demo_env import make_env  # noqa: E402


def timeit(label: str, fn, n: int = 1) -> float:
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    dt = (time.perf_counter() - t0) / n * 1000
    print(f"  {label:<46} {dt:8.2f} ms")
    return dt


def main() -> int:
    print("构建演示环境（真实数据）...")
    app, win, session = make_env(seed_data=True, run_match=True, limit=6)
    print(f"  番剧 {len(win.data['anime'])} · 曲目 {len(win.data['theme'])} · "
          f"匹配 {len(win.data['match'])}")
    for _ in range(10):
        app.processEvents()

    print("\n=== 环境探测（每 5 秒自动跑一次，在 UI 线程上）===")
    timeit("env.probe()", win.env.probe, 3)

    print("\n=== 页面切换 ===")
    for i in range(4):
        timeit(f"_goto({i})", lambda i=i: win._goto(i), 3)
        app.processEvents()

    print("\n=== 步骤③ 选择某一行 → 重建候选面板 ===")
    mp = win.pages[2]
    win._goto(2)
    app.processEvents()
    rows = mp.model.rows
    # 找候选最多的行
    rows_sorted = sorted(range(len(rows)), key=lambda i: -len(rows[i].get("candidates") or []))
    for ri in rows_sorted[:3]:
        r = rows[ri]
        n = len(r.get("candidates") or [])
        if not n:
            continue
        timeit(f"selectRow(第{ri+1}行, {n} 候选) → _show_candidates",
               lambda ri=ri: mp._show_candidates(rows[ri]), 3)
        app.processEvents()

    print("\n=== 表格模型 paint 路径（data() 调用）===")
    m = mp.model
    calls = 0

    def paint_all() -> None:
        nonlocal calls
        for row in range(min(9, m.rowCount())):
            for col in range(m.columnCount()):
                idx = m.index(row, col)
                m.data(idx, Qt.DisplayRole)
                m.data(idx, Qt.BackgroundRole)
                m.data(idx, Qt.ForegroundRole)
                calls += 1

    timeit("9 行 × 9 列 × 3 个 role", paint_all, 5)
    print(f"     共 {calls} 次 data() 调用")

    print("\n=== _pick（选候选）===")
    if rows_sorted:
        r = rows[rows_sorted[0]]
        cands = r.get("candidates") or []
        if cands:
            timeit("_pick(候选)", lambda: mp._pick(r, cands[-1]), 5)

    print("\n=== 复选框切换（layoutChanged.emit）===")
    timeit("layoutChanged.emit()", lambda: m.layoutChanged.emit(), 20)

    print("\n=== 徽标 / 统计刷新 ===")
    timeit("mp._refresh()", mp._refresh, 10)

    win.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
