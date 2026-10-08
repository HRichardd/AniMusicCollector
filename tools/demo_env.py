"""构建 GUI 演示/测试环境（**真实数据**，独立数据库）。

截图、性能剖析、交互回归测试都用这个，避免三处各写一份。
刻意用独立的 DB（默认 `data/demo.db`），不碰正式的 `data/state.db`。

    from demo_env import make_env
    app, win, session = make_env(seed=True, match=True)
"""

from __future__ import annotations

import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

DEMO_DB = os.path.join(_ROOT, "data", "demo.db")


def reset_db(path: str) -> None:
    for ext in ("", "-wal", "-shm"):
        p = path + ext
        if os.path.exists(p):
            os.remove(p)


def seed(session, limit: int = 6, quiet: bool = True):
    """用 AnimeThemes 当季数据填库（不依赖 Bangumi / 浏览器）。"""
    prog = (lambda m: None) if quiet else (lambda m: print(f"    {m}"))
    return session.orchestrator(progress=prog).fetch_season_from_animethemes(
        session.season, limit=limit
    )


def match(session, quiet: bool = True):
    """跑真实网易云搜索与打分（需要网络，约 1 分钟）。"""
    prog = (lambda m: None) if quiet else (lambda m: print(f"    {m}"))
    return session.orchestrator(progress=prog).match_season(session.season)


def make_env(
    db: str | None = None,
    season: str = "2026Q4",
    seed_data: bool = True,
    run_match: bool = False,
    limit: int = 6,
    quiet: bool = True,
    reuse: bool = True,
):
    """返回 (app, window, session)。"""
    from PySide6.QtWidgets import QApplication

    from animusic.gui import style
    from animusic.gui.main_window import MainWindow
    from animusic.gui.session import WizardSession
    from animusic.models import Season
    from animusic.store import Store

    db_path = db or DEMO_DB
    fresh = not (reuse and os.path.exists(db_path))
    if fresh:
        reset_db(db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    session = WizardSession(store=Store(db_path), season=Season.parse(season))
    if seed_data and fresh:
        seed(session, limit=limit, quiet=quiet)
    if run_match:
        # 已有决策的曲目会被跳过，所以重复调用是安全的（增量语义）
        match(session, quiet=quiet)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(style.QSS)
    win = MainWindow(session)
    win.resize(1500, 980)
    win.show()
    for _ in range(10):
        app.processEvents()
    return app, win, session
