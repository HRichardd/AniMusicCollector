"""集中常量 —— 避免魔法值散落在业务代码里。

对应 DESIGN.md §10。选择器按稳定性分级（附录 C），
一级锚点用 `data-log` 的 oid，禁止裸用 CSS Module 哈希类名。
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------- 路径

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
#: **源码树**根目录。开发与测试时的运行根就是它（见 `app_root()`）。
ROOT = os.path.dirname(os.path.dirname(PKG_DIR))


def app_root() -> str:
    """运行时数据（数据库 / 配置 / 报告 / 缓存）的根目录。

    ⚠️ **为什么需要这个函数**：打包成 exe 之后，`__file__` 指的是 PyInstaller
    bundle 内部（onedir 是安装目录、onefile 是每次启动新建的临时目录），
    而 `data/state.db`、`config/`、`reports/` 是**用户数据**，必须待在 bundle 外面。
    不区分的话，打包版会把歌单 id、全部匹配决策、别名表写进 bundle ——
    onefile 每次启动换一个临时目录，用户看到的就是「数据全丢了」。

    优先级（前者优先）：
      1. 环境变量 `ANIMUSIC_HOME` —— 显式指定：便携版指向仓库、或多套数据并存
      2. 冻结运行时（`sys.frozen`）= **exe 所在目录** —— 整个文件夹拷走就搬家
      3. 否则 = 源码树根 —— 开发/测试行为与打包前**一字不变**

    注意它是**函数**（像 `config_dir()` 一样），但下面的 `DATA_DIR` 等常量在
    import 时就取了快照：`ANIMUSIC_HOME` 与冻结状态必须在进程启动前定好。
    实际运行时本来就如此（值来自 exe 的位置或启动它的环境变量）。
    """
    env = os.environ.get("ANIMUSIC_HOME")
    if env:
        return os.path.abspath(env)
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return ROOT


#: 运行根的 import 时快照 —— 下面几个目录都由它派生。
_HOME = app_root()

DATA_DIR = os.path.join(_HOME, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
REPORT_DIR = os.path.join(_HOME, "reports")
DB_PATH = os.path.join(DATA_DIR, "state.db")


def config_dir() -> str:
    """配置目录 —— 可用环境变量覆盖。

    ⚠️ **为什么是函数而不是常量**：测试必须能避开用户的真实配置。
    写成常量的话，值在 import 时就定死了，测试没法拦。
    实测踩过的坑：别名自学习会回写 `aliases.yaml`，
    于是跑一遍单测就把用户真实的别名表写进了测试夹具数据 ——
    **测试绝不能碰用户的真实文件**。

    环境变量对用户也有用：把配置放到别处（便携安装、多套配置）。
    `ANIMUSIC_CONFIG_DIR` 比 `ANIMUSIC_HOME` 更具体，所以优先于它。
    """
    return os.environ.get("ANIMUSIC_CONFIG_DIR") or os.path.join(app_root(), "config")


#: 兼容旧引用；新代码请用 `config_dir()`（它支持环境变量覆盖）
CONFIG_DIR = os.path.join(_HOME, "config")


def token_facts() -> dict:
    """当前进程令牌的**事实**：完整性级别 / 是否受限 / 是否 AppContainer / 是否管理员。

    ⚠️ 只在启动日志和 `selfcheck` 里打印。存在的理由是一次真机反馈：
    「双击打开程序 → 它拉起的网易云客户端写不了自己的数据目录（没托盘图标、没登录态、
    像个全新客户端）；同一个命令行在终端里手工跑就正常；以管理员运行也正常」。

    子进程的写失败是**量到过**的（网易云自己的日志：`Create File Failed ...\\Netease\\...`、
    `Check Drive Write Failed`），但"为什么没权限"只有令牌说得清：

      · **完整性级别**：低(0x1000)/中(0x2000)/高(0x3000)/系统(0x4000)。
        正常用户进程是**中**；被降级会变**低** —— 而低完整性写不了 `%LOCALAPPDATA%`
        （那里是中完整性），于是它拉起的外部程序一起残废。
      · **受限令牌**：沙箱的常见做法是把用户 SID 标成 deny-only，
        于是**用户自己名下的目录也写不进去**（ACL 明写 FullControl 也没用）。
      · **AppContainer**：更严，默认连用户目录都读不到。

    有了这一行，"是不是令牌的问题"从此不用猜。
    """
    facts: dict = {"完整性": None, "受限": None, "AppContainer": None, "管理员": None,
                   "错误": None}
    if os.name != "nt":
        facts["错误"] = "非 Windows"
        return facts
    try:
        import ctypes
        from ctypes import wintypes

        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        TOKEN_QUERY, TOKEN_INTEGRITY, TOKEN_RESTRICTED, TOKEN_APPCONTAINER = 8, 25, 15, 29
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.HANDLE)]
        token = wintypes.HANDLE()
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY,
                                    ctypes.byref(token)):
            facts["错误"] = f"OpenProcessToken 失败：{ctypes.get_last_error()}"
            return facts

        size = wintypes.DWORD()
        adv.GetTokenInformation(token, TOKEN_INTEGRITY, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(max(size.value, 16))
        if adv.GetTokenInformation(token, TOKEN_INTEGRITY, buf, size.value,
                                   ctypes.byref(size)):
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
            adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
            n = adv.GetSidSubAuthorityCount(sid)[0]
            adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
            adv.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.c_ulong)
            rid = adv.GetSidSubAuthority(sid, n - 1)[0]
            facts["完整性"] = {0x1000: "低", 0x2000: "中", 0x3000: "高",
                             0x4000: "系统"}.get(rid, hex(rid))
        for info, key in ((TOKEN_RESTRICTED, "受限"), (TOKEN_APPCONTAINER, "AppContainer")):
            val, got = wintypes.BOOL(), wintypes.DWORD()
            if adv.GetTokenInformation(token, info, ctypes.byref(val),
                                       ctypes.sizeof(val), ctypes.byref(got)):
                facts[key] = bool(val.value)
        try:
            facts["管理员"] = bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        facts["错误"] = f"{type(e).__name__}: {e}"
    return facts


def token_summary(facts: dict | None = None) -> str:
    """把 `token_facts()` 排成一行给人看。"""
    f = token_facts() if facts is None else facts
    if f.get("错误"):
        return f"（读取失败：{f['错误']}）"
    bits = [f"完整性={f.get('完整性') or '?'}"]
    for k in ("受限", "AppContainer", "管理员"):
        if f.get(k) is not None:
            bits.append(f"{k}={'是' if f[k] else '否'}")
    return " ".join(bits)


def low_integrity_warning(facts: dict | None = None) -> str | None:
    """令牌被降级/受限时的**人话解释与解决办法**；正常则返回 None。

    这是把一次真机排查的结论固化下来（实测数据，不是推测）：

      · 那台机器的**仓库所在目录**带着
        `Mandatory Label\\Low Mandatory Level:(OI)(CI)(NW)`，里面的 exe 继承了这个标签
        ⇒ **双击启动的进程被降级成低完整性**（正常用户进程是中完整性）。
      · 低完整性写不了 `%LOCALAPPDATA%`（那里是中完整性）⇒ 本程序拉起的
        网易云客户端/Chrome 也继承同一个令牌，于是写不了自己的数据目录：
        网易云日志里是 `Create File Failed ...\\Netease\\CloudMusic\\Log\\...`、
        `Check Drive Write Failed`，表现为「没托盘图标、没登录态、像个全新客户端」。
      · 旁证全都对得上：手工在终端里跑同一个命令行正常（启动的是**无标签**的
        `cloudmusic.exe` ⇒ 中完整性）、源码版正常（启动的是**无标签**的 `python.exe`）、
        以管理员运行正常（高完整性）。

    **为什么不建议"用管理员运行"**：那只是碰巧绕过（UAC 由系统服务创建进程，
    不受文件标签约束），代价是每次都弹 UAC、而且程序以高权限跑。正确做法是把
    整个文件夹搬到普通位置 —— 标签是**目录级**的，拷出去就不带了。
    """
    f = token_facts() if facts is None else facts
    if f.get("错误"):
        return None
    if not (f.get("完整性") == "低" or f.get("受限") is True or f.get("AppContainer") is True):
        return None
    return (
        f"⚠️ 本程序以【{token_summary(f)}】运行 —— 正常用户进程应该是【完整性=中 受限=否】。\n"
        "   它拉起的 Chrome / 网易云客户端会继承同一个令牌，于是写不了 %LOCALAPPDATA%：\n"
        "   网易云会没托盘图标、没登录态（看起来像全新客户端），Chrome 会报 Crashpad 拒绝访问。\n"
        "   常见原因：程序所在文件夹带「低完整性」标签（实测：沙箱工具会给某个目录加这个标签，\n"
        "   里面的 exe 被启动时就降级），或者启动它的上层进程本身是低完整性。\n"
        f"   解决：把整个文件夹拷到普通位置再双击（例如 "
        f"{suggested_home()}\\）。\n"
        "   以管理员运行也能绕过，但没必要 —— 那只是碰巧不受文件标签约束。"
    )


def suggested_home() -> str:
    """建议把程序拷到哪儿 —— **必须在带标签的目录树之外**。

    为什么不写"拷到 <当前目录>-绿色版"：标签是 `(OI)(CI)` **继承**的，
    在工作区内新建的目录照样带标签，拷进去等于没拷。用户主目录下没有标签
    （实测 `C:\\Users\\<你>` 与 `D:\\` 都干净），所以指那儿。
    """
    return os.path.join(os.path.expanduser("~"), "Apps", "AniMusicCollector")


def needed_write_dirs() -> list[tuple[str, str, str]]:
    """**外部程序必须能写**的那几处：[(名字, 路径, 写不了的后果)]。

    为什么单列出来：一次真机反馈里，本程序自己一切正常，但它拉起的网易云客户端
    写不了自己的数据目录（网易云日志：`Create File Failed ...\\Netease\\CloudMusic\\Log\\...`、
    `Check Drive Write Failed`），表现为「没有托盘图标、没有登录态、像个全新客户端」。
    子进程继承本进程的令牌与权限，所以**先量本进程能不能写这几个地方**，
    就能把问题圈在"令牌/ACL"还是"别的"上。
    """
    local = os.environ.get("LOCALAPPDATA", "")
    return [
        ("程序数据目录", _HOME, "本程序自己的库/配置/报告都在这儿"),
        ("浏览器 profile", BROWSER_PROFILE_DIR, "写不了 → 登录态丢失、Crashpad 报拒绝访问"),
        ("网易云数据目录", os.path.join(local, "Netease", "CloudMusic"),
         "写不了 → 客户端没托盘图标、没登录态，看起来像全新的"),
    ]


def write_probe(path: str) -> str:
    """真写一个探针文件再删掉，返回「可写」/「不可写（原因）」/「不存在，跳过」。

    ⚠️ **绝不创建目录**：这几处里有别的程序的数据目录（网易云的），
    为了体检而凭空造一个出来是越界行为。
    """
    if not os.path.isdir(path):
        return "不存在，跳过"
    probe = os.path.join(path, ".animusic-write-probe")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return "可写"
    except OSError as e:
        return f"不可写（{type(e).__name__}: {e}）"


def env_facts() -> str:
    """启动日志用的一行环境事实 —— 真机排查时这几项最常是元凶。"""
    keys = ("TEMP", "LOCALAPPDATA", "__COMPAT_LAYER")
    bits = []
    for k in keys:
        v = os.environ.get(k)
        bits.append(f"{k}={'（无）' if not v else v}")
    return " · ".join(bits)


def home_paths() -> dict[str, str]:
    """运行时**实际**会用到的路径 —— 启动日志与 `selfcheck` 打印它。

    为什么要有它：打包之后「程序到底在用哪个库、哪份配置」是最容易搞错、
    又最难自己发现的一件事（仓库里的 `data/state.db` 和 exe 旁边的 `data/` 是
    两个不同的文件，界面看起来一模一样）。与其让人猜，不如每次开机就打出来。
    """
    return {
        "程序根目录": _HOME,
        "数据库": DB_PATH,
        "配置目录": config_dir(),
        "报告目录": REPORT_DIR,
        "缓存目录": CACHE_DIR,
        # 启动外部程序时，子进程（Chrome / 网易云客户端）自己的输出落在这里 ——
        # 「启动失败」排查时它常常是唯一能说明原因的地方
        "日志目录": os.path.join(_HOME, "logs"),
        "打包运行": "是" if getattr(sys, "frozen", False) else "否",
    }


# ---------------------------------------------------------------- 季度

SEASON_EN = {1: "Winter", 2: "Spring", 3: "Summer", 4: "Fall"}
SEASON_CN = {1: "冬", 2: "春", 3: "夏", 4: "秋"}
QUARTER_MONTH = {1: 1, 2: 4, 3: 7, 4: 10}
MONTH_QUARTER = {m: q for q, m in QUARTER_MONTH.items()}


def quarter_of_month(month: int) -> int:
    """1~3 → 1，4~6 → 2，7~9 → 3，10~12 → 4。"""
    return (month - 1) // 3 + 1


def playlist_name(year: int, quarter: int) -> str:
    """2026 Q4 → '26年10月'（与用户现有命名习惯一致，见 §4.2）。"""
    return f"{year % 100}年{QUARTER_MONTH[quarter]}月"


# ---------------------------------------------------------------- 噪声与修正

NOISE_PATTERNS = {
    "off_vocal": r"off\s*vocal|instrumental|カラオケ|伴奏|インスト",
    "tv_size": r"tv\s*(ver|size|edit)|-tv-|テレビサイズ|\(tv",
    "cover": r"cover|翻唱|翻自|カバー",
    "remix_live": r"dj\s*版|remix|live\b|ライブ|铃声|ringtone",
}

NOISE_PENALTY = {
    "off_vocal": 0.20,
    "tv_size": 0.50,
    "cover": 0.40,
    "remix_live": 0.60,
}

TAG_ORIGINAL = "原唱"
BONUS_ORIGINAL_TAG = 1.15
BONUS_EXACT_TITLE = 1.10
BONUS_SEARCH_RANK = {0: 1.10, 1: 1.05, 2: 1.03}

# 信号权重（§6.1）
W_TITLE, W_ARTIST, W_ANIME_ALIAS, W_DURATION = 0.35, 0.25, 0.30, 0.10

# 时长判定（毫秒）
DURATION_FULL_MIN, DURATION_FULL_MAX = 150_000, 360_000
DURATION_TV_MAX = 150_000

# ---------------------------------------------------------------- 阈值
#
# 校准依据（实测回归，见附录 G.6 待办 2 的兑现）：
#
#   · 有 alias（番剧标注）时，正确匹配轻松 1.00
#   · **没有 alias 时，即使曲名+艺人全对、full size，天花板也只有 0.847**
#     —— 因为 0.30 那一项永远拿不到分。
#     实测：FACSTORY / MOONWORK / Hurray!! 都是「完全正确」却只有 0.847。
#
#   所以自动门槛必须降到 0.85 以下，否则大量正确匹配会被压进人工审核。
#   同时审核门槛提到 0.60，把「艺人对但曲名错」（实测 0.53~0.56）
#   这类**真错误**挡在审核区之外，直接判为匹配不上。
AUTO_THRESHOLD = 0.80
REVIEW_THRESHOLD = 0.60
EARLY_ACCEPT_SCORE = 0.95
MAX_QUERIES_PER_THEME = 4

# ---------------------------------------------------------------- 网易云

NETEASE_SEARCH_API = "https://music.163.com/api/search/get"
NETEASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Accept": "application/json, text/plain, */*",
}

# 搜索侧的读操作限速（比写宽松，但仍保守）
SEARCH_RATE_RPS = 2.0
# 写歌单的限速（风控保护，§7.3）
WRITE_RATE_RPS = 1.0
# 读歌单曲目（api_playlist）的限速。
# 比搜索更保守，因为**写后回查会循环读同一个歌单**（confirm_timeout/confirm_poll
# ≈ 6 次/首），失败路径上还会连着读几十首 —— 不限速就是拿风控换数据。
PLAYLIST_READ_RATE_RPS = 1.5

NETEASE_DEBUG_PORT = 9222

#: ⚠️ 启动参数**没有**常量版本 —— 唯一来源是 `config.Config.*_launch_args()`。
#: 原来这里有 `NETEASE_LAUNCH_ARGS` / `BROWSER_LAUNCH_ARGS` 两个列表，**全项目零引用**：
#: CLI 走 config 那份、界面又自己拼了一份，等于同一份参数有三处（改一处不生效）。
#: 2026-10-07 删掉死常量，并让界面也走 config（同一个判断只留一份）。

# ---------------------------------------------------------------- 浏览器 / Bangumi

BROWSER_DEBUG_PORT = 9223
BROWSER_PROFILE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "AniMusicCollector", "browser-profile"
)

BANGUMI_API = "https://api.bgm.tv"
#: ⚠️ 这里原来还有一个 `BANGUMI_UA`，**全项目零引用**（实际生效的 UA 是
#: `http.HttpClient` 的默认值 `"AniMusicCollector/0.1"`），而且里面写着
#: `https://github.com/local/animusic` 这种**占位地址** —— 2026-10-07 删除。
#: 真要对外发布时：给维基/Bangumi 的 UA 补上真实联系方式（它们的 API 礼仪要求），
#: 别写假的（出事时对方找不到人）。
BANGUMI_COLLECTION_TYPE_WATCHING = 3
BANGUMI_PAGE_SIZE = 100          # 实测 limit 上限就是 100，超出返回 400
BANGUMI_DOM_PAGE_SIZE = 24       # 实测收藏页每页 24 条

# ---------------------------------------------------------------- AnimeThemes

ANIMETHEMES_API = "https://api.animethemes.moe"
ANIMETHEMES_UA = "AniMusicCollector/0.1"

# ---------------------------------------------------------------- AniList（仅辅助）

ANILIST_API = "https://graphql.anilist.co"

# ---------------------------------------------------------------- 选择器（附录 C）

# 网易云客户端（Chrome 91 / 协议 1.3）
SEL_SONG_CARD = '[data-log*="cell_pc_common_songcard"]'
SEL_SEARCH_INPUT = "input[type=search]"
SEL_CARD_LIST = ".cmd-metacard-content"
SEL_TITLE = ".cmd-typography.header"
SEL_ARTIST = ".cmd-typography.artist"
SEL_TAG = '[aria-label^="Tag: "]'
SEL_MORE_BTN = 'button[title="更多"]'
SEL_MENU_ITEM = 'li[data-log*="cell_common_right_button_option"]'

# Bangumi 收藏页（实测确认，附录 F.2）
SEL_BGM_LIST = "#browserItemList"
SEL_BGM_ITEM = "#browserItemList > li.item"
SEL_BGM_PAGER = ".page_inner a.p"
SEL_BGM_CURRENT_PAGE = ".page_inner strong.p_cur"
SEL_BGM_USER = '.idBadgerNeue a[href*="/user/"]'
SEL_BGM_SUBJECT_LINK = 'a[href^="/subject/"]'

# ---------------------------------------------------------------- 超时

HTTP_TIMEOUT = 30.0
CDP_CONNECT_TIMEOUT = 0.35
CDP_CALL_TIMEOUT = 20.0
LAUNCH_WAIT_SECONDS = 25.0

# ---------------------------------------------------------------- 外部数据源的 HTTP 预算
#
# ⚠️ 为什么这三个外部源（日文维基 / AnimeThemes / AniList）**不能**沿用
# `HTTP_TIMEOUT = 30`。
#
# 用户报「拉取番单耗时非常长」。真机实测（他的 GUI 日志 + `data/cache/` 里
# 每个文件的 mtime）读到的是：
#
#     本步 163.0 秒 · 135 次 HTTP 请求（维基 120 · AnimeThemes 13 · AniList 2）
#     维基 62 次调用 → 120 次网络请求（重试 58 · 缓存命中 0）
#     失败 28 次 · 退避等待 44.2 秒 · **最慢一次只有 2.9 秒**
#
# 也就是说：**没有超时**，时间是「请求数 × 重试」吃掉的。旧预算下
# 28 个失败的 URL 各试 5 次 = 140 次请求 + 202 秒退避；新预算（3 次尝试）
# 是 84 次 + 45 秒 —— 失败路径的代价砍掉一半。但它治不了根：
# 根因是 HTTP 429 限流，见下面那段。
#
# ⚠️ **一次自我更正，留着以免再犯**：我最初把另一次实测里 156.2 秒的停顿
# 归因成「5 次尝试 × 30 秒超时 + 退避 7.5 秒 = 157.5 秒」，算术严丝合缝。
# 后来的实测（最慢一次只有 2.9 秒、根本没有超时）说明那是**巧合**，
# 真实原因是连续 429 失败。数值吻合不等于因果。
FETCH_HTTP_TIMEOUT = 15.0
FETCH_HTTP_RETRIES = 2          # 不含首次 —— 总尝试次数 = retries + 1 = 3

#: 单次请求超过这个秒数就在日志里记一条「慢请求」（0 = 不记）。
#: 3 秒的根据：维基 / AnimeThemes 正常都在 1 秒内，超过 3 秒已经是网络问题，
#: 而不是服务端慢 —— 日志必须能把它从「请求太多」里区分出来。
SLOW_REQUEST_SECONDS = 3.0

# ---------------------------------------------------------------- HTTP 429（限流）
#
# ⚠️ 429 是**限流**，不是网络抖动，所以它不能跟抖动共用那套 0.5/1 秒的指数退避。
# 实测（`server: envoy` / `x-envoy-ratelimited` / `x-cache: cp5020`，正文指向
# Wikimedia 的限流文档）：
#
#     HTTP/1.1 429 Too Many Requests
#     retry-after: 25
#     You are making too many requests to the API.
#
# 服务端明说「等 25 秒」，而当时的代码等 0.5 秒就去重试 —— **注定全失败**。
# 真机那一次：28 个失败的 URL × 3 次尝试 = 84 次请求（占维基请求的 70%）
# + 44.2 秒退避，换来 **0 个成功**。
#
# 而且配额很紧：实测连续到第 6~9 次请求就 429（`action=parse` 与批量接口都会，
# 所以不是接口问题，是**按出口 IP 的窗口配额**），安静几十秒后又恢复。

#: 429 响应**没有**给 `Retry-After` 时等多久。给 5 秒是因为窗口是秒级的：
#: 实测 429 之后隔几次请求（约 5 秒）就能重新成功，等太久纯属浪费。
RATE_LIMIT_DEFAULT_WAIT = 5.0

#: ⚠️ **本步为限流等待的总预算（秒）** —— 这条不是可有可无的，它挡的是一个
#: 比原问题更糟的坑：
#:
#: 真机的实际形态是 **成功与 429 交替**（62 次调用里 34 次成功、28 次 429）。
#: 而「连续计数」会被成功清零 —— 于是熔断**几乎永远不触发**，每条失败的 URL
#: 都老实按 `Retry-After: 25` 等 25 秒：28 × 25 ≈ **700 秒**，
#: 比原来要修的 163 秒还糟 4 倍。
#:
#: 离线模拟（`.tmp/sim_429.py`，复刻这个形态）量到的对照：
#:     有预算（30 秒）：94.9 秒 / 70 次请求        ← 现在的行为
#:     关掉预算：      680.3 秒 / 92 次请求        ← 就是上面那个坑
#: 30 秒的取法：够等一次完整的 `Retry-After: 25`（实测值）把「抖一下」扛过去，
#: 又不至于让一步卡到分钟级。
RATE_LIMIT_WAIT_BUDGET = 30.0

#: 429 **不参与**普通的指数退避，它有自己的（更小的）重试预算。
#: 1 次：服务端已经说了等多久，还不成就说明这个窗口没戏，早点走。
RATE_LIMIT_RETRIES = 1

#: 同一个域名**连续**这么多次 429 就熔断：本步不再向它发请求。
#: 为什么要熔断：一次「找不到词条」的番要试 7~13 个候选名，不熔断就会
#: 把 62 个候选名全撞一遍 —— 明知对方在限流还硬撞，只会让窗口更晚恢复。
RATE_LIMIT_BREAKER = 5

# ⚠️ 试过、然后**去掉了**：收到 429 就把最小请求间隔翻倍（自适应减速）。
# 想法是「间歇限流时靠减速自己走出来」，但离线模拟（`.tmp/sim_429.py`，
# 复刻真机那种「成功与 429 交替」的形态）量出来它是**净亏**：
#     有减速：330.7 秒      没减速：约 95 秒 + 限流等待
# 因为请求数并没有因此降下来，而每条请求都变贵了（1 秒 → 5 秒）。
# 真正有效的两件是**熔断**（停止撞）和**等待预算**（不再老实等 700 秒）。
# 留着这条注释，免得以后有人又想加回来。
