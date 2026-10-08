"""AniMusicCollector GUI 启动入口。

依赖：PySide6（见 tools/bootstrap_deps.py）

    $env:PYTHONPATH = "<工作区>\src;<工作区>\vendor"
    python run_gui.py
    python run_gui.py --season 2026Q4        # 指定季度（默认就是 2026Q4）
    python run_gui.py --db data/other.db     # 换个数据库（便于试错，不污染正式数据）
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "src"))
# 允许直接用 vendor/ 里的 PySide6（沙箱内 pip 不可用时的安装方式）
_vendor = os.path.join(_HERE, "vendor")
if os.path.isdir(_vendor) and _vendor not in sys.path:
    sys.path.insert(0, _vendor)


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("找不到 PySide6。请先运行：python tools/bootstrap_deps.py", file=sys.stderr)
        return 1

    ap = argparse.ArgumentParser(description="AniMusicCollector 向导界面")
    ap.add_argument("--season", default=None,
                    help="季度，形如 2026Q4（默认=今天所在的季度）")
    ap.add_argument("--db", default=None, help="SQLite 路径，默认 data/state.db")
    args = ap.parse_args()

    from animusic.config import ensure_default_config
    from animusic.gui import style
    from animusic.gui.main_window import MainWindow
    from animusic.gui.session import WizardSession
    from animusic.models import Season
    from animusic.store import Store

    ensure_default_config()

    app = QApplication(sys.argv)
    app.setApplicationName("AniMusicCollector")
    app.setStyleSheet(style.QSS)

    try:
        # 不传就用**今天所在季度** —— 不要再写死某一年某一季
        season = Season.parse(args.season) if args.season else Season.current()
    except ValueError as e:
        print(f"季度格式不对：{e}", file=sys.stderr)
        return 2

    store = Store(args.db) if args.db else Store()
    session = WizardSession(store=store, season=season)
    win = MainWindow(session)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
