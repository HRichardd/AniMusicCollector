"""写入歌单的**策略层**：候选 payload、成败判定、幂等核对、限速。

对应 DESIGN.md §7.3（写入链路）、§5.10（推送前二次比对）、原则 2（走 DVA dispatch）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
验证状态（写这个模块时客户端没运行，9222 未监听 —— 别把下面的东西当已实测）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ 已验证（离线纯函数，`tests/test_write_path.py` 覆盖）：
   · id 归一化与「歌单里有没有这首歌」（str/int/float/嵌套/裸标量都吃）
   · 缺席判定的三态（能证明不在 / 不能判断）—— 见 check_membership
   · dispatch 返回值的多形状判读（None / bool / 数字码 / 字符串 / 各种 dict / envelope）
   · 候选 payload 的构造与合法性守卫（空 id 直接报错，不猜）
   · 限速器（注入假时钟/假 sleep，离线可断言）
   · 与 CDP 的往返、回查确认、失败路径 —— 用 tests/test_cdp.py 的假 CDP 覆盖

🟡 半验证（依据 DESIGN.md §7.3 逆出来的 bundle 原文，只确定了**外层**形状）：
   · `async:action/doAction` 的 payload 是 `{actionId, data, parentData, eventType}`
   · `async:action/popMenu` 的 payload 是 `{...data, actionPayload, parentData}`，
     且 `parentData` / `eventType` **同时**在 action 顶层；eventType 分别是 click / rightMenu
   · actionId 枚举里确实有 `addToPlayList` / `favorite` / `favoriteAndCreate`
   · `dispatch` 能从 fiber 的 `memoizedProps.dispatch` 拿到（附录 B「可用，返回新 state」）

⚠️ 完全没有实测依据（本模块的核心不确定性，全部按「多候选 + 回查」处理）：
   · **`payload.data` 的确切形状** —— §7.3「尚未解决的部分」第一条。
     下面 ADD_PAYLOAD_VARIANTS 里 7 条候选**没有一条是实测确认的**，
     每条的 rationale 都写明了它凭什么被列进来、哪一部分是猜的。
   · **歌单创建走哪个 action** —— §7.3 同上第二条，CREATE_PAYLOAD_VARIANTS 同理。
   · `rawEvent`：原文里它是 React 合成事件（`i.persist()` 之类），Python 侧造不出来。
     我们传 `null`，由注入的 JS 换成同形的空实现桩（persist/stopPropagation/...）。
     **这也只是推测** —— 如果 saga 真的读了事件的字段，这条会失败。
   · 一次 dispatch 到底能不能完成「加入歌单」。§7.3 路线 A 第 4 步写的是
     「菜单出现后点『添加到歌单』→ 歌单选择弹窗 → 选中目标歌单」——
     按这个描述，`addToPlayList` 很可能只是**打开弹窗**，单次 dispatch 不够。
     对照候选 `addToPlaylist·对照:不带目标歌单` 就是为区分这两种情况准备的。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
三条硬规则（写操作绝不能假装成功）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. **回查是唯一的成功判据**。dispatch 不抛错 ≠ 加进去了；`parse_action_result`
   返回 True 也只是「dispatch 自报成功」，调用方仍然必须重新读歌单确认。
2. **判不出来就算失败**。`check_membership` 拿不到完整曲目数据时返回
   `present=None`，调用方必须当失败处理（宁可报错让用户去客户端打开歌单，
   也不能写一个「大概成功」进数据库）。
3. **限速**（§7.4）：所有会改状态的 dispatch 都过 `RateLimiter`
   （`constants.WRITE_RATE_RPS`，默认 1 req/s），读回查不限速。

本模块**不 import desktop_cdp**（单向依赖：desktop_cdp → write_path），
所以可以完全离线测试。命令行探针（**推荐用 `-c` 这种写法**：`python -m
animusic.collector.write_path` 也能跑，但会因为 `collector/__init__` 已经 import
过本模块而多出一条无害的 runpy RuntimeWarning）::

    python -c "from animusic.collector.write_path import main; main()"
    # 打印全部候选 payload（离线，不需要客户端）
    ... main(['--probe', '--song-id', '1375305989', '--playlist-id', '1001'])
    # 连客户端 dump「写入探针」（把 props.data 的真实形状挖出来）
    ... main(['--live'])
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence

from .. import constants

__all__ = [
    # action 形状常量（§7.3 实测到的部分）
    "ACTION_ADD_TO_PLAYLIST",
    "ACTION_CREATE",
    "ACTION_CREATE_PLAYLIST",
    "ACTION_FAVORITE_AND_CREATE",
    "ACTION_TYPE_DO_ACTION",
    "ACTION_TYPE_POP_MENU",
    "SYNTHETIC_EVENT_KEY",
    "SYNTHETIC_ACTION_PAYLOAD_KEY",
    "ADD_PAYLOAD_VARIANTS",
    "CREATE_PAYLOAD_VARIANTS",
    "PayloadVariant",
    "build_add_payload",
    "build_create_playlist_payload",
    "describe_payload_variants",
    "render_payload_report",
    "action_to_js_literal",
    # id / 成员判定
    "norm_id",
    "iter_song_ids",
    "contains_song",
    "Membership",
    "check_membership",
    # 结果判读
    "ActionResult",
    "parse_action_result",
    # 策略与限速
    "RateLimiter",
    "WritePolicy",
    "WritePathError",
    "main",
]


class WritePathError(ValueError):
    """候选 payload 构造失败（调用方给的 id 不合法 / 候选序号越界）。"""


# ═══════════════════════════════════════════════════════════════════════════
# 一、§7.3 实测到的 action 形状（外层）+ 候选 payload（内层全是推测）
# ═══════════════════════════════════════════════════════════════════════════

#: §7.3 chunk 2 / module 7 的 actionId 枚举里确实存在的项
ACTION_ADD_TO_PLAYLIST = "addToPlayList"
ACTION_CREATE = "create"
#: 枚举里**没有** createPlaylist —— 但枚举只是「已知项」不是全集，故留一条候选
ACTION_CREATE_PLAYLIST = "createPlaylist"
#: 枚举里有它；语义更像「收藏这首歌并顺手建歌单」，正常建歌单用不到，
#: 所以没有为它生成候选（需要歌曲上下文，不适合纯建歌单）。留常量只为可读性。
ACTION_FAVORITE_AND_CREATE = "favoriteAndCreate"

ACTION_TYPE_DO_ACTION = "async:action/doAction"
ACTION_TYPE_POP_MENU = "async:action/popMenu"

#: bundle 原文里 `actionPayload: { rawEvent: i, ... }` 的 i 是 React 合成事件。
#: Python 侧造不出来 → 传 None，注入的 JS 见到这个键是 None 就换成同形桩。
SYNTHETIC_EVENT_KEY = "rawEvent"
SYNTHETIC_ACTION_PAYLOAD_KEY = "actionPayload"

#: 真实 action 里 `data.callback` 是一个函数（Python 侧造不出来）。
#: 用一个**字符串哨兵**占位，由注入的 JS（`__dcdpPatchSynthetic`）换成空函数。
#: 为什么用「按值」而不是「按键名」识别：`callback` 这个词在别的上下文里也可能出现，
#: 而哨兵串是我们自己造的，绝不会撞上客户端真实数据。
SYNTHETIC_FUNCTION = "__dcdp_fn__noop"

# ── 2026-10 真机实测确认的「加入歌单」action（见 DESIGN.md 附录 G.15） ──────
#
# 来源：在客户端里**手动执行一次**「把《決意の唄》加入 26年4月」，用
# `tools/probe_netease_write.py install` 装的 dispatch 记录器把真实 action 抓下来。
# 抓到的原文（省略歌曲对象的 40 多个字段）：
#
#   { type: "async:action/doAction",
#     payload: {
#       actionId: "splice",
#       data: {
#         resource: {id: "3406947013", name: "決意の唄", ...},
#         resourceType: "track",
#         from: {scene: "playlist", isTableViewerAction: true,
#                resourceIdOfScene: "17977808146", listData: [...], sourceData: {...}},
#         to:   {scene: "playlist", resourceIdOfScene: "17977808146",
#                resource: {...}, insertIndex: 0},
#         callback: function () {},
#         actionPayload: {}
#       },
#       eventType: "drop"
#     } }
#
# 结果经 web API 核对：26年4月 trackCount 43 → 44，3406947013 出现在 trackIds 里。
#
# ⚠️ 关于「这是不是 menu 里的『添加到歌单』」：记录里 from 与 to 是**同一个歌单**，
#    所以它也可能是「拖到当前列表里」。两种解释下 `to.resourceIdOfScene` 都表示
#    **目标歌单**，这正是我们唯一需要的语义。为了忠实复现，本候选把 from 也填成
#    目标歌单（与实测原文一致），并在 `is_control` 之外额外标 `measured=True`。
ACTION_SPLICE = "splice"
RESOURCE_TYPE_TRACK = "track"
SCENE_PLAYLIST = "playlist"

# ── 2026-10 真机实测确认的「创建歌单」action（见 DESIGN.md 附录 G.15） ──────
#
# 同样是「让用户在客户端做一次、记录器抓下来」拿到的。捕获原文：
#
#   { type: "async:hostResource/createPlaylist",
#     payload: { name: "26年10月", isPrivate: false } }
#
# 结果：歌单列表 129 → 130，`26年10月` 出现，id=18457224255，web API 核对
# trackCount=0 / privacy=0。**所以这条是实测的，不是推测。**
#
# 形状上和 splice 完全不是一族：splice 是 `{actionId, data, eventType}` 的
# doAction，而这条是**扁平 payload**，action 类型自带 dva 的 `namespace/method`
# （`async:hostResource` 就是 store 里存我那 130 个歌单的那个 model）。
# 原来那 5 条猜的候选全都套在 doAction 家族上 —— 家族选错了，怎么调字段都没用。
ACTION_TYPE_HOST_CREATE_PLAYLIST = "async:hostResource/createPlaylist"
#: 实测用的字段名是 `isPrivate`（不是 `privacy`），且 false = 公开。
#: 用户现有的季度歌单实测都是 `privacy: 0`（公开），所以默认跟它一致。
DEFAULT_IS_PRIVATE = False


def _host_create_playlist(name: str, *, is_private: bool = DEFAULT_IS_PRIVATE) -> dict:
    """建歌单 —— 真机实测的动作（扁平 payload，没有 actionId/eventType）。"""
    return {
        "type": ACTION_TYPE_HOST_CREATE_PLAYLIST,
        "payload": {"name": name, "isPrivate": bool(is_private)},
    }


# ── 2026-10 真机实测确认的「收藏到歌单」（= 加入歌单） action ──────────────
#
# 捕获原文（n=184，把《虹色の日々Duet ver.》加进「26年10月」）：
#
#   { type: "async:action/doAction",
#     payload: {
#       actionId: "favorite",
#       data: {
#         resource: {id:"3395172821", name:"虹色の日々Duet ver.", ...},  // 30 个字段
#         resourceType: "track",
#         from: {scene: "playlist"},                    // ← 只有 scene
#         to:   {resourceIdOfScene: "18457224255"},     // ← 只有目标歌单
#         callback: function(){},
#         actionPayload: {rawEvent: <合成事件>, fromDownload: false}
#       },
#       eventType: "click",
#       parentData: {from: {scene:"playlist", isTableViewerAction:true,
#                           listData:[...], sourceData:{...},
#                           resourceIdOfScene:"17977808146"}}   // ← 当时在看的歌单
#     } }
#
# ⚠️ **这一条推翻了之前对 `splice` 的判断**（见 ACTION_SPLICE 上方）。
#    真正的「添加到歌单」是 `favorite`，而 `splice` 是**在当前列表里拖动**。
#    之前误判的原因很值得记下来：第一次录的是「人在 26年4月、加进 26年4月」，
#    于是 `splice`（拖动）和 `favorite`（收藏）**同时出现且 from==to**，
#    两个都能解释「trackCount 43→44」，我挑了错的那个。
#    换成「人在 26年4月、加进 26年10月」之后两者立刻分开了：
#    `splice` 压根不再出现，只有 `favorite`，且 `to.resourceIdOfScene` 干净地指向目标。
#
#    教训：**录一次样本就下结论，等于没验**。要让两个假设做出**不同的预测**
#    （这次是「加到一个不是当前视图的歌单」），才能把它们分开。
ACTION_FAVORITE = "favorite"
#: 实测原文里 actionPayload 里除了合成事件还有这个。推测它决定走「收藏」还是
#: 「下载后收藏」两条分支；实测值是 false，照抄。
ACTION_PAYLOAD_FROM_DOWNLOAD = "fromDownload"
DEFAULT_FROM_DOWNLOAD = False


def _favorite_action(song_id: str, playlist_id: str, *,
                     resource: dict | None = None,
                     parent_playlist_id: str | None = None) -> dict:
    """把一首歌收藏进目标歌单 —— 真机实测的写成动作。

    `to.resourceIdOfScene` **是唯一决定写进哪张歌单的字段**，而且实测里
    `data.from` 根本不含歌单信息（只有 `scene`）—— 也就是说这个动作
    **不依赖「客户端正停在目标歌单」**，这正是它能 headless 调用的原因。

    `parent_playlist_id`：实测里 `payload.parentData.from.resourceIdOfScene`
    是**发起时正在看的**歌单。我们没有「正在看」这个概念 → 默认 `None` 表示
    **整个 parentData 都不发**（少发一个不知道语义的字段，好过编一个）。
    想看它有没有用，把它填上再试即可。
    """
    song = dict(resource) if isinstance(resource, dict) and resource else {"id": song_id}
    rid = norm_id(song.get("id"))
    if rid is not None and rid != song_id:
        raise WritePathError(
            f"歌曲 id 打架：song_id={song_id!r} 但 resource.id={rid!r} —— "
            "不知道要加哪首歌，拒绝构造 payload（猜错会往歌单里塞错歌）")
    song["id"] = song_id
    payload: dict = {
        "actionId": ACTION_FAVORITE,
        "data": {
            "resource": song,
            "resourceType": RESOURCE_TYPE_TRACK,
            # 实测：只有 scene，没有 resourceIdOfScene —— 来源不需要被识别
            "from": {"scene": SCENE_PLAYLIST},
            # 实测：只有这一个键。目标歌单由它唯一决定。
            "to": {"resourceIdOfScene": playlist_id},
            "callback": SYNTHETIC_FUNCTION,
            SYNTHETIC_ACTION_PAYLOAD_KEY: {
                SYNTHETIC_EVENT_KEY: None,
                ACTION_PAYLOAD_FROM_DOWNLOAD: DEFAULT_FROM_DOWNLOAD,
            },
        },
        "eventType": "click",
    }
    if parent_playlist_id:
        payload["parentData"] = {"from": {
            "scene": SCENE_PLAYLIST,
            "isTableViewerAction": True,
            "resourceIdOfScene": parent_playlist_id,
        }}
    return {"type": ACTION_TYPE_DO_ACTION, "payload": payload}


def _splice_action(song_id: str, playlist_id: str, *, resource: dict | None = None,
                   scene: str = SCENE_PLAYLIST) -> dict:
    """把一首歌「并入」目标歌单 —— 真机实测的写成动作。

    `resource` 是**歌曲对象**。实测原文给的是完整对象（40+ 字段），
    所以优先用真对象；拿不到时退化成 `{"id": ...}`（是否够用**尚未实测**）。
    `to.resourceIdOfScene` 是目标歌单 id —— 这是整个 payload 里唯一
    「填错就会写错地方」的字段，所以调用方拿不到歌单 id 时应当直接拒绝写入。
    """
    song = dict(resource) if isinstance(resource, dict) and resource else {"id": song_id}
    rid = norm_id(song.get("id"))
    if rid is not None and rid != song_id:
        # 两个 id 打架 = 我们不知道要加哪首歌。**不猜**：猜错的后果是把另一首歌
        # 写进用户的歌单，而回查只能看出「多了一首」，看不出「加错了」。
        raise WritePathError(
            f"歌曲 id 打架：song_id={song_id!r} 但 resource.id={rid!r} —— "
            "不知道要加哪首歌，拒绝构造 payload（猜错会往歌单里塞错歌）")
    song["id"] = song_id
    return {
        "type": ACTION_TYPE_DO_ACTION,
        "payload": {
            "actionId": ACTION_SPLICE,
            "data": {
                "resource": song,
                "resourceType": RESOURCE_TYPE_TRACK,
                "from": {
                    "scene": scene,
                    "isTableViewerAction": True,
                    "resourceIdOfScene": playlist_id,
                },
                "to": {
                    "scene": scene,
                    "resourceIdOfScene": playlist_id,
                    "resource": {"id": playlist_id},
                    "insertIndex": 0,
                },
                "callback": SYNTHETIC_FUNCTION,
                SYNTHETIC_ACTION_PAYLOAD_KEY: {},
            },
            "eventType": "drop",
        },
    }


def _do_action(action_id: str, data: dict) -> dict:
    """严格照抄 §7.3 bundle 原文的 doAction 分支（click）。

    原文：
        u({ type: "async:action/doAction",
            payload: { actionId: t, data: { ...r, actionPayload: {...} },
                       parentData: c, eventType: "click" } })
    —— 注意 eventType 在 payload **里面**，和 popMenu 分支不同。
    """
    return {
        "type": ACTION_TYPE_DO_ACTION,
        "payload": {
            "actionId": action_id,
            "data": {**data, SYNTHETIC_ACTION_PAYLOAD_KEY: {SYNTHETIC_EVENT_KEY: None}},
            "parentData": {},
            "eventType": "click",
        },
    }


def _pop_menu(action_id: str, data: dict, *, parent_data: dict | None = None) -> dict:
    """严格照抄 §7.3 bundle 原文的 popMenu 分支（rightMenu）。

    原文：
        u({ type: "async:action/popMenu",
            payload: { ...r, actionPayload: {...}, parentData: c },
            parentData: c, eventType: "rightMenu" })
    —— `...r` 是把 data **展开**到 payload 顶层（不是塞进 payload.data），
       且 parentData / eventType 在 payload 里和在 action 顶层**各有一份**。
    """
    pd = dict(parent_data or {})
    return {
        "type": ACTION_TYPE_POP_MENU,
        "payload": {
            **data,
            SYNTHETIC_ACTION_PAYLOAD_KEY: {SYNTHETIC_EVENT_KEY: None},
            "parentData": pd,
        },
        "parentData": pd,
        "eventType": "rightMenu",
        # 原文 actionId 不在 popMenu 的 payload 里（它是菜单项自己带的），
        # 但我们要一口气把 action 语义说清楚，故额外带上（标为推测，见 rationale）。
        "actionId": action_id,
    }


@dataclass(frozen=True)
class PayloadVariant:
    """一种候选 payload 形状。

    `build(song_id, playlist_id)` / `build(name)` 返回**直接交给 dispatch 的整个 action**
    （它的 `payload` 字段就是 DVA 的 payload）。
    """

    name: str
    confidence: float            # 0~1，纯主观：按「推测依据的强度」排的序，不是实测概率
    rationale: str               # 推测依据（必须写清哪部分是猜的）
    kind: str                    # "add" / "create"
    action_type: str
    build: Callable[..., dict]
    is_control: bool = False     # 对照候选：预期不会成功，用来区分「形状不对」和「机制要两步」
    #: 这一条是**真机实测抓下来的**（而不是推测）。
    #: 存在的意义：让「哪些还有证据、哪些纯属猜」在代码里就能一眼分开，
    #: 而不是靠读 rationale 里的措辞去猜。
    measured: bool = False

    def describe(self, args: tuple = ()) -> dict:
        payload: dict | None = None
        err: str | None = None
        try:
            payload = self.build(*args)
        except Exception as e:  # pragma: no cover - 正常不会走到
            err = f"{type(e).__name__}: {e}"
        return {
            "kind": self.kind,
            "name": self.name,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "action_type": self.action_type,
            "is_control": self.is_control,
            "measured": self.measured,
            "payload": payload,
            "build_error": err,
        }


# ── 加入歌单：第 0 条是**真机实测**，其余是历史遗留的推测（已被证伪） ──────
#
# ⚠️ 2026-10 更新：第 1 条起那一批「扁平字段名」的形状**全部被实测推翻** ——
#    真实动作是 `{actionId:'splice', data:{resource, resourceType, from, to}}`，
#    里面**没有任何** `playlistId` / `songId` / `pid` 这类扁平字段。
#    它们留着只是当「万一我们对那次记录的理解错了」的兜底网，
#    confidence 已整体下调，且不要让它们掩盖第 0 条的结论。

ADD_PAYLOAD_VARIANTS: tuple[PayloadVariant, ...] = (
    PayloadVariant(
        name="doAction·favorite{resource,to.resourceIdOfScene}【真机实测】",
        confidence=0.90,
        rationale=(
            "**真机实测抓下来的原文**（见 ACTION_FAVORITE 上方的完整注释与 DESIGN.md G.15）："
            "在『人在 26年4月、把《虹色の日々Duet ver.》加进 26年10月』这次操作里抓到的 action。"
            "关键性质：`data.from` 只有 `scene`、**不含任何歌单信息**，目标由 "
            "`to.resourceIdOfScene` 唯一决定 —— 所以它**不依赖客户端当前停在哪个页面**，"
            "这是它能被 headless 调用的原因（对比 splice：那条依赖当前视图，实测发了没效果）。"
            "仍未实测：`payload.parentData` 是否必要（默认整个不发）。"
        ),
        kind="add",
        action_type=ACTION_TYPE_DO_ACTION,
        measured=True,
        build=lambda sid, pid, resource=None: _favorite_action(sid, pid, resource=resource),
    ),
    PayloadVariant(
        name="doAction·splice{resource,from,to}【实测但用错场景】",
        # 排在纯猜测之上（它是**真的**抓下来的动作），但远低于 favorite
        # （它是「拖动」，场景不对）。confidence 只表示「值得一试的程度」。
        confidence=0.06,
        rationale=(
            "**这也是实测抓到的动作，但它不是「添加到歌单」** —— 它是在**当前列表里拖动**"
            "（`eventType:'drop'`，`from`/`to` 都带 `listData`/`sourceData` 那套视图状态）。"
            "实测教训：按这个形状真机发过一次，**服务端一首没加**（trackCount 仍为 0）——"
            "因为把歌并进「我正在看的列表」这件事需要一个已注册的 live scene，而 headless 没有。"
            "保留它只为可读性与复盘；正常路径不该走到这里。"
        ),
        kind="add",
        action_type=ACTION_TYPE_DO_ACTION,
        measured=True,
        build=lambda sid, pid, resource=None: _splice_action(sid, pid, resource=resource),
    ),
    PayloadVariant(
        name="doAction·data{type,id,playlistId}",
        confidence=0.05,
        rationale=(
            "外层形状照抄 §7.3 实测的 doAction 分支（{actionId,data,parentData,eventType} "
            "全在 payload 里）—— 这部分有实测依据。内层是推测：NE_Dawn 的资源标识惯例是 "
            "{type:'song', id}，目标歌单用最常见的 playlistId。"
            "**内层字段名与层级完全没有实测依据**，只是「最像这个组件库」的一条。"
        ),
        kind="add",
        action_type=ACTION_TYPE_DO_ACTION,
        build=lambda sid, pid, resource=None: _do_action(
            ACTION_ADD_TO_PLAYLIST, {"type": "song", "id": sid, "playlistId": pid}),
    ),
    PayloadVariant(
        name="doAction·data{songId,playlistId}",
        confidence=0.03,
        rationale=(
            "同样照抄 doAction 外层；内层换成最扁平的字段名 —— 依据是 store 里歌曲对象的 "
            "id 候选字段名本来就包含 songId（见 desktop_cdp._ID_KEYS，那是从实测数据里收的）。"
            "**「扁平参数」这一族整体是推测**，列出它是为了覆盖另一种常见写法。"
        ),
        kind="add",
        action_type=ACTION_TYPE_DO_ACTION,
        build=lambda sid, pid, resource=None: _do_action(
            ACTION_ADD_TO_PLAYLIST, {"songId": sid, "playlistId": pid}),
    ),
    PayloadVariant(
        name="doAction·data{song{id},playlist{id}}",
        confidence=0.03,
        rationale=(
            "NE_Dawn 的弹窗/确认回调习惯直接传资源对象而不是裸 id；「添加到歌单」的确认一步里 "
            "song 与 playlist 很可能都已经是对象。**纯推测**，无实测支持。"
        ),
        kind="add",
        action_type=ACTION_TYPE_DO_ACTION,
        build=lambda sid, pid, resource=None: _do_action(
            ACTION_ADD_TO_PLAYLIST, {"song": {"id": sid}, "playlist": {"id": pid}}),
    ),
    PayloadVariant(
        name="doAction·data{type,ids[],pid}",
        confidence=0.02,
        rationale=(
            "批量加入的形状：ids 数组 + pid。依据是歌单曲目在 store 里就是 "
            "trackIds:[{id}] 的数组（§7.2 实测），且网易云 web 端「添加到歌单」的惯用参数名是 pid。"
            "**pid 这个字段名是推测**；ids 是数组也是推测（可能是单个 id）。"
        ),
        kind="add",
        action_type=ACTION_TYPE_DO_ACTION,
        build=lambda sid, pid, resource=None: _do_action(
            ACTION_ADD_TO_PLAYLIST, {"type": "song", "ids": [sid], "pid": pid}),
    ),
    PayloadVariant(
        name="doAction·data{track{id},playlistId}",
        confidence=0.02,
        rationale=(
            "行 fiber 的 memoizedProps.track 是 §7.2 实测存在的歌曲对象，应用内部很可能直接把 "
            "track 交给 action。**把 track 塞进 payload.data 是推测**。"
        ),
        kind="add",
        action_type=ACTION_TYPE_DO_ACTION,
        build=lambda sid, pid, resource=None: _do_action(
            ACTION_ADD_TO_PLAYLIST, {"track": {"id": sid}, "playlistId": pid}),
    ),
    PayloadVariant(
        name="popMenu·payload展开+parentData带歌单",
        confidence=0.02,
        rationale=(
            "§7.3「实现路线 A（优先）」把 popMenu 列为优先路线。原文里 parentData 是**调用方组件的 "
            "props**（`parentData: c = {}`）—— 如果用户是在歌单页里右键，parentData 很可能就承载着"
            "「歌单」这个父容器。故这条把目标歌单同时放进 parentData 和 payload。"
            "**「parentData 承载歌单」纯属推测**。"
        ),
        kind="add",
        action_type=ACTION_TYPE_POP_MENU,
        build=lambda sid, pid, resource=None: _pop_menu(
            ACTION_ADD_TO_PLAYLIST,
            {"type": "song", "id": sid, "playlistId": pid},
            parent_data={"type": "playlist", "id": pid}),
    ),
    PayloadVariant(
        name="doAction·对照:不带目标歌单",
        confidence=0.01,
        rationale=(
            "**对照组，不是可用的写入形状**。依据：§7.3 路线 A 第 4 步写的是「点『添加到歌单』"
            "→ 歌单选择弹窗 → 选中目标歌单」，暗示 addToPlayList 只是**打开弹窗**。"
            "这条预期不会让歌进歌单；它的作用是区分两种失败：「所有形状都不对」"
            "（连这条也没反应）与「机制本身要两步」（这条有反应但没加入）。"
            "注意它**可能在客户端弹出一个歌单选择弹窗**（它就是「打开弹窗」那条）——"
            "只有前面所有候选都失败后才会轮到它，弹出来请直接关掉。"
        ),
        kind="add",
        action_type=ACTION_TYPE_DO_ACTION,
        build=lambda sid, pid, resource=None: _do_action(
            ACTION_ADD_TO_PLAYLIST, {"type": "song", "id": sid}),
        is_control=True,
    ),
)


# ── 创建歌单：第 0 条是**真机实测**，其余 5 条是没有任何依据的旧猜测 ──────
#
# ⚠️ 那 5 条全押在 doAction 家族上（`{actionId:'create', data:{...}}`），
#    而实测证明建歌单走的是**另一族**动作（扁平的
#    `async:hostResource/createPlaylist`）。家族选错了，字段怎么调都不会对。
#    保留它们只为「万一实测那条挑环境」时还有网可兜，confidence 已压到极低。

CREATE_PAYLOAD_VARIANTS: tuple[PayloadVariant, ...] = (
    PayloadVariant(
        name="hostResource/createPlaylist{name,isPrivate}【真机实测】",
        confidence=0.90,
        rationale=(
            "**真机实测抓下来的原文**（见 ACTION_TYPE_HOST_CREATE_PLAYLIST 上方的注释"
            "与 DESIGN.md G.15）：用户在客户端新建「26年10月」时记录器抓到的 action："
            "{type:'async:hostResource/createPlaylist', payload:{name, isPrivate:false}}。"
            "效果已核对：歌单列表 129 → 130，新歌单 id=18457224255，"
            "web API 读到 trackCount=0 / privacy=0。**不是推测。**"
            "仍未实测的一点：`isPrivate` 是否必须显式给（这里显式给 false，与实测一致）。"
        ),
        kind="create",
        action_type=ACTION_TYPE_HOST_CREATE_PLAYLIST,
        measured=True,
        build=lambda name: _host_create_playlist(name),
    ),
    PayloadVariant(
        name="doAction·actionId=create·data{playlistName}",
        confidence=0.04,
        rationale=(
            "唯一有一点点依据的部分：actionId `create` 确实出现在 §7.3 的 actionId 枚举里。"
            "**其余全是推测** —— §7.3「尚未解决的部分」第二条明确写着「歌单创建流程的具体 action」"
            "没解决，所以连「用 create 这个 actionId」本身都不可靠。"
        ),
        kind="create",
        action_type=ACTION_TYPE_DO_ACTION,
        build=lambda name: _do_action(ACTION_CREATE, {"playlistName": name}),
    ),
    PayloadVariant(
        name="doAction·actionId=create·data{name}",
        confidence=0.03,
        rationale="同上外层（照抄 doAction）；字段名换成最常见的 name。**纯推测**。",
        kind="create",
        action_type=ACTION_TYPE_DO_ACTION,
        build=lambda name: _do_action(ACTION_CREATE, {"name": name}),
    ),
    PayloadVariant(
        name="doAction·actionId=createPlaylist·data{name,privacy}",
        confidence=0.02,
        rationale=(
            "枚举只是「已知 actionId」不是全集，组件也可能自己拼一个 createPlaylist；"
            "顺带带上隐私位（网易云建歌单的接口确实有 privacy 参数）。"
            "**actionId 与字段名都是推测**，可信度最低的一族。"
        ),
        kind="create",
        action_type=ACTION_TYPE_DO_ACTION,
        build=lambda name: _do_action(
            ACTION_CREATE_PLAYLIST, {"name": name, "privacy": 0}),
    ),
    PayloadVariant(
        name="popMenu·actionId=create·payload展开",
        confidence=0.02,
        rationale="把上面第 1 条的语义换成 popMenu 外层形状（§7.3 路线 A 优先 popMenu）。**推测叠推测**。",
        kind="create",
        action_type=ACTION_TYPE_POP_MENU,
        build=lambda name: _pop_menu(ACTION_CREATE, {"playlistName": name}),
    ),
    PayloadVariant(
        name="doAction·actionId=create·data{title}",
        confidence=0.01,
        rationale="同 create 外层；字段名换成 title（NE_Dawn 组件里 title 也常见）。**纯推测**。",
        kind="create",
        action_type=ACTION_TYPE_DO_ACTION,
        build=lambda name: _do_action(ACTION_CREATE, {"title": name}),
    ),
)

PLACEHOLDER_SONG_ID = "<SONG_ID>"
PLACEHOLDER_PLAYLIST_ID = "<PLAYLIST_ID>"
PLACEHOLDER_NAME = "<歌单名>"


def _variant_at(variants: Sequence[PayloadVariant], variant: int, what: str) -> PayloadVariant:
    if isinstance(variant, bool) or not isinstance(variant, int):
        raise WritePathError(f"{what} 的 variant 必须是 int，收到 {type(variant).__name__}")
    if not 0 <= variant < len(variants):
        raise WritePathError(
            f"{what} 的 variant 超出范围：{variant}（有 {len(variants)} 条候选，0~{len(variants) - 1}）")
    return variants[variant]


def build_add_payload(song_id: str | int, playlist_id: str | int, *, variant: int = 0,
                      resource: dict | None = None) -> dict:
    """第 `variant` 种「加入歌单」候选 action（直接交给 dispatch 的那个对象）。

    ⚠️ 没有一种候选是实测确认的（见模块 docstring 与每条 variant 的 rationale）。
    调用方必须拿 dispatch 之后**回查歌单**的结果来判定成败，不能信这个 payload 本身。

    守卫：`song_id` 空、或（非对照候选时）`playlist_id` 空 → `WritePathError`。
    **目标歌单未知时绝不猜** —— 猜错就是把歌推进别人的歌单。
    """
    v = _variant_at(ADD_PAYLOAD_VARIANTS, variant, "加入歌单")
    sid = norm_id(song_id)
    if sid is None:
        raise WritePathError("song_id 为空 —— 不知道要加哪首歌，拒绝构造 payload")
    pid = norm_id(playlist_id)
    if pid is None and not v.is_control:
        raise WritePathError(
            f"playlist_id 为空 —— 不知道加到哪个歌单，拒绝构造 payload（候选 {v.name!r}）")
    return v.build(sid, pid or "", dict(resource) if isinstance(resource, dict) else None)


def build_create_playlist_payload(name: str, *, variant: int = 0) -> dict:
    """第 `variant` 种「创建歌单」候选 action。

    ⚠️ §7.3 明确写着「歌单创建流程的具体 action」尚未解决 —— 这 5 条**全是推测**。
    调用方必须在 dispatch 之后重新拉歌单列表确认它真的出现了。
    """
    v = _variant_at(CREATE_PAYLOAD_VARIANTS, variant, "创建歌单")
    nm = ("" if name is None else str(name)).strip()
    if not nm:
        raise WritePathError("歌单名称为空 —— 拒绝构造创建 payload")
    return v.build(nm)


def describe_payload_variants(
    *,
    song_id: str | int | None = None,
    playlist_id: str | int | None = None,
    name: str | None = None,
    kind: str | None = None,
) -> list[dict]:
    """全部候选的「族谱」+ 具体 payload —— 探针（人工核对）用。

    `song_id` / `playlist_id` / `name` 给 None 时用占位符，方便直接打印给人看。
    """
    out: list[dict] = []
    if kind in (None, "add"):
        sid = norm_id(song_id) or PLACEHOLDER_SONG_ID
        pid = norm_id(playlist_id) or PLACEHOLDER_PLAYLIST_ID
        for i, v in enumerate(ADD_PAYLOAD_VARIANTS):
            row = v.describe((sid, pid, None))
            row["variant"] = i
            out.append(row)
    if kind in (None, "create"):
        nm = ("" if name is None else str(name)).strip() or PLACEHOLDER_NAME
        for i, v in enumerate(CREATE_PAYLOAD_VARIANTS):
            row = v.describe((nm,))
            row["variant"] = i
            out.append(row)
    return out


def render_payload_report(rows: Sequence[dict]) -> str:
    """把 describe_payload_variants() 的结果渲染成给人看的文本。"""
    lines: list[str] = []
    for kind, title in (("add", "加入歌单"), ("create", "创建歌单")):
        sub = [r for r in rows if r.get("kind") == kind]
        if not sub:
            continue
        lines.append(f"{'=' * 78}")
        lines.append(f"{title} —— {len(sub)} 条候选（confidence 只是「推测依据强弱」的排序，不是实测概率）")
        lines.append("=" * 78)
        for r in sub:
            ctl = "  ⚠ 对照候选（预期不会成功，只用于区分失败原因）" if r.get("is_control") else ""
            lines.append("")
            lines.append(f"[{kind}#{r['variant']}] {r['name']}   confidence={r['confidence']}{ctl}")
            lines.append(f"    action.type : {r['action_type']}")
            lines.append(f"    推测依据    : {r['rationale']}")
            payload = json.dumps(r.get("payload"), ensure_ascii=False, indent=2)
            lines.append("    payload     : " + payload.replace("\n", "\n" + " " * 18))
            if r.get("build_error"):
                lines.append(f"    ✗ 构造失败  : {r['build_error']}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# 二、id 归一化与「歌单里有没有这首歌」（纯函数）
# ═══════════════════════════════════════════════════════════════════════════

#: 歌曲对象里可能承载歌曲 id 的字段名（顺序 = 优先级）。
#: 前四个是任务点名的 id/songId/trackId；其余来自 desktop_cdp._ID_KEYS 的实测收集，
#: 加上 playlist_songs() 归一化后的 netease_song_id。
SONG_ID_KEYS: tuple[str, ...] = (
    "id", "songId", "trackId", "netease_song_id",
    "song_id", "track_id", "neteaseId", "netease_id",
    "s_songId", "resourceId", "resource_id", "sid", "track_id_str",
)
#: 归一化歌曲字典/接口对象里，id 可能嵌在这些键下面
NESTED_SONG_KEYS: tuple[str, ...] = ("song", "track", "data", "resource", "raw", "songs")

_NUMERIC_DOT_ZERO = re.compile(r"^(\d+)\.0+$")


def norm_id(v: Any) -> str | None:
    """把任何形状的 id 归一成字符串；归一不出来返回 None（**不猜、不造**）。

    · `bool` 明确排除（`True` 是 `int` 的子类，会变成 "1" 误命中）
    · `501` / `"501"` / `" 501 "` / `501.0` / `"501.0"` 全部 → `"501"`
    · 空串 / None / 容器 → None
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return str(int(v)) if float(v).is_integer() else repr(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        m = _NUMERIC_DOT_ZERO.match(s)
        return m.group(1) if m else s
    return None


def iter_song_ids(song: Any) -> Iterator[str]:
    """产出一个「歌曲对象」里所有可能的 id（顺序即优先级）。"""
    if isinstance(song, dict):
        for k in SONG_ID_KEYS:
            if k in song:
                got = norm_id(song.get(k))
                if got:
                    yield got
        for k in NESTED_SONG_KEYS:
            sub = song.get(k)
            if isinstance(sub, dict):
                for kk in SONG_ID_KEYS:
                    got = norm_id(sub.get(kk))
                    if got:
                        yield got
    elif isinstance(song, (list, tuple)):
        for x in song:
            yield from iter_song_ids(x)
    else:
        got = norm_id(song)
        if got:
            yield got


def contains_song(songs: Sequence[Any] | None, song_id: str | int) -> bool:
    """`songs` 里有没有 `song_id` 这首歌。

    兼容：id 是 str/int/float；字段名是 id/songId/trackId/netease_song_id/...；
    元素本身是裸 id（`["501"]`）、嵌套对象（`{"song": {"id": 501}}`）
    或 `playlist_songs()` 归一化后的 `{"netease_song_id": "501", ...}`。

    只回答「在不在」，**不回答「能不能确定不在」** —— 后者用 `check_membership()`。
    """
    sid = norm_id(song_id)
    if sid is None or not isinstance(songs, (list, tuple)):
        return False
    for s in songs:
        for cand in iter_song_ids(s):
            if cand == sid:
                return True
    return False


@dataclass(frozen=True)
class Membership:
    """「这首歌在不在这个歌单里」的判定结果。

    `present` 是三态：
        True  —— 确认在里面
        False —— 能**证明**不在（看全了该歌单的曲目）
        None  —— 证据不足（曲目数据不完整 / 读不到）。调用方必须当失败处理。
    """

    present: bool | None
    reason: str
    complete: bool = False
    resolved: int = 0
    declared: int | None = None

    @property
    def unknown(self) -> bool:
        return self.present is None


def check_membership(
    songs: Sequence[Any] | None,
    song_id: str | int,
    *,
    declared_count: int | None = None,
    entries: int | None = None,
    source: str = "store",
) -> Membership:
    """判定歌曲是否在歌单里，并明确「能不能证明不在」。

    `declared_count`：歌单对象自报的 trackCount（store 里读到的）。
    `entries`：歌单对象里的曲目条目数（trackIds/tracks 数组长度），默认 = `len(songs)`。
    `source`：这批曲目数据**从哪来的**（`"store"` / `"web API"` …）。
        只影响说明文字，不影响判定。为什么要它：同一套判定逻辑现在有两个
        数据来源（客户端 store 恒缺曲目 → 补了 web API），而**给用户的
        下一步建议完全不同** —— store 不全时该说「去客户端点开那个歌单」，
        web API 不全时那句话是错的建议。

    为什么要有 None 这一态：§7.3/§7.2 都记着同一个坑 —— store 里的歌单可能只有
    `trackIds`，而它指向的歌曲对象**还没被客户端拉下来**（用户没打开过该歌单）。
    这时「解析出 0 首」不代表「歌单是空的」。把它当 False 用，就会重复加歌；
    把它当 True 用，就会假装成功。
    """
    src = str(source or "store")
    resolved = len(songs) if isinstance(songs, (list, tuple)) else 0
    ent = resolved if entries is None else int(entries)
    sid = norm_id(song_id)
    declared = declared_count if (isinstance(declared_count, int)
                                 and not isinstance(declared_count, bool)) else None

    if sid is None:
        return Membership(None, "song_id 为空 —— 无法判断歌单里有没有这首歌", False,
                          resolved, declared)
    if contains_song(songs, sid):
        return Membership(True, f"歌单里已经有 {sid}", True, resolved, declared)
    if declared == 0:
        return Membership(False, f"{src} 里歌单自报 trackCount=0（空歌单）→ 可以确定不在里面",
                          True, resolved, declared)
    if declared is not None:
        if resolved >= declared:
            return Membership(
                False,
                f"{src} 里歌单自报 {declared} 首，读到 {resolved} 首，里面没有 {sid}",
                True, resolved, declared)
        hint = ("（先在客户端打开这个歌单，让它把曲目拉下来）"
                if src == "store" else "")
        return Membership(
            None,
            f"{src} 里歌单自报 {declared} 首，但只读到 {resolved} 首 —— "
            f"曲目数据不完整，**不能据此断定 {sid} 不在歌单里**{hint}",
            False, resolved, declared)
    if ent > 0 and resolved >= ent:
        return Membership(
            False,
            f"{src} 里该歌单有 {ent} 条曲目条目、全部还原成歌曲对象，里面没有 {sid}",
            True, resolved, declared)
    if ent > 0:
        return Membership(
            None,
            f"{src} 里该歌单有 {ent} 条曲目条目，但只还原出 {resolved} 首"
            f"（trackIds 指向的歌曲对象不在 store 里）—— 证据不足",
            False, resolved, declared)
    return Membership(
        None,
        f"{src} 里该歌单既没有曲目条目也没有 trackCount —— "
        "分不清「空歌单」和「还没加载」，证据不足",
        False, resolved, declared)


# ═══════════════════════════════════════════════════════════════════════════
# 三、dispatch 返回值的判读
# ═══════════════════════════════════════════════════════════════════════════

_OK_STRINGS = frozenset({"ok", "success", "succeeded", "done", "true", "y", "yes", "成功"})
_FAIL_STRINGS = frozenset({"error", "fail", "failed", "false", "n", "no", "denied",
                           "失败", "错误"})
_ERROR_HINTS = re.compile(
    r"fail|error|错误|失败|异常|denied|deny|refus|invalid|unauthor|forbidden"
    r"|timeout|超时|无权限|not\s*allow|不能|无法",
    re.I,
)
#: 网易云 API 的成功码（200 是常规成功码，0/20000 在别的接口里也出现过）
_CODE_OK = frozenset({0, 200, 20000})
_STATE_MIN_KEYS = 5
_MARKER_KEYS = frozenset({
    "threw", "called", "returned", "error", "err", "errmsg", "errorMessage",
    "code", "status", "statusCode", "msg", "message", "ok", "success", "succeeded",
    "result", "data",
})
_MAX_DEPTH = 6


@dataclass(frozen=True)
class ActionResult:
    """对 dispatch 返回值的判读结果。

    ⚠️ `ok=True` 的含义**只是「dispatch 自己报告成功」**，不是「歌已经进歌单了」。
    唯一能证明写入成功的是**回查**（重新读歌单曲目）。调用方不许拿 ok=True 当成功。
    """

    ok: bool | None
    reason: str
    evidence: str = "raw"
    kind: str = "unknown"

    @property
    def decisive(self) -> bool:
        return self.ok is not None

    def summary(self) -> str:
        label = {True: "dispatch 自报成功", False: "dispatch 报失败",
                 None: "不可判定"}[self.ok]
        return f"[{label}] kind={self.kind} via {self.evidence} —— {self.reason}"


def parse_action_result(raw: Any) -> ActionResult:
    """从 dispatch 的返回值里判读成败，容忍多种返回形状。

    认识的形状（实测依据：附录 B「调用 dispatch → 可用，返回新 state」，
    所以**返回一个像 state 的大对象是预期内的**，那种情况只能判「不可判定」）：

        None                     → None（saga 异步 action 常见）
        True / False             → 原样
        200 / 0 / 400            → 按网易云 code 语义
        "ok" / "error" / "200"   → 按字符串语义
        '{"code":200}'           → 解析成 JSON 再判
        {"code":200} / {"code":400}
        {"ok":true} / {"success":false}
        {"error":"..."} / {"msg":"失败: ..."}
        {"threw":true,"error":"..."}          ← 我们注入的 JS 的 envelope
        {"called":false,"error":"no-dispatch"}
        {"called":true,"returned":{...}}      ← 递归判 returned
        {"user":{...},"playlist":{...},...}   ← 像 state → None

    **绝不**在缺少明确成功标记的情况下返回 True。
    """
    if isinstance(raw, ActionResult):
        return raw
    return _parse_action_result(raw, "raw", 0)


def _parse_action_result(raw: Any, ev: str, depth: int) -> ActionResult:
    if depth > _MAX_DEPTH:
        return ActionResult(None, "返回值嵌套太深，放弃解析", ev, "deep")

    if raw is None:
        return ActionResult(
            None,
            "dispatch 没有返回值（redux-saga 的异步 action 常返回 undefined）"
            "—— 成败只能靠回查确认",
            ev, "null")

    if isinstance(raw, bool):
        return ActionResult(raw, f"dispatch 直接返回了 {raw}", ev, "bool")

    if isinstance(raw, float) and not float(raw).is_integer():
        return ActionResult(None, f"dispatch 返回了一个非整数数字 {raw!r} —— 看不出成败",
                            ev, "number")

    if isinstance(raw, (int, float)):
        code = int(raw)
        if code in _CODE_OK:
            return ActionResult(True, f"dispatch 返回 code={code}（成功码）", ev, "code")
        return ActionResult(False, f"dispatch 返回 code={code}（非成功码）", ev, "code")

    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return ActionResult(None, "dispatch 返回了空字符串 —— 看不出成败", ev, "str")
        if s[:1] in "{[":
            try:
                return _parse_action_result(json.loads(s), f"{ev}(json)", depth + 1)
            except ValueError:
                pass
        if s.isdigit():
            return _parse_action_result(int(s), ev, depth)
        low = s.lower()
        if low in _OK_STRINGS:
            return ActionResult(True, f"dispatch 返回字符串 {s!r}（成功字样）", ev, "str")
        if low in _FAIL_STRINGS:
            return ActionResult(False, f"dispatch 返回字符串 {s!r}（失败字样）", ev, "str")
        if _ERROR_HINTS.search(s):
            return ActionResult(False, f"dispatch 返回的字符串像错误信息：{s[:120]!r}",
                                ev, "str")
        return ActionResult(None, f"dispatch 返回了字符串 {s[:120]!r}，看不出成败", ev, "str")

    if isinstance(raw, dict):
        # 1) 我们自己注入的 JS 的 envelope（threw / called / returned）
        if raw.get("threw") is True:
            return ActionResult(
                False,
                f"JS 侧执行 dispatch 时抛错：{raw.get('error') or '(无消息)'}",
                f"{ev}.threw", "envelope")
        if "called" in raw and raw.get("called") is not True:
            return ActionResult(
                False,
                f"没能拿到/调用 dispatch：{raw.get('error') or '(无消息)'}",
                f"{ev}.called", "envelope")
        # 2) 显式错误字段
        for k in ("error", "err", "errmsg", "errorMessage"):
            v = raw.get(k)
            if v is not None and v != "" and v is not False and v != 0:
                return ActionResult(False, f"dispatch 返回了错误字段 {k}={v!r}",
                                    f"{ev}.{k}", "dict")
        # 3) 显式成败布尔
        for k in ("ok", "success", "succeeded"):
            if isinstance(raw.get(k), bool):
                return ActionResult(raw[k], f"dispatch 返回 {k}={raw[k]}",
                                    f"{ev}.{k}", "dict")
        # 4) 数字码（注意 bool 已被上面拦掉）
        code = raw.get("code", raw.get("statusCode", raw.get("status")))
        if isinstance(code, bool):
            code = None
        if isinstance(code, (int, float)):
            return _parse_action_result(int(code), f"{ev}.code", depth)
        if isinstance(code, str) and code.strip().isdigit():
            return _parse_action_result(int(code.strip()), f"{ev}.code", depth)
        # 5) 包了一层：递归
        for k in ("returned", "result"):
            if k in raw:
                return _parse_action_result(raw[k], f"{ev}.{k}", depth + 1)
        # 6) 消息字段
        for k in ("msg", "message"):
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                if _ERROR_HINTS.search(v):
                    return ActionResult(False, f"dispatch 返回的消息像失败：{v[:120]!r}",
                                        f"{ev}.{k}", "dict")
                if v.strip().lower() in _OK_STRINGS:
                    return ActionResult(True, f"dispatch 返回的消息像成功：{v[:120]!r}",
                                        f"{ev}.{k}", "dict")
                return ActionResult(None, f"dispatch 返回消息 {k}={v[:120]!r}，看不出成败",
                                    f"{ev}.{k}", "dict")
        # 7) 像 redux state（附录 B 记录的「返回新 state」）
        if len(raw) >= _STATE_MIN_KEYS and not any(k in raw for k in _MARKER_KEYS):
            return ActionResult(
                None,
                f"dispatch 返回了一个像 redux state 的大对象（{len(raw)} 个顶层键）"
                f"—— 从它看不出这次 action 成不成功",
                ev, "state")
        keys = sorted(str(k) for k in raw)[:8]
        return ActionResult(None, f"dispatch 返回了无法判读的 dict（键：{keys}）",
                            ev, "dict")

    if isinstance(raw, (list, tuple)):
        return ActionResult(None, f"dispatch 返回了 {type(raw).__name__}（{len(raw)} 项）"
                                  f"，看不出成败", ev, "list")
    return ActionResult(None, f"dispatch 返回了 {type(raw).__name__}，看不出成败",
                        ev, type(raw).__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 四、限速（§7.4）与写入策略
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class RateLimiter:
    """令牌间隔式限速：保证两次「会改状态的 dispatch」之间至少隔 1/rps 秒。

    `clock` / `sleep` 可注入 —— 于是能在**不真的睡**的情况下离线断言限速行为
    （见 tests/test_write_path.py::TestRateLimiter）。

    `rps <= 0` 表示不限速（**只该用在测试里**；真机默认 constants.WRITE_RATE_RPS = 1）。
    """

    rps: float = constants.WRITE_RATE_RPS
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _next_at: float = field(default=0.0, init=False, repr=False)

    @property
    def interval(self) -> float:
        r = float(self.rps)
        return 1.0 / r if r > 0 else 0.0

    def wait(self) -> float:
        """必要时睡一觉；返回实际睡了多久（秒）。"""
        iv = self.interval
        now = self.clock()
        slept = 0.0
        if iv > 0 and now < self._next_at:
            slept = self._next_at - now
            self.sleep(slept)
            now = self.clock()
        self._next_at = max(now, self._next_at) + iv
        return slept

    def reset(self) -> None:
        self._next_at = 0.0


@dataclass
class WritePolicy:
    """写入策略（可注入，便于测试与 GUI 调参）。"""

    #: 写入限速（§7.4，constants.WRITE_RATE_RPS = 1 req/s）
    rate_rps: float = constants.WRITE_RATE_RPS
    #: 最多试几条「加入歌单」候选。
    #:
    #: **默认 1**（2026-10 真机实测后改的）。原来是 None（= 全试一遍），
    #: 那时的前提是「7 条候选里总有一条对」；实测把这个前提推翻了 ——
    #: 真实的 action 是 `{actionId:'splice', ...}`，剩下 7 条**形状全错**，
    #: 它们的 payload 里连 `playlistId` / `songId` 这些字段都不存在。
    #: 再试它们不是「多一层保险」，而是**明知无效还往账号上打 7 次**：
    #: 真机实测那次失败推送就白发了 8 个 action（服务端确认一首没加）。
    #:
    #: 想重新盲试（例如换了客户端版本、怀疑实测那条过时了）就显式设回 None。
    max_add_variants: int | None = 1
    #: 最多试几条「创建歌单」候选。默认 **1**，理由同上：实测证明建歌单走
    #: `async:hostResource/createPlaylist`，另外 5 条押在 doAction 家族上是错的。
    #: 创建比加入更该保守 —— 盲试可能给你攒一堆垃圾歌单。
    max_create_variants: int = 1
    #: 回查确认的等待上限/轮询间隔（秒）。写入后客户端 store 不会立刻更新。
    confirm_timeout: float = 3.0
    confirm_poll: float = 0.5
    #: 曲目数据不完整（check_membership 返回 None）时是否拒绝写入。默认拒绝：
    #: 判不出来就当失败，宁可让用户先去客户端打开那个歌单。
    require_complete_membership: bool = True
    #: 窗口被遮挡/隐藏时是否允许写入。默认不允许（DESIGN 原则 3：hidden 会让
    #: 渲染停止、portal 被清空，DOM 操作静默失败）。
    allow_hidden_window: bool = False

    def add_variant_order(self, *, hint: int | None = None) -> list[int]:
        """要尝试的候选序号，按「先信推测依据强的，再信上次成功的」排。

        `hint`：上一次成功的候选（会话内缓存）—— 命中过就把它排到最前面，
        避免每首歌都从头盲试一遍（每首盲试 6 次 × 1 req/s = 6 秒，40 首就是 4 分钟）。
        """
        all_idx = [i for i, v in enumerate(ADD_PAYLOAD_VARIANTS) if not v.is_control]
        ctl_idx = [i for i, v in enumerate(ADD_PAYLOAD_VARIANTS) if v.is_control]
        # 只有「非对照候选」成功过才算数：对照组本来就是预期不会成功的形状
        order = [hint] if (hint is not None and hint in all_idx) else []
        order += [i for i in all_idx if i != hint]
        order += ctl_idx                       # 对照组放最后（预期不会成功）
        cap = self.max_add_variants
        return order if cap is None else order[:max(0, int(cap))]

    def create_variant_order(self) -> list[int]:
        n = len(CREATE_PAYLOAD_VARIANTS)
        cap = max(0, int(self.max_create_variants))
        return list(range(min(n, cap)))


# ═══════════════════════════════════════════════════════════════════════════
# 五、把 action 安全地嵌进 JS
# ═══════════════════════════════════════════════════════════════════════════

#: ES2019 之前 U+2028/U+2029 在 JS 字符串字面量里非法（歌单名里可能有）
_JS_LINE_TERMINATORS = (("\u2028", "\\u2028"), ("\u2029", "\\u2029"))


def action_to_js_literal(action: dict) -> str:
    """把 action 序列化成可以**直接嵌进 JS 源码**的字面量。

    用 json.dumps 保证引号/反斜杠/换行/emoji 都被正确转义
    （歌单名是用户输入，必须当作不可信数据处理）。
    """
    s = json.dumps(action, ensure_ascii=False, separators=(",", ":"))
    for ch, esc in _JS_LINE_TERMINATORS:
        s = s.replace(ch, esc)
    return s


# ═══════════════════════════════════════════════════════════════════════════
# 六、命令行探针
# ═══════════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    """写入链路探针。

        --probe   打印全部候选 payload（离线，不需要客户端）
        --live    连客户端，dump "写入探针"（fiber 上的 dispatch / menu+data 组件形状）
        --js      只打印要注入的 JS 表达式

    推荐这样调（`python -m animusic.collector.write_path` 会多一条无害警告）::

        python -c "from animusic.collector.write_path import main; main()"
        python -c "from animusic.collector.write_path import main; main(['--probe','--live'])"
    """
    ap = argparse.ArgumentParser(
        prog="python -m animusic.collector.write_path",
        description="写入歌单：候选 payload 探针（desktop_cdp 的写入实现依赖本模块）")
    ap.add_argument("--probe", action="store_true", help="打印全部候选 payload（离线）")
    ap.add_argument("--live", action="store_true",
                    help="连接客户端 dump 写入探针（需要客户端带 --remote-debugging-port）")
    ap.add_argument("--js", action="store_true", help="只打印要注入的 JS")
    ap.add_argument("--port", type=int, default=constants.NETEASE_DEBUG_PORT,
                    help=f"客户端 CDP 端口（默认 {constants.NETEASE_DEBUG_PORT}）")
    ap.add_argument("--song-id", default="", help="探针里用的真实歌曲 id（可选）")
    ap.add_argument("--playlist-id", default="", help="探针里用的真实歌单 id（可选）")
    ap.add_argument("--name", default="", help="探针里用的歌单名（可选）")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - 老环境
            pass

    from . import desktop_cdp as dc          # 局部 import：避免模块级循环依赖

    if args.js:
        print("===== JS_WRITE_PROBE =====")
        print(dc.JS_WRITE_PROBE)
        print()
        print("===== js_dispatch(示例 action) =====")
        print(dc.js_dispatch(build_add_payload(args.song_id or "0",
                                              args.playlist_id or "0", variant=0)))
        return 0

    print(render_payload_report(describe_payload_variants(
        song_id=args.song_id or None, playlist_id=args.playlist_id or None,
        name=args.name or None)))
    print()
    print("提醒：以上没有一条 payload 是实测确认的 —— 先用 --live 看客户端真实的")
    print("      `props.data` 形状，再决定 pin 哪一条（DesktopCDPCollector(add_variant=...)）。")

    if not args.live:
        return 0

    print()
    print("=" * 78)
    print("客户端写入探针（需要网易云客户端带 --remote-debugging-port 在跑）")
    print("=" * 78)
    collector = dc.DesktopCDPCollector(args.port, timeout=20.0)
    report = collector.probe_write_path(song_id=args.song_id,
                                        playlist_id=args.playlist_id)
    if report.get("live_error"):
        print(f"✗ 连不上 / 探测失败：{report['live_error']}")
        print("  （端口没监听就是客户端没带调试参数启动 —— 这不是脚本出错）")
        return 0
    print(json.dumps(report.get("live"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - 手动跑的探针
    sys.exit(main())
