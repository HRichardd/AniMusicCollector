#!/usr/bin/env python
"""探测「歌单曲目」这条路 —— 把 web API 路径从**推测**变成**实测事实**。

前置：网易云客户端带调试参数运行（快捷方式目标后面加）：
    --remote-debugging-port=9222

它回答四个问题，逐条打印结论：

    1. 客户端 store 里的歌单对象**到底有没有**曲目数组？
       （`desktop_cdp` 的 docstring 断言「没有」，这个脚本是那条断言的证据）
    2. web API（`api/v6/playlist/detail`）对每个真实歌单返回什么：
       `trackCount` / `len(trackIds)` / `len(tracks)` —— **`trackCount == len(trackIds)`
       是「这条 id 表可信」的全部依据**，所以必须逐个对照着看
    3. `_membership()` 在两个来源上分别给出什么判定（三态），以及合起来是什么
       —— 用一个**确实在歌单里**的 id 和一个**肯定不在**的 id 各试一次
    4. `playlist_songs()` 现在返回多少首、来自哪个源（`last_songs_source`）

**只读**：不 dispatch、不建歌单、不加歌。真机也没有任何写入路径被这个脚本触发。

用法：
    python tools/probe_netease_playlist.py
    python tools/probe_netease_playlist.py --name 26年4月
    python tools/probe_netease_playlist.py --pid 17977808146
    python tools/probe_netease_playlist.py --playlists 12      # 对照多个歌单
    python tools/probe_netease_playlist.py --no-client         # 只打 web API 那一半
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from animusic import constants  # noqa: E402
from animusic.cdp import port_open  # noqa: E402
from animusic.collector import api_playlist as ap  # noqa: E402
from animusic.collector import desktop_cdp as dc  # noqa: E402
from animusic.collector import write_path as wp  # noqa: E402  (仅用于说明，不调用)
from animusic.models import PlaylistRef  # noqa: E402


def head(msg: str) -> None:
    print("\n" + "─" * 72)
    print(msg)
    print("─" * 72)


def _state_playlists(state, self_id):
    items, source = dc.extract_playlists(state, self_id)
    return items, source


def main() -> int:
    ap_ = argparse.ArgumentParser(description="探测歌单曲目的 web API 路径")
    ap_.add_argument("--port", type=int, default=constants.NETEASE_DEBUG_PORT)
    ap_.add_argument("--name", default="", help="只看这个歌单名（可模糊）")
    ap_.add_argument("--pid", default="", help="直接指定歌单 id（跳过客户端）")
    ap_.add_argument("--playlists", type=int, default=5,
                     help="对照几个歌单（0 = 只测 --name/--pid）")
    ap_.add_argument("--no-client", action="store_true",
                     help="不碰客户端，只打 web API 那一半")
    args = ap_.parse_args()

    web = ap.WebPlaylistReader()

    # ────────────────────────────────────────────────── 1. 客户端 store
    state = None
    items: list[dict] = []
    source = "（没读）"
    if not args.no_client:
        if not port_open(args.port):
            print(f"⚠️ 端口 {args.port} 没在听 —— 客户端没带 "
                  f"--remote-debugging-port={args.port} 启动。\n"
                  f"   跳过 store 那一半（那是它**本来的**角色，不影响下面的结论）。")
        else:
            col = dc.DesktopCDPCollector(args.port, use_web_playlist=False)
            st = col.health()
            print(f"客户端健康：ok={st.ok} {st.detail}")
            print(f"登录用户：{st.extra.get('user') if isinstance(st.extra, dict) else '?'}")
            try:
                state = col.dump_state(force=True)
            except Exception as e:  # noqa: BLE001
                print(f"⚠️ 读 store 失败：{type(e).__name__}: {e}")
            if state is not None:
                col.last_store_diag = getattr(col, "last_store_diag", None)
                items, source = _state_playlists(state, None)
                print(f"store 取法：{col.last_store_diag}")
                print(f"歌单来源：{source}    共 {len(items)} 个歌单")

    # ────────────────────────────────────────── 2. store 里有没有曲目数组
    head("① store 里的歌单对象到底有没有曲目数据")
    if items:
        with_tracks = [it for it in items if it.get("tracks")]
        with_count = [it for it in items if it.get("track_count") is not None]
        print(f"歌单总数          : {len(items)}")
        print(f"带曲目数组的      : {len(with_tracks)}   ← 期望 0（这是本模块存在的理由）")
        print(f"带 trackCount 的  : {len(with_count)}")
        keys = set()
        for it in items[:200]:
            keys.update(it.get("raw", {}).keys())
        for k in ("tracks", "trackIds", "songIds", "songs", "trackCount", "songCount"):
            n = sum(1 for it in items if k in (it.get("raw") or {}))
            print(f"  含 {k:<12}: {n}/{len(items)}")
        sample = items[0]
        print(f"\n样例：{sample['name']!r} id={sample['id']} "
              f"track_count={sample['track_count']} "
              f"tracks={len(sample['tracks'])}")
    else:
        print("（没读到 store，跳过）")

    # ────────────────────────────────────────────── 3. web API 逐个对照
    todo: list[tuple[str, str]] = []
    if args.pid:
        todo.append((f"id={args.pid}", str(args.pid)))
    if args.name and items:
        from animusic.collector.desktop_cdp import rank_playlist_names

        ranked = rank_playlist_names(
            args.name, [it["name"] for it in items],
            allow_yearless=True, min_score=dc.SCORE_YEARLESS_QUARTER)
        if ranked:
            it = items[ranked[0][0]]
            print(f"\n名字 {args.name!r} → {it['name']!r} (id={it['id']}, 分数 {ranked[0][2]})")
            todo.append((it["name"], str(it["id"])))
        else:
            print(f"\n⚠️ store 里没有歌单名匹配 {args.name!r}")
    if not todo and args.playlists:
        for it in items[: args.playlists]:
            todo.append((it["name"], str(it["id"])))

    head("② web API 的歌单曲目表（`trackCount == len(trackIds)` 才可信）")
    if not todo:
        print("没有可测的歌单（用 --pid / --name，或让客户端跑起来）")
        return 0
    print(f"{'名称':<16} {'id':>14} {'trackCount':>10} {'len(ids)':>9} "
          f"{'len(tracks)':>11} {'完整':>4}  privacy")
    snaps: dict[str, ap.PlaylistSnapshot] = {}
    for name, pid in todo:
        try:
            snap = web.songs(pid)
        except ap.PlaylistNotFound as e:
            print(f"{name[:16]:<16} {pid:>14} {'—':>10} {'—':>9} {'—':>11} "
                  f"{'—':>4}  不存在：{e}")
            continue
        except ap.WebPlaylistError as e:
            print(f"{name[:16]:<16} {pid:>14} 读失败：{e}")
            continue
        snaps[pid] = snap
        print(f"{name[:16]:<16} {pid:>14} {str(snap.declared_count):>10} "
              f"{len(snap.track_ids):>9} {len(snap.songs):>11} "
              f"{'✅' if snap.ids_complete else '❌':>4}  {snap.privacy}")
    verdict = [s for s in snaps.values() if s.ids_complete]
    print(f"\n完整可信的 id 表：{len(verdict)}/{len(snaps)}")
    if snaps and len(verdict) < len(snaps):
        print("⚠️ 有不完整的 —— 那些歌单在**未登录**视角下被截断了，"
              "不能据此断定某首歌不在里面")

    # ────────────────────────────────── 4. 三态判定（在 / 不在 各一次）
    head("③ `_membership()` 三态：CLI 层看到的最终判定")
    if not args.no_client and port_open(args.port) and snaps:
        col = dc.DesktopCDPCollector(args.port, use_web_playlist=True)
        for pid, snap in list(snaps.items())[:2]:
            if not snap.track_ids:
                print(f"\n{snap.name!r}(id={pid}) 是空歌单 —— 跳过")
                continue
            inside = snap.track_ids[0]
            outside = str(int(inside) + 987_654_321) if inside.isdigit() else "0"
            ref = PlaylistRef(name=snap.name or "", netease_id=pid)
            print(f"\n歌单 {snap.name!r}(id={pid})，共 {snap.declared_count} 首")
            m_in = col._membership(ref, inside, refresh=True)
            print(f"  在里面的 {inside:<14} → present={m_in.present}  "
                  f"complete={m_in.complete}  {m_in.reason[:90]}")
            m_out = col._membership(ref, outside, refresh=True)
            print(f"  不在的   {outside:<14} → present={m_out.present}  "
                  f"complete={m_out.complete}  {m_out.reason[:90]}")
            if m_in.present is not True:
                print("  ⚠️ 在里面的歌没被判成「在」—— 幂等会失效（会重复加歌）")
            if m_out.present is not False:
                print("  ⚠️ 不在的歌没被判成「不在」—— 会拒写（保守，但推不动）")
    else:
        print("（需要客户端在跑才能测这条 —— 它同时要读 store 和 web API）")

    # ──────────────────────────────────────── 5. playlist_songs 走哪个源
    head("④ `playlist_songs()` 现在返回多少、来自哪个源")
    if not args.no_client and port_open(args.port) and snaps:
        col = dc.DesktopCDPCollector(args.port, use_web_playlist=True)
        for pid, snap in list(snaps.items())[:2]:
            songs = col.playlist_songs(pid)
            print(f"\nid={pid} {snap.name!r}：{len(songs)} 首  "
                  f"来源={col.last_songs_source}  web_error={col.last_web_error}")
            for s in songs[:6]:
                print(f"    {s['netease_song_id']:>12}  {s['name'][:34]:<34} "
                      f"{'/'.join(s['artists'])[:26]}")
            if len(songs) > 6:
                print(f"    … 另 {len(songs) - 6} 首")
            if not songs:
                print("    ⚠️ 一首都没读到 —— 去重比对会退化成「什么都不知道」")
    else:
        print("（需要客户端在跑）")

    head("小结")
    print(f"web API 限速     : {constants.PLAYLIST_READ_RATE_RPS} rps")
    print(f"本次 web 读次数  : {web.reads}")
    print(f"最后一条错误     : {web.last_error}")
    print("\n⚠️ 本脚本**只读**。写入链路的 payload 形状（§7.3）仍未被验证，"
          "\n   那是 M3 的另一半，要用 probe_write_path() 在真机上对照。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
