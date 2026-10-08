"""配色与 QSS。

深色主题，贴近网易云客户端观感。
"""

# ---------------------------------------------------------------- 调色板

BG = "#1b1c1f"
PANEL = "#242629"
PANEL_ALT = "#2b2e32"
BORDER = "#383b40"
BORDER_SOFT = "#2f3236"
TEXT = "#e8e9ea"
TEXT_MUTED = "#93989e"
TEXT_DIM = "#6d7278"

ACCENT = "#ec4141"        # 网易云红
ACCENT_DIM = "#a83232"

OK = "#4caf6d"
WARN = "#e0a33e"
ERR = "#d9534f"
INFO = "#4a9eda"
MANUAL = "#3a3a2a"        # 手工数据行底色
MANUAL_BORDER = "#6b6b3a"

# 置信度 → 颜色
SCORE_COLORS = {
    "auto": OK,
    "pending_review": WARN,
    "low": ERR,
    "missing_netease": TEXT_DIM,
    "missing_source": TEXT_DIM,
}


QSS = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 12px;
}}

QMainWindow, QDialog {{ background: {BG}; }}

/* ---------------- 面板 ---------------- */
QGroupBox {{
    background: {PANEL};
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
    margin-top: 14px;
    padding: 10px 10px 8px 10px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    color: {TEXT_MUTED};
    font-weight: 600;
}}

QFrame#Card {{
    background: {PANEL};
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
}}
QFrame#Toolbar {{
    background: {PANEL};
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
}}
QFrame#Divider {{ background: {BORDER_SOFT}; max-height: 1px; border: none; }}

/* ---------------- 候选卡片 ----------------
   用 objectName + 全局 QSS，避免每个卡片单独 setStyleSheet
   （实测：每个卡片一次 setStyleSheet 会让重建候选面板从几毫秒涨到 ~27ms） */
QFrame#CandCard {{
    background: {PANEL};
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
}}
QFrame#CandCardSel {{
    background: {PANEL_ALT};
    border: 1px solid {ACCENT};
    border-radius: 6px;
}}
QLabel#CandName {{ font-weight: 700; }}
QLabel#CandSub {{ color: {TEXT_MUTED}; font-size: 11px; }}
QLabel#CandAlias {{ color: {INFO}; font-size: 11px; }}
QLabel#Tag {{
    color: {TEXT_MUTED};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 0 4px;
    font-size: 10px;
}}
QLabel#TagVip {{ color: {ACCENT}; }}
QLabel#ScoreBig {{ font-size: 13px; font-weight: 700; }}
QLabel#ScoreBig[state="auto"] {{ color: {OK}; }}
QLabel#ScoreBig[state="pending_review"] {{ color: {WARN}; }}
QLabel#ScoreBig[state="low"] {{ color: {ERR}; }}
QLabel#ScoreBig[state="confirmed"] {{ color: {INFO}; }}
QLabel#ScoreBig[state="missing_netease"],
QLabel#ScoreBig[state="missing_source"] {{ color: {TEXT_DIM}; }}
QLabel#ScoreDetail {{ color: {TEXT_DIM}; font-size: 10px; }}

/* ---------------- 按钮 ---------------- */
QPushButton {{
    background: {PANEL_ALT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 12px;
    color: {TEXT};
}}
QPushButton:hover {{ background: #34383d; border-color: #4a4e54; }}
QPushButton:pressed {{ background: #2a2d31; }}
QPushButton:disabled {{ color: {TEXT_DIM}; background: #26282b; border-color: #2f3236; }}

QPushButton#Primary {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#Primary:hover {{ background: #f04b4b; }}
QPushButton#Primary:disabled {{ background: #5a2a2a; border-color: #5a2a2a; color: #b98a8a; }}

QPushButton#Ghost {{
    background: transparent;
    border: 1px solid {BORDER};
    color: {TEXT_MUTED};
}}
QPushButton#Ghost:hover {{ color: {TEXT}; border-color: #4a4e54; }}

/* ---------------- 输入 ---------------- */
QLineEdit, QComboBox, QSpinBox {{
    background: {PANEL_ALT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    selection-background-color: {ACCENT_DIM};
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT_DIM}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {PANEL_ALT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT_DIM};
    outline: none;
}}

/* ---------------- 表格 ---------------- */
QTableView {{
    background: {PANEL};
    gridline-color: {BORDER_SOFT};
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
    selection-background-color: #3a3f45;
    selection-color: {TEXT};
    outline: none;
}}
QTableView::item {{ padding: 4px 6px; border: none; }}
QTableView::item:selected {{ background: #3a3f45; }}

QHeaderView::section {{
    background: {PANEL_ALT};
    color: {TEXT_MUTED};
    padding: 6px 8px;
    border: none;
    border-right: 1px solid {BORDER_SOFT};
    border-bottom: 1px solid {BORDER_SOFT};
    font-weight: 600;
}}
QHeaderView::section:hover {{ background: #31353a; }}
QTableCornerButton::section {{ background: {PANEL_ALT}; border: none; }}

/* ---------------- 列表 / 滚动 ---------------- */
QListWidget {{
    background: {PANEL};
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
    outline: none;
}}
QListWidget::item {{ padding: 8px; border-bottom: 1px solid {BORDER_SOFT}; }}
QListWidget::item:selected {{ background: #3a3f45; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #464a50; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #565b62; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #464a50; border-radius: 5px; min-width: 30px; }}

/* ---------------- 文本 ---------------- */
QLabel#H1 {{ font-size: 15px; font-weight: 700; }}
QLabel#Muted {{ color: {TEXT_MUTED}; }}
QLabel#Dim {{ color: {TEXT_DIM}; font-size: 11px; }}
QLabel#SectionTitle {{ font-size: 13px; font-weight: 600; color: {TEXT}; }}

QTextEdit, QPlainTextEdit {{
    background: #1f2124;
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
    color: {TEXT_MUTED};
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 11px;
}}

/* ---------------- 进度 ---------------- */
QProgressBar {{
    background: {PANEL_ALT};
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}

/* ---------------- 复选 ---------------- */
QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background: {PANEL_ALT};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

/* ---------------- 分割器 ---------------- */
QSplitter::handle {{ background: {BORDER_SOFT}; }}
QSplitter::handle:horizontal {{ width: 2px; }}
QSplitter::handle:vertical {{ height: 2px; }}

/* ---------------- 状态栏 ---------------- */
QStatusBar {{ background: {PANEL}; color: {TEXT_MUTED}; border-top: 1px solid {BORDER_SOFT}; }}
QStatusBar::item {{ border: none; }}

QToolTip {{
    background: #35383d;
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px 8px;
}}
"""
