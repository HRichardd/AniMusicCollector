"""向导会话 —— GUI 与后端（Store + Orchestrator）之间的适配层。

设计目标：**页面代码零改动**。

`build()` 产出的字典结构与 `mock_data.build()` 完全一致，
所以从假数据切到真实数据时，四个页面一行都不用改。
真实数据与展示格式的差异（数据库用 anime_id 外键、候选的 alias 存成单字段、
artists 存成 JSON 文本……）全部在这里抹平。
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from .. import constants
from ..config import Config
from ..models import (
    Anime,
    DecisionStatus,
    Origin,
    Season,
    SeasonBucket,
    Stage,
    StageStatus,
    Theme,
    ThemeKind,
    artists_from_json,
    artists_to_json,
)
from ..orchestrator import Orchestrator
from ..store import Store

logger = logging.getLogger(__name__)

# 状态 → 展示文案。
STATUS_LABEL = {
    "auto": "🟢 自动",
    "confirmed": "🔵 已确认",
    "pending_review": "🟡 待审核",
    "low": "🔴 低分",
    "missing_netease": "⬜ 未上架",
    "missing_source": "⬜ 未收录",
    "missing_match": "🔴 匹配不上",
    "skipped": "⬜ 已跳过",
    # 还没跑过匹配 —— 必须和「跑过但需要审核」区分开，
    # 否则刚拉完番单时整个列表都显示「待审核」，用户会以为匹配已经跑过了。
    "pending": "⏳ 待匹配",
}

BUCKET_LABEL = {
    SeasonBucket.TARGET: "当季",
    SeasonBucket.CROSS_SEASON: "跨季",
    SeasonBucket.OTHER: "其他",
    SeasonBucket.UNKNOWN: "未知",
}

_ORIGIN_LABEL = {
    Origin.BANGUMI: "Bangumi",
    Origin.ANIMETHEMES: "数据源",
    Origin.SEARCH: "搜索",
    Origin.MANUAL: "手工",
    Origin.PENDING: "待补",
}

#: 界面状态 → **能落库的**决策状态。
#:
#: ⚠️ 任何写进 `decision.status` 的值都必须先过这张表。原因是一次真实事故：
#: 第④步点「完成」时把推送页的行（`status` 是「待推送」这种给人看的中文）
#: 喂给了 `save_decisions()`，字符串原样进 SQL，撞上 `decision` 表的
#: `CHECK (status IN ...)` → `IntegrityError`，整步保存失败。
#: 界面上显示什么是**展示**问题，落库是什么是**领域**问题，两者不能共用一列字符串。
#: 映射表里没有的键 = 未知状态 → 一律不写（见 `_DECISION_NO_WRITE`）。
_DECISION_UI_STATUS = {
    "auto": DecisionStatus.AUTO,
    "confirmed": DecisionStatus.CONFIRMED,
    "pending_review": DecisionStatus.PENDING_REVIEW,
    "skipped": DecisionStatus.SKIPPED,
    "missing_source": DecisionStatus.MISSING_SOURCE,
    "missing_netease": DecisionStatus.MISSING_NETEASE,
    "missing_match": DecisionStatus.MISSING_MATCH,
    # 🔴 低分：库里没有这个状态，它的**含义**就是「要人来判断」→ 待审核。
    # 候选项卡片用 low 只是配色分级（scorer 的阈值），不是决策语义。
    "low": DecisionStatus.PENDING_REVIEW,
}

#: 明确**不落库**的界面状态：⏳ 待匹配 = 「还没做过决定」。
#: 库里没有 pending —— 因为「没有决定」就应该**没有 decision 行**
#: （`_match_rows` 靠「没有行」来显示 ⏳ 待匹配）。随便写一个值，
#: 等于把「没决定」记成「已决定」，用户下次就再也看不出来哪些还没匹配。
_DECISION_NO_WRITE = {"pending"}


class WizardSession:
    def __init__(
        self,
        config: Config | None = None,
        store: Store | None = None,
        season: Season | None = None,
        aliases=None,
    ):
        self.config = config or Config.load()
        self.store = store or Store()
        self.season = season or Season.current()
        self._aliases = aliases      # 注入用（测试避免碰真实 config）
        self._orch: Orchestrator | None = None

    # ---------------------------------------------------------------- 生命周期

    @property
    def season_id(self) -> int:
        return self.store.get_season_id(self.season.year, self.season.quarter)

    @property
    def playlist_name(self) -> str:
        return constants.playlist_name(self.season.year, self.season.quarter)

    def set_season(self, season: Season) -> None:
        self.season = season
        self._orch = None

    def orchestrator(self, progress=None) -> Orchestrator:
        """惰性构造 —— Orchestrator 会建 BrowserBridge，但不会主动去连。"""
        if progress is not None or self._orch is None:
            self._orch = Orchestrator(
                config=self.config, store=self.store, progress=progress,
                aliases=self._aliases,
            )
        return self._orch

    def close(self) -> None:
        self.store.close()

    def run_operation(self, operation: str, season: Season, *, progress=None, should_stop=None):
        orch = self.orchestrator(progress=progress)
        orch.should_stop = should_stop
        try:
            return getattr(orch, operation)(season)
        finally:
            # 取消的旧任务不能让之后的人工保存/别名学习也一直处于取消状态。
            orch.should_stop = None

    @property
    def aliases(self):
        """别名表 —— 单一来源，直接从编排层取，避免两份实例各改各的。"""
        return self.orchestrator().aliases

    # ================================================================ 读：组装页面数据

    def build(self) -> dict[str, list[dict]]:
        sid = self.season_id
        anime_rows = self._anime_rows(sid)
        theme_rows, missing_source = self._theme_rows(sid)
        match_rows, push_rows = self._match_rows(sid, anime_rows)
        return {
            "anime": anime_rows,
            "theme": theme_rows,
            "match": match_rows,
            "push": push_rows,
            "missing_source": missing_source,
        }

    def stats(self, data: dict[str, list[dict]] | None = None) -> dict[str, int]:
        d = data or self.build()
        m = d["match"]
        return {
            "anime": len([r for r in d["anime"] if r["included"]]),
            "theme": len([r for r in d["theme"] if r["included"]]),
            #: 曲目**总行数**（含未勾选的）。
            #:
            #: 为什么要两个数：`theme` 只数「收录」的行，而它是界面上唯一露出的
            #: 曲目数 —— 用户在步骤②取消勾选一条之后，界面就从 15 变成 14，
            #: 而日志/报告说的是「写入 15 条」。两个数字都没错，但只说一个
            #: 就会让人以为丢了数据（实测被问过一次）。
            "theme_total": len(d["theme"]),
            #: 差额**为什么**存在 —— 两个原因分开数，因为它们去处不同：
            #: 前者回②把曲目勾上，后者回①把番勾上。
            #:
            #: ⚠️ 两者都**不是**「未收录」：本项目里「未收录」= 数据源里没有
            #: （`MISSING_SOURCE`）。状态栏原来把差额一律写成「N 条未收录」，
            #: 用户会去手动补几行**本来就有数据**、只是自己没勾的曲目 ——
            #: 措辞指错方向就是让人白干活（§6 的口径问题）。
            "theme_unchecked": len([r for r in d["theme"]
                                    if not r.get("theme_included", True)]),
            "theme_anime_excluded": len([r for r in d["theme"]
                                         if r.get("theme_included", True)
                                         and not r.get("anime_included", True)]),
            "match_total": len(m),
            "auto": len([r for r in m if r["status"] in ("auto", "confirmed")]),
            "review": len([r for r in m if r["status"] == "pending_review"]),
            "missing": len([r for r in m if str(r["status"]).startswith("missing")]),
            "push": len(d["push"]),
        }

    # ---- 步骤① 番单

    def _anime_rows(self, sid: int) -> list[dict]:
        out = []
        for r in self.store.anime_list(sid, only_included=False):
            bucket = SeasonBucket(r["season_bucket"] or "unknown")
            origin = Origin(r["origin"] or "bangumi")
            cn, jp, romaji = (r["title_cn"] or ""), (r["title_jp"] or ""), (r["title_romaji"] or "")

            # AnimeThemes 只给罗马音、AniList 给日文名，中文名经常是空的。
            # 表格里必须有个可读的名字，所以做回退显示；
            # 但**绝不能把回退值当成真的中文名写回库里**（否则编辑一次就污染数据），
            # 所以同时记下「这个值是回退来的」以及它的原始内容。
            display = cn or jp or romaji
            out.append({
                "anime_id": r["id"],
                "bgm_id": str(r["bgm_id"] or ""),
                "animethemes_slug": r["animethemes_slug"] or "",
                "title_cn": display,
                "_cn_auto": (not cn) and bool(display),
                "_cn_original": display,
                "title_jp": jp,
                "title_romaji": romaji,
                "air_date": r["air_date"] or "",
                "bucket": str(bucket),
                "bucket_label": BUCKET_LABEL.get(bucket, str(bucket)),
                "origin": str(origin),
                "origin_label": _ORIGIN_LABEL.get(origin, str(origin)),
                "note": r["note"] or "",
                "included": bool(r["included"]),
            })
        return out

    # ---- 步骤② 曲目

    def _theme_rows(self, sid: int) -> tuple[list[dict], list[str]]:
        rows: list[dict] = []
        missing_source: list[str] = []
        # 数据源没收录的番（有番但没有任何 theme）要显式列出来
        have_theme = {
            r["anime_id"] for r in self.store.conn.execute(
                "SELECT DISTINCT anime_id FROM theme"
            )
        }
        for a in self.store.anime_list(sid):
            if a["id"] not in have_theme:
                missing_source.append(a["title_cn"] or a["title_jp"] or "?")

        #: 番剧级的「纳入」：**必须用 `only_included=False` 取**。
        #:
        #: 用户实测报的 bug（①里取消勾选的番，②里它的曲目仍显示勾选）根因就在这一行：
        #: 原来写的是 `self.store.anime_list(sid)`，而它默认 `only_included=True` ——
        #: 被排除的番**压根不在结果里**，于是下面 `next(..., 1)` 的兜底值 1（True）
        #: 生效，曲目一律显示"已收录"。兜底值本身是给"番行缺失"这种异常情况准备的，
        #: 却在最常见的情况下被触发了。
        anime_included = {
            r["id"]: bool(r["included"])
            for r in self.store.anime_list(sid, only_included=False)
        }
        for r in self.store.theme_list(sid, only_included=False):
            origin = Origin(r["origin"] or "animethemes")
            anime_on = anime_included.get(r["anime_id"], True)
            rows.append({
                "theme_id": r["id"],
                "anime": r["anime_cn"] or r["anime_jp"] or "?",
                "anime_id": r["anime_id"],
                "kind": r["kind"],
                "seq": r["sequence"] or 1,
                "song_title": r["song_title"] or "",
                "artists": artists_from_json(r["artists"]),
                "origin": str(origin),
                "origin_label": _ORIGIN_LABEL.get(origin, str(origin)),
                #: 这一行的番在①里有没有被纳入 —— 界面上「已排除」的计数要用它区分
                #: 「这条曲目被排除了」和「它的番被排除了」（两者都表现为勾没打上）
                "anime_included": anime_on,
                #: **曲目自己**有没有被勾上（不含番的影响）。
                #: 为什么不直接用下面那个 `included`：`included` 是「曲目 ∧ 番」，
                #: 而状态栏要分别说清「②里取消勾选 N 条」和「所属番在①被排除 M 条」
                #: —— 两个原因**去处不同**（回②勾上 / 回①勾番）。
                "theme_included": bool(r["included"]),
                "included": bool(r["included"]) and anime_on,
                "note": r["note"] or "",
            })
        rows.sort(key=lambda r: (r["anime"], r["kind"], r["seq"]))
        return rows, missing_source

    # ---- 步骤③④ 匹配与推送

    def _match_rows(
        self, sid: int, anime_rows: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        jp_by_cn = {r["title_cn"]: r["title_jp"] for r in anime_rows}
        decisions = self.store.decision_map(sid)
        # 第④步**上次保存过的选择**（勾选 + 逐行目标歌单）。
        # 不回读它的话：用户在最后一步逐行改好的歌单，任何一次 reload 都会
        # 被这里按季度名重建 —— 看起来「改了，下次打开又变回去了」。
        saved_rows = self.store.push_selection_rows(sid)
        selection = {(r["theme_id"], r["netease_song_id"]): r for r in saved_rows}
        # 已推送台账 —— 让步骤④ 一打开就能看到「哪些早就进歌单了」，
        # 而不是永远显示「待推送」让用户以为每次都要重推。
        # 按**该行自己的目标歌单**查（用户可以把某一行推到别的歌单，
        # 那时用季度歌单的台账判断就会显示错的「已推送」）。
        pushed_cache: dict[str, set[str]] = {}

        def pushed_in(playlist: str) -> set[str]:
            if playlist not in pushed_cache:
                pushed_cache[playlist] = self.store.pushed_songs(playlist)
            return pushed_cache[playlist]

        match_rows: list[dict] = []
        push_rows: list[dict] = []

        for r in self.store.theme_list(sid):
            tid = r["id"]
            d = decisions.get(tid)
            # 没有决策 = 还没跑过匹配，不能显示成「待审核」
            status = d["status"] if d else "pending"

            cands = [self._cand_dict(c, i) for i, c in enumerate(self.store.candidate_list(tid))]
            chosen = None
            if d and d["chosen_candidate_id"]:
                chosen = next((c for c in cands if c.get("_cid") == d["chosen_candidate_id"]), None)
            # ⚠️ `skipped` 的行**故意不回退**到第一条候选：用户刚在界面上说过
            # 「候选都不对」，重新载入后又把第一首显示成「选中歌曲」，是自相矛盾的
            # （而且 status 还是「已跳过」，两个列互相打脸）。
            if chosen is None and cands and status != "skipped":
                chosen = cands[0]

            for c in cands:
                c.pop("_cid", None)

            cn = r["anime_cn"] or r["anime_jp"] or "?"
            match_rows.append({
                "theme_id": tid,
                "anime": cn,
                "kind": r["kind"],
                "seq": r["sequence"] or 1,
                "theme_title": r["song_title"] or "",
                "theme_artists": "、".join(artists_from_json(r["artists"])),
                "chosen_name": (chosen or {}).get("name") or "—",
                "chosen_artists": "、".join((chosen or {}).get("artists") or []) or "—",
                "chosen_id": (chosen or {}).get("netease_song_id") or "",
                "chosen_duration": (chosen or {}).get("duration_ms"),
                "score": (chosen or {}).get("score") or 0.0,
                "status": status,
                "status_label": STATUS_LABEL.get(status, status),
                "candidates": cands[:8],
                "note": (d["note"] if d else "") or "",
                "manual": bool(d and d["decided_by"] == "human"),
                "detail": (chosen or {}).get("detail"),
            })

            if chosen and status in ("auto", "confirmed"):
                sid_ = chosen["netease_song_id"]
                saved = selection.get((tid, str(sid_))) or selection.get((None, str(sid_))) or {}
                target = str(saved.get("playlist") or "").strip() or self.playlist_name
                push_rows.append({
                    # 缺省是勾选（新歌、新季度）；用户取消过勾选的行记住为不勾
                    "included": bool(saved.get("included", True)),
                    "name": chosen["name"],
                    "artists": "、".join(chosen.get("artists") or []),
                    "netease_song_id": sid_,
                    "playlist": target,
                    "status": "已推送" if sid_ in pushed_in(target) else "待推送",
                    "anime": cn,
                    "kind": r["kind"],
                    "theme_id": tid,
                })

        match_rows.sort(key=lambda r: (r["anime"], r["kind"], r["seq"]))
        push_rows.sort(key=lambda r: (r["anime"], r["kind"]))
        return match_rows, push_rows

    def _cand_dict(self, c: Any, rank: int) -> dict:
        from ..scorer import classify
        alias = c["anime_alias"]
        try:
            tags = json.loads(c["tags"] or "[]")
        except (ValueError, TypeError):
            tags = []
        try:
            detail = json.loads(c["score_detail"] or "{}")
        except (ValueError, TypeError):
            detail = {}
        return {
            "_cid": c["id"],
            "netease_song_id": str(c["netease_song_id"]),
            "name": c["name"] or "",
            "artists": artists_from_json(c["artists"]),
            "album": c["album"] or "",
            "cover": c["cover"] or "",
            "duration_ms": c["duration_ms"] or 0,
            "tags": tags,
            "alias": [alias] if alias else [],
            "score": c["score"] or 0.0,
            "detail": detail,
            "rank": c["search_rank"] if c["search_rank"] is not None else rank,
            "status": classify(c["score"] or 0,
                               auto_threshold=self.config.match.auto_threshold,
                               review_threshold=self.config.match.review_threshold),
        }

    # ================================================================ 写：回存人工修改

    def save_anime(self, rows: list[dict]) -> None:
        """按数据库身份保存，页面上的名字不能成为更新主键。"""
        sid = self.season_id
        existing_rows = {r["id"]: r for r in self.store.anime_list(sid, only_included=False)}
        prepared = []
        seen = set()
        for r in rows:
            cn = r.get("title_cn") or ""
            if r.get("_cn_auto") and cn == r.get("_cn_original"):
                cn = ""
            aid = r.get("anime_id")
            old = existing_rows.get(aid)
            if aid is not None and (old is None or aid in seen):
                raise ValueError("番剧 ID 不属于当前季度或在页面中重复")
            if aid is not None:
                seen.add(aid)
            jp = r.get("title_jp") or ""
            if not (cn or jp or (old and old["title_romaji"])):
                raise ValueError("番剧名称不能为空")
            raw_date = str(r.get("air_date") or "").strip()
            air = date.fromisoformat(raw_date).isoformat() if raw_date else None
            bucket = str(SeasonBucket(r.get("bucket") or "unknown"))
            changed = old is None or any((old[k] or "") != (v or "") for k, v in
                                        (("title_cn", cn), ("title_jp", jp), ("air_date", air)))
            prepared.append((r, old, cn, jp, air, bucket, changed))
        assigned = []
        with self.store.transaction():
            changed_page = False
            for r, old, cn, jp, air, bucket, changed in prepared:
                included = int(bool(r.get("included", True)))
                note = r.get("note") or ""
                if old is not None:
                    aid = old["id"]
                    self.store.conn.execute(
                        "UPDATE anime SET title_cn=?, title_jp=?, air_date=?, included=?, note=?, origin=? WHERE id=?",
                        (cn or None, jp or None, air, included, note,
                         str(Origin.MANUAL) if changed else old["origin"], aid))
                    changed_page |= changed or old["included"] != included
                else:
                    cur = self.store.conn.execute(
                        "INSERT INTO anime(season_id,title_cn,title_jp,bgm_id,air_date,season_bucket,origin,included,note) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (sid, cn or None, jp or None,
                         int(r["bgm_id"]) if str(r.get("bgm_id") or "").isdigit() else None,
                         air, bucket, str(Origin.MANUAL), included, note))
                    aid = int(cur.lastrowid)
                    changed_page = True
                assigned.append((r, aid))
            self.store.set_stage(sid, Stage.ANIME, StageStatus.CONFIRMED, confirmed=True)
            if changed_page:
                self.store.mark_downstream_dirty(sid, Stage.ANIME)
        for r, aid in assigned:
            r["anime_id"] = aid

    def save_themes(self, rows: list[dict]) -> None:
        """整页原子保存；缺席行不等于删除，排除必须由 included 明确表达。"""
        sid = self.season_id
        anime_ids = {r["id"]: (r["title_cn"] or r["title_jp"] or r["title_romaji"])
                     for r in self.store.anime_list(sid, only_included=False)}
        existing_rows = {r["id"]: r for r in self.store.theme_list(sid, only_included=False)}
        prepared = []
        seen = set()
        for r in rows:
            tid = r.get("theme_id")
            old = existing_rows.get(tid)
            if tid is not None and (old is None or tid in seen):
                raise ValueError("曲目 ID 不属于当前季度或在页面中重复")
            if tid is not None:
                seen.add(tid)
            aid = r.get("anime_id")
            if aid is None:
                matches = [k for k, v in anime_ids.items() if v == r.get("anime")]
                if len(matches) != 1:
                    raise ValueError("请选择唯一的所属番剧")
                aid = matches[0]
            if aid not in anime_ids:
                raise ValueError("所属番剧不在当前季度")
            title = (r.get("song_title") or "").strip()
            if not title:
                raise ValueError("曲名不能为空")
            kind = str(ThemeKind(r.get("kind") or "OP"))
            seq = int(r.get("seq") or 1)
            if seq < 1:
                raise ValueError("曲目序号必须为正整数")
            artists = r.get("artists") or []
            if isinstance(artists, str):
                artists = [x.strip() for x in artists.replace("、", ",").split(",") if x.strip()]
            artist_json = artists_to_json(artists)
            match_changed = old is None or (old["anime_id"], old["kind"], old["song_title"],
                                           artists_from_json(old["artists"])) != (aid, kind, title, artists)
            changed = match_changed or (old["sequence"] or 1) != seq
            # 番剧排除造成的未勾选不能污染曲目本身的选择。
            included = bool(r.get("included", True)) if r.get("anime_included", True) else bool(
                r.get("theme_included", r.get("included", True)))
            prepared.append((r, old, aid, title, kind, seq, artist_json, included, changed, match_changed))
        assigned = []
        with self.store.transaction():
            changed_page = False
            for r, old, aid, title, kind, seq, artists, included, changed, match_changed in prepared:
                note = r.get("note") or ""
                if old is not None:
                    tid = old["id"]
                    self.store.conn.execute(
                        "UPDATE theme SET anime_id=?,kind=?,song_title=?,artists=?,sequence=?,included=?,note=?,origin=? WHERE id=?",
                        (aid, kind, title, artists,
                         old["sequence"] if (old["sequence"] or 1) == seq else seq,
                         int(included), note, str(Origin.MANUAL) if changed else old["origin"], tid))
                    if match_changed:
                        self.store.conn.execute(
                            "UPDATE decision SET status='pending_review',chosen_candidate_id=NULL,decided_by='auto',"
                            "note='曲目内容已修改，请重新审核',decided_at=datetime('now') WHERE theme_id=?", (tid,))
                    changed_page |= changed or bool(old["included"]) != included
                else:
                    cur = self.store.conn.execute(
                        "INSERT INTO theme(anime_id,kind,song_title,artists,sequence,included,note,source,origin) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (aid, kind, title, artists, seq, int(included), note, "manual", "manual"))
                    tid = int(cur.lastrowid)
                    changed_page = True
                assigned.append((r, tid, aid))
            self.store.set_stage(sid, Stage.THEME, StageStatus.CONFIRMED, confirmed=True)
            if changed_page:
                self.store.mark_downstream_dirty(sid, Stage.THEME)
        for r, tid, aid in assigned:
            r["theme_id"], r["anime_id"] = tid, aid

    def save_decisions(self, rows: list[dict]) -> dict:
        """步骤③ 的人工确认落库，**并顺手学别名**。

        用户选定后必须**记住是人工定的**（`decided_by='human'`），
        否则下次自动重跑会把手选结果覆盖掉。

        状态必须经 `_DECISION_UI_STATUS` 映射后才落库（见那张表的注释）；
        映射不了的行**跳过并报出来**，绝不把界面文案塞进 SQL。

        返回值是别名学习统计 + 这一步的落库情况 —— 人工确认是「贵」的信号，
        把它固化成别名才能让「确认一次、以后自动通过」成立（附录 G.9）。
        """
        sid = self.season_id
        by_id = {r["id"]: r for r in self.store.theme_list(sid)}
        cand_index: dict[tuple[int, str], int] = {}
        for tid in by_id:
            for c in self.store.candidate_list(tid):
                cand_index[(tid, str(c["netease_song_id"]))] = c["id"]

        written = 0
        #: [(theme_id, 界面上的状态原文)] —— 给用户看的，不能静默吞
        skipped: list[tuple[int, str]] = []

        for r in rows:
            tid = r.get("theme_id")
            if not tid or tid not in by_id:
                continue

            raw = str(r.get("status") or "pending_review")
            status = _DECISION_UI_STATUS.get(raw)
            if status is None:
                skipped.append((tid, raw))
                continue

            song_id = str(r.get("chosen_id") or "")
            cid = cand_index.get((tid, song_id))
            if song_id and cid is None:
                # 手工粘贴进来的歌：建一条 manual 候选，避免被后续重跑清掉
                from ..models import Candidate

                cid = self.store.upsert_candidate(tid, Candidate(
                    theme_id=tid, netease_song_id=song_id,
                    name=r.get("chosen_name") or "(手动指定)",
                    artists=[x for x in str(r.get("chosen_artists") or "").replace("、", ",").split(",") if x.strip()],
                    origin=Origin.MANUAL,
                ))
            human = bool(r.get("manual")) or status == DecisionStatus.CONFIRMED
            self.store.set_decision(
                tid, status, cid,
                decided_by="human" if human else "auto",
                note=r.get("note") or "",
            )
            written += 1

        if skipped:
            logger.warning(
                "save_decisions：%d 行的状态没有落库映射，已跳过（例如 %r）",
                len(skipped), skipped[0][1],
            )

        # 顺手从人工确认里学别名（只学 human 的 —— 从自动结果学有把错误固化的风险）
        try:
            stat = dict(self.orchestrator().learn_from_season(self.season, confirm_only=True))
        except Exception as e:  # noqa: BLE001 - 学别名失败不该让保存结果回滚
            stat = {"checked": 0, "title": 0, "artist": 0, "anime": 0, "error": str(e)}
        stat["written"] = written
        stat["skipped"] = skipped
        return stat

    def save_push_selection(self, rows: list[dict]) -> dict:
        """步骤④ 的选择落库：**勾选** + 逐行「目标歌单」。

        ⚠️ 这里绝不能调 `save_decisions()`（曾经就是那么错的）：
        推送页的 `status` 是「待推送」这种**给人看的文案**，
        而 `decision.status` 有 CHECK 约束。两者语义完全无关 ——
        一个是「匹配结果」，一个是「这次推不推、推到哪」。
        """
        sid = self.season_id
        saved = self.store.save_push_selection(sid, rows)
        usable = [r for r in rows if str(r.get("netease_song_id") or "").strip()]
        return {
            "saved": saved,
            "rows": len(rows),
            # 没有 ID 的行（刚点「+ 手动加歌」还没填）存不了，如实报出来
            "unsaved": len(rows) - len(usable),
            "checked": len([r for r in usable if r.get("included")]),
        }

    @staticmethod
    def _date(s: Any) -> date | None:
        if not s:
            return None
        try:
            return date.fromisoformat(str(s)[:10])
        except ValueError:
            return None
