r"""把向导四步各渲染成 PNG，用于自查 UI 效果。

**用的是真实数据**（AnimeThemes 拉取 + 真实网易云搜索匹配），
库落在独立的 `data/demo.db`，不动正式数据。

用法：
    $env:PYTHONPATH = "<工作区>\src;<工作区>\vendor"
    python tools/screenshot_ui.py                 # 复用已有演示库（快）
    python tools/screenshot_ui.py --rebuild       # 重建并重跑匹配（约 1~2 分钟）
    python tools/screenshot_ui.py --no-match      # 只拉番单不跑匹配

输出到 ai doc/界面验收/ui/
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from demo_env import make_env  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
OUT = os.path.join(_ROOT, "ai doc", "界面验收", "ui")
NAMES = ["step1_anime", "step2_theme", "step3_match", "step4_push"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="重建演示库并跑匹配")
    ap.add_argument("--no-match", action="store_true", help="只拉番单，不跑网易云匹配")
    ap.add_argument("--limit", type=int, default=6)
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)

    app, win, session = make_env(
        seed_data=True,
        run_match=not args.no_match,
        limit=args.limit,
        reuse=not args.rebuild,
        quiet=False,
    )
    st = session.stats(win.data)
    print("演示库统计:", st)

    for i, name in enumerate(NAMES):
        win._goto(i)
        for _ in range(8):
            app.processEvents()
        pm = win.grab()
        path = os.path.join(OUT, f"{name}.png")
        ok = pm.save(path)
        print(f"  {name}: {pm.width()}x{pm.height()}  saved={ok}  -> {path}")

    # 步骤③ 里选一条有候选的，把候选面板也渲染出来
    win._goto(2)
    mp = win.pages[2]
    pick = None
    for prefer in ("pending_review", "auto", "missing_match"):
        for idx, r in enumerate(mp.model.rows):
            if r["status"] == prefer and r.get("candidates"):
                pick = idx
                break
        if pick is not None:
            break
    if pick is not None:
        mp.table.selectRow(pick)
        for _ in range(12):
            app.processEvents()
        pm = win.grab()
        path = os.path.join(OUT, "step3_candidates.png")
        pm.save(path)
        print(f"  step3_candidates: {pm.width()}x{pm.height()}  -> {path}")
    else:
        print("  （没有带候选的行，跳过候选面板截图）")

    win.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
