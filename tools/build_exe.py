"""把程序打成**绿色版 exe**（窗口版 + 控制台版）—— 构建、验收、报告一体。

    python tools/build_exe.py                 # 全量测试 → 构建 → 用产物自检 → 报体积
    python tools/build_exe.py --no-tests      # 跳过测试（只在自己已经跑过时用）
    python tools/build_exe.py --zip           # 额外产出可直接搬走的 zip

产物：`dist/<时间戳-唯一编号>/AniMusicCollector/`，保留旧产物和用户数据：

    AniMusicCollector.exe   窗口版（双击即用，无黑框）
    animusic.exe            控制台版（selfcheck / fetch / match / push / export）
    使用说明.txt             本脚本生成（含数据位置与 CC BY-SA 署名）

**为什么构建脚本要自己跑一遍产物**：PyInstaller 不报错 ≠ 打出来的东西能用。
实测踩过的两类：pykakasi 的词典（9.5 MB 的 `.db`）默认**不会**进 bundle，
`import pykakasi` 照样成功、崩在查词典那一刻；Qt 插件缺失时 import 也正常、
创建窗口才炸。所以这里必须拿产物跑 `selfcheck`（离线、不碰账号）才敢说"能用了"。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
VENDOR = ROOT / "vendor"
SPEC = ROOT / "AniMusicCollector.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_DIR = DIST / "AniMusicCollector"
#: 自检用的"程序根目录"：放在 .tmp/ 下，构建完的 dist/ 里就不会多出一份
#: `config/settings.yaml`（那是用户第一次运行时才该出现的东西）
SMOKE_HOME = ROOT / ".tmp" / "exe-smoke-home"
LOG_DIR = ROOT / ".tmp"


def configure_run() -> None:
    """每次构建独占目录，旧版用户数据和失败现场都不清理。"""
    global DIST, BUILD, APP_DIR, SMOKE_HOME, LOG_DIR
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    DIST = ROOT / "dist" / run_id
    LOG_DIR = ROOT / ".tmp" / ("build-exe-" + run_id)
    BUILD = LOG_DIR / "work"
    SMOKE_HOME = LOG_DIR / "smoke-home"
    APP_DIR = DIST / "AniMusicCollector"


def isolated_env(home: Path, *, source: bool = False) -> dict:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.update(ANIMUSIC_HOME=str(home), ANIMUSIC_CONFIG_DIR=str(home / "config"),
               QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    if source:
        env["PYTHONPATH"] = os.pathsep.join([str(SRC), str(VENDOR)])
        env["ANIMUSIC_BUILD_ROOT"] = str(ROOT)
    return env


def say(msg: str = "") -> None:
    print(msg, flush=True)


def head(msg: str) -> None:
    say("")
    say(f"=== {msg} " + "=" * max(0, 60 - len(msg)))


def decode_output(raw: bytes) -> str:
    """把子进程输出解码成文本 —— **逐行**判断，不要整文件一刀切。

    实测（`.tmp/build_exe_tests.log` / `build_exe_selfcheck.log`）：整个日志是
    UTF-8，**只有一行**不是 —— Qt 自己往 stderr 写的那句字体警告，它用的是
    本机 ANSI 代码页（`\\xa7\\xb3` 这种字节）。整文件"先试 UTF-8、失败就整体按
    GBK 读"会把好端端的中文全变成乱码，于是"selfcheck 全部通过"这个标记也匹配不上，
    白白报一个"打包成功但用不了"的假警报（真踩过这一次）。

    逐行判断则两种文件都对：控制台版 exe 的日志是纯 UTF-8；
    窗口版 exe（GUI 子系统、没有控制台、走不到 `cli._setup_console()`）是纯 GBK。
    """
    import locale

    fallback = locale.getpreferredencoding(False)
    out: list[str] = []
    for line in raw.splitlines(keepends=True):
        try:
            out.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            out.append(line.decode(fallback, errors="replace"))
    return "".join(out)


def run_logged(cmd: list[str], log: Path, env: dict | None = None,
               cwd: Path | None = None) -> tuple[int, str]:
    """跑一个子进程，输出**写文件**而不是管道。

    （管道在某些受限环境里会被拒；写文件到工作区内则一路畅通，而且出事时
    日志还在，能直接给人看。）
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as f:
        rc = subprocess.call(
            cmd, stdout=f, stderr=subprocess.STDOUT, env=env, cwd=str(cwd or ROOT)
        )
    return rc, decode_output(log.read_bytes())


def tail(text: str, n: int = 25) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def dir_size(p: Path) -> tuple[int, int]:
    total = files = 0
    for f in p.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
            files += 1
    return total, files


def mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


# ==================================================================== 各步骤

def preflight() -> dict:
    head("环境")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        say("✗ 没有 PyInstaller。先装：python -m pip install pyinstaller")
        raise SystemExit(2)
    from PyInstaller import __version__ as pv

    if not VENDOR.is_dir():
        say(f"✗ 找不到依赖目录 {VENDOR}")
        say("  先跑：python tools/bootstrap_deps.py")
        raise SystemExit(2)
    for f in (SPEC, ROOT / "run_gui.py", ROOT / "run_cli.py"):
        if not f.exists():
            say(f"✗ 缺少打包入口：{f}")
            raise SystemExit(2)

    say(f"Python       {sys.version.split()[0]}  ({sys.executable})")
    say(f"PyInstaller  {pv}")
    say(f"仓库根       {ROOT}")
    say(f"依赖目录     {VENDOR}（{mb(dir_size(VENDOR)[0])}）")
    return {"pyinstaller": pv, "python": sys.version.split()[0]}


def step_tests(env: dict) -> None:
    head("① 全量离线测试（跳过请用 --no-tests）")
    t0 = time.time()
    rc, text = run_logged(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "tests"],
        LOG_DIR / "build_exe_tests.log",
        env=env,
    )
    say(tail(text, 6))
    if rc != 0:
        say(f"✗ 测试没过（退出码 {rc}）—— 构建中止。完整输出：{LOG_DIR / 'build_exe_tests.log'}")
        raise SystemExit(1)
    say(f"✓ 测试通过（{time.time() - t0:.0f} 秒）")


def step_clean() -> None:
    head("② 准备独立构建目录（保留旧产物及数据）")
    for d in (BUILD, DIST, SMOKE_HOME):
        if d.exists():
            raise SystemExit(f"拒绝复用已有目录：{d.resolve()}")
        d.mkdir(parents=True)
        say(f"  新建 {d.resolve()}")


def step_build(env: dict) -> None:
    head("③ PyInstaller 构建（几分钟，取决于磁盘）")
    t0 = time.time()
    rc, text = run_logged(
        [
            sys.executable, "-m", "PyInstaller", str(SPEC),
            "--noconfirm",
            "--distpath", str(DIST),
            "--workpath", str(BUILD),
            "--log-level", "WARN",
        ],
        LOG_DIR / "build_exe_pyinstaller.log",
        env=env,
    )
    if rc != 0 or not APP_DIR.is_dir():
        say(tail(text, 30))
        say(f"✗ 构建失败（退出码 {rc}）。完整输出：{LOG_DIR / 'build_exe_pyinstaller.log'}")
        raise SystemExit(1)
    say(f"✓ 构建完成（{time.time() - t0:.0f} 秒）")


def step_smoke() -> None:
    """拿**产物**跑两件事，缺一不可。

    为什么两件都要：控制台版能自检通过，不能证明**窗口版**那个 exe 也能加载 Qt
    （它们是两个不同的 PE 文件）；反过来，窗口版 `--help` 只证明 Qt 能加载，
    证明不了词典/DLL 那些数据文件到位了。
    """
    head("④ 产物验收（跑的是 dist 里的 exe，不是源码）")
    SMOKE_HOME.mkdir(parents=True, exist_ok=True)
    exe_cli = APP_DIR / "animusic.exe"
    exe_gui = APP_DIR / "AniMusicCollector.exe"
    for f in (exe_cli, exe_gui):
        if not f.exists():
            say(f"✗ 产物里没有 {f.name}")
            raise SystemExit(1)

    # ⚠️ 故意**不给 PYTHONPATH**：打包产物必须自带依赖。
    #    留着它，产物偷偷从源码树 import 也会"通过"，把漏打的文件全掩盖掉。
    smoke_env = isolated_env(SMOKE_HOME)

    # (1) 控制台版：离线自检（词典 / DLL / 路径 / Qt 构造）
    rc, text = run_logged([str(exe_cli), "selfcheck"],
                          LOG_DIR / "build_exe_selfcheck.log", env=smoke_env)
    say(tail(text, 18))
    if rc != 0 or "selfcheck: 依赖自检通过" not in text:
        say(f"✗ 产物自检没过（退出码 {rc}）—— 这就是「打包成功但用不了」的样子。")
        say(f"  完整输出：{LOG_DIR / 'build_exe_selfcheck.log'}")
        raise SystemExit(1)
    say("✓ 控制台版自检通过")

    # (2) 换个工作目录再跑一次：打包最常见的坑是「只有在构建目录里启动才行」
    #     （偷偷依赖相对路径或外部环境）。从产物自己的目录启动才算自洽。
    rc, text = run_logged([str(exe_cli), "selfcheck"],
                          LOG_DIR / "build_exe_selfcheck_cwd.log",
                          env=smoke_env, cwd=APP_DIR)
    if rc != 0 or "selfcheck: 依赖自检通过" not in text:
        say(tail(text, 18))
        say(f"✗ 从产物目录启动就失败了（退出码 {rc}）—— 说明它依赖构建目录")
        raise SystemExit(1)
    say("✓ 从产物自己的目录启动同样通过（不依赖构建目录）")

    # (3) 窗口版：`--help` 会在建窗口**之前**退出，所以既能验证 Qt 真的能加载，
    #     又不会在无人看着的时候弹出界面（AGENTS.md §2）。
    #
    # ⚠️ 判据只能用 **ASCII**（`--season`），不能用中文：
    #    窗口版是 GUI 子系统程序 —— 没有控制台、也走不到 `cli._setup_console()`，
    #    它输出的是本机 ANSI 代码页（这里是 GBK）字节。踩过一次：按 UTF-8 读日志，
    #    中文全变乱码，于是"窗口版跑不起来"是个**假警报**（退出码明明是 0）。
    #    rc==0 本身已经证明 Qt 加载成功：`from PySide6.QtWidgets import QApplication`
    #    失败时 `run_gui.main()` 会打印提示并 return 1，根本走不到 argparse。
    rc, text = run_logged([str(exe_gui), "--help"],
                          LOG_DIR / "build_exe_gui_help.log", env=smoke_env)
    if rc != 0 or "--season" not in text or "--db" not in text:
        say(tail(text, 20))
        say(f"✗ 窗口版连 --help 都跑不起来（退出码 {rc}）—— Qt 没打进产物")
        raise SystemExit(1)
    say("✓ 窗口版能加载 Qt（--help 通过，没有弹窗）")

    # (4) 文件系统级检查：补上 selfcheck **测不到**的那一格。
    #     selfcheck 用的是 **offscreen** 平台插件，而用户双击走的是 **qwindows**。
    #     不能无人值守地真开一次窗口，所以退一步查文件在不在 —— 这是**代理指标**，
    #     不是"我验证过窗口能开"（§5：说清是实测还是替代观测）。
    must = [
        (APP_DIR / "_internal" / "PySide6" / "plugins" / "platforms" / "qwindows.dll",
         "Windows 平台插件 —— 缺它双击后什么都不发生"),
        (APP_DIR / "_internal" / "PySide6" / "plugins" / "styles" / "qmodernwindowsstyle.dll",
         "原生控件样式 —— 缺它界面会是老式外观"),
        (APP_DIR / "_internal" / "rapidfuzz.libs", "rapidfuzz 的 CRT DLL 目录"),
        (APP_DIR / "_internal" / "pykakasi" / "data", "pykakasi 词典（9.5 MB 的 .db）"),
    ]
    for p, why in must:
        if not p.exists():
            say(f"✗ 产物里缺：{p}（{why}）")
            raise SystemExit(1)
        say(f"    ✓ {p.relative_to(APP_DIR)}")


def step_readme() -> None:
    """在产物里放一份说明 —— 打包版最容易踩的两个坑都写在这儿。"""
    head("⑤ 生成 使用说明.txt")
    sys.path.insert(0, str(SRC))
    sys.path.insert(0, str(VENDOR))
    from animusic.sources.wikipedia import ATTRIBUTION  # 署名只有这一处定义（§7）

    text = f"""AniMusicCollector —— 绿色版（免安装）
{"=" * 46}

怎么用（四步）
  双击 AniMusicCollector.exe 打开界面。第一次先做一件事：
  在「环境」那两行各点一次「浏览…」——
      · 浏览器：Chrome / Edge / Brave 这类 Chromium 内核的（Firefox 不行）
      · 网易云音乐客户端：cloudmusic.exe
  选完会自动记住，下次不用再选。

  然后照着走四步：
      ① 点「从 Bangumi 拉取「在看」」→ 不要的番取消「纳入」→ 点「确认并下一步」
      ② 点「拉取 / 更新 OP/ED」      → 不要的曲目取消「收录」→ 点「确认并下一步」
      ③ 点「开始匹配网易云」         → 需要过目的行点候选确认  → 点「确认并下一步」
      ④ 点「试运行」看清单           → 确认无误再点「推送到歌单」写入

  每步点按钮后，底部日志面板会写它做了什么、每个数字什么意思。

出问题
  · 先跑这个（离线、不联网、不碰账号）：
        animusic.exe selfcheck
  · 看全部子命令：animusic.exe --help
  · 网页/客户端连不上：检查那两行的路径与端口，或点「复制命令」自己在终端启动
  · 网易云起来后没有托盘图标、没有登录态：把「整个文件夹」拷到一个普通位置再双击
    （程序被「低完整性」标签降级了；界面启动日志里「进程令牌」那行会显示 完整性=…）

本目录里放了什么
  · AniMusicCollector.exe   图形界面（双击即用）
  · animusic.exe            命令行版（selfcheck / fetch / match / push / export …）
  · _internal\\              依赖与运行时（注意：别把 exe 单独拷走）

数据在哪（默认就在本目录，不需要额外配置）
  · 数据库  data\\state.db      ← 你的番单、曲目、匹配决策、推送记录
  · 配置    config\\            ← 两个程序路径、端口、别名表
  · 报告    reports\\           ← 导出的 CSV / 清单
  · 日志    logs\\              ← 启动外部程序时子进程自己的输出
  · 想搬位置或沿用旧数据：设环境变量 ANIMUSIC_HOME=<目录>，
    或把 data\\ 与 config\\ 整个拷到新位置（本目录：{APP_DIR}）

数据来源与许可
  {ATTRIBUTION}
  转发界面截图或导出的报告时，请一并保留这行署名。
"""
    out = APP_DIR / "使用说明.txt"
    out.write_text(text, encoding="utf-8")
    say(f"✓ {out}（引用 sources/wikipedia.py 的 ATTRIBUTION，没有抄第二份）")


def read_integrity_label(path: Path) -> int | None:
    """用 Windows API 读取强制标签，避免依赖 icacls 的本地化输出。"""
    import ctypes as ct
    from ctypes import wintypes as wt

    api = ct.WinDLL("advapi32", use_last_error=True)
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    get = api.GetNamedSecurityInfoW
    get.argtypes = [wt.LPWSTR, wt.DWORD, wt.DWORD] + [ct.POINTER(ct.c_void_p)] * 5
    get.restype = wt.DWORD
    convert = api.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [ct.c_void_p, wt.DWORD, wt.DWORD,
                        ct.POINTER(ct.c_void_p), ct.POINTER(wt.DWORD)]
    convert.restype = wt.BOOL
    kernel.LocalFree.argtypes = [ct.c_void_p]
    kernel.LocalFree.restype = ct.c_void_p
    descriptor, output = ct.c_void_p(), ct.c_void_p()
    rc = get(str(path), 1, 0x10, None, None, None, None, ct.byref(descriptor))
    if rc:
        raise OSError(rc, f"读取强制标签失败：{path}")
    try:
        if not convert(descriptor, 1, 0x10, ct.byref(output), None):
            raise ct.WinError(ct.get_last_error())
        sddl = ct.wstring_at(output)
        labels = re.findall(r"\(ML;[^)]*;;;([^;)]+)\)", sddl)
        if not labels:
            if "(ML;" in sddl:
                raise ValueError(f"无法识别强制标签：{sddl}")
            return None
        known = {"UN": 0, "LW": 4096, "ME": 8192, "MP": 8448,
                 "HI": 12288, "SI": 16384}
        values = []
        for sid in labels:
            if sid in known:
                values.append(known[sid])
            elif sid.startswith("S-1-16-"):
                values.append(int(sid.rsplit("-", 1)[1]))
            else:
                raise ValueError(f"未知完整性 SID：{sid}")
        return min(values)
    finally:
        if output:
            kernel.LocalFree(output)
        kernel.LocalFree(descriptor)


def step_label_check() -> list[dict]:
    """产物的**完整性标签**检查 —— 一次真机排查换来的检查项。

    实测踩过：工作区 `D:\\...\\AniMusicCollector` 带着
    `Mandatory Label\\Low Mandatory Level:(OI)(CI)(NW)`，里面的 exe 继承它 ⇒
    **双击启动的程序被降级成低完整性**，于是它拉起的 Chrome / 网易云写不了
    `%LOCALAPPDATA%`（网易云没托盘图标、没登录态），而手工在终端里跑同一个命令
    却正常（启动的是无标签的 exe）、源码版也正常（启动的是无标签的 python.exe）。

    构建结束读取目录和两个 exe 的标签；低标签或无法读取都阻止交付。
    文件标签不能证明启动上下文正常，实际进程令牌仍须真机确认。
    """
    head("⑥ 产物完整性标签")
    results = []
    failed = False
    for path in (APP_DIR, APP_DIR / "animusic.exe", APP_DIR / "AniMusicCollector.exe"):
        try:
            rid = read_integrity_label(path)
            status = "通过" if rid is None or rid >= 8192 else "失败"
            results.append({"path": path.name, "rid": rid, "status": status})
            say(f"  {path.name}: {status}（标签 RID={rid}）")
            failed |= status != "通过"
        except Exception as e:
            failed = True
            results.append({"path": path.name, "status": "无法检查", "error": str(e)})
            say(f"  {path.name}: 无法检查（{e}）")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "integrity.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if failed:
        raise SystemExit("完整性标签检查未通过，停止交付；未修改系统权限。")
    say("  仅验证文件标签；双击启动的实际进程令牌和外部程序行为仍需真机验收。")
    return results


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_hashes() -> dict[str, str]:
    paths = [SPEC, ROOT / "run_cli.py", ROOT / "run_gui.py", Path(__file__)]
    paths.extend(SRC.rglob("*.py"))
    return {p.relative_to(ROOT).as_posix(): file_hash(p) for p in sorted(paths)}


def step_manifest(info: dict, sources: dict, tests_run: bool, labels: list) -> None:
    if sources != source_hashes():
        raise SystemExit("构建期间源码发生变化，请重新构建，避免清单与产物不一致。")
    dependencies = {}
    for metadata in sorted(VENDOR.glob("*.dist-info/METADATA")):
        from email.parser import Parser
        parsed = Parser().parsestr(metadata.read_text(encoding="utf-8"))
        dependencies[parsed["Name"]] = parsed["Version"]
    manifest = {"built_at_utc": datetime.now(timezone.utc).isoformat(),
                "tools": info, "dependencies": dependencies, "sources_sha256": sources,
                "tests": "passed" if tests_run else "skipped",
                "offline_smoke": "dependencies_passed", "integrity": labels,
                "environment_details": "see build_exe_selfcheck.log in log_directory",
                "log_directory": str(LOG_DIR),
                "real_machine_acceptance": "pending",
                "exe_sha256": {p.name: file_hash(p) for p in APP_DIR.glob("*.exe")}}
    (APP_DIR / "build-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def step_report(zip_it: bool) -> None:
    head("⑦ 实测结果")
    total, files = dir_size(APP_DIR)
    say(f"产物目录     {APP_DIR}")
    say(f"体积         {mb(total)}（{files} 个文件）")
    for name in ("AniMusicCollector.exe", "animusic.exe", "使用说明.txt"):
        f = APP_DIR / name
        if f.exists():
            say(f"  {name:<24} {f.stat().st_size / 1024 / 1024:8.2f} MB")

    # 谁在吃体积 —— 只报实测，不估算
    sizes = []
    for d in sorted(APP_DIR.iterdir()):
        if d.is_dir():
            sizes.append((dir_size(d)[0], d.name))
        else:
            sizes.append((d.stat().st_size, d.name))
    sizes.sort(reverse=True)
    say(f"  体积最大的几项（顶层共 {len(sizes)} 项）：")
    for size, name in sizes[:8]:
        say(f"    {mb(size):>10}  {name}")
    # `_internal/` 一般吃掉九成体积，再往里看一眼是哪个包
    inner = APP_DIR / "_internal"
    if inner.is_dir():
        deep = sorted(((dir_size(d)[0], d.name) for d in inner.iterdir() if d.is_dir()),
                      reverse=True)
        if deep:
            say("    _internal/ 里最大的是：")
            for size, name in deep[:5]:
                say(f"      {mb(size):>10}  {name}")

    if zip_it:
        head("⑦ 打 zip")
        zpath = shutil.make_archive(str(DIST / "AniMusicCollector"),
                                    "zip", root_dir=str(DIST),
                                    base_dir="AniMusicCollector")
        say(f"✓ {zpath}（{mb(Path(zpath).stat().st_size)}）")

    head("完成")
    say(f"双击试用：{APP_DIR / 'AniMusicCollector.exe'}")
    say(f"出问题时：{APP_DIR / 'animusic.exe'} selfcheck")


def main() -> int:
    ap = argparse.ArgumentParser(description="打包成绿色版 exe（窗口版 + 控制台版）")
    ap.add_argument("--no-tests", action="store_true",
                    help="跳过全量离线测试（只在你刚跑过时用）")
    ap.add_argument("--skip-build", action="store_true",
                    help="不重新构建，只拿现有 dist 跑验收（改验收逻辑时省时间；"
                         "也可以拿它复验一个已经拷到别处的产物）")
    ap.add_argument("--zip", action="store_true", help="额外产出一个可分发的 zip")
    ap.add_argument("--app-dir", type=Path, help="--skip-build 时指定已有产物目录（只读复验）")
    args = ap.parse_args()
    if args.app_dir and not args.skip_build:
        ap.error("--app-dir 只能与 --skip-build 一起使用")
    if args.skip_build and args.zip:
        ap.error("复验模式不打 zip，避免打入既有用户数据")

    existing_app = args.app_dir.resolve() if args.app_dir else ROOT / "dist" / "AniMusicCollector"
    configure_run()
    global APP_DIR
    if args.skip_build:
        APP_DIR = existing_app

    info = preflight()

    env = isolated_env(LOG_DIR / "test-home", source=True)
    sources = source_hashes()

    if args.skip_build:
        if not APP_DIR.is_dir():
            say(f"✗ --skip-build 需要已有产物：{APP_DIR}")
            return 1
        head("①②③ 已按 --skip-build 跳过（不重新构建）")
    else:
        if args.no_tests:
            head("① 全量离线测试 —— 按 --no-tests 跳过")
        else:
            step_tests(env)
        step_clean()
        step_build(env)
    step_smoke()
    labels = step_label_check()
    if not args.skip_build:
        step_readme()
        step_manifest(info, sources, not args.no_tests, labels)
    step_report(args.zip)
    say(f"（本次构建：Python {info['python']} / PyInstaller {info['pyinstaller']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
