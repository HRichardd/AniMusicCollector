"""SQLite 存储 —— 实现 DESIGN.md 附录 D。

两条硬约束（§8.3）：
    1. **手工数据（origin='manual'）在重跑拉取时绝不被覆盖**，
       新值只用于填空白字段。否则手填内容会被下一次「拉取」冲掉，
       直接摧毁向导流程的信任基础。
    2. 幂等性：candidate(theme_id, song_id) 与 pushed(playlist, song_id)
       都是唯一约束，重复运行不会产生重复记录。

三态缺失（§5.12）：区分「暂时没有」与「确实没有」。

**连接模型：每线程一条**（见 `Store.conn`）。这不是优化，是修 bug ——
GUI 的后台任务（`gui/workers.py` 的 `TaskWorker`）在别的线程里调这里的方法，
共用一条 `sqlite3.Connection` 会直接抛 `ProgrammingError`。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import date, datetime
from typing import Any, Iterable

from . import constants
from .models import (
    Anime,
    Candidate,
    Decision,
    DecisionStatus,
    Origin,
    SeasonBucket,
    Stage,
    StageStatus,
    Theme,
    ThemeKind,
    artists_from_json,
    artists_to_json,
)

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS season (
    id            INTEGER PRIMARY KEY,
    year          INTEGER NOT NULL,
    quarter       INTEGER NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    playlist_name TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (year, quarter)
);

CREATE TABLE IF NOT EXISTS stage_state (
    id           INTEGER PRIMARY KEY,
    season_id    INTEGER NOT NULL REFERENCES season(id) ON DELETE CASCADE,
    stage        TEXT    NOT NULL CHECK (stage IN ('anime','theme','match','push')),
    status       TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','fetched','confirmed','dirty')),
    fetched_at   TEXT,
    confirmed_at TEXT,
    UNIQUE (season_id, stage)
);

CREATE TABLE IF NOT EXISTS anime (
    id               INTEGER PRIMARY KEY,
    season_id        INTEGER NOT NULL REFERENCES season(id) ON DELETE CASCADE,
    title_jp         TEXT,
    title_cn         TEXT,
    title_romaji     TEXT,
    bgm_id           INTEGER,
    animethemes_slug TEXT,
    anilist_id       INTEGER,
    popularity       INTEGER,
    air_date         TEXT,
    season_bucket    TEXT,
    origin           TEXT NOT NULL DEFAULT 'bangumi',
    included         INTEGER NOT NULL DEFAULT 1,
    note             TEXT,
    UNIQUE (season_id, animethemes_slug)
);
CREATE INDEX IF NOT EXISTS idx_anime_season ON anime(season_id);
CREATE INDEX IF NOT EXISTS idx_anime_bgm ON anime(season_id, bgm_id);

CREATE TABLE IF NOT EXISTS theme (
    id         INTEGER PRIMARY KEY,
    anime_id   INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL CHECK (kind IN ('OP','ED')),
    sequence   INTEGER,
    song_title TEXT    NOT NULL,
    artists    TEXT    NOT NULL,
    source     TEXT    NOT NULL,
    raw        TEXT,
    origin     TEXT NOT NULL DEFAULT 'animethemes',
    included   INTEGER NOT NULL DEFAULT 1,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_theme_anime ON theme(anime_id);

CREATE TABLE IF NOT EXISTS candidate (
    id              INTEGER PRIMARY KEY,
    theme_id        INTEGER NOT NULL REFERENCES theme(id) ON DELETE CASCADE,
    query_used      TEXT    NOT NULL DEFAULT '',
    netease_song_id TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    artists         TEXT,
    album           TEXT,
    cover           TEXT,
    duration_ms     INTEGER,
    tags            TEXT,
    anime_alias     TEXT,
    trans_names     TEXT,
    fee             INTEGER,
    mvid            INTEGER,
    search_rank     INTEGER,
    score           REAL    NOT NULL DEFAULT 0,
    score_detail    TEXT,
    origin          TEXT NOT NULL DEFAULT 'search',
    fetched_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (theme_id, netease_song_id)
);
CREATE INDEX IF NOT EXISTS idx_candidate_theme ON candidate(theme_id, score DESC);

CREATE TABLE IF NOT EXISTS decision (
    id                  INTEGER PRIMARY KEY,
    theme_id            INTEGER NOT NULL UNIQUE REFERENCES theme(id) ON DELETE CASCADE,
    chosen_candidate_id INTEGER REFERENCES candidate(id),
    status              TEXT NOT NULL CHECK (status IN
                          ('auto','pending_review','confirmed','skipped',
                           'missing_source','missing_netease','missing_match')),
    decided_by          TEXT NOT NULL DEFAULT 'auto',
    decided_at          TEXT NOT NULL DEFAULT (datetime('now')),
    last_checked_at     TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    note                TEXT
);

CREATE TABLE IF NOT EXISTS playlist (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    netease_id TEXT,
    season_id  INTEGER REFERENCES season(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pushed (
    id              INTEGER PRIMARY KEY,
    playlist_name   TEXT NOT NULL,
    netease_song_id TEXT NOT NULL,
    theme_id        INTEGER REFERENCES theme(id),
    pushed_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (playlist_name, netease_song_id)
);

-- 步骤④ 的**用户选择**（勾选 + 逐行目标歌单）。
--
-- 为什么不复用 decision：decision.status 有 CHECK 约束、语义是「匹配结果」，
-- 而这一列是「这次推不推、推到哪」。两者曾经被混用 —— 第④步点「完成」
-- 把界面上那串中文状态（"待推送"）塞进 decision.status，必然 IntegrityError
-- （离线复现过：CHECK constraint failed）。
-- 也**不能**只放在内存里：第④步的「目标歌单」列是用户逐行改出来的，
-- 任何一次 reload 都会由 session 按季度名重建，改过的值会当场蒸发。
CREATE TABLE IF NOT EXISTS push_selection (
    id              INTEGER PRIMARY KEY,
    season_id       INTEGER NOT NULL REFERENCES season(id) ON DELETE CASCADE,
    netease_song_id TEXT    NOT NULL,
    theme_id        INTEGER REFERENCES theme(id) ON DELETE SET NULL,
    target_playlist TEXT    NOT NULL DEFAULT '',
    included        INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (season_id, netease_song_id)
);

CREATE TABLE IF NOT EXISTS http_cache (
    url        TEXT PRIMARY KEY,
    status     INTEGER,
    body       BLOB,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Store:
    """SQLite 存储。

    **每个线程拿到自己的连接**（`conn` 是 property，不是实例属性）。
    为什么必须这样 —— 实测事故（离线可复现，`.tmp/repro_bugs.py`）：

        ProgrammingError: SQLite objects created in a thread can only be used
        in that same thread. The object was created in thread id 8196 and this
        is thread id 4284.

    `run_gui.py` 在主线程建 `Store`，而 GUI 的拉取/匹配/推送都在 `TaskWorker`
    线程里跑，`Orchestrator` 拿着同一个 `Store` —— 于是「从 Bangumi 拉取」
    和「推送到歌单」都必炸。推送那次更隐蔽：歌单建成了，歌一首没进，
    报错还是「准备歌单失败」。
    """

    def __init__(self, path: str | None = None):
        self.path = path or constants.DB_PATH
        # `:memory:` 这类**没有目录**的路径要放过 —— `os.makedirs("")` 会抛
        # FileNotFoundError，于是内存库根本建不起来（离线自检要用它）。
        # ⚠️ 内存库只适合**单线程冒烟**：本类每线程一条连接，
        # 而 `:memory:` 的库是**连接私有**的 —— 每个线程各看到一个空库。
        _dir = os.path.dirname(self.path)
        if _dir:
            os.makedirs(_dir, exist_ok=True)
        #: 每线程一条连接；首次访问 `conn` 时懒建
        self._local = threading.local()
        #: 所有建过的连接（只为 close() 能各自关掉 —— sqlite 要求
        #: 在**创建它的那个线程**里 close，测试里由主线程统一收尾）
        self._conns: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._closed = False
        self.conn.executescript(SCHEMA)     # 建表（顺带在当前线程建出第一条连接）
        self.conn.commit()

    # ---------------------------------------------------------------- 连接

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        # ⚠️ `foreign_keys` 是**每连接**的开关，不是库的属性。
        # SCHEMA 里那句 PRAGMA 只对跑 executescript 的那条连接生效，
        # 新线程自己的连接必须重新打开 —— 否则外键约束在新线程里**静默失效**
        # （不报错，只是约束不生效，脏数据悄悄写进去）。
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL 下一个线程写、另一个线程读会短暂争锁；30 秒足够熬过任何一次写事务，
        # 而不必让调用方处理 "database is locked"。
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """当前线程的连接（懒建）。

        做成 property 而不是 `self.conn = ...` 是刻意的：全项目 60 多处
        `store.conn.execute(...)` 一行都不用改，收口就在这一处。
        """
        if self._closed:
            raise RuntimeError(
                "Store 已关闭 —— 关闭后不能再访问。"
                "（在 GUI 里意味着窗口正在退出，此时的后台任务必须已经停下）")
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
            with self._lock:
                self._conns.append(c)
        return c

    def close_thread(self) -> None:
        """关闭**当前线程**的连接，并把它从登记表里摘掉。

        谁该调它：别的线程用完就走的时候（`TaskWorker` 收工时，见
        `gui/workers.py` 的 `cleanup`）。不调也不会错，但那条连接要等 GC 才释放 ——
        在 Windows 上表现为「数据库文件删不掉 / 被占用」，测试里非常难查。
        """
        c = getattr(self._local, "conn", None)
        if c is None:
            return
        self._local.conn = None
        with self._lock:
            try:
                self._conns.remove(c)
            except ValueError:
                pass
        try:
            c.close()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        """关闭所有连接。

        ⚠️ sqlite 只允许**创建它的那个线程**关闭连接，否则抛
        `ProgrammingError: SQLite objects created in a thread...`（实测确认）。
        所以别的线程建的那些连接我们关不掉 —— 只能丢掉引用，让 GC 回收
        （对象析构不检查线程）。这也意味着 `close()` 之后必须**不再使用**
        这个 Store（`_closed` 会拦住，而不是给出一个诡异的 sqlite 错误）。
        """
        with self._lock:
            self._closed = True
            conns, self._conns = self._conns, []
            self._local = threading.local()
        for c in conns:
            try:
                c.close()
            except sqlite3.Error:   # 别的线程建的 —— 见上面，交给 GC
                pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ================================================================ 季度

    def get_season_id(self, year: int, quarter: int) -> int:
        cur = self.conn.execute(
            "SELECT id FROM season WHERE year=? AND quarter=?", (year, quarter)
        )
        row = cur.fetchone()
        if row:
            return row["id"]
        name = constants.playlist_name(year, quarter)
        cur = self.conn.execute(
            "INSERT INTO season (year, quarter, playlist_name) VALUES (?,?,?)",
            (year, quarter, name),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def season_playlist_name(self, season_id: int) -> str:
        row = self.conn.execute(
            "SELECT playlist_name FROM season WHERE id=?", (season_id,)
        ).fetchone()
        return row["playlist_name"] if row else ""

    # ================================================================ 步骤状态

    def get_stage(self, season_id: int, stage: Stage | str) -> StageStatus:
        s = str(stage)
        row = self.conn.execute(
            "SELECT status FROM stage_state WHERE season_id=? AND stage=?",
            (season_id, s),
        ).fetchone()
        return StageStatus(row["status"]) if row else StageStatus.PENDING

    def set_stage(
        self,
        season_id: int,
        stage: Stage | str,
        status: StageStatus | str,
        *,
        fetched: bool = False,
        confirmed: bool = False,
    ) -> None:
        s, st = str(stage), str(status)
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT INTO stage_state (season_id, stage, status, fetched_at, confirmed_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(season_id, stage) DO UPDATE SET
                status = excluded.status,
                fetched_at = COALESCE(excluded.fetched_at, stage_state.fetched_at),
                confirmed_at = COALESCE(excluded.confirmed_at, stage_state.confirmed_at)
            """,
            (
                season_id,
                s,
                st,
                now if fetched else None,
                now if confirmed else None,
            ),
        )
        self.conn.commit()

    def mark_downstream_dirty(self, season_id: int, from_stage: Stage | str) -> None:
        """上游被改过 → 下游标 dirty，但**保留数据不清空**（§8.4）。"""
        order = ["anime", "theme", "match", "push"]
        try:
            i = order.index(str(from_stage))
        except ValueError:
            return
        for s in order[i + 1:]:
            if self.get_stage(season_id, s) in (StageStatus.CONFIRMED, StageStatus.FETCHED):
                self.set_stage(season_id, s, StageStatus.DIRTY)
        self.conn.commit()

    # ================================================================ 番剧

    def upsert_anime(self, season_id: int, a: Anime) -> int:
        """按 (season_id, slug) 或 (season_id, bgm_id) 找已有记录。

        **手工数据保护**：已有的 origin='manual' 记录只填空字段。
        """
        existing = None
        if a.animethemes_slug:
            existing = self.conn.execute(
                "SELECT * FROM anime WHERE season_id=? AND animethemes_slug=?",
                (season_id, a.animethemes_slug),
            ).fetchone()
        if existing is None and a.bgm_id:
            existing = self.conn.execute(
                "SELECT * FROM anime WHERE season_id=? AND bgm_id=?",
                (season_id, a.bgm_id),
            ).fetchone()

        if existing is None:
            cur = self.conn.execute(
                """
                INSERT INTO anime (season_id, title_jp, title_cn, title_romaji, bgm_id,
                                   animethemes_slug, anilist_id, popularity, air_date,
                                   season_bucket, origin, included, note)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    season_id, a.title_jp, a.title_cn, a.title_romaji, a.bgm_id,
                    a.animethemes_slug, a.anilist_id, a.popularity,
                    a.air_date.isoformat() if a.air_date else None,
                    str(a.season_bucket), str(a.origin), int(a.included), a.note or "",
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid)

        # 已存在
        protected = existing["origin"] == str(Origin.MANUAL)
        fields = {
            "title_jp": a.title_jp,
            "title_cn": a.title_cn,
            "title_romaji": a.title_romaji,
            "bgm_id": a.bgm_id,
            "animethemes_slug": a.animethemes_slug,
            "anilist_id": a.anilist_id,
            "popularity": a.popularity,
            "air_date": a.air_date.isoformat() if a.air_date else None,
            "season_bucket": str(a.season_bucket),
        }
        sets, vals = [], []
        for k, v in fields.items():
            if v is None or v == "":
                continue
            if protected and existing[k] not in (None, ""):
                continue          # 手工值优先，跳过
            if existing[k] == v:
                continue
            sets.append(f"{k}=?")
            vals.append(v)
        if sets:
            vals.append(existing["id"])
            self.conn.execute(f"UPDATE anime SET {', '.join(sets)} WHERE id=?", vals)
            self.conn.commit()
        return int(existing["id"])

    def anime_list(self, season_id: int, only_included: bool = True) -> list[sqlite3.Row]:
        q = "SELECT * FROM anime WHERE season_id=?"
        if only_included:
            q += " AND included=1"
        q += " ORDER BY air_date IS NULL, air_date, title_cn"
        return list(self.conn.execute(q, (season_id,)))

    def set_anime_included(self, anime_id: int, included: bool) -> None:
        self.conn.execute("UPDATE anime SET included=? WHERE id=?", (int(included), anime_id))
        self.conn.commit()

    def exclude_supplemental_anime(self, season_id: int) -> list[str]:
        """把「仅从 AnimeThemes 拉当季」带进来的番**取消纳入**，返回它们的名字。

        为什么需要：那个按钮按设计会把**整季**番写进番单（用户看到 49 部），
        而其中大部分不是"在看"的。第①步的「排除」是逐行的，几十行点起来很烦。

        **只改 `included`，绝不删除**：曲目、候选、匹配决策、推送记录全部保留，
        想恢复就把「纳入」勾回来。

        判据：`origin='animethemes'` 且 `bgm_id IS NULL` —— 即"不是从 Bangumi 来的、
        也没有和 Bangumi 对上号"的番。手工新增的（`origin='manual'`）不算，
        那是用户自己加的。
        """
        rows = self.conn.execute(
            "SELECT id, title_cn, title_jp, title_romaji FROM anime "
            "WHERE season_id=? AND included=1 AND origin=? AND bgm_id IS NULL",
            (season_id, str(Origin.ANIMETHEMES)),
        ).fetchall()
        if rows:
            self.conn.executemany(
                "UPDATE anime SET included=0 WHERE id=?", [(r["id"],) for r in rows]
            )
            self.conn.commit()
        return [r["title_cn"] or r["title_jp"] or r["title_romaji"] or "?" for r in rows]

    # ================================================================ 曲目

    def upsert_theme(self, anime_id: int, t: Theme) -> int:
        existing = self.conn.execute(
            """
            SELECT * FROM theme
            WHERE anime_id=? AND kind=? AND COALESCE(sequence,0)=COALESCE(?,0)
              AND song_title=?
            """,
            (anime_id, str(t.kind), t.sequence, t.song_title),
        ).fetchone()
        if existing is None and t.song_title:
            existing = self.conn.execute(
                "SELECT * FROM theme WHERE anime_id=? AND kind=? AND song_title=?",
                (anime_id, str(t.kind), t.song_title),
            ).fetchone()

        if existing is None:
            cur = self.conn.execute(
                """
                INSERT INTO theme (anime_id, kind, sequence, song_title, artists,
                                   source, origin, included, note)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    anime_id, str(t.kind), t.sequence, t.song_title,
                    artists_to_json(t.artists), t.source, str(t.origin),
                    int(t.included), t.note or "",
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid)

        protected = existing["origin"] == str(Origin.MANUAL)
        fields = {
            "artists": artists_to_json(t.artists) if t.artists else None,
            "sequence": t.sequence,
        }
        sets, vals = [], []
        for k, v in fields.items():
            if v is None or v == "":
                continue
            if protected and existing[k] not in (None, ""):
                continue
            if existing[k] == v:
                continue
            sets.append(f"{k}=?")
            vals.append(v)
        if sets:
            vals.append(existing["id"])
            self.conn.execute(f"UPDATE theme SET {', '.join(sets)} WHERE id=?", vals)
            self.conn.commit()
        return int(existing["id"])

    def theme_list(self, season_id: int, only_included: bool = True) -> list[sqlite3.Row]:
        q = """
            SELECT t.*, a.title_cn AS anime_cn, a.title_jp AS anime_jp,
                   a.animethemes_slug AS anime_slug
            FROM theme t JOIN anime a ON a.id = t.anime_id
            WHERE a.season_id=?
        """
        if only_included:
            q += " AND a.included=1 AND t.included=1"
        q += " ORDER BY a.title_cn, t.kind, COALESCE(t.sequence,0)"
        return list(self.conn.execute(q, (season_id,)))

    # ================================================================ 候选

    def upsert_candidate(self, theme_id: int, c: Candidate) -> int:
        import json as _json

        existing = self.conn.execute(
            "SELECT id, origin FROM candidate WHERE theme_id=? AND netease_song_id=?",
            (theme_id, c.netease_song_id),
        ).fetchone()

        # 顺序与下方 SQL 的占位符严格一致（UPDATE 不含 theme_id / song_id）
        fields = (
            c.query_used,
            c.name,
            artists_to_json(c.artists),
            c.album,
            c.cover,
            c.duration_ms,
            _json.dumps(c.tags or [], ensure_ascii=False),
            c.anime_alias,
            _json.dumps(c.trans_names or [], ensure_ascii=False),
            c.fee,
            c.mvid,
            c.search_rank,
            c.score,
            _json.dumps(c.score_detail or {}, ensure_ascii=False),
        )

        if existing is None:
            cur = self.conn.execute(
                """
                INSERT INTO candidate (theme_id, query_used, netease_song_id, name,
                    artists, album, cover, duration_ms, tags, anime_alias, trans_names,
                    fee, mvid, search_rank, score, score_detail, origin)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (theme_id, c.query_used, c.netease_song_id, *fields[1:], str(c.origin)),
            )
            self.conn.commit()
            return int(cur.lastrowid)

        if existing["origin"] == str(Origin.MANUAL):
            return int(existing["id"])   # 手工候选不动

        self.conn.execute(
            """
            UPDATE candidate SET query_used=?, name=?, artists=?, album=?, cover=?,
                duration_ms=?, tags=?, anime_alias=?, trans_names=?, fee=?, mvid=?,
                search_rank=?, score=?, score_detail=?, fetched_at=datetime('now')
            WHERE id=?
            """,
            (*fields, existing["id"]),
        )
        self.conn.commit()
        return int(existing["id"])

    def candidate_list(self, theme_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM candidate WHERE theme_id=? ORDER BY score DESC, search_rank",
                (theme_id,),
            )
        )

    # ================================================================ 决策

    def set_decision(
        self,
        theme_id: int,
        status: DecisionStatus | str,
        candidate_id: int | None = None,
        *,
        decided_by: str = "auto",
        note: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO decision (theme_id, chosen_candidate_id, status, decided_by,
                                  decided_at, last_checked_at, note)
            VALUES (?,?,?,?,datetime('now'),datetime('now'),?)
            ON CONFLICT(theme_id) DO UPDATE SET
                chosen_candidate_id = excluded.chosen_candidate_id,
                status = excluded.status,
                decided_by = excluded.decided_by,
                decided_at = datetime('now'),
                last_checked_at = datetime('now'),
                retry_count = CASE WHEN excluded.status LIKE 'missing%'
                                   THEN decision.retry_count + 1
                                   ELSE decision.retry_count END,
                note = excluded.note
            """,
            (theme_id, candidate_id, str(status), decided_by, note),
        )
        self.conn.commit()

    def decision_map(self, season_id: int) -> dict[int, sqlite3.Row]:
        rows = self.conn.execute(
            """
            SELECT d.* FROM decision d
            JOIN theme t ON t.id = d.theme_id
            JOIN anime a ON a.id = t.anime_id
            WHERE a.season_id=?
            """,
            (season_id,),
        )
        return {r["theme_id"]: r for r in rows}

    def retryable_themes(self, season_id: int) -> list[sqlite3.Row]:
        """只挑 missing_source / missing_netease 重试，不碰 missing_match（§5.12）。"""
        return list(
            self.conn.execute(
                """
                SELECT t.* FROM theme t
                JOIN anime a ON a.id = t.anime_id
                JOIN decision d ON d.theme_id = t.id
                WHERE a.season_id=?
                  AND d.status IN ('missing_source','missing_netease')
                """,
                (season_id,),
            )
        )

    def decision_summary(self, season_id: int) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT d.status AS s, COUNT(*) AS n FROM decision d
            JOIN theme t ON t.id = d.theme_id
            JOIN anime a ON a.id = t.anime_id
            WHERE a.season_id=? GROUP BY d.status
            """,
            (season_id,),
        )
        return {r["s"]: r["n"] for r in rows}

    # ================================================================ 推送

    def is_pushed(self, playlist_name: str, song_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM pushed WHERE playlist_name=? AND netease_song_id=?",
            (playlist_name, str(song_id)),
        ).fetchone()
        return row is not None

    def mark_pushed(self, playlist_name: str, song_id: str, theme_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO pushed (playlist_name, netease_song_id, theme_id) VALUES (?,?,?)",
            (playlist_name, str(song_id), theme_id),
        )
        self.conn.commit()

    def pushed_songs(self, playlist_name: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT netease_song_id FROM pushed WHERE playlist_name=?", (playlist_name,)
        )
        return {r["netease_song_id"] for r in rows}

    def save_playlist(self, name: str, netease_id: str | None, season_id: int | None) -> None:
        self.conn.execute(
            """
            INSERT INTO playlist (name, netease_id, season_id) VALUES (?,?,?)
            ON CONFLICT(name) DO UPDATE SET netease_id=excluded.netease_id
            """,
            (name, netease_id, season_id),
        )
        self.conn.commit()

    # ================================================================ 步骤④ 的选择

    def save_push_selection(self, season_id: int, rows: Iterable[dict]) -> int:
        """把第④步的勾选与逐行「目标歌单」落库。返回写入的行数。

        **只动 `push_selection`，绝不碰 `decision`** —— 这是之前那个
        IntegrityError 的根因（见 SCHEMA 里 `push_selection` 的注释）。

        没有 `netease_song_id` 的行（刚点「+ 手动加歌」还没填 ID 的）跳过：
        标识不了就没法存，存了也会在回读时错位。
        页面上已经不存在的行会被删掉，否则回读时会把用户删掉的歌复活。
        """
        keep: list[str] = []
        for r in rows:
            sid = str(r.get("netease_song_id") or "").strip()
            if not sid:
                continue
            keep.append(sid)
            tid = r.get("theme_id")
            self.conn.execute(
                """
                INSERT INTO push_selection (season_id, netease_song_id, theme_id,
                                            target_playlist, included, updated_at)
                VALUES (?,?,?,?,?,datetime('now'))
                ON CONFLICT(season_id, netease_song_id) DO UPDATE SET
                    theme_id        = excluded.theme_id,
                    target_playlist = excluded.target_playlist,
                    included        = excluded.included,
                    updated_at      = datetime('now')
                """,
                (
                    season_id, sid,
                    tid if isinstance(tid, int) else None,
                    str(r.get("playlist") or ""),
                    int(bool(r.get("included", True))),
                ),
            )

        existing = {
            r["netease_song_id"] for r in self.conn.execute(
                "SELECT netease_song_id FROM push_selection WHERE season_id=?", (season_id,)
            )
        }
        for gone in existing - set(keep):
            self.conn.execute(
                "DELETE FROM push_selection WHERE season_id=? AND netease_song_id=?",
                (season_id, gone),
            )
        self.conn.commit()
        return len(keep)

    def push_selection_map(self, season_id: int) -> dict[str, dict[str, Any]]:
        """`song_id → {"included": bool, "playlist": str}`。

        **没有记录的行不在返回值里** —— 缺省即「勾选、用季度歌单名」，
        调用方不要用 `.get(sid, {})` 之外的默认值把缺省改掉。
        """
        rows = self.conn.execute(
            "SELECT netease_song_id, target_playlist, included "
            "FROM push_selection WHERE season_id=?",
            (season_id,),
        )
        return {
            r["netease_song_id"]: {
                "included": bool(r["included"]),
                "playlist": r["target_playlist"] or "",
            }
            for r in rows
        }

    # ================================================================ 调试

    def counts(self, season_id: int) -> dict[str, int]:
        def one(sql: str, *args) -> int:
            return int(self.conn.execute(sql, args).fetchone()[0])

        return {
            "anime": one("SELECT COUNT(*) FROM anime WHERE season_id=?", season_id),
            "anime_included": one(
                "SELECT COUNT(*) FROM anime WHERE season_id=? AND included=1", season_id
            ),
            "theme": one(
                "SELECT COUNT(*) FROM theme t JOIN anime a ON a.id=t.anime_id "
                "WHERE a.season_id=?", season_id
            ),
            "candidate": one(
                "SELECT COUNT(*) FROM candidate c JOIN theme t ON t.id=c.theme_id "
                "JOIN anime a ON a.id=t.anime_id WHERE a.season_id=?", season_id
            ),
            "decision": one(
                "SELECT COUNT(*) FROM decision d JOIN theme t ON t.id=d.theme_id "
                "JOIN anime a ON a.id=t.anime_id WHERE a.season_id=?", season_id
            ),
        }
