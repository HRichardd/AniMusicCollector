"""启动第三方程序（浏览器 / 网易云客户端）—— 一处实现，两处调用。

为什么要单独成模块：这条路径有两个调用方（GUI 的「启动并接管」和 CLI 的
`animusic launch`），而它踩过的坑全在**看不见的进程上下文**上 ——
两处各写一份，必然只有一处被修好。

## 已经踩过的那次（真机反馈）

打包成 exe 之后：**同一个命令行，用户在终端里手工启动正常；
由程序用 subprocess 拉起来就坏**（Chrome 弹出「进程启动后立即退出
exit code 2147483651」但其实浏览器起来了；网易云客户端起了一个没托盘图标、
没登录态的残缺实例）。

`.tmp/probe_child.py` 是个假"浏览器"：被启动后把自己拿到的上下文 dump 成 JSON。
用它分别经**终端**和**打包版程序**启动，逐项对比，差别只有三处 —— 都在这里收口：

| # | 差别 | 后果 | 这里怎么处理 |
|---|---|---|---|
| 1 | `PATH` 被 PyInstaller 插进了 bundle 的两段（`_internal\\PySide6`、`_internal`），另有 `QT_PLUGIN_PATH` / `QML2_IMPORT_PATH` / `PYSIDE6_OPTION_PYTHON_ENUM` | 子进程会继承：**任何按名字加载 DLL 的第三方程序**都可能加载到我们的 DLL（bundle 里有 `ucrtbase` / `MSVCP140` / `VCRUNTIME140` / `opengl32sw`），Qt 写的程序还会去加载我们这套 Qt 插件 | `clean_env()` 摘掉，让子进程环境和"手工在终端启动"一致 |
| 2 | 三个标准句柄是 **NULL**（`DETACHED_PROCESS` + `close_fds` 的结果） | 手工启动时是有效句柄；NULL 句柄会让程序写日志时走进 CRT 的无效参数路径 | 指向**有效**的日志文件（拿不到就退 `NUL`） |
| 3 | `DETACHED_PROCESS` 让子进程连控制台都没有 | 手工启动是有控制台的。而且子进程能活过父进程**不需要**这个标志（Windows 本来就不会因为父进程退出而杀子进程） | 不再使用 |

另外把子进程的 stdout/stderr **落到日志文件**（`<程序根目录>/logs/launch-*.log`）：
这次排查最大的障碍就是"Chrome 自己的报错我们一个字都看不到"。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import IO

from . import constants

#: 第三方子进程**不该**从我们这里继承的环境变量。
#:
#: 全是 PyInstaller / PySide6 的运行时钩子塞进来的，分三类：
#:   1. 指向**我们 bundle** 的路径 —— 子进程会照着它去找 DLL
#:   2. Qt 插件路径 —— Qt 写的第三方程序会加载我们这套插件（版本不匹配时
#:      表现为"起来了但残废"，例如没有托盘图标、登录态出不来）
#:   3. Python / PyInstaller 自己的开关
_STRIP_ENV = (
    "QT_PLUGIN_PATH",
    "QML2_IMPORT_PATH",
    "PYSIDE6_OPTION_PYTHON_ENUM",
    # 我们自己的界面用 offscreen 跑测试时，别把第三方程序也按到 offscreen
    "QT_QPA_PLATFORM",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "_PYI_ARCHIVE_FILE",
    "_PYI_PARENT_PROCESS_LEVEL",
)


def bundle_roots() -> list[str]:
    """属于**我们自己**的目录（子进程不该从这里找 DLL）。

    onedir 打包下 `sys._MEIPASS` 就是 `<程序目录>\\_internal`。
    源码运行时它不存在、`sys.frozen` 也是假 ⇒ 返回空列表 ⇒ 什么都不改，
    行为与打包前一致（测试因此可以正常跑）。
    """
    roots: list[str] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(os.path.normcase(os.path.abspath(meipass)))
    if getattr(sys, "frozen", False):
        roots.append(
            os.path.normcase(os.path.abspath(os.path.dirname(sys.executable)))
        )
    return roots


def clean_env(base: dict | None = None) -> dict[str, str]:
    """给第三方程序的环境 —— 目标是「和用户手工在终端里启动**一模一样**」。

    `base` 只为测试而存在（默认取 `os.environ`）。
    """
    env = dict(os.environ if base is None else base)
    for k in _STRIP_ENV:
        env.pop(k, None)

    roots = bundle_roots()
    path = env.get("PATH")
    if roots and path:
        keep: list[str] = []
        for part in path.split(os.pathsep):
            if not part:
                # PATH 里的空项语义是「当前目录」。不替用户改语义，原样留着。
                keep.append(part)
                continue
            p = os.path.normcase(os.path.abspath(part))
            if any(p == r or p.startswith(r + os.sep) for r in roots):
                continue
            keep.append(part)
        env["PATH"] = os.pathsep.join(keep)
    return env


def log_path(label: str) -> str:
    """子进程输出落到哪儿。放 `<程序根目录>/logs/` 下，按目标分文件。"""
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:32]
    return os.path.join(constants.app_root(), "logs", f"launch-{slug or 'target'}.log")


#: 子进程日志的上限。Chrome 的 stderr 很啰嗦，长年累月会堆成个巨无霸 —— 超过就重来。
_LOG_CAP = 2 * 1024 * 1024


def _child_output(label: str) -> tuple[IO | None, dict]:
    """给子进程准备的 stdout/stderr —— 一个**追加写**的日志文件。

    为什么不留 DEVNULL/NULL：这次排查最大的障碍就是"Chrome 自己的报错我们
    一个字都看不到"。留个文件，下次它有话说就能看到。

    ⚠️ 代价：子进程活着的时候这个文件**被它占着**（Windows 上删不掉、改不了名）。
    所以构建脚本清 `dist/` 时，会因为"由本程序启动的 Chrome 还开着"而失败 ——
    那条提示写在 `tools/build_exe.py::step_clean` 里。

    拿不到可写位置（例如程序装在 Program Files 下）就退回 DEVNULL ——
    启动第三方程序**不能**因为写不了日志而失败。
    """
    try:
        p = log_path(label)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        try:
            if os.path.getsize(p) > _LOG_CAP:
                with open(p, "wb"):
                    pass    # 截断重来（改名会因为文件正被占用而失败）
        except OSError:
            pass
        f = open(p, "ab")
        f.write(
            f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {label} =====\n".encode(
                "utf-8", "replace"
            )
        )
        f.flush()
        return f, {"stdout": f, "stderr": f}
    except Exception:  # noqa: BLE001
        return None, {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}


def external(exe: str, args: list[str], *, label: str = "", cwd: str | None = None):
    """拉起一个第三方程序，返回 `subprocess.Popen`。

    与 `subprocess.Popen` 的默认值故意有三处不同，每处都由实测差异驱动（见文件头）：
    `env=clean_env()`、标准句柄是**有效**的、**不用** `DETACHED_PROCESS`。
    """
    logf, out = _child_output(label or os.path.basename(exe))
    try:
        return subprocess.Popen(
            [exe, *args],
            cwd=cwd or (os.path.dirname(exe) or None),
            env=clean_env(),
            stdin=subprocess.DEVNULL,
            close_fds=True,
            **out,
        )
    finally:
        # 子进程已经拿到自己的句柄副本了，父进程这份可以关掉
        # （不关的话每启动一次漏一个句柄；关掉**不影响**子进程继续写）。
        if logf is not None:
            try:
                logf.close()
            except Exception:  # noqa: BLE001
                pass


def wait_for_port(proc, port: int, timeout: float, *, on_waited=None) -> dict:
    """等调试端口，**不因为子进程退出就判失败**。

    实测（用户真机）：Chrome 的 `chrome.exe` 只是**交接用的桩** ——
    它把命令行交给真正的浏览器进程之后就退出，退出码是 `0x80000003`
    （2147483651）。原来的实现一见退出码就报「进程启动后立即退出」，
    而浏览器其实已经起来了、端口一两秒后就开 —— 那是一个**误报**。

    所以：进程退出只是记下来，继续把时间等完；只有端口始终不开才算失败。
    返回 `{"ok": bool, "msg": str, "exit_code": int | None}`。
    """
    t0 = time.time()
    exited: int | None = None
    while time.time() - t0 < timeout:
        if _port_open(port):
            msg = f"已接管，端口 {port} 就绪（{time.time() - t0:.1f}s）"
            if exited is not None:
                msg += f"；启动器进程已退出（code {exited}），属正常交接"
            return {"ok": True, "msg": msg, "exit_code": exited}
        rc = proc.poll()
        if rc is not None and exited is None:
            exited = rc
            if on_waited is not None:
                on_waited(
                    f"启动器进程已退出（code {rc}），继续等调试端口 {port} …"
                    "（Chrome 的 chrome.exe 只是交接用的桩，退出不等于失败）"
                )
        elif on_waited is not None:
            on_waited(f"等待调试端口 {port} … {int(time.time() - t0)}s")
        time.sleep(0.5)

    if exited is not None:
        return {
            "ok": False,
            "exit_code": exited,
            "msg": f"{timeout:.0f} 秒内未开放调试端口 {port}"
                   f"（启动器进程退出码 {exited}）",
        }
    return {
        "ok": False,
        "exit_code": None,
        "msg": f"{timeout:.0f} 秒内未开放调试端口 {port}（进程仍存活）",
    }


def _port_open(port: int, timeout: float = 0.4) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False
