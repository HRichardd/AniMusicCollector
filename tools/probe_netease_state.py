#!/usr/bin/env python
"""探测网易云客户端的 redux store —— 把「未验证的猜测」变成实测事实。

前置：客户端带调试参数运行（客户端快捷方式目标后面加）：
    --remote-debugging-port=9222

它做的事：
    1. 检查 9222 是否在听；不在听就**优雅地**说一声，不抛栈
    2. 打印 CDP 版本与所有页面（确认 orpheus:// 页面在）
    3. 注入 JS 找 DVA store，打印**是怎么找到的**
       （祖先链 / 全树扫 / 全局量，以及 fiber 键名前缀、链上深度）
       —— 这一条最关键：`client.py` 里没有的取 store 逻辑全在这
    4. dump store 的**顶层结构**（键名、类型、长度、再下两层）
    5. 依次试 `CANDIDATE_PLAYLIST_PATHS`，报告歌单实际命中了哪条路径；
       都没中就说明靠全树扫描兜住的（扫到的路径也会打出来）
    6. 找登录用户、试解一个歌单的曲目（看看 trackIds/songs 索引能不能还原）

用法：
    python tools/probe_netease_state.py
    python tools/probe_netease_state.py --playlists 30
    python tools/probe_netease_state.py --save data/probe/netease_state.json
    python tools/probe_netease_state.py --js          # 只打印要注入的 JS

注意：`--save` 会把整份 state（可能几十 MB，含歌单/账号信息）写到磁盘 ——
默认不写，位置由你定，别提交进版本库。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from animusic import constants                                    # noqa: E402
from animusic.cdp import CDPBrowser, CDPError, port_open          # noqa: E402
from animusic.collector import desktop_cdp as dc                  # noqa: E402

# PowerShell 默认是 GBK，中文/emoji 直接 print 会炸；统一成 UTF-8 + 容错
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - 老环境没有 reconfigure
        pass


def head(title: str) -> None:
    print()
    print("─" * 72)
    print(title)
    print("─" * 72)


def port_closed_hint(port: int) -> None:
    print()
    print(f"⚠  端口 {port} 未监听 —— 客户端没在带调试参数运行，或者根本没开。")
    print()
    print("   这不是脚本出错。要让它能探测，请这样启动网易云音乐：")
    print(f"       1) 右键客户端快捷方式 → 属性 → 目标，末尾加： --remote-debugging-port={port}")
    print("          （形如：\"D:\\CloudMusic\\cloudmusic.exe\" --remote-debugging-port=9222）")
    print("       2) 完全退出客户端后重新打开它")
    print(f"       3) 确认 http://127.0.0.1:{port}/json/version 能返回 JSON")
    print()
    print("   在此之前，desktop_cdp.py 的 store 取法与歌单路径都只能算「未验证」。")
    print("   本脚本跑通后，请把打印出来的 diag / 命中路径回填进 desktop_cdp.py 的注释。")


def print_tree(node: Any, indent: int = 0, max_lines: int = 200) -> int:
    """打印 describe_state() 的摘要树，返回打印行数（用于截断）。"""
    lines = 0

    def emit(s: str) -> None:
        nonlocal lines
        if lines < max_lines:
            print("  " * indent + s)
        lines += 1

    t = node.get("type")
    if t == "dict":
        for k, sub in (node.get("keys") or {}).items():
            st = sub.get("type")
            if st == "dict":
                emit(f"{k}: dict({sub.get('len')})")
                print_tree(sub, indent + 1, max_lines - lines)
            elif st == "list":
                emit(f"{k}: list({sub.get('len')})"
                     + (f"  首元素={sub.get('item0', {}).get('type')}" if "item0" in sub else ""))
                if "item0" in sub and sub["item0"].get("type") == "dict":
                    print_tree(sub["item0"], indent + 1, max_lines - lines)
            elif st == "str":
                emit(f"{k}: str(len={sub.get('len')}) {sub.get('preview', '')!r}")
            elif st == "null":
                emit(f"{k}: null")
            else:
                emit(f"{k}: {st} = {sub.get('value')!r}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="探测网易云客户端 redux store 结构")
    ap.add_argument("--port", type=int, default=constants.NETEASE_DEBUG_PORT,
                    help=f"客户端 CDP 端口（默认 {constants.NETEASE_DEBUG_PORT}）")
    ap.add_argument("--depth", type=int, default=3, help="结构摘要深度（默认 3）")
    ap.add_argument("--max-items", type=int, default=25, help="每层最多列几个键（默认 25）")
    ap.add_argument("--playlists", type=int, default=12, help="最多打印几个歌单名（默认 12）")
    ap.add_argument("--save", default=None, metavar="PATH",
                    help="把整份 state 存成 JSON（默认不存）")
    ap.add_argument("--js", action="store_true", help="只打印要注入的 JS 表达式")
    args = ap.parse_args(argv)

    if args.js:
        for name in ("JS_HEALTH", "JS_STATE", "JS_DOM_SONGS"):
            print(f"\n===== {name} =====")
            print(getattr(dc, name))
        return 0

    port = args.port
    if not port_open(port, timeout=0.5):
        port_closed_hint(port)
        return 0

    browser = CDPBrowser(port, timeout=20.0)
    head(f"① CDP 端点 :{port}")
    try:
        info = browser.version()
        print(f"Browser   : {info.browser}")
        print(f"Protocol  : {info.protocol}")
    except CDPError as e:
        print(f"✗ 读不到 /json/version：{e}")
        return 0

    tabs = browser.list_tabs()
    print(f"页面数    : {len(tabs)}")
    for t in tabs:
        print(f"  · [{t.id[:12]}] {t.url}   ({t.title})")

    tab = browser.find_tab(dc.ORPHEUS_URL_RE)
    if tab is None:
        print()
        print("✗ 没有 orpheus:// 页面 —— 客户端前端可能还没加载完，或这不是网易云客户端。")
        return 0
    print(f"目标页面  : {tab.url}")

    collector = dc.DesktopCDPCollector(port, timeout=20.0)

    # ---- store 是怎么找到的（最关键的未知项）
    head("② 健康检查 / store 定位")
    st = collector.health()
    print(f"ok        : {st.ok}")
    print(f"detail    : {st.detail}")
    diag = st.extra.get("store_diag")
    print(f"store diag: {json.dumps(diag, ensure_ascii=False) if diag else '(未知)'}")
    if diag:
        via = diag.get("via")
        explain = {
            "ancestor": "从 DOM 节点的 fiber 沿 return 链往上找到的（说明 Provider 在祖先链上）",
            "tree": "沿祖先链没找到，靠根 fiber 全树扫描找到的",
            "global": f"靠全局量找到的：{diag.get('name')}",
            "cache": f"命中了 window 上缓存的 store —— 第一次是用 "
                     f"{diag.get('firstVia')} 找到的",
            "none": "没找到 —— fiber 结构或全局量都不是预期那样",
        }.get(via, "")
        print(f"解释      : {explain}")
        if diag.get("fiberKey"):
            print(f"fiber 键名: {diag.get('fiberKey')}（前缀形状符合 React 16 的预期）")
    print(f"top keys  : {st.extra.get('top_keys')}")
    if st.extra.get("js_error"):
        print(f"JS error  : {st.extra['js_error']}")
    if not st.ok:
        print()
        print("⚠  健康检查没过 —— 后面的 store dump 多半也会失败。先看上面的 detail。")

    # ---- 顶层结构
    head("③ store 顶层结构")
    try:
        state = collector.dump_state(force=True)
    except (CDPError, dc.DesktopCDPError) as e:
        print(f"✗ 取不到 state：{e}")
        return 0
    raw = json.dumps(state, ensure_ascii=False)
    print(f"state 大小: {len(raw) / 1024:.1f} KB，顶层键 {len(state)} 个")
    print()
    summary = dc.describe_state(state, max_depth=args.depth, max_items=args.max_items)
    print_tree(summary)

    if args.save:
        path = os.path.abspath(args.save)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
        print(f"\n已保存整份 state → {path}（含账号信息，别提交进版本库）")

    # ---- 歌单路径命中情况
    head("④ 歌单：猜测路径 vs 全树扫描")
    user = dc.find_self_user(state)
    self_id = dc.user_id_of(user)
    print(f"登录用户  : {(user or {}).get('nickname')!r} userId={self_id}")
    print("  路径候选依次尝试（命中即止）：")
    hit = None
    for path in dc.CANDIDATE_PLAYLIST_PATHS:
        sub = dc.dig_state(state, path)
        mark = "命中" if sub is not None else "无"
        print(f"    [{mark}] {'.'.join(path)}")
        if sub is not None and hit is None:
            hit = ".".join(path)
    items, source = dc.extract_playlists(state, self_id=self_id)
    print()
    print(f"实际使用  : {source}")
    print(f"歌单数量  : {len(items)}（含收藏/未过滤；「我创建的」还要按 subscribed/creator 过一遍）")
    if source.startswith("scan:"):
        print("  ⚠ 所有猜测路径都没命中 → 是全树扫描兜住的。")
        print("    请把上面的实际路径补进 desktop_cdp.py 的 CANDIDATE_PLAYLIST_PATHS，"
              "并在注释里把「未验证」改成实测结论。")
    created = [i for i in items if i.get("created") is not False]
    print(f"其中「我创建的」（宽松判定）: {len(created)}")
    for it in items[:max(0, args.playlists)]:
        flag = {True: "我创建", False: "收藏/他人", None: "?"}[it.get("created")]
        print(f"  · id={it.get('id') or '?':>12}  {flag:<7} "
              f"trackCount={it.get('track_count')}  曲目条目={len(it.get('tracks') or [])}  "
              f"名字={it.get('name')!r}")
        print(f"      路径: {it.get('path')}")

    # ---- 曲目还原
    head("⑤ 曲目还原（trackIds/tracks → 歌曲对象）")
    idx = dc.song_index(state)
    print(f"全 state 里识别到的歌曲对象: {len(idx)} 个")
    if items:
        first = next((i for i in items if i.get("tracks")), items[0])
        songs = dc.songs_from_playlist_entry(first, state, index=idx)
        print(f"取「{first.get('name')}」({first.get('id')}) 的曲目: "
              f"{len(songs)} 首（曲目条目 {len(first.get('tracks') or [])} 个）")
        if not songs and first.get("tracks"):
            print("  ⚠ 有曲目条目但一首都没还原出来 → trackIds 指向的歌曲对象不在 store 里，")
            print("    需要先让客户端把这个歌单打开（或改用 DOM 兜底，见 playlist_songs 参数）。")
        for s in songs[:10]:
            print(f"  · {s['netease_song_id']:>12}  {s['name'][:34]:<36} "
                  f"{'/'.join(s['artists'])[:24]:<26} {s['duration_ms']}ms  "
                  f"{s['anime_alias'] or ''}")
    else:
        print("（没有歌单可试）")

    head("完成")
    print("把 ③ 的顶层结构、④ 的实际路径、② 的 store diag 回填到 desktop_cdp.py 的注释里，")
    print("并把 status 从「未验证」改成「实测」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
