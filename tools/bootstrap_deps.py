"""绕过 pip 安装依赖。

为什么不用 pip / venv：
    本机 Python 3.14 的 ensurepip 与 pip 在清理临时目录时会调用 chmod，
    在受限环境下被拒绝（WinError 5），导致 venv 创建失败、pip 报错。

    因此这里直接：
      1. 从 PyPI 镜像读 simple index
      2. 用 urllib 下载合适的 wheel（纯 Python 或 win_amd64）
      3. 用 zipfile 解压到 vendor/
    全程不碰 tempfile，也不依赖 pip。

用法：
    python tools/bootstrap_deps.py                 # 装基础依赖
    python tools/bootstrap_deps.py pykakasi        # 追加安装
    python tools/bootstrap_deps.py --list          # 只看会装什么
"""

from __future__ import annotations

import os
import re
import sys
import zipfile
import urllib.parse
import urllib.request

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(WS, "vendor")

# 基础依赖。PySide6-Essentials 含 QtCore/QtGui/QtWidgets，够用且比完整版小很多。
BASE_PACKAGES = ["shiboken6", "pyside6-essentials"]

# 追加依赖（按需）：
#   pykakasi   —— 汉字/假名 → 罗马音，用于跨书写系统的曲名匹配（见 scorer.py）
#   rapidfuzz  —— 更高质量的模糊匹配（可选，缺失时 scorer 退化为 difflib）
EXTRA_PACKAGES = ["pykakasi"]

# wheel 文件名里可接受的平台/ABI 标记，按优先级从上到下
TAG_PRIORITY = [
    "py3-none-any",
    "py2.py3-none-any",
    "cp310-abi3-win_amd64",
    "cp39-abi3-win_amd64",
    "cp314-win_amd64",
    "cp313-win_amd64",
    "cp312-win_amd64",
]


def fetch_index(pkg: str) -> str:
    url = f"{MIRROR}/{pkg}/"
    req = urllib.request.Request(url, headers={"User-Agent": "AniMusicCollector-bootstrap/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def parse_wheels(html: str, index_url: str) -> list[tuple[str, str, int]]:
    """返回 [(版本, 绝对下载地址, tag 优先级)]，按 tag 优先级筛选。"""
    out = []
    for href in re.findall(r'href="([^"#]+\.whl)(?:#[^"]*)?"', html):
        name = os.path.basename(href)
        rank = next((i for i, t in enumerate(TAG_PRIORITY) if t in name), None)
        if rank is None:
            continue
        m = re.search(r"-(\d+\.\d+\.\d+(?:\.\d+)?)-", name)
        if not m:
            continue
        out.append((m.group(1), urllib.parse.urljoin(index_url, href), rank))
    return out


def vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def pick_version(per_pkg: dict[str, list[tuple[str, str, int]]]) -> str:
    """选一个所有包都有的最高版本。"""
    sets = [set(v for v, _, _ in wheels) for wheels in per_pkg.values()]
    common = set.intersection(*sets) if sets else set()
    if not common:
        detail = {k: sorted({v for v, _, _ in w})[-3:] for k, w in per_pkg.items()}
        raise SystemExit(f"找不到共同版本。各包可用版本：{detail}")
    return max(common, key=vkey)


def download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "AniMusicCollector-bootstrap/1.0"})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                print(f"\r    {os.path.basename(dest)}: {pct:3d}%  ({done >> 20}/{total >> 20} MB)", end="")
    print()


def extract(whl: str, target: str) -> int:
    n = 0
    with zipfile.ZipFile(whl) as z:
        for info in z.infolist():
            z.extract(info, target)
            n += 1
    return n


def main() -> int:
    extra = [a for a in sys.argv[1:] if not a.startswith("-")]
    packages = BASE_PACKAGES + (extra or EXTRA_PACKAGES if "--base-only" not in sys.argv else [])

    os.makedirs(VENDOR, exist_ok=True)
    print(f"工作区  : {WS}")
    print(f"安装到  : {VENDOR}")
    print(f"镜像    : {MIRROR}")
    print(f"包      : {', '.join(packages)}")
    print()

    per_pkg: dict[str, list[tuple[str, str, int]]] = {}
    for pkg in packages:
        print(f"读取索引 {pkg} ...")
        wheels = parse_wheels(fetch_index(pkg), f"{MIRROR}/{pkg}/")
        if not wheels:
            print(f"    ⚠ 没有匹配当前 Python/平台的 wheel，跳过")
            continue
        per_pkg[pkg] = wheels
        best = min(w[2] for w in wheels)
        print(f"    找到 {len(wheels)} 个 wheel（最佳 tag 优先级 {best}）")

    if not per_pkg:
        print("没有可安装的包")
        return 1

    # 独立包各自选最高版本即可（PySide6 系列需要版本一致）
    qt_pkgs = [p for p in per_pkg if p in ("shiboken6", "pyside6-essentials")]
    chosen: dict[str, str] = {}
    if qt_pkgs:
        v = pick_version({p: per_pkg[p] for p in qt_pkgs})
        for p in qt_pkgs:
            chosen[p] = v
    for p in per_pkg:
        if p not in chosen:
            chosen[p] = max({x[0] for x in per_pkg[p]}, key=vkey)

    print("\n选定版本:")
    for p, v in chosen.items():
        print(f"    {p} {v}")
    print()

    for pkg, version in chosen.items():
        cands = [(r, u) for (v, u, r) in per_pkg[pkg] if v == version]
        rank, url = min(cands, key=lambda x: x[0])
        whl = os.path.join(VENDOR, os.path.basename(url))
        if os.path.exists(whl) and os.path.getsize(whl) > 0:
            print(f"已存在，跳过下载: {os.path.basename(whl)}")
        else:
            print(f"下载 {pkg} {version} ...")
            download(url, whl)
        print(f"解压 {os.path.basename(whl)} ...")
        n = extract(whl, VENDOR)
        print(f"    {n} 个文件")

    print("\n清理 wheel ...")
    for f in os.listdir(VENDOR):
        if f.endswith(".whl"):
            os.remove(os.path.join(VENDOR, f))

    print("\n校验导入 ...")
    sys.path.insert(0, VENDOR)
    ok = True
    for mod, attr in [("PySide6", "__version__"), ("pykakasi", None), ("rapidfuzz", "__version__")]:
        try:
            m = __import__(mod)
            ver = getattr(m, attr) if attr else getattr(m, "__version__", "?")
            print(f"    {mod:<12} {ver}  OK")
        except ImportError:
            print(f"    {mod:<12} 未安装")
        except Exception as e:
            print(f"    {mod:<12} 导入失败: {type(e).__name__}: {e}")
            ok = False

    print("\n完成。运行 GUI：")
    print(f'    $env:PYTHONPATH = "{VENDOR}"')
    print("    python run_gui.py")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
