"""网易云桌面客户端访问器（只读）—— 走 CDP，数据从 DVA/redux store 里读。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
验证状态（很重要，别当成已实测的东西）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
写这个模块时**客户端没有运行**（9222 未监听），所以：

  ✅ 已验证（离线）：
     · 模糊匹配歌单名（`match_playlist_name` / `season_key`）—— 纯函数，单测覆盖
     · 从任意形状的 state 里抽取歌单 / 歌曲（`extract_playlists` / `song_index`）
       —— 纯函数，单测用**假造的 state** 覆盖了「已知路径命中」与「未知布局深扫」两条路
     · 与 CDP 的往返、错误上报 —— 用 `tests/test_cdp.py` 的假 CDP 服务器覆盖

  🟡 半验证（用 mock 在 node 里跑过注入的 JS）：
     · 取 store 的**逻辑分支**都对：Provider 挂在祖先链上 / store 只在
       `fiber.dependencies.firstContext.memoizedValue` 上 / 祖先链没有得靠根 fiber
       全树扫 / 完全没 fiber 时退到 `g_app._store`、`NE_DAWN.getStore()`；
       缓存命中（`via: cache`）仍保留「第一次是怎么找到的」。
     · DOM 兜底那条路：行元素的 fiber `memoizedProps.track` 优先于 DOM 文本。
     · **写入侧**（`js_dispatch()` / `JS_WRITE_PROBE`）：在 node 24 里对着手搓的
       React 16 fiber 树跑过 33 项断言 —— dispatch 的 4 条取法（fiber / store /
       全树扫 / 全局量）、action 原样送达、`rawEvent` 被换成同形桩、抛错如实上报
       （`threw`）、返回大对象只做摘要、探针能挖出 `props.data` 的键与 `parentData`。
       复现方式：`.tmp/gen_write_js_check.py` 生成脚本 → `node .tmp/write_js_check.js`
       （临时脚本，不入库；跑完输出 33 项断言全绿）。
     · 但这只证明「JS 逻辑对 mock 的 React 16 形状成立」，
       **不证明**真实客户端的前端就是这些形状 —— 那必须要客户端在跑。

  ⚠️ 未验证（客户端没开，无法验证，代码里按「多候选 + 兜底」写）：
     · fiber → redux store 的**取法**。React 16 的 fiber 键名
       （`__reactInternalInstance$<random>`）来自 DESIGN.md §7.3 的实测记录，
       但那里实测的是 `memoizedProps.dispatch`，**不是** `store.getState()`。
       本模块依次尝试：祖先链上的 props/store/stateNode/context → 根 fiber 全树扫
       → 全局量（`__DVA_APP__` / `g_app` / `NE_DAWN` …）。
     · **写入链路的 `payload.data` 形状**（§7.3「尚未解决的部分」第一条）。
       候选形状、推测依据、失败判据全在 `write_path.py` —— 那里 7 条候选
       **没有一条是实测确认的**。本模块只负责「派发 + 回查 + 判成败」，
       不负责猜 payload。真机第一件事应该是跑 `probe_write_path()`
       把客户端真实的 `props.data` 形状 dump 出来（见 `js_dispatch` 上方注释）。
     · store 里歌单数据挂在**哪个路径**。`_CANDIDATE_PLAYLIST_PATHS` 里的路径全是**猜测**，
       任何一条命中都只是「先试它」的意思，不是已知事实。
       真正的兜底是 `extract_playlists()` 的**全树扫描**（按结构特征识别歌单对象），
       它不依赖路径名。
     · `health()` 里的登录用户判定路径（`user.profile` 等）同样是猜测 + 深扫兜底。

  想把这些「未验证」变成「已验证」，在有客户端的机器上跑：
      python tools/probe_netease_state.py
  它会打印 store 顶层结构、store 是怎么被找到的、以及歌单实际命中了哪条路径。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么这么写
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
· DOM 类名是 CSS Module 哈希（`SearchWrapper_s14tpfr6`），客户端一升级就变，
  所以歌单/歌曲一律优先从 redux store 读；DOM 只在显式开关下当兜底（见下）。
· 网易云客户端状态里对象形状很杂，所以抽取逻辑按**结构特征**（有 id + name +
  trackCount/trackIds/creator… 且不像歌曲）而不是按路径行事。
· `playlist_songs()` 的 DOM 兜底默认**关闭**：DOM 里渲染的是「当前视图」，
  用户此刻可能正停在搜索结果页，把它误当成歌单曲目会导致重复推送/漏判
  （DESIGN.md §5.10 的二次比对就靠这个数据）。要开得调用方显式说
  `allow_dom_fallback=True`，并自己确认当前视图确实是目标歌单。

读写分工（写这一半的实现状态见下）：
    · **读**：`health` / `list_playlists` / `find_playlist` / `playlist_songs` 只读，
      不派发 action、不写 store、不点 DOM。
    · **写**：`ensure_playlist()` / `add_to_playlist()` 会 dispatch DVA action（§7.3）。
      策略与 payload 候选在 `write_path.py`（纯逻辑、可离线测）。

歌单曲目**不能只读 store**（这是实测撞出来的，不是设计选择）：
    实测 129 个 `createPlaylist` + 179 个 `favPlaylist` 歌单对象，
    **没有一个**带曲目数组 —— store 里只有 `trackCount` 一个数字。后果是
    `playlist_songs()` 恒为空、`check_membership()` 恒「证据不足」、
    `add_to_playlist()` 的幂等预检恒拒绝写入。于是曲目改从 **web API** 读
    （`api_playlist.WebPlaylistReader`，一次 GET 拿到完整 `trackIds`，
    实测 `len(trackIds) == trackCount`），store 退为「零网络的快路径」。
    两个源在 `_membership()` 里先到先得，两边都判不出来才算证据不足。

写操作的三条硬约束（不允许为了「看起来能用」而放宽）：
    1. **回查是唯一的成功判据**。dispatch 不抛错、甚至返回 code=200，
       都只算「dispatch 自报成功」；`ensure_playlist` 要重新 `list_playlists()`
       看到歌单真的出现，`add_to_playlist` 要重新读歌单曲目看到歌真的进去。
    2. **拿不到确认 = 失败**。曲目数据不完整（`check_membership` 返回 None）时
       `add_to_playlist` 直接返回 False 并给出原因（store 与 web API 两边的
       理由都会写出来），绝不写一个「大概成功」进数据库（DESIGN §5.10 的
       二次比对就靠这个数据）。
    3. **限速**（§7.4）：每次会改状态的 dispatch 都过 `write_path.RateLimiter`
       （`constants.WRITE_RATE_RPS`，默认 1 req/s）；读回查不限速。
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence

from .. import constants
from ..cdp import CDPBrowser, CDPError, CDPPage, port_open
from ..models import HealthStatus, PlaylistRef, Tab
from . import write_path as wp
from .api_playlist import PlaylistNotFound, WebPlaylistReader, WebPlaylistError
from .base import CollectorError

logger = logging.getLogger(__name__)

__all__ = [
    "DesktopCDPCollector",
    "DesktopCDPError",
    "SeasonKey",
    "season_key",
    "normalize_playlist_name",
    "score_playlist_name",
    "rank_playlist_names",
    "match_playlist_name",
    "describe_state",
    "dig_state",
    "find_self_user",
    "looks_like_user",
    "extract_playlists",
    "song_index",
    "songs_from_playlist_entry",
    "normalize_song",
    "JS_HEALTH",
    "JS_STATE",
    "JS_DOM_SONGS",
    "JS_WRITE_PROBE",
    "JS_MARKERS",
    "js_dispatch",
    "ORPHEUS_URL_RE",
    "CANDIDATE_PLAYLIST_PATHS",
]

# 客户端内嵌页面（DESIGN.md 附录 A.2）
ORPHEUS_URL_RE = r"orpheus://"


# ---------------------------------------------------------------- 写入结果标记
#
# `last_write_note` 是给**人**看的说明文字；判断逻辑必须读
# `last_write_action` 这个结构化的字段。
#
# 为什么要有它（实测踩过）：编排层一开始用「自己的台账里有没有这条记录」
# 来判断「本次新推」还是「早就有了」。但真相只有 collector 知道 ——
# 歌单里本来就有这首歌时，collector 会跳过（返回 True，零 dispatch），
# 而台账里没有 → 编排层就报成了「新推送」。
# 报告说推了一首，实际一个写入请求都没发。
WRITE_ACTION_ADDED = "added"        # 真的写进去了
WRITE_ACTION_SKIPPED = "skipped"    # 已经在了，跳过（没写）
WRITE_ACTION_CREATED = "created"    # 新建了歌单
WRITE_ACTION_REUSED = "reused"      # 复用了已有歌单
WRITE_ACTION_FAILED = "failed"      # 失败（原因在 last_write_error）


class DesktopCDPError(CollectorError):
    """客户端不可达 / 前端结构变了 / 返回值解析不了。"""


# ═══════════════════════════════════════════════════════════════════════════
# 一、歌单名模糊匹配（纯函数，不碰网络、不需要客户端）
# ═══════════════════════════════════════════════════════════════════════════
#
# 实测的用户命名习惯（三种写法混用）：
#     25.04      25年4月      25.1
# 目标格式由 constants.playlist_name(year, quarter) 生成：26年10月
#
# 做法：把任何名字规范化成 (年, 月份集合)，再比集合。
#   · 25.04 / 25.1 里的第二个数是**月份**（`25.04` 带前导零，只有月份才这么写；
#     季度从来写成 `Q4`）。这一点按任务给的实测证据定，不再猜。
#   · Q4 → {10,11,12}，所以 `26Q4` 与 `26年10月`、与 `26年11月` 都能对上
#     （任务明确要求 10/11/12 月都算 Q4）。
#   · 年份必须能对上：`25年10月` 与 `26年10月` 月份相同但年份不同 → 直接否决。

_MONTHS_OF_QUARTER: dict[int, frozenset[int]] = {
    1: frozenset({1, 2, 3}),
    2: frozenset({4, 5, 6}),
    3: frozenset({7, 8, 9}),
    4: frozenset({10, 11, 12}),
}
_ALL_MONTHS: frozenset[int] = frozenset(range(1, 13))
_SEASON_CN_MONTHS: dict[str, frozenset[int]] = {
    "冬": _MONTHS_OF_QUARTER[1],
    "春": _MONTHS_OF_QUARTER[2],
    "夏": _MONTHS_OF_QUARTER[3],
    "秋": _MONTHS_OF_QUARTER[4],
}
# 注意顺序：冬春夏秋 —— 别写成「冬春夏季」（踩过：秋永远匹配不上）
_SEASON_CLASS = "[冬春夏秋]"
_CN_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_MONTHS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
              "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}

# 年份：4 位（1900~2999）或 2 位（→ 20xx）
_Y = r"(?<!\d)(\d{4}|\d{2})"
_M = r"(1[0-2]|0?[1-9])"

# 有序：先挑信息最全的（带年份的），再退到不带年份的
_P_YEAR_MONTH = re.compile(_Y + r"\s*年\s*" + _M + r"\s*月份?")
_P_YEAR_QUARTER = re.compile(
    _Y + r"\s*年\s*(?:第\s*)?([Qq][1-4]|[1-4一二三四]\s*季度|" + _SEASON_CLASS + r")")
_P_YEAR_Q = re.compile(_Y + r"\s*[Qq]([1-4])(?![0-9])")
_P_YEAR_DOTTED = re.compile(_Y + r"\s*[.\-/]\s*" + _M + r"(?!\d)")
_P_YEAR_BARE = re.compile(_Y + r"\s*年\s*" + _M + r"(?!\d)")
_P_CN_YEAR_MONTH = re.compile(
    r"([〇零一二三四五六七八九]{2,4})\s*年\s*([一二三四五六七八九十]{1,3})\s*月份?")
_P_CN_YEAR_SEASON = re.compile(
    r"([〇零一二三四五六七八九]{2,4})\s*年\s*(?:第\s*)?([Qq][1-4]|[1-4一二三四]\s*季度|"
    + _SEASON_CLASS + r")")
_P_YEAR_ONLY = re.compile(_Y + r"\s*年")
_P_Q_BARE = re.compile(r"(?<![A-Za-z0-9])[Qq]([1-4])(?![0-9])")
_P_JIDU_BARE = re.compile(r"(?<!\d)(?:第\s*)?([1-4]|[一二三四])\s*季度")
_P_MONTH_BARE = re.compile(r"(?<!\d)" + _M + r"\s*月份?(?!\d)")
_P_CN_MONTH_BARE = re.compile(r"([一二三四五六七八九十]{1,3})\s*月份?(?![\u4e00-\u9fff])")
_P_SEASON_BARE = re.compile(r"(" + _SEASON_CLASS + r")(?![\u4e00-\u9fff])")

# 分数（阈值见 match_playlist_name）
SCORE_EXACT = 1.00          # 规范化后逐字相同
SCORE_YEAR_MONTH = 0.95     # 年月都明确且月份一致
SCORE_YEAR_QUARTER = 0.85   # 年月明确，但一边写了季度（月份集合相交）
SCORE_YEARLESS = 0.70       # 候选没写年份（需要 allow_yearless）
SCORE_YEARLESS_QUARTER = 0.60
SCORE_YEAR_ONLY = 0.50      # 只写了年份（如「26年」），月份未知
DEFAULT_MIN_SCORE = 0.80


def normalize_playlist_name(name: str) -> str:
    """NFKC + 去空白 —— 用于比较与正则，「年 / 月 / 空白 / 全角」都不再影响解析。"""
    s = unicodedata.normalize("NFKC", "" if name is None else str(name))
    s = s.replace("－", "-").replace("—", "-").replace("−", "-")
    return re.sub(r"\s+", "", s)


@dataclass(frozen=True)
class SeasonKey:
    """歌单名的规范化结果：年份（可能未知）+ 该名覆盖到的月份集合。"""

    year: int | None
    months: frozenset[int]
    year_only: bool = False     # 只写了年份，月份完全未知

    def __bool__(self) -> bool:
        return bool(self.months) and (self.year is not None or not self.year_only)


def _year(tok: str | None) -> int | None:
    if not tok or not tok.isdigit():
        return None
    n = int(tok)
    if len(tok) == 2:
        n += 2000
    return n if 1900 <= n <= 2999 else None


def _cn_year(tok: str | None) -> int | None:
    if not tok:
        return None
    digits = "".join(str(_CN_DIGITS[c]) for c in tok if c in _CN_DIGITS)
    if len(digits) != len(tok) or len(digits) not in (2, 4):
        return None
    return _year(digits)


def _cn_month(tok: str | None) -> int | None:
    if not tok:
        return None
    return _CN_MONTHS.get(tok)


def _quarter_num(tok: str | None) -> int | None:
    """'Q4' / '4' / '4季度' / '第四季度' / '四季度' → 4。"""
    if not tok:
        return None
    t = re.sub(r"\s+", "", tok)
    t = re.sub(r"^第", "", t).replace("季度", "")
    m = re.fullmatch(r"[Qq]([1-4])", t)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"([1-4]|[一二三四])", t)
    if m:
        g = m.group(1)
        return int(g) if g.isdigit() else _CN_MONTHS[g]
    return None


def _quarter_months(tok: str) -> frozenset[int] | None:
    q = _quarter_num(tok)
    return _MONTHS_OF_QUARTER[q] if q else None


def _quarter_or_season_months(tok: str) -> frozenset[int] | None:
    return _quarter_months(tok) or _SEASON_CN_MONTHS.get(re.sub(r"\s+", "", tok))


def season_key(name: str) -> SeasonKey | None:
    """把歌单名解析成 (年, 月份集合)；解析不出季节信息 → None。

    认识的写法（大小写 / 全角 / 中文数字都吃）：
        26年10月 · 26年10月份 · 2026年10月 · 26年10
        26.10 · 2026-10 · 2026/10 · 25.04 · 25.1
        26Q4 · 2026Q4 · 26年第四季度 · 26年秋
        二〇二六年十月 · 二六年十月
        26年（只给年份 → months=全年，year_only=True）
        Q4 · 10月 · 十月 · 秋（不带年份）
    """
    s = normalize_playlist_name(name)
    if not s:
        return None

    m = _P_YEAR_MONTH.search(s)
    if m:
        y = _year(m.group(1))
        if y:
            return SeasonKey(y, frozenset({int(m.group(2))}))

    m = _P_YEAR_QUARTER.search(s)
    if m:
        y = _year(m.group(1))
        months = _quarter_or_season_months(m.group(2))
        if y and months:
            return SeasonKey(y, months)

    m = _P_YEAR_Q.search(s)
    if m:
        y = _year(m.group(1))
        if y:
            return SeasonKey(y, _MONTHS_OF_QUARTER[int(m.group(2))])

    m = _P_YEAR_DOTTED.search(s)
    if m:
        y = _year(m.group(1))
        if y:
            return SeasonKey(y, frozenset({int(m.group(2))}))

    m = _P_YEAR_BARE.search(s)
    if m:
        y = _year(m.group(1))
        if y:
            return SeasonKey(y, frozenset({int(m.group(2))}))

    m = _P_CN_YEAR_MONTH.search(s)
    if m:
        y, mo = _cn_year(m.group(1)), _cn_month(m.group(2))
        if y and mo:
            return SeasonKey(y, frozenset({mo}))

    m = _P_CN_YEAR_SEASON.search(s)
    if m:
        y = _cn_year(m.group(1))
        months = _quarter_or_season_months(m.group(2))
        if y and months:
            return SeasonKey(y, months)

    m = _P_YEAR_ONLY.search(s)
    if m:
        y = _year(m.group(1))
        if y:
            return SeasonKey(y, _ALL_MONTHS, year_only=True)

    m = _P_Q_BARE.search(s)
    if m:
        q = _quarter_num(m.group(1))
        if q:
            return SeasonKey(None, _MONTHS_OF_QUARTER[q])
    m = _P_JIDU_BARE.search(s)
    if m:
        q = _quarter_num(m.group(1))
        if q:
            return SeasonKey(None, _MONTHS_OF_QUARTER[q])

    m = _P_MONTH_BARE.search(s)
    if m:
        return SeasonKey(None, frozenset({int(m.group(1))}))
    m = _P_CN_MONTH_BARE.search(s)
    if m:
        mo = _cn_month(m.group(1))
        if mo:
            return SeasonKey(None, frozenset({mo}))
    m = _P_SEASON_BARE.search(s)
    if m:
        return SeasonKey(None, _SEASON_CN_MONTHS[m.group(1)])
    return None


def score_playlist_name(target: str, candidate: str) -> float:
    """0.0 = 不是同一个季度；越大越可信。硬否决（0.0）优先于一切模糊。"""
    if not target or not candidate:
        return 0.0
    tk, ck = season_key(target), season_key(candidate)
    if tk is None or ck is None:
        return 0.0
    if normalize_playlist_name(target) == normalize_playlist_name(candidate):
        return SCORE_EXACT
    # 年份：两边都知道却又不同 → 一票否决（这就是 25年10月 / 26年10月 的分界）
    if tk.year is not None and ck.year is not None and tk.year != ck.year:
        return 0.0
    if not (tk.months & ck.months):
        return 0.0
    if tk.year_only or ck.year_only:
        both_known = tk.year is not None and ck.year is not None
        return SCORE_YEAR_ONLY if both_known else 0.0
    same_months = tk.months == ck.months
    if tk.year is None or ck.year is None:
        return SCORE_YEARLESS if same_months else SCORE_YEARLESS_QUARTER
    return SCORE_YEAR_MONTH if same_months else SCORE_YEAR_QUARTER


def rank_playlist_names(
    target: str,
    candidates: Sequence[str],
    *,
    allow_yearless: bool = False,
    allow_year_only: bool = False,
    min_score: float = 0.0,
) -> list[tuple[int, str, float]]:
    """按分数降序返回 (原始下标, 名字, 分数)；同分保持候选原始顺序（稳定、可预测）。

    `allow_yearless`：是否考虑没写年份的候选（「10月」「Q4」）——默认不考虑。
    `allow_year_only`：是否考虑只写年份的候选（「26年」）——它能对上该年任何一个
    季度，默认不考虑（宁可漏，也不能推错）。
    """
    ranked: list[tuple[int, str, float]] = []
    for i, cand in enumerate(candidates):
        sc = score_playlist_name(target, cand)
        if sc < min_score or sc <= 0.0:
            continue
        ck = season_key(cand)
        if ck is None:
            continue
        if not allow_year_only and ck.year_only:
            continue
        if not allow_yearless and ck.year is None and not ck.year_only:
            continue
        ranked.append((i, cand, sc))
    ranked.sort(key=lambda t: (-t[2], t[0]))
    return ranked


def match_playlist_name(
    target: str,
    candidates: Sequence[str],
    *,
    allow_yearless: bool = False,
    allow_year_only: bool = False,
    min_score: float = DEFAULT_MIN_SCORE,
) -> str | None:
    """在候选歌单名里找 `target` 指向的那一个，返回**候选里的原始字符串**。

    默认只认带年份的候选（≥0.80）：错推到别的年份/季度比找不到更糟
    —— 找不到时用户可以新建歌单，推错了是静默污染。
    """
    ranked = rank_playlist_names(target, candidates, allow_yearless=allow_yearless,
                                 allow_year_only=allow_year_only, min_score=min_score)
    return ranked[0][1] if ranked else None


# ═══════════════════════════════════════════════════════════════════════════
# 二、redux state 解析（纯函数；单测用假造 state 覆盖）
# ═══════════════════════════════════════════════════════════════════════════

_ID_KEYS = ("id", "songId", "netease_song_id", "neteaseId", "trackId", "s_songId", "resourceId")
_PLAYLIST_ID_KEYS = ("id", "playlistId", "listId", "resourceId", "pid", "dissId",
                     "playlist_id", "list_id", "specialId")
_NAME_KEYS = ("name", "title", "songName", "playlistName", "playlist_name")
_PLAYLIST_HINT_KEYS = ("trackCount", "songCount", "tracksCount", "trackIds", "tracks",
                       "songIds", "songs", "creator", "coverImgUrl", "subscribed",
                       "playCount", "specialType", "privacy", "createTime", "updateTime",
                       "track_count", "song_count")
_SONG_SHAPE_KEYS = ("ar", "al", "dt", "artists", "artist", "album", "duration",
                    "duration_ms", "fee", "mvid", "alias")
_TRACK_LIST_KEYS = ("tracks", "songs", "trackIds", "songIds", "track_ids", "song_ids", "list")

# ✅ **已实测确认**（2026-10，客户端 3.1.7.203649 / CEF 91）。
#    实测输出：`createPlaylist` 129 个、`favPlaylist` 179 个，
#    与设计文档记录的「创建 128 / 收藏 179」吻合。
#
#    ⚠️ 注意：**下面那一堆猜测路径全部 miss**，真正命中的是前两条。
#    这就是为什么要保留全树扫描兜底 —— 猜路径的价值远低于形状识别。
CANDIDATE_PLAYLIST_PATHS: tuple[tuple[str, ...], ...] = (
    # ---- 实测确认（放最前）
    ("async:hostResource", "createPlaylist"),
    ("async:hostResource", "favPlaylist"),
    # ---- 以下均为猜测，仅作其它版本客户端的兼容
    ("playlist", "myPlaylist"),
    ("playlist", "myPlaylists"),
    ("playlist", "created"),
    ("playlist", "createdPlaylist"),
    ("playlist", "mine"),
    ("myPlaylist",),
    ("myPlaylists",),
    ("createdPlaylist",),
    ("createdPlaylists",),
    ("mine",),
    ("playlists",),
    ("user", "playlists"),
    ("user", "myPlaylist"),
    ("user", "createdPlaylists"),
    ("library", "playlists"),
    ("library", "myPlaylist"),
    ("data", "playlists"),
)
CANDIDATE_USER_PATHS: tuple[tuple[str, ...], ...] = (
    ("user", "profile"),
    ("user", "user"),
    ("user", "account"),
    ("account", "profile"),
    ("account", "user"),
    ("user",),
    ("account",),
    ("profile",),
    ("userInfo",),
    ("user", "data"),
    ("data", "user"),
)


def describe_state(state: Any, *, max_depth: int = 3, max_items: int = 12) -> Any:
    """把（可能几十 MB 的）state 变成「顶层结构摘要」，给探测脚本和人看。"""
    def node(v: Any, depth: int) -> Any:
        if isinstance(v, dict):
            d: dict[str, Any] = {"type": "dict", "len": len(v), "keys": {}}
            if depth < max_depth:
                for i, (k, sub) in enumerate(v.items()):
                    if i >= max_items:
                        d["keys"]["…"] = f"+{len(v) - max_items} more keys"
                        break
                    d["keys"][str(k)] = node(sub, depth + 1)
            return d
        if isinstance(v, (list, tuple)):
            d = {"type": "list", "len": len(v)}
            if v and depth < max_depth:
                d["item0"] = node(v[0], depth + 1)
            return d
        if v is None:
            return {"type": "null"}
        if isinstance(v, bool):
            return {"type": "bool", "value": v}
        if isinstance(v, (int, float)):
            return {"type": "number", "value": v}
        s = str(v)
        return {"type": "str", "len": len(s), "preview": s[:60]}

    return node(state, 0)


def _first(d: dict, keys: Iterable[str]) -> Any:
    for k in keys:
        if k in d:
            v = d[k]
            if v is not None and v != "":
                return v
            if v is not None:
                return v
    return None


def _first_str(d: dict, keys: Iterable[str]) -> str | None:
    v = _first(d, keys)
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def _song_id(d: dict) -> str | None:
    if not isinstance(d, dict):
        return None
    for k in _ID_KEYS:
        v = d.get(k)
        if isinstance(v, (dict, list)) or v is None or v == "":
            continue
        return str(v)
    return None


def _playlist_id(d: dict) -> str | None:
    for k in _PLAYLIST_ID_KEYS:
        v = d.get(k)
        if isinstance(v, (dict, list)) or v is None or v == "":
            continue
        return str(v)
    return None


def looks_like_song(d: Any) -> bool:
    """歌曲对象：有 id + 名字 + 歌曲特征字段，且没有歌单特征字段。"""
    if not isinstance(d, dict):
        return False
    if _song_id(d) is None:
        return False
    if not _first_str(d, _NAME_KEYS):
        return False
    if any(k in d for k in ("trackCount", "trackIds", "subscribed", "creator", "songCount")):
        return False
    return any(k in d for k in _SONG_SHAPE_KEYS)


def looks_like_playlist(d: Any) -> bool:
    """歌单对象：有 id + 名字 + 至少一个歌单特征字段，且不是歌曲对象。"""
    if not isinstance(d, dict) or looks_like_song(d):
        return False
    if _playlist_id(d) is None:
        return False
    if not _first_str(d, _NAME_KEYS):
        return False
    return any(k in d for k in _PLAYLIST_HINT_KEYS)


def looks_like_user(d: Any) -> bool:
    """登录用户对象：有 id 类字段 + 昵称/头像/VIP 类字段。"""
    if not isinstance(d, dict):
        return False
    has_id = any(d.get(k) is not None for k in ("userId", "uid", "id"))
    has_me = any(d.get(k) is not None for k in
                 ("nickname", "avatarUrl", "vipType", "userType", "name"))
    return has_id and has_me


def user_id_of(d: dict | None) -> str | None:
    if not isinstance(d, dict):
        return None
    for k in ("userId", "uid", "id"):
        v = d.get(k)
        if isinstance(v, (dict, list)) or v is None or v == "":
            continue
        return str(v)
    return None


def _dig(state: Any, path: Sequence[str]) -> Any:
    v = state
    for k in path:
        if not isinstance(v, dict) or k not in v:
            return None
        v = v[k]
    return v


def dig_state(state: Any, path: Sequence[str]) -> Any:
    """按路径取 state 子树（探测脚本诊断候选路径用）。"""
    return _dig(state, path)


def _walk(state: Any, *, max_depth: int = 12, max_nodes: int = 200_000
          ) -> Iterator[tuple[tuple[str, ...], Any]]:
    """深度优先遍历所有容器（dict/list），产出 (路径, 值)。节点数/深度都设上限防炸。"""
    stack: list[tuple[tuple[str, ...], Any]] = [((), state)]
    n = 0
    while stack:
        path, v = stack.pop()
        n += 1
        if n > max_nodes:
            return
        yield path, v
        if len(path) >= max_depth:
            continue
        if isinstance(v, dict):
            for k, sub in v.items():
                if isinstance(sub, (dict, list)):
                    stack.append((path + (str(k),), sub))
        elif isinstance(v, list):
            for i, sub in enumerate(v):
                if isinstance(sub, (dict, list)):
                    stack.append((path + (str(i),), sub))


def _path_str(path: Sequence[str]) -> str:
    return ".".join(path) if path else "<root>"


def find_self_user(state: Any) -> dict | None:
    """找登录用户对象：先试候选路径（未验证），再全树扫兜底。"""
    for path in CANDIDATE_USER_PATHS:
        v = _dig(state, path)
        if looks_like_user(v):
            return v
    for _path, v in _walk(state):
        if looks_like_user(v):
            return v
    return None


def _completeness(d: dict) -> int:
    return sum(1 for k in ("name", "ar", "al", "dt", "artists", "album", "duration",
                           "fee", "alias", "coverImgUrl", "trackCount") if k in d)


def _track_items(d: dict) -> list[Any]:
    """歌单对象里可能的曲目容器：优先取第一个「非空 list」。"""
    for k in _TRACK_LIST_KEYS:
        v = d.get(k)
        if isinstance(v, list) and v:
            return v
    return []


def _created_flag(d: dict, self_id: str | None) -> bool | None:
    """True=我创建 / False=我收藏 / None=判断不了（判断不了时上层按宽松处理）。"""
    subs = d.get("subscribed")
    if isinstance(subs, bool):
        return not subs
    creator = d.get("creator") or d.get("owner") or d.get("user")
    if isinstance(creator, dict):
        cid = user_id_of(creator)
        if cid and self_id:
            return cid == self_id
    return None


def _norm_playlist(d: dict, path: Sequence[str] = (), self_id: str | None = None) -> dict:
    count = _first(d, ("trackCount", "songCount", "tracksCount", "track_count", "song_count"))
    return {
        "id": _playlist_id(d) or "",
        "name": _first_str(d, _NAME_KEYS) or "",
        "created": _created_flag(d, self_id),
        "track_count": count if isinstance(count, (int, float)) and not isinstance(count, bool) else None,
        "tracks": _track_items(d),
        "path": _path_str(path),
        "raw": d,
    }


def _collect_playlists(value: Any, path: Sequence[str], self_id: str | None) -> list[dict]:
    """从某个子树里收集歌单对象（子树的形状可能是 list，也可能是 id→歌单 的 dict）。"""
    out: list[dict] = []
    if looks_like_playlist(value):
        out.append(_norm_playlist(value, path, self_id))
        return out
    if isinstance(value, list):
        for i, item in enumerate(value):
            if looks_like_playlist(item):
                out.append(_norm_playlist(item, path + (str(i),), self_id))
    elif isinstance(value, dict):
        for k, item in value.items():
            if looks_like_playlist(item):
                out.append(_norm_playlist(item, path + (str(k),), self_id))
    return out


def _dedupe_playlists(items: list[dict]) -> list[dict]:
    """按 id 去重（保留字段更全的那个）；没有 id 的按名字去重。"""
    best: dict[str, dict] = {}
    order: list[str] = []
    for it in items:
        key = it["id"] or ("name:" + it["name"])
        if key not in best:
            best[key] = it
            order.append(key)
        elif _completeness(it["raw"]) > _completeness(best[key]["raw"]):
            best[key] = it
    return [best[k] for k in order]


def extract_playlists(state: Any, self_id: str | None = None) -> tuple[list[dict], str]:
    """从 state 里抽出歌单列表。

    返回 (歌单列表, 命中来源描述)。来源字符串会写进日志，方便把「猜测的路径」
    换成实测结果 —— 这就是 `tools/probe_netease_state.py` 要打印的东西。

    顺序：先试 CANDIDATE_PLAYLIST_PATHS（猜测），都不中就全树扫描（形状识别）。
    """
    if not isinstance(state, dict):
        return [], "none"

    for path in CANDIDATE_PLAYLIST_PATHS:
        sub = _dig(state, path)
        if sub is None:
            continue
        items = _dedupe_playlists(_collect_playlists(sub, path, self_id))
        items = [it for it in items if it["name"]]
        if items:
            return items, ".".join(path)

    scanned: list[dict] = []
    first_path: str | None = None
    for path, v in _walk(state):
        if not path or not looks_like_playlist(v):
            continue
        scanned.append(_norm_playlist(v, path, self_id))
        if first_path is None:
            first_path = _path_str(path)
    scanned = [it for it in _dedupe_playlists(scanned) if it["name"]]
    if scanned:
        return scanned, f"scan:{first_path}（全树扫描，路径名未知）"
    return [], "none"


def song_index(state: Any) -> dict[str, dict]:
    """全 state 的 歌曲id → 歌曲对象 索引（歌单对象里的 trackIds 靠它还原）。"""
    idx: dict[str, dict] = {}
    for _path, v in _walk(state):
        if not looks_like_song(v):
            continue
        sid = _song_id(v)
        if not sid:
            continue
        cur = idx.get(sid)
        if cur is None or _completeness(v) > _completeness(cur):
            idx[sid] = v
    return idx


def _artists_of(d: dict) -> list[str]:
    v = d.get("ar")
    if v is None:
        v = d.get("artists")
    if v is None:
        v = d.get("artist")
    if isinstance(v, list):
        out = []
        for a in v:
            if isinstance(a, str):
                if a.strip():
                    out.append(a.strip())
            elif isinstance(a, dict):
                nm = a.get("name") or a.get("nickname")
                if nm:
                    out.append(str(nm))
            elif a is not None:
                out.append(str(a))
        return out
    if isinstance(v, str):
        return [p.strip() for p in re.split(r"\s*/\s*", v) if p.strip()]
    return []


def normalize_song(d: Any) -> dict:
    """把各种形状的歌曲对象（客户端 store / DOM fiber / 搜索结果）拉平成统一字典。"""
    if not isinstance(d, dict):
        d = {}
    sid = _song_id(d) or ""
    album = None
    al = d.get("al")
    if isinstance(al, dict):
        album = al.get("name")
    elif isinstance(d.get("album"), str):
        album = d.get("album")
    duration = _first(d, ("duration_ms", "duration", "dt"))
    fee = d.get("fee")
    alias = d.get("alias") or d.get("aliases") or []
    if isinstance(alias, str):
        alias = [alias]
    alias = [str(a) for a in alias if isinstance(a, (str, int, float))] if isinstance(alias, list) else []
    tags = d.get("tags") or []
    anime_alias = d.get("anime_alias") or (alias[0] if alias else None)
    return {
        "netease_song_id": sid,
        "name": _first_str(d, _NAME_KEYS) or "",
        "artists": _artists_of(d),
        "album": album,
        "duration_ms": int(duration) if isinstance(duration, (int, float))
        and not isinstance(duration, bool) else None,
        "fee": int(fee) if isinstance(fee, (int, float)) and not isinstance(fee, bool) else None,
        "alias": alias,
        "anime_alias": str(anime_alias) if isinstance(anime_alias, str) else None,
        "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
        "raw": d,
    }


def songs_from_playlist_entry(
    entry: dict,
    state: Any = None,
    *,
    index: dict[str, dict] | None = None,
) -> list[dict]:
    """把歌单对象里的曲目还原成歌曲字典列表（顺序保持歌单顺序，按 id 去重）。"""
    if not isinstance(entry, dict):
        return []
    raw = entry.get("raw") if isinstance(entry.get("raw"), dict) else entry
    items = entry.get("tracks") if isinstance(entry.get("tracks"), list) else None
    if not items:
        items = _track_items(raw)
    if not items:
        return []
    idx = index if index is not None else (song_index(state) if state is not None else {})

    out: list[dict] = []
    seen: set[str] = set()
    for it in items:
        song: dict | None = None
        if isinstance(it, dict):
            if looks_like_song(it):
                song = it
            else:
                sid = _song_id(it)
                if sid and sid in idx:
                    song = idx[sid]
        else:
            sid = str(it)
            if sid in idx:
                song = idx[sid]
        if song is None:
            continue
        sid = _song_id(song) or ""
        if sid and sid in seen:
            continue
        if sid:
            seen.add(sid)
        out.append(normalize_song(song))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 三、注入到客户端页面里的 JS
# ═══════════════════════════════════════════════════════════════════════════
#
# 每条表达式都以 `/*DCDP:<KIND>*/` 开头 —— 方便假 CDP 服务器按前缀分派响应，
# 也方便在 eval 日志里一眼看出客户端到底收了我们哪条探测。
#
# ⚠️ 下面这段「fiber → redux store」的 JS 是本模块最大的**未验证**部分。
#    依据：DESIGN.md §7.3 / 附录 B —— fiber 键名 `__reactInternalInstance$<random>`
#    与 `memoizedProps.dispatch` 是实测过的；但 reducer store 的取法没有实测记录。
#    所以这里把能想到的挂载点全试一遍，并把「最后是用哪条路找到的」
#    记在 LAST 里，由 probe() 暴露出来给探测脚本打印。

_JS_LIB = """(function () {
  var W = window;
  var CACHE = '__animusic_dcdp_store__';
  var LAST = null;
  function note(via, extra) {
    LAST = { via: via };
    if (extra) { for (var k in extra) { LAST[k] = extra[k]; } }
  }
  function cache(store, via, info) {
    try { W[CACHE] = { store: store, via: via, info: info || {} }; } catch (e) {}
  }
  function pfx(el, p) {
    var out = [], ks, i;
    if (!el) return out;
    try { ks = Object.keys(el); } catch (e) { return out; }
    for (i = 0; i < ks.length; i++) { if (ks[i].indexOf(p) === 0) out.push(ks[i]); }
    return out;
  }
  function fiberEntry(el) {
    if (!el) return null;
    var k = pfx(el, '__reactInternalInstance$');
    if (k.length) return { f: el[k[0]], key: k[0] };
    k = pfx(el, '__reactFiber$');
    if (k.length) return { f: el[k[0]], key: k[0] };
    k = pfx(el, '__reactContainer$');
    if (k.length) return { f: el[k[0]], key: k[0] };
    return null;
  }
  function rootEntry() {
    var el = document.getElementById('root') || document.body;
    if (!el) return null;
    var e = fiberEntry(el);
    if (e) return { f: e.f, key: e.key, root: 'element' };
    var r = el._reactRootContainer;
    if (r && r._internalRoot && r._internalRoot.current) {
      return { f: r._internalRoot.current, key: '_reactRootContainer', root: 'legacy' };
    }
    if (r && r.current) {
      return { f: r.current, key: '_reactRootContainer', root: 'legacy' };
    }
    return null;
  }
  function isStore(o) {
    return !!o && typeof o.getState === 'function' && typeof o.dispatch === 'function'
      && typeof o.subscribe === 'function';
  }
  function pick(o) {
    if (!o || typeof o !== 'object') return null;
    if (isStore(o)) return o;
    if (isStore(o.store)) return o.store;
    if (isStore(o._store)) return o._store;
    return null;
  }
  function fromDeps(f) {
    var d = f.dependencies, c, v;
    while (d) {
      c = d.firstContext;
      while (c) { v = pick(c.memoizedValue); if (v) return v; c = c.next; }
      d = d.next;
    }
    return null;
  }
  function fromNode(f) {
    if (!f) return null;
    var s = f.stateNode, v;
    v = pick(f.memoizedProps);
    if (v) return v;
    if (s && s.context) { v = pick(s.context.store); if (v) return v; }
    v = pick(s);
    if (v) return v;
    v = pick(f.memoizedState);
    if (v) return v;
    return fromDeps(f);
  }
  function fromGlobal() {
    var names = ['__DVA_APP__', 'g_app', 'dvaApp', '__REDUX_STORE__', '__STORE__',
                 '__store__', 'app', 'store'], i, v;
    for (i = 0; i < names.length; i++) {
      try { v = pick(W[names[i]]); } catch (e) { v = null; }
      if (v) return { store: v, name: names[i] };
    }
    var nd = null;
    try { nd = W.NE_DAWN; } catch (e2) { nd = null; }
    if (nd) {
      v = pick(nd);
      if (v) return { store: v, name: 'NE_DAWN' };
      try {
        if (typeof nd.getStore === 'function') {
          v = pick(nd.getStore());
          if (v) return { store: v, name: 'NE_DAWN.getStore()' };
        }
      } catch (e3) {}
      try {
        if (typeof nd.getState === 'function' && typeof nd.dispatch === 'function') {
          return { name: 'NE_DAWN.getState', store: {
            getState: function () { return nd.getState(); },
            dispatch: nd.dispatch,
            subscribe: function () { return function () {}; } } };
        }
      } catch (e4) {}
    }
    return null;
  }
  function find() {
    var c = null;
    try { c = W[CACHE]; } catch (e) { c = null; }
    if (c && isStore(c.store)) {
      // 命中缓存也要带上「第一次是怎么找到的」，否则探测脚本会看不出取法
      var ci = c.info || {};
      note('cache', { firstVia: c.via, fiberKey: ci.fiberKey, depth: ci.depth,
                      root: ci.root, name: ci.name, nodes: ci.nodes });
      return c.store;
    }
    var re = rootEntry();
    if (re) {
      var n = re.f, g = 0, s;
      while (n && g++ < 400) {
        s = fromNode(n);
        if (s) {
          var ia = { depth: g, fiberKey: re.key, root: re.root };
          note('ancestor', ia);
          cache(s, 'ancestor', ia);
          return s;
        }
        n = n.return;
      }
      var top = re.f;
      while (top && top.return) top = top.return;
      var stack = [top], cnt = 0, x;
      while (stack.length && cnt++ < 40000) {
        x = stack.pop();
        s = fromNode(x);
        if (s) {
          var it = { nodes: cnt, fiberKey: re.key, root: re.root };
          note('tree', it);
          cache(s, 'tree', it);
          return s;
        }
        if (x.child) stack.push(x.child);
        if (x.sibling) stack.push(x.sibling);
      }
    }
    var gm = fromGlobal();
    if (gm) {
      note('global', { name: gm.name });
      cache(gm.store, 'global', { name: gm.name });
      return gm.store;
    }
    note('none', { hasRoot: !!re });
    return null;
  }
  var USER_PATHS = [['user', 'profile'], ['user', 'user'], ['user', 'account'],
                    ['account', 'profile'], ['account', 'user'], ['user'], ['account'],
                    ['profile'], ['userInfo'], ['user', 'data'], ['data', 'user']];
  function dig(o, path) {
    var i, v = o;
    for (i = 0; i < path.length; i++) {
      if (!v || typeof v !== 'object') return null;
      v = v[path[i]];
    }
    return v || null;
  }
  function isArr(v) { return Object.prototype.toString.call(v) === '[object Array]'; }
  function looksUser(v) {
    if (!v || typeof v !== 'object' || isArr(v)) return false;
    var hasId = v.userId != null || v.id != null || v.uid != null;
    var hasMe = v.nickname != null || v.name != null || v.avatarUrl != null
      || v.vipType != null || v.userType != null;
    return !!(hasId && hasMe);
  }
  function userOf(st) {
    if (!st || typeof st !== 'object') return null;
    var i, v;
    for (i = 0; i < USER_PATHS.length; i++) { v = dig(st, USER_PATHS[i]); if (looksUser(v)) return v; }
    var stack = [st], n = 0, keys, j, k, nv;
    while (stack.length && n++ < 20000) {
      var cur = stack.pop();
      if (!cur || typeof cur !== 'object') continue;
      if (isArr(cur)) {
        for (j = 0; j < cur.length && j < 200; j++) stack.push(cur[j]);
        continue;
      }
      if (looksUser(cur)) return cur;
      keys = Object.keys(cur);
      for (j = 0; j < keys.length; j++) {
        k = keys[j];
        try { nv = cur[k]; } catch (e) { continue; }
        if (nv && typeof nv === 'object') stack.push(nv);
      }
    }
    return null;
  }
  return { find: find, isStore: isStore, dig: dig, userOf: userOf, diag: function () { return LAST; },
           pfx: pfx, fiberEntry: fiberEntry, rootEntry: rootEntry };
})()"""

_JS_HEALTH_BODY = """
var out = {hasStore: false, hidden: null, title: null, url: null,
           topKeys: [], user: null, error: null};
try { out.hidden = !!document.hidden; out.title = document.title || '';
      out.url = location.href; } catch (e0) {}
var s = __dcdp.find();
if (s) {
  out.hasStore = true;
  try {
    var st = s.getState();
    if (st && typeof st === 'object') out.topKeys = Object.keys(st);
    var u = __dcdp.userOf(st);
    if (u) {
      out.user = {
        userId: (u.userId != null ? u.userId : (u.id != null ? u.id : u.uid)),
        nickname: (u.nickname || u.name || ''),
        vipType: (u.vipType != null ? u.vipType : null),
        avatarUrl: (u.avatarUrl || null)
      };
    }
  } catch (e1) { out.error = String((e1 && e1.message) || e1); }
} else {
  out.error = 'no-store';
}
out.diag = __dcdp.diag();
return JSON.stringify(out);
"""

_JS_STATE_BODY = """
var s = __dcdp.find();
if (!s) return JSON.stringify({__dcdp_error__: 'no-store'});
// ⚠️ redux-saga 的 store 里有**循环引用**（`_parentOrParents._subscriptions`
// 指回自己），裸 `JSON.stringify` 会直接抛
// "Converting circular structure to JSON" —— 实测真实客户端就是这样，
// 整个 state 命令直接失败。所以必须用**循环安全**的序列化。
//
// 用 WeakSet 记录已访问对象，重复出现时写成一个占位串而不是丢弃整个分支：
// 丢弃会让上层误以为那个键不存在，而占位串能让人看出「这里有个环」。
var seen = new WeakSet();
function safeStringify(v) {
  return JSON.stringify(v, function (k, val) {
    if (typeof val === 'object' && val !== null) {
      if (seen.has(val)) return '[Circular]';
      seen.add(val);
    }
    if (typeof val === 'function') return '[Function]';
    if (typeof val === 'bigint') return String(val);
    return val;
  });
}
try { return safeStringify(s.getState()); }
catch (e) { return JSON.stringify({__dcdp_error__: 'stringify: ' + String((e && e.message) || e)}); }
"""

# DOM 兜底：DESIGN.md §7.2 的读法（data-log oid 定位行 → fiber props 里的 track）。
# 只读、不改状态；但读到的是「当前渲染的列表」，未必是目标歌单，故默认不启用。
_JS_DOM_SONGS_BODY = """
function inst(el) {
  var ks = Object.keys(el), i;
  for (i = 0; i < ks.length; i++) {
    if (ks[i].indexOf('__reactInternalInstance$') === 0) return el[ks[i]];
  }
  for (i = 0; i < ks.length; i++) {
    if (ks[i].indexOf('__reactFiber$') === 0) return el[ks[i]];
  }
  return null;
}
function trackOf(row) {
  var f = inst(row), n = 0;
  while (f && n++ < 12) {
    if (f.memoizedProps && f.memoizedProps.track) return f.memoizedProps.track;
    f = f.return;
  }
  return null;
}
function textOf(el) { return el ? String(el.textContent || '').trim() : ''; }
function songIdOf(row) {
  try {
    var dl = JSON.parse(row.getAttribute('data-log') || '{}');
    return String((dl.params || {}).s_songId || '');
  } catch (e) { return ''; }
}
var rows = [].slice.call(document.querySelectorAll('[data-log*="cell_pc_common_songcard"]'));
var out = rows.slice(0, 500).map(function (row) {
  var t = trackOf(row) || {};
  var artists = [].slice.call(row.querySelectorAll('.cmd-typography.artist'))
    .map(textOf).filter(Boolean);
  if (!artists.length) { var a = textOf(row.querySelector('.cmd-typography.artist')); if (a) artists = [a]; }
  var al = t.al && t.al.name ? t.al.name : (typeof t.album === 'string' ? t.album : null);
  return {
    netease_song_id: String(t.id || songIdOf(row) || ''),
    name: t.name || textOf(row.querySelector('.cmd-typography.header')),
    artists: artists,
    album: al,
    duration_ms: (t.duration || t.dt || null),
    fee: (t.fee != null ? t.fee : null),
    alias: (t.alias || null),
    anime_alias: (t.alias && t.alias.length ? t.alias[0] : null)
  };
}).filter(function (x) { return x.netease_song_id || x.name; });
return JSON.stringify({rows: out, context: {title: document.title || '', url: location.href,
                                            count: rows.length}});
"""


def _build_js(kind: str, body: str) -> str:
    """把标记 + JS 库 + 具体逻辑拼成一条自包含的表达式。"""
    return f"/*DCDP:{kind}*/(function(){{var __dcdp = {_JS_LIB};{body}}})()"


# ── 写入链路的 JS（§7.3）──────────────────────────────────────────────────
#
# 这段是**写入**唯一真正碰账号的部分。它的不确定性不在「能不能调 dispatch」
# （附录 B 实测过 `f.memoizedProps.dispatch(action)` 可用），而在
# **payload 形状**和**一次 dispatch 够不够**。所以这里的 JS 只做三件事：
#   ① 找到 dispatch（fiber → store → 全局量，逐条降级，并报告用的是哪条）
#   ② 把 action 派发出去，**把返回值原样（或摘要）报回来**，绝不自己判成功
#   ③ （探针）把客户端真实的 `props.menu` / `props.data` 形状 dump 出来
#
# ⚠️ rawEvent：bundle 原文里是 React 合成事件，Python 侧造不出来。我们传 null，
#    由下面的 synthEvent() 换成同形的空实现桩。**这是推测**：如果 saga 真的读了
#    事件的字段（比如 clientX/currentTarget），这条路会失败 —— 但失败会被
#    「回查」抓到，不会静默成功。

_JS_WRITE_PRELUDE = """
  function __dcdpIsFn(x) { return typeof x === 'function'; }
  function __dcdpSynthEvent() {
    var noop = function () {};
    return { type: 'click', bubbles: true, cancelable: true,
             target: null, currentTarget: null, nativeEvent: {},
             button: 0, clientX: 0, clientY: 0, timeStamp: 0,
             persist: noop, stopPropagation: noop, preventDefault: noop,
             isDefaultPrevented: function () { return false; },
             isPropagationStopped: function () { return false; } };
  }
  // 把 Python 侧造不出来的值换成同形桩。
  //
  // 两类：
  //   ① `rawEvent: null` → React 合成事件桩（§7.3 原文里它是真事件）
  //   ② 值等于 __DCDP_FN_SENTINEL__ 的字符串 → 空函数
  //      （真机实测的 splice action 里 `data.callback` 是个函数）
  //
  // ② 按**值**识别而不是按键名：`callback` 这个词在别的上下文里也可能出现，
  // 而哨兵串是我们自己造的，绝不会与客户端真实数据撞上。
  // 计数写回 out.patchedRawEvent / out.patchedFunctions，探测脚本能看出生效没。
  var __dcdpPatchedFns = 0;
  function __dcdpPatchRawEvent(node, depth) {
    if (!node || typeof node !== 'object' || depth > 8) return node;
    var isArr = Object.prototype.toString.call(node) === '[object Array]';
    var i, ks, k, v;
    if (isArr) {
      for (i = 0; i < node.length; i++) __dcdpPatchRawEvent(node[i], depth + 1);
      return node;
    }
    try { ks = Object.keys(node); } catch (e) { return node; }
    for (i = 0; i < ks.length; i++) {
      k = ks[i];
      try { v = node[k]; } catch (e2) { continue; }
      if (v === __DCDP_FN_SENTINEL__) {
        try { node[k] = function () {}; __dcdpPatchedFns++; } catch (e3) {}
      } else if (k === 'rawEvent' && (v === null || v === undefined)) {
        try { node[k] = __dcdpSynthEvent(); } catch (e4) {}
      } else if (v && typeof v === 'object') {
        __dcdpPatchRawEvent(v, depth + 1);
      }
    }
    return node;
  }
  function __dcdpDispatchFromElement(el) {
    var e = __dcdp.fiberEntry(el), f, n = 0;
    if (!e) return null;
    f = e.f;
    while (f && n++ < 400) {
      if (f.memoizedProps && __dcdpIsFn(f.memoizedProps.dispatch)) {
        return { d: f.memoizedProps.dispatch, via: 'fiber.memoizedProps.dispatch',
                 where: 'dom', depth: n, fiberKey: e.key };
      }
      f = f.return;
    }
    return null;
  }
  function __dcdpFindDispatch() {
    var got = null, els = [], i, root, f, top, stack, x, n = 0, W = window;
    try { els = [].slice.call(document.querySelectorAll('[data-log]')).slice(0, 200); }
    catch (e0) { els = []; }
    if (document.body) els.push(document.body);
    root = document.getElementById('root');
    if (root) els.push(root);
    for (i = 0; i < els.length; i++) {
      got = __dcdpDispatchFromElement(els[i]);
      if (got) return got;
    }
    try {
      var s = __dcdp.find();
      if (s && __dcdpIsFn(s.dispatch)) {
        return { d: function (a) { return s.dispatch(a); },
                 via: 'store.dispatch', where: '__dcdp.find()', depth: null };
      }
    } catch (e1) {}
    try {
      var re = __dcdp.rootEntry();
      if (re) {
        top = re.f;
        while (top && top.return) top = top.return;
        stack = [top];
        while (stack.length && n++ < 40000) {
          x = stack.pop();
          if (x && x.memoizedProps && __dcdpIsFn(x.memoizedProps.dispatch)) {
            return { d: x.memoizedProps.dispatch, via: 'fiber.tree',
                     where: 'root-scan', depth: n };
          }
          if (x && x.child) stack.push(x.child);
          if (x && x.sibling) stack.push(x.sibling);
        }
      }
    } catch (e2) {}
    try {
      var nd = W.NE_DAWN;
      if (nd && __dcdpIsFn(nd.getDispatch)) {
        f = nd.getDispatch();
        if (__dcdpIsFn(f)) {
          return { d: f, via: 'NE_DAWN.getDispatch', where: 'global', depth: null };
        }
      }
      var names = ['g_app', '__DVA_APP__', 'dvaApp', 'app'], j, a;
      for (j = 0; j < names.length; j++) {
        try { a = W[names[j]]; } catch (e4) { continue; }
        if (a && a._store && __dcdpIsFn(a._store.dispatch)) {
          return { d: function (act) { return a._store.dispatch(act); },
                   via: names[j] + '._store.dispatch', where: 'global', depth: null };
        }
        if (a && __dcdpIsFn(a.dispatch)) {
          return { d: a.dispatch, via: names[j] + '.dispatch', where: 'global', depth: null };
        }
      }
    } catch (e5) {}
    return null;
  }
  function __dcdpPrim(v) {
    if (v === null) return 'null';
    var t = typeof v;
    if (t === 'string' || t === 'number' || t === 'boolean' || t === 'function') return t;
    if (Object.prototype.toString.call(v) === '[object Array]') return 'array';
    return t === 'object' ? 'object' : t;
  }
  function __dcdpPreview(v) {
    var t = typeof v;
    if (t === 'string') return v.length > 80 ? v.slice(0, 80) + '…' : v;
    if (t === 'number' || t === 'boolean') return v;
    return null;
  }
  function __dcdpShape(v, depth, maxKeys) {
    var cap = maxKeys || 20, o = { type: __dcdpPrim(v) }, ks = [], i, k, sub;
    if (depth < 0) return o;
    if (o.type === 'array') {
      try { o.len = v.length; if (v.length) o.item0 = __dcdpShape(v[0], depth - 1, cap); }
      catch (e) {}
      return o;
    }
    if (o.type === 'object') {
      try { ks = Object.keys(v); } catch (e2) { ks = []; }
      o.keys = ks.slice(0, cap);
      if (ks.length > cap) o.more = ks.length - cap;
      o.values = {};
      for (i = 0; i < o.keys.length; i++) {
        k = o.keys[i];
        try { sub = v[k]; } catch (e3) { continue; }
        o.values[k] = { type: __dcdpPrim(sub), preview: __dcdpPreview(sub) };
        if (o.values[k].type === 'array') { try { o.values[k].len = sub.length; } catch (e4) {} }
        if (o.values[k].type === 'object' && depth > 0) {
          o.values[k].shape = __dcdpShape(sub, depth - 1, 10);
        }
      }
      return o;
    }
    o.preview = __dcdpPreview(v);
    return o;
  }
  function __dcdpSummarize(v) {
    var t = Object.prototype.toString.call(v), i, ks = [], k, out, picked = {};
    var MARK = ['code', 'status', 'statusCode', 'ok', 'success', 'succeeded',
                'error', 'err', 'errmsg', 'errorMessage', 'msg', 'message'];
    if (v === null || v === undefined) return null;
    if (t === '[object Array]') return { __dcdp_kind__: 'array', len: v.length };
    if (typeof v === 'function') return { __dcdp_kind__: 'function' };
    if (typeof v === 'string') {
      return v.length <= 200 ? v : { __dcdp_kind__: 'str', len: v.length, preview: v.slice(0, 200) };
    }
    if (typeof v === 'number' || typeof v === 'boolean') return v;
    if (typeof v !== 'object') return null;
    out = { __dcdp_kind__: 'object' };
    try { ks = Object.keys(v); } catch (e) { ks = []; }
    out.len = ks.length;
    out.keys = ks.slice(0, 20);
    for (i = 0; i < MARK.length; i++) {
      k = MARK[i];
      if (Object.prototype.hasOwnProperty.call(v, k)) {
        var pv = v[k];
        if (pv === null || typeof pv === 'string' || typeof pv === 'number'
            || typeof pv === 'boolean') { picked[k] = pv; }
      }
    }
    for (i = 0; i < ks.length; i++) {
      if (Object.keys(picked).length >= 8) break;
      if (ks[i].indexOf('code') >= 0 || ks[i].indexOf('rror') >= 0) {
        var z = v[ks[i]];
        if (z === null || typeof z !== 'object') { picked[ks[i]] = z; }
      }
    }
    out.picked = picked;
    return out;
  }
"""

#: 函数哨兵：把 `write_path.SYNTHETIC_FUNCTION` 注入 JS 的**唯一**落点。
#:
#: 为什么在 Python 侧替换而不是把哨兵串直接写进 JS：两处各写一遍就是两份真相，
#: 改一处忘一处**不会报错**，只会在真机上静默漏掉一个函数字段。
#: `tests/test_write_path.py` 里有一条断言钉住「JS 里出现的哨兵 == Python 常量」。
_JS_WRITE_PRELUDE = _JS_WRITE_PRELUDE.replace(
    "__DCDP_FN_SENTINEL__", json.dumps(wp.SYNTHETIC_FUNCTION))
assert "__DCDP_FN_SENTINEL__" not in _JS_WRITE_PRELUDE, "哨兵占位符没被替换"

# ⚠️ 未验证：下面的 ACTION 里 `payload.data` 的形状全部是**推测**（见 write_path.py）。
#    这个 JS 的职责只是「如实派发 + 如实回报返回值」，成败由 Python 侧回查判定。
_JS_DISPATCH_BODY = """
  var ACTION = __DCDP_ACTION__;
  var out = { called: false, via: null, where: null, depth: null, threw: false,
              error: null, returned: null, patchedRawEvent: false,
              actionType: (ACTION && ACTION.type) || null };
  var got = __dcdpFindDispatch();
  if (!got) {
    out.error = 'no-dispatch（fiber / store / 全局量三条路都没找到 dispatch）';
    return JSON.stringify(out);
  }
  out.via = got.via;
  out.where = got.where || null;
  out.depth = (got.depth === undefined ? null : got.depth);
  try {
    __dcdpPatchRawEvent(ACTION, 0);
    out.patchedRawEvent = true;
    out.patchedFunctions = __dcdpPatchedFns;
    out.returned = __dcdpSummarize(got.d(ACTION));
    out.called = true;
  } catch (err) {
    out.threw = true;
    out.error = String((err && err.message) || err);
  }
  return JSON.stringify(out);
"""

# ⚠️ 探针：把「payload 到底该长什么样」这个未知项变成可读的事实。
#    重点扫 §7.3 路线 A 第 2 步说的那种组件 —— props 上同时有 menu 和 data
#    （它们**不在**歌曲行的祖先链上，必须全树扫）。
_JS_WRITE_PROBE_BODY = """
  var out = { ok: false, dispatch: null, menuWrappers: [], songRows: [],
              globals: {}, errors: [] };
  function __dcdpTypeName(f) {
    try {
      var t = f.type || f.elementType;
      if (typeof t === 'string') return t;
      if (t && t.displayName) return t.displayName;
      if (t && t.name) return t.name;
    } catch (e) {}
    return null;
  }
  try {
    var re = __dcdp.rootEntry();
    if (!re) {
      out.errors.push('no-root-fiber（React 16 的 __reactInternalInstance$ 没找到）');
    } else {
      var top = re.f;
      while (top && top.return) top = top.return;
      var stack = [top], n = 0, x, found = 0;
      while (stack.length && n++ < 40000) {
        x = stack.pop();
        if (x && x.memoizedProps && typeof x.memoizedProps === 'object' && found < 5) {
          var p = x.memoizedProps;
          var hasMenu = (p.menu !== undefined && p.menu !== null);
          var hasData = (p.data !== undefined && p.data !== null);
          if (hasMenu && hasData) {
            found++;
            out.menuWrappers.push({
              component: __dcdpTypeName(x),
              propsKeys: Object.keys(p).slice(0, 24),
              hasDispatch: typeof p.dispatch === 'function',
              menu: __dcdpShape(p.menu, 1, 12),
              data: __dcdpShape(p.data, 2, 20),
              parentData: (p.parentData !== undefined ? __dcdpShape(p.parentData, 1, 12) : null)
            });
          }
        }
        if (x && x.child) stack.push(x.child);
        if (x && x.sibling) stack.push(x.sibling);
      }
      out.ok = found > 0;
      if (!found) {
        out.errors.push('没扫到 props 上同时有 menu 和 data 的组件 —— '
                        + '要么当前没有渲染歌曲行，要么 §7.3 的这条定位方式已失效');
      }
    }
  } catch (e0) { out.errors.push('scan: ' + String((e0 && e0.message) || e0)); }
  try {
    var rows = [].slice.call(
      document.querySelectorAll('[data-log*="cell_pc_common_songcard"]')).slice(0, 3);
    for (var i = 0; i < rows.length; i++) {
      var fe = __dcdp.fiberEntry(rows[i]);
      if (!fe) continue;
      var f2 = fe.f, k = 0;
      while (f2 && k++ < 12) {
        if (f2.memoizedProps && f2.memoizedProps.track) {
          out.songRows.push({
            fiberKey: fe.key, depth: k,
            track: __dcdpShape(f2.memoizedProps.track, 2, 20),
            propsKeys: Object.keys(f2.memoizedProps).slice(0, 24),
            hasDispatch: typeof f2.memoizedProps.dispatch === 'function'
          });
          break;
        }
        f2 = f2.return;
      }
    }
  } catch (e1) { out.errors.push('songRows: ' + String((e1 && e1.message) || e1)); }
  var got = __dcdpFindDispatch();
  out.dispatch = got ? { via: got.via, where: got.where || null,
                         depth: (got.depth === undefined ? null : got.depth) } : null;
  out.globals = {
    NE_DAWN: (typeof window.NE_DAWN !== 'undefined'),
    g_app: (typeof window.g_app !== 'undefined'),
    webpackJsonp: (typeof window.webpackJsonp !== 'undefined')
  };
  return JSON.stringify(out);
"""


def js_dispatch(action: dict) -> str:
    """把一条 action 派发出去并**如实回报返回值**的 JS 表达式。

    返回值是 JSON 字符串，形如::

        {"called": true, "via": "fiber.memoizedProps.dispatch", "where": "dom",
         "threw": false, "error": null, "patchedRawEvent": true,
         "returned": {"__dcdp_kind__": "object", "keys": ["..."], "picked": {"code": 200}}}

    注意 `called=true` 只表示「dispatch 被调用了」，**不表示写入成功**；
    `parse_action_result` 判读的也是「dispatch 自报的成败」，最终判据永远是回查。
    """
    body = _JS_DISPATCH_BODY.replace("__DCDP_ACTION__", wp.action_to_js_literal(action), 1)
    return _build_js("DISPATCH", _JS_WRITE_PRELUDE + body)


JS_HEALTH = _build_js("HEALTH", _JS_HEALTH_BODY)
JS_STATE = _build_js("STATE", _JS_STATE_BODY)
JS_DOM_SONGS = _build_js("DOMSONGS", _JS_DOM_SONGS_BODY)
JS_WRITE_PROBE = _build_js("WRITEPROBE", _JS_WRITE_PRELUDE + _JS_WRITE_PROBE_BODY)
JS_MARKERS = {
    "health": "/*DCDP:HEALTH*/",
    "state": "/*DCDP:STATE*/",
    "dom": "/*DCDP:DOMSONGS*/",
    "dispatch": "/*DCDP:DISPATCH*/",
    "write_probe": "/*DCDP:WRITEPROBE*/",
}


# ═══════════════════════════════════════════════════════════════════════════
# 四、Collector
# ═══════════════════════════════════════════════════════════════════════════

def _focus_emulate(page: CDPPage) -> bool:
    """让**页面自己**认为它是可见/聚焦的（真机实测有效）。

    背景：客户端窗口被遮挡时 Chromium 报 `document.hidden=true` 并停止渲染，
    写入前的可写性检查于是拒绝。但 CDP 的
    `Emulation.setFocusEmulationEnabled` 能把 `document.hidden` 变成 false、
    `visibilityState` 变成 visible —— **完全不用去动用户的窗口**。

    实测（同一台机器上逐个试过）：

        Page.bringToFront                      ❌ 无效（hidden 仍为 true）
        Page.setWebLifecycleState('active')     ❌ 单独无效
        Emulation.setDeviceMetricsOverride      ❌ 无效
        Emulation.setFocusEmulationEnabled      ✅ hidden → false、visible

    ⚠️ 它是**按 CDP 会话**生效的：会话一断开就恢复 hidden。所以必须和真正要跑的
    那条 evaluate 放在**同一个会话**里 —— 这正是 `_session(force_visible=True)`
    的用途，也是为什么不能「先开一次、后面就不用管了」。

    这**不是绕过守卫**：守卫担心的是「前端停止渲染 → 写入静默失败」，
    而我们确实消除了那个条件。消除不了（老 CEF 没这个方法）就照旧拒绝写入
    —— `_require_writable()` 会在同一个会话里重新读 `document.hidden`。
    """
    try:
        page.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        return True
    except Exception:  # noqa: BLE001 - 老版本 CEF 可能没这个方法
        return False


class DesktopCDPCollector:
    """网易云客户端访问器（读 + 写）。

    接口与 `PlaylistCollector` 对齐（`health` / `list_playlists` /
    `find_playlist` / `ensure_playlist` / `add_to_playlist` / `playlist_songs`）。

    写入部分的两条不可退让的语义（见模块 docstring 顶部）：
        · 回查是唯一的成功判据；
        · 拿不到确认 = 失败（`add_to_playlist` 返回 False，`ensure_playlist` 抛错）。
    """

    def __init__(
        self,
        port: int = constants.NETEASE_DEBUG_PORT,
        timeout: float = 20.0,
        *,
        allow_dom_fallback: bool = False,
        state_ttl: float = 2.0,
        write_policy: wp.WritePolicy | None = None,
        rate_limiter: wp.RateLimiter | None = None,
        add_variant: int | None = None,
        web_playlist: WebPlaylistReader | None = None,
        use_web_playlist: bool = True,
    ):
        self.port = int(port)
        self.timeout = float(timeout)
        self.browser = CDPBrowser(self.port, self.timeout)
        # DOM 兜底有「读到别的列表」的风险，默认关（见模块 docstring）
        self.allow_dom_fallback = bool(allow_dom_fallback)
        self.state_ttl = float(state_ttl)
        # 诊断信息（探测脚本 / 日志用）
        self.last_playlist_source: str | None = None
        self.last_dom_context: dict | None = None
        self.last_store_diag: dict | None = None
        self._state_cache: dict | None = None
        self._state_at = 0.0
        # 解析结果按 state 对象身份缓存：state 可能几十 MB，
        # 同一份 state 不该每次调用都重扫一遍（用对象身份比较，不用 id()）
        self._extract_state: dict | None = None
        self._extract_val: tuple[list[dict], str] | None = None
        self._index_state: dict | None = None
        self._index_val: dict[str, dict] | None = None

        # ---- 曲目的**服务端真相**（store 里恒缺曲目，见模块 docstring）
        #
        # 为什么默认开着：客户端 store 的歌单对象**没有曲目数组**（实测 129+179
        # 个全都没有），所以只读 store 的话 `playlist_songs()` 恒为空、
        # `add_to_playlist()` 的幂等预检恒「证据不足」→ 写入链路一封都发不出去。
        # web API 一次 GET 拿到完整的 `trackIds`（实测 len == trackCount）。
        #
        # 可注入 / 可关：单测不该真的联网；离线环境也能退回纯 store 路径。
        self.web_playlist: WebPlaylistReader | None = (
            (web_playlist if web_playlist is not None
             else WebPlaylistReader()) if use_web_playlist else None)
        #: 诊断：为什么没用上 web API（None = 没出错）
        self.last_web_error: str | None = None
        #: 诊断：本次的歌单曲目到底是从哪读到的
        self.last_songs_source: str | None = None

        # ---- 写入（§7.3 / §7.4）
        self.write_policy = write_policy or wp.WritePolicy()
        # 限速器可注入：测试里换成假时钟/假 sleep，就不必真的等 1 秒
        self.rate_limiter = rate_limiter or wp.RateLimiter(self.write_policy.rate_rps)
        # 会话内记住「哪条候选成功过」，下一首先试它 —— 否则每首歌都要
        # 从头盲试 6 条候选（每条约 1 req/s，40 首要 4 分钟）
        self._add_variant_hint: int | None = None
        #: 显式 pin 一条候选（None = 按 write_policy.add_variant_order 试探）
        self._pinned_add_variant = add_variant
        # 写入诊断（GUI 的「失败原因」列直接读 last_write_error）
        self.last_write_error: str | None = None
        self.last_write_note: str | None = None
        self.last_write_wait: float = 0.0
        self.last_add_variant: int | None = None
        self.last_create_variant: int | None = None
        self.last_dispatch_diag: dict | None = None
        self.last_write_probe: dict | None = None
        #: 诊断：上一次写入前的可写性检查是不是**靠 focus emulation 才「可见」**的
        #: （True = 窗口本来被遮挡，是我们把它变成 visible 的；只影响日志理解）
        self.last_write_forced_visible: bool = False

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<DesktopCDPCollector port={self.port} timeout={self.timeout}>"

    # ------------------------------------------------------------ 底层通道

    @contextmanager
    def _session(self, *, force_visible: bool = False) -> Iterator[CDPPage]:
        page = self.browser.page(self._tab())
        try:
            if force_visible:
                _focus_emulate(page)
            yield page
        finally:
            page.close()

    def _tab(self) -> Tab:
        if not port_open(self.port, constants.CDP_CONNECT_TIMEOUT):
            raise DesktopCDPError(
                f"网易云客户端调试端口 {self.port} 未监听 —— "
                f"请带 --remote-debugging-port={self.port} 启动客户端"
            )
        try:
            tab = self.browser.find_tab(ORPHEUS_URL_RE)
        except CDPError as e:
            raise DesktopCDPError(f"CDP 端点异常：{e}") from None
        if tab is None:
            raise DesktopCDPError(
                f"CDP 端点里没有 orpheus:// 页面（端口 {self.port}）—— "
                "客户端可能还在启动"
            )
        return tab

    def _eval(self, expression: str, *, force_visible: bool = False) -> Any:
        with self._session(force_visible=force_visible) as page:
            return page.evaluate(expression)

    def _eval_json(self, expression: str, what: str, *,
                   force_visible: bool = False) -> Any:
        raw = self._eval(expression, force_visible=force_visible)
        if raw is None:
            raise DesktopCDPError(f"{what}：JS 没有返回值（可能不是客户端页面）")
        if not isinstance(raw, str):
            return raw  # 允许未来实现直接回对象
        try:
            return json.loads(raw)
        except ValueError:
            raise DesktopCDPError(
                f"{what}：返回的不是 JSON：{raw[:200]!r}") from None

    # ------------------------------------------------------------ 健康检查

    def health(self, *, force_visible: bool = False) -> HealthStatus:
        """端口通不通 + 是不是客户端页面 + 有没有登录。

        `force_visible=True`：在这个会话里先开 focus emulation 再探测 ——
        写入前的可写性检查靠它（见 `_focus_emulate`）。
        """
        if not port_open(self.port, constants.CDP_CONNECT_TIMEOUT):
            return HealthStatus(
                False,
                f"网易云客户端调试端口 {self.port} 未监听 —— "
                f"请用 --remote-debugging-port={self.port} 启动客户端",
                {"port": self.port},
            )
        try:
            info = self.browser.version()
        except CDPError as e:
            return HealthStatus(False, f"CDP 端点异常：{e}", {"port": self.port})

        extra: dict[str, Any] = {
            "port": self.port,
            "browser": info.browser,
            "protocol": info.protocol,
        }
        try:
            tab = self.browser.find_tab(ORPHEUS_URL_RE)
        except CDPError as e:
            return HealthStatus(False, f"CDP 端点异常：{e}", extra)
        if tab is None:
            try:
                urls = [t.url for t in self.browser.list_tabs()]
            except CDPError:
                urls = []
            extra["tabs"] = urls
            return HealthStatus(
                False,
                f"没找到 orpheus:// 页面（当前 {len(urls)} 个 CDP 页面）—— "
                "客户端可能还没加载完前端",
                extra,
            )

        extra["tab_url"] = tab.url
        if force_visible:
            # 诊断：这次探测是**开着 focus emulation** 做的。
            # 判定「可见不可见」仍然只看 probe 里的 hidden —— 这一条只是溯源。
            extra["focus_emulation"] = True
        try:
            probe = self._eval_json(JS_HEALTH, "health", force_visible=force_visible)
        except (CDPError, DesktopCDPError) as e:
            return HealthStatus(False, f"前端求值失败：{e}", extra)
        if not isinstance(probe, dict):
            return HealthStatus(False, f"health 探测返回了意外类型：{type(probe).__name__}", extra)

        self.last_store_diag = probe.get("diag") if isinstance(probe.get("diag"), dict) else None
        extra.update({
            "hidden": probe.get("hidden"),
            "title": probe.get("title"),
            "has_store": bool(probe.get("hasStore")),
            "store_diag": self.last_store_diag,
            "top_keys": probe.get("topKeys") or [],
            "js_error": probe.get("error"),
        })

        if not probe.get("hasStore"):
            return HealthStatus(
                False,
                "找到了客户端页面，但没能从 React fiber / 全局量上拿到 redux store"
                "（DVA）—— 前端结构可能已变化，请跑 tools/probe_netease_state.py 看详情",
                extra,
            )
        user = probe.get("user")
        if not looks_like_user(user):
            return HealthStatus(
                False,
                "读到了 store，但里面没有登录用户信息 —— 客户端未登录",
                extra,
            )
        uid = user_id_of(user)
        nickname = str(user.get("nickname") or "").strip()
        extra["user_id"] = uid
        extra["nickname"] = nickname
        detail = f"网易云客户端 {info.browser}，已登录 {nickname or uid}"
        if probe.get("hidden"):
            detail += "（窗口当前隐藏 —— 只读没事，写入前请恢复窗口）"
        return HealthStatus(True, detail, extra)

    def require(self) -> None:
        st = self.health()
        if not st.ok:
            raise DesktopCDPError(st.detail)

    # ------------------------------------------------------------ 读歌单

    def dump_state(self, *, force: bool = False) -> dict:
        """整份 redux state（带 TTL 缓存）。给探测脚本与调试用 —— 可能几十 MB。"""
        now = time.monotonic()
        if (not force and self._state_cache is not None
                and (now - self._state_at) <= self.state_ttl):
            return self._state_cache
        data = self._eval_json(JS_STATE, "state")
        if isinstance(data, dict) and "__dcdp_error__" in data:
            raise DesktopCDPError(
                f"读不到 redux store：{data['__dcdp_error__']}"
                "（前端结构可能已变化，跑 tools/probe_netease_state.py 看详情）"
            )
        if not isinstance(data, dict):
            raise DesktopCDPError(f"store.getState() 不是对象：{type(data).__name__}")
        self._state_cache = data
        self._state_at = now
        return data

    # 兼容别名：探测脚本/调用方更容易理解的名字
    state = dump_state

    def self_user(self, state: dict | None = None) -> dict | None:
        return find_self_user(self.dump_state() if state is None else state)

    def _playlists_of(self, state: dict) -> tuple[list[dict], str]:
        if self._extract_state is state and self._extract_val is not None:
            return self._extract_val
        self_id = user_id_of(find_self_user(state))
        val = extract_playlists(state, self_id=self_id)
        self._extract_state, self._extract_val = state, val
        return val

    def _song_index_of(self, state: dict) -> dict[str, dict]:
        if self._index_state is state and self._index_val is not None:
            return self._index_val
        self._index_state, self._index_val = state, song_index(state)
        return self._index_val

    def list_playlists(self, *, only_created: bool = True,
                       refresh: bool = False) -> list[PlaylistRef]:
        """我创建的歌单（`only_created=False` 时连收藏的也返回）。

        「我创建的」判定顺序：`subscribed` 字段 → `creator.userId` 与登录用户比对
        → 判断不了就**宽松保留**（宁可多列，也不让目标歌单消失）。

        `refresh=True` 绕过 state 的 TTL 缓存 —— **写操作的「回查」必须用它**，
        否则查的是写入前的旧快照，回查就变成了走过场。
        """
        state = self.dump_state(force=refresh)
        items, source = self._playlists_of(state)
        self.last_playlist_source = source
        out: list[PlaylistRef] = []
        seen: set[tuple[str, str]] = set()
        for it in items:
            if only_created and it.get("created") is False:
                continue
            name = (it.get("name") or "").strip()
            if not name:
                continue
            pid = it.get("id") or ""
            key = (pid, name)
            if key in seen:
                continue
            seen.add(key)
            out.append(PlaylistRef(name=name, netease_id=pid or None))
        return out

    def find_playlist(self, name: str, *, refresh: bool = False) -> PlaylistRef | None:
        """模糊匹配（25.04 / 25年4月 / 25.1 / 25Q4 都能对上 25年4月/10月 这套）。

        带年份的候选优先；只有实在没有年份信息时才退而认「10月」「Q4」这类
        （min_score=0.6，仍高于「只写年份」的 0.5）。

        `refresh=True`：绕过 TTL 缓存重新读 store（写后回查用）。
        """
        refs = self.list_playlists(refresh=refresh)
        ranked = rank_playlist_names(
            name, [r.name for r in refs], allow_yearless=True,
            min_score=SCORE_YEARLESS_QUARTER,
        )
        if not ranked:
            return None
        return refs[ranked[0][0]]

    # ------------------------------------------------------------ 读歌单曲目

    def playlist_songs(
        self,
        playlist: PlaylistRef | str | int,
        *,
        allow_dom_fallback: bool | None = None,
        refresh: bool = False,
        source: str = "auto",
    ) -> list[dict]:
        """歌单里的歌（只读）。

        数据来源顺序（`source="auto"`）：

          1. **web API**（`api_playlist.WebPlaylistReader`）—— 服务端真相，
             完整（实测 `len(trackIds) == trackCount`）且带歌单顺序，
             还不需要用户先在客户端点开那个歌单；
          2. store 里该歌单对象自带的曲目（`tracks` 完整对象 / `trackIds`
             配全局歌曲索引）—— 零网络，但**实测几乎恒为空**；
          3. 显式开启时的 DOM 兜底（读当前渲染的歌曲行 fiber props）。
             有读错列表的风险，默认关，见模块 docstring。

        `source` 可钉死成 `"web"` / `"store"` / `"dom"`（诊断与单测用）；
        只有 `"auto"` 会走上面那条降级链。`source="web"` 时读不到就返回 `[]`，
        不会偷偷退回去读 store —— 否则「这条路到底通不通」就测不出来了。

        **纯数字的字符串当 id，不当名字**（`"17977808146"`）。理由：歌单名不可能
        是纯数字串，而 id 是从客户端/日志/上个命令里抄来抄去的东西，很容易变成
        字符串。踩过：真机 id 是 17977808146 这种大数，从命令行传进来必然是 str，
        当成名字去找 → 找不到 → **静默返回空列表**，看起来像「歌单是空的」。
        要按名字找就传名字，要明确就传 `PlaylistRef`。

        拿不到时返回 `[]`（不猜、不造数据）。诊断读写 `last_songs_source`
        与 `last_web_error`。
        """
        ref = playlist if isinstance(playlist, PlaylistRef) else None
        want_id = None
        want_name = None
        if ref is not None:
            want_id, want_name = ref.netease_id, ref.name
        elif isinstance(playlist, int) and not isinstance(playlist, bool):
            want_id = str(playlist)
        elif isinstance(playlist, str) and playlist.strip().isdigit():
            want_id = playlist.strip()
        else:
            want_name = str(playlist)

        self.last_web_error = None
        self.last_songs_source = None

        if source in ("auto", "web") and self.web_playlist is not None:
            # web API 只认 id，不认名字 → 没有 id 就先在 store 里换一个
            pid = want_id
            if not pid and want_name:
                pid = self._lookup_playlist_id(want_name)
            if pid:
                try:
                    snap = self.web_playlist.songs(pid)
                except WebPlaylistError as e:
                    self.last_web_error = str(e)
                else:
                    if snap.songs:
                        self.last_songs_source = f"web:{pid}"
                        return snap.songs
                    self.last_web_error = (
                        f"web API 读到了 {len(snap.track_ids)} 个 trackId，"
                        "但歌曲详情一个都没拿到")
            else:
                self.last_web_error = f"拿不到歌单 {want_name!r} 的 id，无法走 web API"
            if source == "web":
                return []

        state = self.dump_state(force=refresh)
        items, src = self._playlists_of(state)
        self.last_playlist_source = src
        entry = self._pick_entry(items, want_id, want_name)
        index = self._song_index_of(state)
        songs = songs_from_playlist_entry(entry, state, index=index) if entry else []
        if songs:
            self.last_songs_source = "store"

        use_dom = self.allow_dom_fallback if allow_dom_fallback is None else bool(allow_dom_fallback)
        if not songs and (use_dom or source == "dom"):
            songs = self._songs_from_dom()
            if songs:
                self.last_songs_source = "dom"
        return songs

    def _lookup_playlist_id(self, name: str) -> str | None:
        """按名字在 store 里换一个歌单 id（web API 只认 id，不认名字）。

        拿不到就返回 None —— 这里**只是名字→id 的翻译**，
        不代表对「歌单在不在」下了任何判断。
        """
        s = ("" if name is None else str(name)).strip()
        if not s:
            return None
        if s.isdigit():
            # 与 playlist_songs() 同一条规则：纯数字串是 id，不是名字。
            return s
        try:
            ref = self.find_playlist(s)
        except (CDPError, DesktopCDPError):
            return None
        return (ref.netease_id or None) if ref is not None else None

    @staticmethod
    def _pick_entry(items: list[dict], want_id: str | None, want_name: str | None) -> dict | None:
        if want_id:
            for it in items:
                if it.get("id") and str(it["id"]) == str(want_id):
                    return it
        if want_name:
            for it in items:
                if it.get("name") == want_name:
                    return it
            # 名字对不上就再走一次模糊匹配（歌单名写法不统一）
            ranked = rank_playlist_names(want_name, [it.get("name") or "" for it in items],
                                        allow_yearless=True,
                                        min_score=SCORE_YEARLESS_QUARTER)
            if ranked:
                return items[ranked[0][0]]
        return None

    def _songs_from_dom(self) -> list[dict]:
        """⚠️ 兜底：读**当前渲染**的歌曲行（可能是搜索结果，不一定是目标歌单）。"""
        data = self._eval_json(JS_DOM_SONGS, "dom-songs")
        if not isinstance(data, dict):
            return []
        self.last_dom_context = data.get("context") if isinstance(data.get("context"), dict) else None
        rows = data.get("rows")
        if not isinstance(rows, list):
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            song = normalize_song(row)
            sid = song["netease_song_id"]
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append(song)
        return out

    # ------------------------------------------------------------ 写入（§7.3 / §7.4）
    #
    # 这一段的每一步都遵守同一件事：**dispatch 的返回值不是判据**。
    #   · ensure_playlist：创建后重新 list_playlists() 看到它才算数
    #   · add_to_playlist：写入后重新读歌单曲目看到歌才算数
    # 拿不到确认时，一个抛错、一个返回 False —— 都不许静默当成功。

    def _throttle(self) -> float:
        """写入限速（§7.4）。返回这次实际睡了多久。"""
        slept = self.rate_limiter.wait()
        self.last_write_wait = slept
        return slept

    def _require_writable(self) -> HealthStatus:
        """写入前的可写性检查：端口/页面/store/登录 都要好，窗口不能是隐藏的。

        「窗口隐藏」这一条**先尝试修复再判定**：`force_visible=True` 会在同一个
        会话里打开 focus emulation，然后**重新读** `document.hidden`。读出来仍是
        true（老 CEF 没这个方法）才拒绝 —— 于是这条守卫从「一被遮挡就没法用」
        变成了「一被遮挡就自动修，修不动才报错」。
        """
        st = self.health(force_visible=True)
        self.last_write_forced_visible = bool(
            st.extra.get("focus_emulation") and not st.extra.get("hidden"))
        if not st.ok:
            raise DesktopCDPError(f"写入前的健康检查没过：{st.detail}")
        if st.extra.get("hidden") and not self.write_policy.allow_hidden_window:
            raise DesktopCDPError(
                "客户端窗口当前确实被遮挡/隐藏（document.hidden=true，且 focus "
                "emulation 没能把它变成 visible）—— 缺 "
                "--disable-features=CalculateNativeWinOcclusion 时前端会停止渲染、"
                "清空 portal，写入可能静默失败；请把客户端窗口切到前台再试"
            )
        return st

    def _dispatch_action(self, action: dict) -> tuple[dict, wp.ActionResult, str | None]:
        """派发一次 action，并**如实**回报结果。

        返回 `(envelope, 判读结果, 错误消息)`。判读结果里的 `ok=True` 只表示
        「dispatch 自报成功」，**调用方仍然必须回查**（见 `parse_action_result` 的说明）。
        """
        try:
            # force_visible：和可写性检查一样，在**同一个会话**里让页面认为自己是
            # 可见的。窗口被遮挡时前端会停止渲染，dispatch 有静默失败的风险；
            # emulation 随会话失效，所以这里必须再来一次。
            env = self._eval_json(js_dispatch(action), "dispatch", force_visible=True)
        except (CDPError, DesktopCDPError) as e:
            msg = f"CDP 层失败：{e}"
            self.last_dispatch_diag = {
                "action_type": action.get("type"), "env": None, "result": msg}
            return {}, wp.ActionResult(False, msg, "cdp", "error"), msg
        if not isinstance(env, dict):
            env = {"called": True, "returned": env, "threw": False}
        res = wp.parse_action_result(env)
        payload = action.get("payload")
        self.last_dispatch_diag = {
            "action_type": action.get("type"),
            "payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else None,
            "via": env.get("via"),
            "threw": env.get("threw"),
            "error": env.get("error"),
            "returned": env.get("returned"),
            "result": res.summary(),
        }
        err = None if res.ok is not False else res.reason
        return env, res, err

    def _store_membership(self, playlist: PlaylistRef, song_id: str, *,
                          refresh: bool) -> wp.Membership:
        """从**客户端 store** 读一次歌单曲目，判断 `song_id` 在不在里面。

        ⚠️ 实测：store 里的歌单对象**没有曲目数组**，所以这条路几乎恒返回
        `None`（证据不足）。它留着是因为代价为零，且用户真在客户端点开过
        那个歌单时它确实能给答案。真正的答案由 `_web_membership` 给。
        """
        state = self.dump_state(force=refresh)
        items, source = self._playlists_of(state)
        self.last_playlist_source = source
        entry = self._pick_entry(items, playlist.netease_id, playlist.name)
        if entry is None:
            return wp.Membership(
                None,
                f"store 里找不到歌单 {playlist.name!r}(id={playlist.netease_id}) "
                f"—— 无法核对曲目（歌单来源：{source}）",
                False, 0, None, )
        songs = songs_from_playlist_entry(entry, state, index=self._song_index_of(state))
        if entry.get("track_count") == 0 and not wp.contains_song(songs, song_id):
            # 客户端 store 可能尚未刷新，0 不足以证明服务端为空；交给 web API 核对。
            return wp.Membership(
                None, "store 自报 trackCount=0，但可能尚未加载或刷新，需要 web API 核对",
                False, 0, 0)
        return wp.check_membership(songs, song_id,
                                   declared_count=entry.get("track_count"),
                                   entries=len(entry.get("tracks") or []),
                                   source="store")

    def _web_membership(self, playlist: PlaylistRef, song_id: str) -> wp.Membership | None:
        """从 **web API** 读一次歌单曲目，判断 `song_id` 在不在里面。

        返回 None = 这条路**根本没走**（没开 web 读取器 / 歌单对象没有 id）；
        其余情况一律返回 Membership，包括「读失败」与「歌单不存在」——
        它们都是 `present=None` 的「证据不足」，而不是「不在里面」。
        让它们返回 None 会把 web 这一侧的理由整个丢掉，用户就只看到 store
        那句在这种场景下并不成立的建议。

        为什么用 `trackIds` 而不是 `tracks` 判「在不在」：实测
        `len(trackIds) == trackCount`（完整），而 `tracks` 恒只有前 10 个。
        """
        if self.web_playlist is None:
            return None
        pid = wp.norm_id(playlist.netease_id)
        if pid is None:
            self.last_web_error = f"歌单 {playlist.name!r} 没有 netease_id，读不了 web API"
            return None
        try:
            snap = self.web_playlist.snapshot(pid)
        except PlaylistNotFound as e:
            # 「歌单不存在」是**确定的答案**，不是读失败：这个 id 指向的歌单没了。
            self.last_web_error = str(e)
            return wp.Membership(
                None,
                f"web API 说歌单 id={pid} 不存在（{playlist.name!r}）—— "
                "歌单可能已被删除；不能据此断定歌曲不在里面",
                False, 0, None)
        except WebPlaylistError as e:
            # 读失败也**照样返回一个 Membership**（present=None），而不是 None。
            # 区别很要紧：返回 None 会让 `_membership()` 把 web 这一侧整个丢掉，
            # 于是报给用户的只有 store 那句「先去客户端打开这个歌单」——
            # 一句在这种情况下**根本不成立**的建议，而且看不出网络出了问题。
            self.last_web_error = str(e)
            return wp.Membership(
                None, f"web API 读不到曲目：{e}", False, 0, None)
        self.last_web_error = None
        return wp.check_membership(snap.track_ids, song_id,
                                   declared_count=snap.declared_count,
                                   entries=len(snap.track_ids),
                                   source="web API")

    def _membership(self, playlist: PlaylistRef, song_id: str,
                    *, refresh: bool) -> wp.Membership:
        """判断 `song_id` 在不在歌单里（三态，见 write_path）。

        两个数据源，**先到先得**：

          1. store —— 零网络、零风控；能给出结论就直接用；
          2. web API —— store 判不出来时补上（实测这才是常态）。

        两边的结论都是「确定」的就用确定的；都不确定时返回**证据不足**，
        并把两边的理由都写进去（否则用户只看到一句「判不出来」，
        完全不知道该修哪一边）。
        """
        store = self._store_membership(playlist, song_id, refresh=refresh)
        if store.present is not None:
            return store

        web = self._web_membership(playlist, song_id)
        if web is None:
            return store
        if web.present is not None:
            return web

        # 两边都只有「证据不足」：合成一条，两个理由都留着
        return wp.Membership(
            None,
            "store 与 web API 都判不出来 —— "
            f"store：{store.reason} ／ web API：{web.reason}",
            False, max(store.resolved, web.resolved),
            web.declared if web.declared is not None else store.declared)

    def _await_membership(self, playlist: PlaylistRef, song_id: str, *,
                          timeout: float | None = None,
                          poll: float | None = None) -> wp.Membership:
        """回查：写入后轮询到「确认在里面」或超时。

        超时也返回最后一次的判定（present 可能是 False/None）—— 调用方按失败处理。
        """
        t = self.write_policy.confirm_timeout if timeout is None else float(timeout)
        p = self.write_policy.confirm_poll if poll is None else float(poll)
        deadline = time.monotonic() + max(0.0, t)
        last = wp.Membership(None, "还没开始回查", False)
        while True:
            try:
                last = self._membership(playlist, song_id, refresh=True)
            except (CDPError, DesktopCDPError) as e:
                last = wp.Membership(None, f"回查失败：{e}", False)
            if last.present is True:
                return last
            if time.monotonic() >= deadline:
                return last
            time.sleep(max(0.01, p))

    def _await_playlist(self, name: str, *, timeout: float | None = None,
                        poll: float | None = None) -> PlaylistRef | None:
        """回查：创建后重新拉歌单列表，直到看到它出现或超时。"""
        t = self.write_policy.confirm_timeout if timeout is None else float(timeout)
        p = self.write_policy.confirm_poll if poll is None else float(poll)
        deadline = time.monotonic() + max(0.0, t)
        while True:
            try:
                ref = self.find_playlist(name, refresh=True)
            except (CDPError, DesktopCDPError):
                ref = None
            if ref is not None:
                return ref
            if time.monotonic() >= deadline:
                return None
            time.sleep(max(0.01, p))

    def _playlist_names(self) -> list[str]:
        """当前 store 里所有歌单的名字（失败返回 []，只用于诊断，不参与判断）。"""
        try:
            return [r.name for r in self.list_playlists(only_created=False, refresh=True)]
        except (CDPError, DesktopCDPError):
            return []

    # ------------------------------------------------------------ ensure_playlist

    def ensure_playlist(self, name: str, *, refresh: bool = False) -> PlaylistRef:
        """存在就复用；不存在就创建，**创建后必须回查确认**。

        回查不到就抛 `DesktopCDPError`（绝不返回一个「大概建好了」的 ref）。
        """
        target = ("" if name is None else str(name)).strip()
        if not target:
            raise DesktopCDPError("歌单名称为空 —— 拒绝查找/创建空名歌单")

        self.last_write_error = None
        self.last_write_note = None
        self.last_create_variant = None
        self.last_write_action = None

        hit = self.find_playlist(target, refresh=refresh)
        if hit is not None:
            self.last_write_note = f"复用已有歌单 {hit.name!r}（id={hit.netease_id}），未创建"
            self.last_write_action = WRITE_ACTION_REUSED
            return hit

        self._require_writable()
        before = self._playlist_names()

        tried: list[str] = []
        for idx in self.write_policy.create_variant_order():
            v = wp.CREATE_PAYLOAD_VARIANTS[idx]
            action = wp.build_create_playlist_payload(target, variant=idx)
            self._throttle()
            _env, res, _err = self._dispatch_action(action)
            confirmed = self._await_playlist(target)
            if confirmed is not None:
                self.last_create_variant = idx
                self.last_write_note = (
                    f"创建成功（候选 {idx} {v.name}）：{confirmed.name!r}"
                    f"（id={confirmed.netease_id}）；dispatch {res.summary()}")
                self.last_write_action = WRITE_ACTION_CREATED
                return confirmed
            tried.append(f"候选 {idx}（{v.name}）：dispatch {res.summary()}；回查没看到歌单出现")

        after = self._playlist_names()
        new_names = [n for n in after if n not in before]
        lines = [
            f"创建歌单 {target!r} 失败：试过的候选都被回查否决了 ——",
            *[f"  - {t}" for t in tried],
            "注意：§7.3 明确写着「歌单创建流程的具体 action」尚未解决，"
            "所以这 5 条候选全是推测，全部失败是**预期内**的结果之一。",
        ]
        if new_names:
            lines.append(
                f"⚠ 但期间 store 里多出了这些歌单：{new_names} —— "
                "客户端可能真的建了，只是名字/字段对不上；请人工确认，别重复创建。")
        lines.append(
            "下一步：跑 DesktopCDPCollector.probe_write_path() 看客户端真实的 "
            "props.data 形状，再决定 pin 哪条候选（或换网页版兜底路线）。")
        msg = "\n".join(lines)
        self.last_write_error = msg
        logger.error("ensure_playlist 失败：%s", msg)
        raise DesktopCDPError(msg)

    # ------------------------------------------------------------ add_to_playlist

    def add_to_playlist(self, song_id: str, playlist: PlaylistRef) -> bool:
        """把一首歌加进歌单。**幂等**；写入后必须回查确认，确认不到就返回 False。

        返回 True 的两种含义（都在 `last_write_note` 里说明）：
            · 已经在歌单里（跳过，没发任何写入请求）
            · 真的写进去了，且回查看到了

        ⚠️ 「payload 形状」是已知空白（§7.3）：默认按 confidence 依次试多条候选，
        每次都以回查为准；某条成功后记进会话缓存，下一首先试它。
        要 pin 一条就 `DesktopCDPCollector(add_variant=N)`。
        失败原因写在 `self.last_write_error`（含所有试过的候选与回查结论）。
        """
        self.last_write_error = None
        self.last_write_note = None
        self.last_add_variant = None
        self.last_dispatch_diag = None
        #: 本次**实际做了什么**的结构化标记，取值见 `WRITE_ACTION_*`。
        #: 存在的意义：`last_write_note` 是给**人**看的说明文字，
        #: 拿它做逻辑判断（例如 `"跳过" in note`）非常脆 ——
        #: 一改文案就静默失效，而且不会报错。调用方要判断
        #: 「这次是真写进去了还是本来就已存在」必须读这个字段。
        self.last_write_action: str | None = None

        sid = wp.norm_id(song_id)
        if sid is None:
            return self._write_fail("song_id 为空 —— 拒绝写入")
        if not isinstance(playlist, PlaylistRef):
            return self._write_fail(
                f"playlist 必须是 PlaylistRef，收到 {type(playlist).__name__}")
        if wp.norm_id(playlist.netease_id) is None:
            return self._write_fail(
                f"歌单 {playlist.name!r} 没有 netease_id —— 不知道加到哪个歌单，"
                "拒绝写入（**不猜目标歌单**）")

        # ① 幂等预检（用缓存态即可：TTL 只有 2 秒）
        try:
            pre = self._membership(playlist, sid, refresh=False)
        except (CDPError, DesktopCDPError) as e:
            return self._write_fail(f"读歌单曲目失败，无法做幂等预检：{e}")
        if pre.present is True:
            self.last_write_note = f"{sid} 已经在 {playlist.name!r} 里 —— 跳过（幂等）"
            self.last_write_action = WRITE_ACTION_SKIPPED
            return True
        if pre.present is None and self.write_policy.require_complete_membership:
            return self._write_fail(
                f"无法确认 {sid} 是否已在歌单 {playlist.name!r} 里：{pre.reason}。\n"
                "策略 require_complete_membership=True → 拒绝写入。"
                "判不出来就当失败：既怕重复加歌，更怕假装成功。")

        # ② 可写性
        try:
            self._require_writable()
        except DesktopCDPError as e:
            return self._write_fail(str(e))

        # ③ 依次试候选，每次都用回查判成败
        order = ([self._pinned_add_variant]
                 if self._pinned_add_variant is not None
                 else self.write_policy.add_variant_order(hint=self._add_variant_hint))
        tried: list[str] = []
        # 真机实测的 splice action 里 `data.resource` 是**完整歌曲对象**，
        # 不是裸 id。我们手上有 web API 能取到同一个域对象，所以先取一次：
        # 忠实复现比「猜 {id} 够不够」更值得花这一次请求（每首一次，不循环）。
        resource = self._song_resource(sid)
        for idx in order:
            v = wp.ADD_PAYLOAD_VARIANTS[idx]
            action = wp.build_add_payload(sid, wp.norm_id(playlist.netease_id),
                                          variant=idx, resource=resource)
            self._throttle()
            _env, res, _err = self._dispatch_action(action)
            post = self._await_membership(playlist, sid)
            if post.present is True:
                self.last_add_variant = idx
                if not v.is_control:
                    self._add_variant_hint = idx
                self.last_write_note = (
                    f"成功：{sid} → {playlist.name!r}（候选 {idx} {v.name}）；"
                    f"dispatch {res.summary()}；回查：{post.reason}")
                self.last_write_action = WRITE_ACTION_ADDED
                return True
            tried.append(f"候选 {idx}（{v.name}）：dispatch {res.summary()}；回查：{post.reason}")

        msg = [
            f"把 {sid} 加进歌单 {playlist.name!r}(id={playlist.netease_id}) 失败："
            "所有试过的候选都没能通过回查 ——",
            *[f"  - {t}" for t in tried],
            "提醒：候选 payload 形状是 §7.3 的已知空白，全部失败属于预期结果之一；"
            "不代表写入一定没发生（回查数据本身可能不完整）。",
            "下一步：跑 probe_write_path() 核对客户端真实的 props.data 形状。",
        ]
        if not tried:  # pragma: no cover - max_add_variants=0 的配置错误
            msg.append("（write_policy.add_variant_order() 返回了空列表 —— 检查 max_add_variants）")
        return self._write_fail("\n".join(msg))

    def _song_resource(self, song_id: str) -> dict | None:
        """取歌曲对象，喂给实测 payload 里的 `data.resource`。

        真机抓到的 action 里这一项是**完整对象**（40+ 字段），不是 `{id}`。
        最忠实的做法就是拿同一族的服务端对象 —— 客户端那份本来就是同一个接口来的。

        · 客户端把 id 统一成**字符串**（实测 `"id": "3406947013"`），这里跟着做，
          免得「数字 vs 字符串」这种差异在不该出错的地方出错；
        · 拿不到就返回 None，让 payload 退化成 `{"id": ...}`（是否够用**尚未实测**）。
          **不抛错**：一个可选字段取不到，不该让整次写入直接失败。
        """
        if self.web_playlist is None:
            return None
        try:
            got = self.web_playlist.song_details([song_id])
        except WebPlaylistError as e:
            self.last_web_error = str(e)
            return None
        if not got:
            return None
        raw = got[0].get("raw")
        if not isinstance(raw, dict):
            return None
        out = dict(raw)
        out["id"] = song_id           # 与客户端一致：字符串 id
        return out

    def _write_fail(self, reason: str) -> bool:
        self.last_write_error = reason
        self.last_write_action = WRITE_ACTION_FAILED
        logger.error("写入失败：%s", reason)
        return False

    # ------------------------------------------------------------ 写入探针

    def probe_write_path(self, *, song_id: str | int = "", playlist_id: str | int = "",
                         name: str = "") -> dict:
        """写入链路探针：**把「推测」变成「事实」的那一步**。

        返回::

            {"offline": [每条候选：name/confidence/rationale/payload],
             "live": {客户端里的实际情况} | None,
             "live_error": str | None}

        `live` 里最要紧的是 `menuWrappers[*].data` —— §7.3 路线 A 第 2 步说的那种
        「props 上同时有 menu 和 data」的组件，它承载的 `data` 形状就是 payload
        该长什么样。拿到它之后，把 `write_path.ADD_PAYLOAD_VARIANTS` 里对应的那条
        提到最前（或直接 `add_variant=` pin 住），推测就变成了事实。
        """
        offline = wp.describe_payload_variants(
            song_id=song_id or None, playlist_id=playlist_id or None,
            name=name or None)
        out: dict[str, Any] = {"offline": offline, "live": None, "live_error": None}
        try:
            live = self._eval_json(JS_WRITE_PROBE, "write-probe")
        except (CDPError, DesktopCDPError) as e:
            out["live_error"] = str(e)
            return out
        out["live"] = live
        self.last_write_probe = live if isinstance(live, dict) else None
        return out
