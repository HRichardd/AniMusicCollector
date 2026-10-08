"""AniMusicCollector 命令行入口 —— 打包成**控制台版** exe 时用它当入口。

为什么需要这个文件：`python -m animusic` 走的是 `src/animusic/__main__.py`，
而那里是相对导入（`from .cli import main`）。PyInstaller 只能拿一个脚本当入口，
把 `__main__.py` 直接当脚本会让相对导入失效 —— 所以要一层薄壳。

源码运行时同样可用（行为和 `python -m animusic` 一模一样）：

    $env:PYTHONPATH = "<工作区>\\src;<工作区>\\vendor"
    python run_cli.py selfcheck
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# 源码运行时要能找到 src/ 与 vendor/。打包后这两个目录不存在，
# 判断一下再插 —— 免得往 sys.path 里塞不存在的路径。
for _p in (os.path.join(_HERE, "src"), os.path.join(_HERE, "vendor")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)


def main() -> int:
    from animusic.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
