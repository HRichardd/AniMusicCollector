"""命令行入口 —— `animusic <command>`。

GUI 之外的等价入口，便于调试与自动化。
用 argparse 而非 typer：省掉 click/annotated-doc/shellingham 一串传递依赖。
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

from . import constants
from .models import Season


def _setup_console() -> None:
    """Windows 控制台默认是 GBK(936) 代码页，中文输出会乱码。

    两层一起做：
      1. 把控制台输出代码页切到 UTF-8（SetConsoleOutputCP）
      2. 把 Python 的 stdout/stderr 也按 UTF-8 重新配置
    只做第 2 步不够 —— 往 GBK 控制台写 UTF-8 字节依然是乱码。
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass
    for stream in ("stdout", "stderr"):
        s = getattr(sys, stream, None)
        if s is not None and hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


_setup_console()

# ---------------------------------------------------------------- 输出

try:
    from rich.console import Console

    _console = Console()

    def out(msg: str = "", style: str = "") -> None:
        _console.print(msg, style=style or None, highlight=False)

    def head(msg: str) -> None:
        _console.rule(f"[bold]{msg}")

except ImportError:  # pragma: no cover
    def out(msg: str = "", style: str = "") -> None:
        print(msg)

    def head(msg: str) -> None:
        print(f"\n=== {msg} ===")


def _progress(msg: str) -> None:
    out(f"  {msg}")


# ---------------------------------------------------------------- doctor

def cmd_doctor(args) -> int:
    from .config import Config
    from .http import HttpClient

    head("环境自检")
    ok = True

    out(f"Python      {sys.version.split()[0]}  ({sys.executable})")

    # 依赖
    deps = ["yaml", "websockets", "rapidfuzz", "pykakasi"]
    for d in deps:
        try:
            __import__(d)
            out(f"依赖 {d:<12} OK")
        except ImportError:
            out(f"依赖 {d:<12} [red]缺失[/red]（功能会降级）", "yellow")

    # 配置
    cfg = Config.load()
    out(f"配置文件    {cfg.path}{'' if os.path.exists(cfg.path) else '  [不存在，用默认值]'}")

    # 数据源
    head("数据源")

    # ⚠️ 顺序有讲究：**维基现在是曲目主数据源**，所以先查它。
    #    以前这里只查 AnimeThemes（那只是回退路径），于是维基挂了自检照样报
    #    "自检通过"，用户在 fetch 阶段才撞上 —— 自检没查到最要紧的那一环。
    from .sources.wikipedia import WikipediaSource, ATTRIBUTION

    try:
        # ⚠️ `retries=0`：自检要回答的是「**此刻**通不通」，而带着重试去问
        #    会把 429（限流）自己放大 3 倍 —— 自检反倒成了压垮出口 IP 的
        #    那一下。真机实测过：一次冷启动里 62 次维基调用变成 120 次网络请求，
        #    多出来的 58 次全是重试，28 次失败全是 `HTTP 429 Too Many Requests`。
        wp = WikipediaSource(retries=0)
        # 用一个**必然存在**且标题唯一的词条做连通性探测。
        # 别用容易撞名的（试过 "名探偵プリキュア"，返回的首条是 "名探偵コナン" ——
        # 功能没错，但打印出来像是坏了，自检输出必须一眼可信）。
        titles = wp.search("鬼滅の刃", limit=1)
        if titles:
            out(f"维基百科    OK  曲目主数据源（探测到「{titles[0]}」）")
            out(f"                  {ATTRIBUTION}")
        elif wp.stats.get("api_failed"):
            # 区分「没问到」与「确实没有」—— 单次请求就失败，那是网络/限流，
            # 不是「维基没收录鬼滅の刃」（对称的错误见 §6 / G.11）。
            reason = wp.stats.get("last_error") or "请求失败"
            hint = ("（单次请求就被拒 —— 说明是这个出口 IP 在被限流，"
                    "换个节点再试）" if "429" in reason else
                    "（单次请求就失败 —— 这是网络问题，不是「维基没有这个条目」）")
            out(f"维基百科    失败：{reason}{hint}", "red")
            ok = False
        else:
            out("维基百科    失败：搜索返回空结果", "red")
            ok = False
    except Exception as e:  # noqa: BLE001
        out(f"维基百科    失败：{type(e).__name__}: {e}", "red")
        ok = False

    from .sources.animethemes import AnimeThemesSource

    try:
        at = AnimeThemesSource()
        s = Season.parse(args.season)
        animes = at.fetch_season(s)
        out(f"AnimeThemes  OK  {s.key} 共 {len(animes)} 部"
            f"（有 OP/ED 的 {len([a for a in animes if a.themes])} 部）"
            f" —— 仅作**回退**路径")
    except Exception as e:  # noqa: BLE001
        out(f"AnimeThemes  失败：{type(e).__name__}: {e}", "red")
        ok = False

    from .collector.api_search import ApiSearchCollector

    st = ApiSearchCollector().health()
    out(f"网易云搜索   {'OK' if st.ok else '失败'}  {st.detail}", "green" if st.ok else "red")
    ok = ok and st.ok

    # CDP 目标
    head("CDP 目标")
    from .cdp import port_open

    if port_open(cfg.browser.debug_port):
        from .sources.browser_bridge import BrowserBridge

        br = BrowserBridge(cfg.browser.debug_port)
        st = br.health()
        out(f"浏览器 :{cfg.browser.debug_port}  {'OK' if st.ok else '异常'}  {st.detail}",
            "green" if st.ok else "yellow")
        try:
            uid = br.bangumi_uid()
            out(f"Bangumi 登录    {'UID=' + uid if uid else '未登录'}", "green" if uid else "yellow")
            if not uid:
                ok = False
        except Exception as e:  # noqa: BLE001
            out(f"Bangumi 登录    读不到：{e}", "yellow")
    else:
        out(f"浏览器 :{cfg.browser.debug_port}  未监听 —— "
            f"请用 `animusic launch browser` 或手动带调试参数启动", "yellow")

    if port_open(cfg.netease.debug_port):
        from .cdp import CDPBrowser

        try:
            info = CDPBrowser(cfg.netease.debug_port).version()
            out(f"网易云 :{cfg.netease.debug_port}  OK  {info.browser}", "green")
        except Exception as e:  # noqa: BLE001
            out(f"网易云 :{cfg.netease.debug_port}  异常：{e}", "yellow")
    else:
        out(f"网易云 :{cfg.netease.debug_port}  未监听（M1 不需要，M3 写入时才需要）", "yellow")

    head("结论")
    out("自检通过" if ok else "有项目失败，见上文", "green" if ok else "red")
    return 0 if ok else 1


# ---------------------------------------------------------------- selfcheck

def cmd_selfcheck(args) -> int:
    """**离线**自检 —— 打包产物的验收命令（不联网、不碰账号、不写用户数据）。

    为什么不能拿 `doctor` 当冒烟测试：
      · doctor 要联网查维基，构建脚本没法依赖它（代理一断就红）
      · doctor 只 `__import__` 依赖 —— 而「库能 import」和「它的数据文件真的进了
        bundle」是两件完全不同的事。实测：pykakasi 没有 PyInstaller hook，
        它的词典是 9.5 MB 的 `.db`，默认**不会**被打进去；`import pykakasi` 照样成功，
        崩的是真正查词典那一刻。rapidfuzz 同理（靠 `rapidfuzz.libs` 兄弟目录里的 DLL）。

    所以这里每一节都**真的用一遍**：建词典 → 算相似度 → 构造一次 Qt 窗口。
    输出用固定的 `selfcheck:` 前缀，构建脚本按「退出码 0 + 出现『全部通过』」断言。
    """
    from . import constants as C

    head("离线自检（不联网 / 不碰账号 / 不写用户数据）")
    fails: list[str] = []

    # ---- 1) 运行时路径：打包版最容易错的地方，先摆出来
    out("  [bold]运行时路径[/bold]")
    for k, v in C.home_paths().items():
        out(f"    {k:<10} {v}")

    # ---- 2) 纯 Python 依赖：import + 用一次
    out("  [bold]依赖[/bold]")
    try:
        import yaml

        if yaml.safe_load("a: 1") != {"a": 1}:
            raise RuntimeError("yaml.safe_load 结果不对")
        out(f"    yaml        OK  {yaml.__version__}")
    except Exception as e:  # noqa: BLE001
        fails.append(f"yaml: {type(e).__name__}: {e}")
        out(f"    yaml        失败  {type(e).__name__}: {e}", "red")
    try:
        import websockets  # noqa: F401

        out(f"    websockets  OK  {websockets.__version__}")
    except Exception as e:  # noqa: BLE001
        fails.append(f"websockets: {type(e).__name__}: {e}")
        out(f"    websockets  失败  {type(e).__name__}: {e}", "red")

    # ---- 3) 词典与打分：走**程序自己**的代码路径（`normalize`），不是裸 import
    out("  [bold]词典与相似度（走程序自己的代码路径）[/bold]")
    from .normalize import name_similarity, romanize, romanize_available, title_ratio

    if not romanize_available():
        fails.append("pykakasi 词典不可用（罗马音匹配会退化）")
        out("    pykakasi    失败  词典不可用", "red")
    else:
        rom = romanize("消えない理由")
        out(f"    pykakasi    OK  消えない理由 → {rom!r}")
        if not rom:
            fails.append("romanize 返回空串（词典文件可能没打进 bundle）")
            out("    pykakasi    失败  romanize 返回空串", "red")

    tr = title_ratio("Kienai Riyuu", "消えない理由")
    ns = name_similarity("梅田サイファー", "Umeda Cypher")
    # 打印**实测值**，不打印"应该达到"的漂亮数字（§6）
    out(f"    相似度      曲名 Kienai Riyuu ↔ 消えない理由 = {tr:.3f}")
    out(f"                艺人 梅田サイファー ↔ Umeda Cypher = {ns:.3f}")
    if tr <= 0.0 or ns <= 0.0:
        fails.append(f"相似度算不出来（曲名 {tr:.3f} / 艺人 {ns:.3f}）—— rapidfuzz 或词典有问题")

    # ---- 4) Qt：真构造一次窗口（offscreen，不弹窗、无需人看着）
    #
    # 用**内存库** + 注入的别名表：不碰用户的 state.db / aliases.yaml，
    # 也不需要任何可写目录（临时目录在某些受限环境下建得了目录、建不了文件）。
    # 为什么非要构造 MainWindow 而不只是 `import PySide6`：
    # 缺 Qt 插件（platforms / styles / imageformats / tls）时 import 一切正常，
    # 崩在创建 QApplication 或第一次画窗口那一刻。
    out("  [bold]Qt 界面（offscreen 构造一次）[/bold]")
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 绝不弹窗（§2）
        from PySide6.QtWidgets import QApplication
        import PySide6

        from .gui import style
        from .gui.main_window import MainWindow
        from .gui.session import WizardSession
        from .models import Season
        from .normalize import AliasTable
        from .store import Store

        app = QApplication.instance() or QApplication([])
        app.setStyleSheet(style.QSS)
        store = Store(":memory:")
        sess = WizardSession(store=store, season=Season.current(), aliases=AliasTable())
        # 离线自检只构造与断言，不能启动CDP探测线程后又强制析构其父窗口。
        win = MainWindow(sess, probe_enabled=False)
        win.close()                     # closeEvent 会停掉状态灯定时器
        # 当场析构，不留给 GC —— 这个项目已经因为「Qt 对象丢给 GC、在别的线程里
        # 析构」吃过一次间歇性崩溃（0xC0000409，见 HANDOFF §0）。
        from PySide6.QtCore import QEvent

        win.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        store.close()
        out(f"    PySide6     OK  {PySide6.__version__}（MainWindow 构造 + 关闭成功）")
    except Exception as e:  # noqa: BLE001
        fails.append(f"Qt: {type(e).__name__}: {e}")
        out(f"    PySide6     失败  {type(e).__name__}: {e}", "red")

    # ---- 5) 运行环境：**该能写的地方到底能不能写**
    #
    # 这一条是用户真机逼出来的，而且第一版还写错过（只测了程序自己的数据目录 ——
    # 那个目录在"工作区"里，受限环境照样能写，于是漏掉了真正的问题）。
    #
    # 真正要写的是**外部程序**的数据目录（浏览器 profile、网易云数据目录），
    # 都在 `%LOCALAPPDATA%` 下。写不了的话：
    #   · 网易云写不了自己的数据目录 —— 它的日志里是
    #     `Create File Failed ...\Netease\CloudMusic\Log\...` / `Check Drive Write Failed`，
    #     表现为「没有托盘图标、没有登录态、像个全新客户端」
    #   · Chrome 写不了 Crashpad（`拒绝访问 (0x5)`）
    # 诊断顺序很重要：**先看令牌**。令牌不对（低完整性/受限）＝启动上下文的问题；
    # 令牌正常却写不了 ＝ 路径本身的 ACL / 组策略 / 安全软件问题。两者修法完全不同。
    #
    # ⚠️ 只**警告**、不算失败：程序本身是好的，是运行环境受限。
    out("  [bold]运行环境（该能写的地方）[/bold]")
    tf = C.token_facts()
    out(f"    进程令牌          {C.token_summary(tf)}")
    out(f"    环境              {C.env_facts()}")
    blocked: list[str] = []
    for name, path, why in C.needed_write_dirs():
        res = C.write_probe(path)
        bad = res.startswith("不可写")
        if bad:
            blocked.append(name)
        out(f"    {name:<14} {res}", "yellow" if bad else "")
        out(f"        {path}")
        if bad:
            out(f"        {why}", "yellow")
    if blocked:
        # 这几句与界面启动日志共用同一份文案（constants.low_integrity_warning），
        # 免得两处说法不一致 —— 这个项目已经因为"同一个判断写两份"踩过好几次。
        warn = C.low_integrity_warning(tf)
        if warn:
            for line in warn.splitlines():
                out(f"    {line}", "yellow")
        else:
            out("    ⚠️ 令牌看起来正常（中完整性、不受限），但这几个目录写不了 ——", "yellow")
            out("       那更像是**这几条路径本身**被 ACL / 组策略 / 安全软件拦住了。", "yellow")
            out("       请把上面这段原样发给我。", "yellow")

    # ---- 6) 结论
    head("结论")
    if fails:
        for f in fails:
            out(f"  ✗ {f}", "red")
        out("selfcheck: 失败", "red")
        return 1
    out("selfcheck: 全部通过")
    return 0


# ---------------------------------------------------------------- bangumi

def cmd_bangumi(args) -> int:
    from .config import Config
    from .sources.bangumi import BangumiSource
    from .sources.browser_bridge import BrowserBridge

    cfg = Config.load()
    br = BrowserBridge(cfg.browser.debug_port)
    st = br.health()
    if not st.ok:
        out(f"浏览器不可用：{st.detail}", "red")
        return 1

    if args.action == "whoami":
        uid = br.bangumi_uid()
        out(f"Bangumi UID = {uid}" if uid else "未登录 Bangumi", "green" if uid else "yellow")
        return 0 if uid else 1

    if args.action == "probe":
        src = BangumiSource(br, uid=cfg.bangumi.uid, read_paths=["api_tab", "dom"])
        head("依次试各读取路径")
        for path, ok, msg, secs in src.diagnose():
            mark = "OK  " if ok else "FAIL"
            out(f"  {mark} {path:<10} {secs:5.1f}s  {msg}", "green" if ok else "red")
        return 0

    if args.action == "list":
        src = BangumiSource(br, uid=cfg.bangumi.uid,
                            read_paths=cfg.bangumi.read_paths)
        res = src.read_watching()
        out(f"路径={res.path}，共 {len(res.subjects)} 条", "green")
        for s in res.subjects:
            out(f"  {s.bgm_id:<8} {s.title_cn or '':<28} {s.title_jp or '':<32} {s.air_date or ''}")
        return 0

    return 1


# ---------------------------------------------------------------- launch

def cmd_launch(args) -> int:
    from . import spawn
    from .config import Config
    from .cdp import port_open

    cfg = Config.load()
    if args.target == "browser":
        exe, port, argv = (
            cfg.browser.exe_path,
            cfg.browser.debug_port,
            cfg.browser_launch_args(),
        )
        label, key = "浏览器", "browser.exe_path"
    else:
        exe, port, argv = (
            cfg.netease.client_path,
            cfg.netease.debug_port,
            cfg.netease_launch_args(),
        )
        label, key = "网易云客户端", "netease.client_path"

    if port_open(port):
        out(f"{label} 的调试端口 {port} 已在监听，直接复用", "green")
        return 0
    if not exe.strip():
        # 路径默认为空（刻意：机器特有路径不该猜）—— 说清怎么配，别甩一个"找不到文件："
        out(f"还没配置{label}的程序路径。", "red")
        out("  在界面里点那一行的「浏览…」选一次即可（会自动记住）；"
            f"也可以手工填 {cfg.path or 'config/settings.yaml'} 的 {key}", "yellow")
        return 1
    if not os.path.exists(exe):
        out(f"找不到可执行文件：{exe}", "red")
        return 1

    cmdline = " ".join(f'"{a}"' if " " in a else a for a in [exe, *argv])
    out(f"启动 {label}：\n  {cmdline}")

    try:
        proc = spawn.external(exe, argv, label=label)
    except Exception as e:  # noqa: BLE001
        out(f"无法启动：{type(e).__name__}: {e}", "red")
        return 1

    # 子进程退出 ≠ 失败：Chrome 的 chrome.exe 只是交接用的桩（见 spawn 模块注释）。
    # 所以退出了也继续等端口，只有端口始终不开才算失败。
    res = spawn.wait_for_port(
        proc, port, constants.LAUNCH_WAIT_SECONDS,
        on_waited=lambda m: out(f"  {m}", "yellow"),
    )
    if res["ok"]:
        out(res["msg"], "green")
        out(f"子进程输出（若有）会写到：{spawn.log_path(label)}")
        return 0

    out(res["msg"], "red")
    out("常见原因：① 目标已经在运行（调试端口只能在启动时指定，需先完全退出）"
        "② 仅当端口始终不开时才需要往下查", "yellow")
    out(f"子进程日志：{spawn.log_path(label)}", "yellow")
    out(f"也可手动执行：\n  {cmdline}", "yellow")
    return 1


def cmd_netease(args) -> int:
    """网易云客户端只读访问（歌单列表 / 查找 / 状态探测）。

    ⚠️ 这里的 store 取法**尚未在真机上验证**（见 desktop_cdp.py 文件头）。
    所以 `state` 这个动作很重要：客户端起来后先跑它，把打印出来的
    store 结构、命中路径、能否还原曲目回填进代码注释。
    """
    import json

    from .config import Config
    from .collector.desktop_cdp import DesktopCDPCollector

    cfg = Config.load()
    col = DesktopCDPCollector(cfg.netease.debug_port)

    if args.action == "health":
        st = col.health()
        out(f"{'OK' if st.ok else '失败'}  {st.detail}", "green" if st.ok else "red")
        for k, v in (st.extra or {}).items():
            out(f"  {k}: {v}")
        return 0 if st.ok else 1

    if args.action == "state":
        try:
            state = col.dump_state()
        except Exception as e:  # noqa: BLE001
            out(f"读取 store 失败：{type(e).__name__}: {e}", "red")
            out("提示：客户端需带 --remote-debugging-port=9222 启动，"
                "且要先完全退出已有实例。", "yellow")
            return 1
        from .collector.desktop_cdp import describe_state

        out("store 取法: " + (
            json.dumps(col.last_store_diag, ensure_ascii=False)
            if col.last_store_diag else "（未记录）"
        ), "green")
        out(f"歌单来源: {col.last_playlist_source or '（无）'}")
        out("")
        out(json.dumps(describe_state(state), ensure_ascii=False, indent=2)[:4000])
        if args.save:
            with open(args.save, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            out(f"\n完整 state 已写出：{args.save}", "green")
        return 0

    st = col.health()
    if not st.ok:
        out(f"客户端不可用：{st.detail}", "red")
        return 1

    if args.action == "playlists":
        items = col.list_playlists()
        out(f"我创建的歌单：{len(items)} 个（来源 {col.last_playlist_source}）", "green")
        for p in items[: args.limit]:
            out(f"  {p.netease_id or '?':<14} {p.name}")
        return 0

    if args.action == "find":
        got = col.find_playlist(args.name)
        if got:
            out(f"匹配到：{got.name}  (id={got.netease_id})", "green")
            songs = col.playlist_songs(got)
            out(f"  歌单内曲目：{len(songs)} 首")
            for s in songs[:10]:
                out(f"    {s.get('name')} / {s.get('artists')}")
        else:
            out(f"没有找到匹配「{args.name}」的歌单", "yellow")
        return 0

    return 1


def cmd_aliases(args) -> int:
    """别名表的查看与自学习。

    别名表是**剩余命中率的主要出路**（附录 G.9）：算法做到约 57% 可用率后卡住，
    剩下的主要是 pykakasi 读不准的片假名外来语、团体简称、人名 ——
    这些靠算法永远猜不准，但用户确认一次就能永久记住。
    """
    from .config import Config
    from .normalize import AliasTable

    table = AliasTable.load()
    out(f"别名表 {table.path}{'' if os.path.exists(table.path or '') else '（尚未创建）'}")

    if args.action == "show":
        st = table.stats()
        out(f"  番剧 {st['anime']} · 艺人 {st['artist']} · 曲名 {st['title']}")
        out("")
        for kind, label in (("anime", "番剧"), ("artist", "艺人"), ("title", "曲名")):
            d = getattr(table, kind)
            if not d:
                continue
            head(f"{label}别名")
            for canonical, variants in sorted(d.items()):
                out(f"  {canonical}")
                for v in variants:
                    out(f"      ↔ {v}")
        return 0

    if args.action == "learn":
        season = Season.parse(args.season)
        orch = _orch(_progress)
        stat = orch.learn_from_season(season, confirm_only=not args.all)
        head("学习结果")
        out(f"  检查的人工确认 {stat['checked']} 条")
        out(f"  新增：曲名 {stat['title']} · 艺人 {stat['artist']} · 番剧 {stat['anime']}")
        if stat["checked"] == 0:
            out("  （没有人工确认过的匹配 —— 先在向导里确认几条，再回来学）", "yellow")
        st = AliasTable.load().stats()
        out(f"  别名表现有：番剧 {st['anime']} · 艺人 {st['artist']} · 曲名 {st['title']}")
        return 0

    if args.action == "check":
        # 自查：别名表里有没有互相矛盾的条目（同一名字挂在两组里）
        bad = []
        for kind in AliasTable.KINDS:
            d = getattr(table, kind)
            from .normalize import normalize_text
            seen: dict[str, str] = {}
            for canonical, variants in d.items():
                for name in [canonical, *variants]:
                    key = normalize_text(name)
                    if key in seen and seen[key] != canonical:
                        bad.append((kind, name, seen[key], canonical))
                    seen[key] = canonical
        if bad:
            out(f"发现 {len(bad)} 处冲突：", "red")
            for kind, name, g1, g2 in bad:
                out(f"  {kind}: {name!r} 同时出现在 {g1!r} 和 {g2!r}")
            return 1
        out("别名表无冲突", "green")
        return 0

    return 0


def cmd_update(args) -> int:
    """季度内增量重跑（周更）。

    赛季持续十几周，这期间新番陆续加入、歌曲陆续上架。
    这个命令只处理「新的」和「值得重试的」，并报告到底变了什么。
    """
    season = Season.parse(args.season)
    head(f"季度内增量重跑 —— {season.key}（{season.label}）")
    out(f"  库：{_db_note()}")
    orch = _orch(_progress)
    try:
        rep = orch.update_season(
            season,
            do_fetch=not args.no_fetch,
            do_match=not args.no_match,
            do_learn=not args.no_learn,
        )
    except Exception as e:  # noqa: BLE001
        out(f"失败：{type(e).__name__}: {e}", "red")
        if args.verbose:
            traceback.print_exc()
        return 1

    out("")
    for line in orch.describe_update(rep):
        out(line)
    return 0


def cmd_push(args) -> int:
    """把确认过的歌推送到季度歌单。

    ⚠️ 这是**唯一会改动你账号**的命令。默认先试运行看看会推什么，
    加 `--yes` 才真的写。
    """
    season = Season.parse(args.season)
    head(f"推送到歌单 —— {season.key}")
    out(f"  库：{_db_note()}")

    orch = _orch(_progress)
    dry = not args.yes
    if dry:
        out("  ⚠️ 试运行模式（不会写入）。确认无误后加 --yes 真正推送。")

    collector = None
    if not dry:
        from .collector.desktop_cdp import DesktopCDPCollector

        collector = DesktopCDPCollector(orch.config.netease.debug_port)

    try:
        rep = orch.push_season(season, collector, dry_run=dry, limit=args.limit)
    except Exception as e:  # noqa: BLE001
        out(f"失败：{type(e).__name__}: {e}", "red")
        if args.verbose:
            traceback.print_exc()
        return 1

    out("")
    for line in orch.describe_push(rep):
        out(line)
    return 0 if rep.ok else 1


# ---------------------------------------------------------------- fetch / match

def _orch(progress):
    from .config import Config
    from .orchestrator import Orchestrator
    from .store import Store

    # `--db` 只是把库换掉，便于试错（跑演示库、跑另一个季度互不干扰）
    store = Store(_DB_OVERRIDE) if _DB_OVERRIDE else Store()
    return Orchestrator(config=Config.load(), store=store, progress=progress)


#: 全局 `--db` 覆盖，由 main() 设置
_DB_OVERRIDE: str | None = None


def _db_note() -> str:
    """告诉用户这次操作的是哪个库 —— 否则「我的数据呢」是最常见的困惑。"""
    from . import constants

    return _DB_OVERRIDE or constants.DB_PATH


def cmd_fetch(args) -> int:
    season = Season.parse(args.season)
    head(f"拉取番单与 OP/ED —— {season.key} ({season.label})")
    orch = _orch(_progress)
    try:
        rep = orch.fetch_season(season, retry_missing=args.retry_missing)
    except Exception as e:  # noqa: BLE001
        out(f"失败：{type(e).__name__}: {e}", "red")
        if args.verbose:
            traceback.print_exc()
        return 1

    head("结果")
    out(f"  Bangumi 读取路径     {rep.read_path}")
    out(f"  在看总数             {rep.subjects_total}")
    out(f"  属于本季             {rep.subjects_in_season}")
    out(f"  新增曲目             {rep.themes_added}"
        + (f"（另有 {rep.themes_written - rep.themes_added} 条是更新已有行）"
           if rep.themes_written > rep.themes_added else ""))
    out("")
    # ⚠️ 这里原来是个**恒真的假指标**（我自己写的，已修）：
    #
    #       with_themes = S - len(unmatched) if unmatched else S
    #       covered     = min(with_themes + len(unmatched), S)
    #
    #   代数上 covered ≡ S —— **永远打 N/N 且标绿**，哪怕一部番一条曲目都没拿到。
    #   我当时还拿它当「维基覆盖 5/5」的证据，那是错的。
    #
    # 现在改成**真的去库里数**：本季有多少部番至少有一条曲目。
    # 数据来源是 store（`theme_list` 的 anime_id 去重），不是拿别的数字凑出来的。
    try:
        _s = Season.parse(args.season)
        _sid = orch.store.get_season_id(_s.year, _s.quarter)
        _anime = orch.store.anime_list(_sid)
        _themes = orch.store.theme_list(_sid)
        _have = {t["anime_id"] for t in _themes}
        _covered = len([a for a in _anime if a["id"] in _have])
        out(f"  有曲目的番           {_covered}/{len(_anime)}"
            f"（库里真的至少有 1 条曲目的番数）",
            "green" if _anime and _covered >= len(_anime) else "yellow")
    except Exception as e:  # noqa: BLE001 - 数不出来就不显示，绝不编一个数
        out(f"  有曲目的番           （数不出来：{type(e).__name__}: {e}）", "yellow")
    out(f"  AnimeThemes 命中     {len(rep.matched)}/{rep.subjects_in_season}"
        f"（仅回退路径用，维基命中时不需要）")
    failed = rep.search_failed_unmatched
    if failed:
        out("")
        out(f"  ⚠️ 其中 {len(failed)} 部是**搜索失败**没能确认 —— 重跑一次即可：", "yellow")
        for m in failed[:5]:
            out(f"    · {m.title_cn}  {m.search_error[:70]}")
    if rep.unmatched:
        out("")
        out(f"  {len(rep.unmatched)} 部 AnimeThemes 没匹配上"
            f"（**不代表没曲目** —— 维基已覆盖的会照常写入）：")
        for m in rep.unmatched:
            out(f"    · {m.title_cn}  AnimeThemes 相似度 {m.score:.2f}")
    return 0


def cmd_match(args) -> int:
    season = Season.parse(args.season)
    mode = "增量重试" if args.retry_missing else "全量匹配"
    head(f"网易云匹配 —— {season.key}（{mode}）")
    orch = _orch(_progress)
    try:
        rep = orch.match_season(season, retry_missing=args.retry_missing)
    except Exception as e:  # noqa: BLE001
        out(f"失败：{type(e).__name__}: {e}", "red")
        if args.verbose:
            traceback.print_exc()
        return 1

    head("本次运行结果（仅统计本次实际保存的匹配结果）")
    out(f"  参与匹配的 theme     {rep.searched} / 共 {rep.themes_total}")
    out(f"  搜索次数             {rep.queries_used}")
    out("")
    out(f"  自动通过 (>=阈值)    {rep.auto}", "green")
    out(f"  待人工审核           {rep.pending_review}", "yellow")
    out(f"  数据源未收录         {rep.missing_source}")
    out(f"  网易云未上架         {rep.missing_netease}")
    out(f"  匹配不上             {rep.missing_match}", "red")
    out("")
    out(f"  ★ 自动通过率         {rep.auto_rate:.1%}",
        "green" if rep.auto_rate >= 0.5 else "yellow")
    out(f"  ★ 候选可用率(自动+待审)  {rep.usable_rate:.1%}",
        "green" if rep.usable_rate >= 0.8 else "yellow")
    return 0


def cmd_export(args) -> int:
    season = Season.parse(args.season)
    head(f"导出报告 —— {season.key}")
    orch = _orch(_progress)
    paths = orch.export(season, out_dir=args.out)
    for k, v in paths.items():
        out(f"  {k:<8} {v}")
    return 0


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="animusic",
        description="每季度把新番 OP/ED 整理进网易云歌单",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="打印异常堆栈")
    p.add_argument("--db", default=None,
                   help="SQLite 路径（默认 data/state.db）—— 换个库便于试错")
    sub = p.add_subparsers(dest="cmd", required=True)
    # 季度默认值**只有这一个来源**：今天所在的季度。
    # 以前 7 个子命令各自写死 default="2026Q4"，加上 run_gui.py 与
    # gui/session.py 一共 9 处 —— 下个季度会静默处理错的季度。
    _season_default = Season.current().key

    d = sub.add_parser("doctor", help="环境自检")
    d.add_argument("--season", default=_season_default,
                        help="季度，形如 2026Q4（默认=今天所在的季度）")
    d.set_defaults(func=cmd_doctor)

    # 离线自检：不联网、不碰账号 —— 打包产物的验收就靠它（构建脚本会跑）
    sc = sub.add_parser("selfcheck", help="离线自检（打包产物验收用，不联网）")
    sc.set_defaults(func=cmd_selfcheck)

    b = sub.add_parser("bangumi", help="Bangumi 读取")
    b.add_argument("action", choices=["whoami", "probe", "list"])
    b.set_defaults(func=cmd_bangumi)

    l = sub.add_parser("launch", help="用正确参数启动并接管外部程序")
    l.add_argument("target", choices=["browser", "netease"])
    l.set_defaults(func=cmd_launch)

    n = sub.add_parser("netease", help="网易云客户端只读访问（歌单/状态）")
    n.add_argument("action", choices=["health", "playlists", "find", "state"])
    n.add_argument("--name", default="", help="find 用：目标歌单名，如 26年10月")
    n.add_argument("--limit", type=int, default=40)
    n.add_argument("--save", default=None, help="state 用：把完整 store 写到文件")
    n.set_defaults(func=cmd_netease)

    al = sub.add_parser("aliases", help="别名表：查看 / 从人工确认里自学习")
    al.add_argument("action", nargs="?", default="show", choices=["show", "learn", "check"])
    al.add_argument("--season", default=_season_default,
                        help="季度，形如 2026Q4（默认=今天所在的季度）")
    al.add_argument("--all", action="store_true",
                    help="连自动结果一起学（默认只学人工确认的，更安全）")
    al.set_defaults(func=cmd_aliases)

    f = sub.add_parser("fetch", help="读在看列表 + 拉 OP/ED")
    f.add_argument("--season", default=_season_default,
                        help="季度，形如 2026Q4（默认=今天所在的季度）")
    f.add_argument("--retry-missing", action="store_true",
                   help="重试 missing_*（不碰 missing_match）")
    f.set_defaults(func=cmd_fetch)

    m = sub.add_parser("match", help="搜索网易云 + 打分")
    m.add_argument("--season", default=_season_default,
                        help="季度，形如 2026Q4（默认=今天所在的季度）")
    m.add_argument("--retry-missing", action="store_true")
    m.set_defaults(func=cmd_match)

    e = sub.add_parser("export", help="导出报告")
    e.add_argument("--season", default=_season_default,
                        help="季度，形如 2026Q4（默认=今天所在的季度）")
    e.add_argument("--out", default=None)
    e.set_defaults(func=cmd_export)

    u = sub.add_parser("update", help="季度内增量重跑（周更：只处理新的与被重试的）")
    u.add_argument("--season", default=_season_default,
                        help="季度，形如 2026Q4（默认=今天所在的季度）")
    u.add_argument("--no-fetch", action="store_true", help="跳过重新拉番单")
    u.add_argument("--no-match", action="store_true", help="跳过匹配，只报告当前状态")
    u.add_argument("--no-learn", action="store_true", help="跳过别名自学习")
    u.set_defaults(func=cmd_update)

    ps = sub.add_parser("push", help="推送到网易云歌单（默认试运行，加 --yes 才真写）")
    ps.add_argument("--season", default=_season_default,
                        help="季度，形如 2026Q4（默认=今天所在的季度）")
    ps.add_argument("--yes", action="store_true",
                    help="真的写入（不加则只试运行，不会改动账号）")
    ps.add_argument("--limit", type=int, default=None, help="只推前 N 首（先小批量试探）")
    ps.set_defaults(func=cmd_push)

    return p


def main(argv: list[str] | None = None) -> int:
    global _DB_OVERRIDE
    args = build_parser().parse_args(argv)
    _DB_OVERRIDE = getattr(args, "db", None)
    try:
        # 首次运行时落一份 settings.yaml，方便用户改
        from .config import ensure_default_config

        ensure_default_config()
    except Exception:  # noqa: BLE001
        pass
    try:
        return args.func(args)
    except KeyboardInterrupt:
        out("\n已中断", "yellow")
        return 130
    except Exception as e:  # noqa: BLE001
        out(f"未处理的异常：{type(e).__name__}: {e}", "red")
        if getattr(args, "verbose", False):
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
