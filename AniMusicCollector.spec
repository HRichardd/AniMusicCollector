# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配方 —— 两个 exe（窗口版 + 控制台版）共用一份 COLLECT。

**别直接 `pyinstaller AniMusicCollector.spec`**，用构建脚本：

    python tools/build_exe.py

理由：构建脚本会先跑全量测试、再构建、再拿产物跑一遍离线自检（`selfcheck`），
并把**实测**体积打出来。spec 只负责"打成什么形状"。

几个非默认决定，每条都有实测依据（见 DESIGN.md 附录 G.21）：

  · **pykakasi 的词典要显式收集**：它没有 PyInstaller hook，而
    `pykakasi/properties.py` 运行时用 `importlib.resources.files(__package__)/"data"`
    读那 9.5 MB 的 `.db`。不收集的话 `import pykakasi` 照样成功 ——
    崩在真正查词典那一刻（罗马音匹配整条失效，而且只在匹配时才暴露）。
  · **rapidfuzz 的 DLL 要摆到包的兄弟目录**：`rapidfuzz/__init__.py` 里
    `os.add_dll_directory(<pkg>/../rapidfuzz.libs)`。PyInstaller 默认不会把
    `vendor/rapidfuzz.libs/` 里的 DLL 一起收，也不认得这个布局。
  · **upx=False**：UPX 压缩是杀软误报的常见来源，省下的那点体积不值当。
  · **窗口版 console=False**：不要黑框；**控制台版 console=True**：它才是崩溃时
    唯一的线索（窗口版启动失败时屏幕上一片空白）。
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

# 仓库根。spec 是被 exec 的，没有 `__file__` —— 正常路径是构建脚本通过环境变量
# 显式传进来；手动运行时退回 spec 所在目录（或当前目录）。
_ENV_ROOT = os.environ.get("ANIMUSIC_BUILD_ROOT")
ROOT = Path(_ENV_ROOT).resolve() if _ENV_ROOT else Path(
    globals().get("SPECPATH", os.getcwd())
).resolve()

SRC = ROOT / "src"
VENDOR = ROOT / "vendor"

# ---------------------------------------------------------------- 数据 / 二进制

#: 靠 hook 收不到的那两样（见文件头注释）
_DATAS = list(collect_data_files("pykakasi"))          # pykakasi/data/*.db
_BINARIES = [
    (str(dll), "rapidfuzz.libs")                       # 必须保持这个兄弟目录名
    for dll in sorted((VENDOR / "rapidfuzz.libs").glob("*.dll"))
]

#: `cli.cmd_doctor` 用 `__import__(名字)` 动态探依赖 —— 静态分析看不到名字，
#: 显式列出来（这几个本来也能被别的模块带到，属于防御性冗余）
_HIDDEN = ["yaml", "websockets", "rapidfuzz", "pykakasi"]

#: 明确用不到的标准库。**每一个都由构建后的 selfcheck 兜底**：
#: 万一排错了，跑 `python tools/build_exe.py` 会红，不会悄悄出一个坏 exe。
_EXCLUDES = ["tkinter", "unittest", "pydoc_data", "test"]


def _analysis(script: Path):
    """两个入口共用同一份配置。

    每次传**新列表**：Analysis 会就地加工传进去的 datas/binaries，
    共用同一个列表会让第二个入口收到重复项。
    """
    return Analysis(
        [str(script)],
        pathex=[str(SRC), str(VENDOR)],
        binaries=list(_BINARIES),
        datas=list(_DATAS),
        hiddenimports=list(_HIDDEN),
        excludes=list(_EXCLUDES),
        noarchive=False,
    )


# ---------------------------------------------------------------- 窗口版

a_gui = _analysis(ROOT / "run_gui.py")
pyz_gui = PYZ(a_gui.pure)
exe_gui = EXE(
    pyz_gui, a_gui.scripts, [],
    exclude_binaries=True,
    name="AniMusicCollector",
    console=False,          # 双击不要黑框
    upx=False,
    # 有图标了再打开这行（仓库里目前没有 .ico）
    # icon=str(ROOT / "assets" / "app.ico"),
)

# ---------------------------------------------------------------- 控制台版

a_cli = _analysis(ROOT / "run_cli.py")
pyz_cli = PYZ(a_cli.pure)
exe_cli = EXE(
    pyz_cli, a_cli.scripts, [],
    exclude_binaries=True,
    name="animusic",
    console=True,           # 崩溃时唯一的线索
    upx=False,
)

# ---------------------------------------------------------------- 一份目录装两个 exe
#
# 为什么不是两个独立的 onedir：那会把 200 MB 的 Qt **装两遍**。
# COLLECT 一次收齐两个入口的 binaries/datas，它们共用同一份依赖。

coll = COLLECT(
    exe_gui, a_gui.binaries, a_gui.datas,
    exe_cli, a_cli.binaries, a_cli.datas,
    strip=False,
    upx=False,
    name="AniMusicCollector",
)
